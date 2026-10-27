"""Mandatory, narrow launch boundary for legacy real-tool probes (HDL-R10 / SEC-05).

Only trusted host orchestration writes reports. Every native tool, including metadata/config
commands, runs through SandboxRunner; there is no environment-variable or --wrap opt-out.
The source/tool mounts are immutable, state is per-probe, and /tmp + the network are private.
"""
from __future__ import annotations

import contextlib
import os
import signal
import subprocess
import sys
import threading
from dataclasses import replace
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "src"))

from svweave.domain.errors import ErrorCode, SvwError  # noqa: E402
from svweave.security.python_runtime import python_binds, python_links  # noqa: E402
from svweave.security.sandbox import (  # noqa: E402
    IsolationMode,
    SandboxRunner,
    SandboxSpec,
    mount_providing,
    validate_spec,
)

LAUNCH_ENV = {"PATH": "/usr/bin:/bin"}
BASE_ENV = {**LAUNCH_ENV, "LANG": "C.UTF-8", "PYTHONDONTWRITEBYTECODE": "1"}
ENV_KEYS = frozenset({*BASE_ENV, "HOME", "XDG_CONFIG_HOME", "XDG_CACHE_HOME", "XDG_DATA_HOME",
                      "CBM_CACHE_DIR", "CBM_RUNTIME_DIR", "SERENA_HOME", "PYTHONNOUSERSITE", "UV_NO_SYNC"})


def probe_spec(*, sources=(), tools=(), writable=(), cwd=None, env=None, python_venvs=(), tmp_dirs=()):
    """Construct a spec with explicit source/tool inputs and disjoint writable state/output.

    Python's established narrow runtime binds/links are reused, but their whole-prefix fallback
    is rejected: an unfamiliar interpreter layout is a blocker, not permission to expose more.
    """
    immutable = [Path(p).resolve() for p in (*sources, *tools)]
    ro = list(immutable)
    venvs = [Path(v).absolute() for v in python_venvs]
    for venv in venvs:
        # The installed venv is a tool input, not the full interpreter installation prefix.
        if (venv / "pyvenv.cfg").is_file():
            ro.append(venv.resolve())
        runtime = python_binds(venv)
        real = (venv / "bin/python").resolve()
        if real.parents[1] in runtime:
            raise SvwError(ErrorCode.POLICY_DENIED, "probe refuses a broad Python runtime mount")
        ro.extend(runtime)
    rw = [Path(p).resolve() for p in writable]
    for w in rw:
        if w == REPO or REPO in w.parents:
            roots = (REPO / ".svweave", REPO / ".toolchain/state")
            if not any(root in w.parents for root in roots):
                raise SvwError(ErrorCode.POLICY_DENIED, "probe writes must stay in dedicated runtime state")
        if any(w == r or w in r.parents or r in w.parents for r in ro):
            raise SvwError(ErrorCode.POLICY_DENIED, "probe writable path overlaps a source/tool input")
    environment = dict(BASE_ENV if env is None else env)
    if set(environment) - ENV_KEYS:
        raise SvwError(ErrorCode.POLICY_DENIED, "probe environment contains a non-allowlisted key")
    spec = SandboxSpec(ro_paths=tuple(dict.fromkeys(ro)), rw_paths=tuple(rw),
                       protected_paths=(REPO, *immutable), rw_exempt=tuple(rw),
                       chdir=Path(cwd).resolve() if cwd is not None else None,
                       env=environment, tmp_dirs=tuple(tmp_dirs))
    links = [link for venv in venvs for link in python_links(venv, spec)]
    for venv in venvs:
        first = venv / "bin/python"
        # The adapter normally mounts the entire venv. A global interpreter has no
        # pyvenv.cfg, so recreate its first link too when outside existing RO mounts.
        if first.is_symlink() and mount_providing(spec, first) is None:
            links.append((os.readlink(first), first))
    spec = replace(spec, symlinks=tuple(dict.fromkeys(links)))
    validate_spec(spec)
    return spec


def serena_spec(toolchain, state, project, context, env, *, edit_candidate=False):
    """The locked editable Serena install needs its exact source tree, not the whole toolchain."""
    sources = (Path(context),) if edit_candidate else (project, Path(context))
    writable = (state / "work", project) if edit_candidate else (state / "work",)
    return probe_spec(sources=sources,
                      tools=(toolchain / "serena-venv", toolchain / "serena-src", toolchain / "verible/bin"),
                      writable=writable, cwd=project, env=env,
                      python_venvs=(toolchain / "serena-venv",))


def _validate(spec):
    if spec.network or spec.etc != "minimal" or set(spec.env) - ENV_KEYS:
        raise SvwError(ErrorCode.POLICY_DENIED, "probe sandbox policy must remain mandatory and minimal")
    validate_spec(spec)


def wrap(spec, argv):
    _validate(spec)
    runner = SandboxRunner(allow_degraded=False)
    command = runner.wrap(spec, argv)
    if command.isolation != IsolationMode.BWRAP:
        raise SvwError(ErrorCode.BACKEND_UNAVAILABLE, "probe requires bwrap isolation")
    return command


def run(spec, argv, *, timeout=60, max_stdout_bytes=64 * 1024 * 1024,
        max_stderr_bytes=1024 * 1024):
    """Bound both pipes and reap the entire namespace/process group, including on timeout.

    stdout contains the raw pyslang JSON (64 MiB cap); stderr is evidence, not an unlimited
    buffer. Overflow is a failure, never a truncated successful result. bwrap's PID namespace
    kills detached grandchildren as well when its supervisor dies.
    """
    command = wrap(spec, argv)
    proc = subprocess.Popen(command.argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, env=LAUNCH_ENV, start_new_session=True)
    chunks = [bytearray(), bytearray()]
    overflow = threading.Event()

    def kill_tree():
        with contextlib.suppress(ProcessLookupError):
            os.killpg(proc.pid, signal.SIGKILL)

    def read(stream, dest, limit):
        while block := stream.read(65536):
            remaining = max(0, limit - len(dest))
            dest.extend(block[:remaining])
            if len(block) > remaining:
                overflow.set()
                kill_tree()

    readers = [threading.Thread(target=read, args=(stream, dest, limit), daemon=True)
               for stream, dest, limit in ((proc.stdout, chunks[0], max_stdout_bytes),
                                           (proc.stderr, chunks[1], max_stderr_bytes))]
    try:
        for reader in readers:
            reader.start()
        proc.wait(timeout=timeout)
    finally:
        # Also reap stragglers after a normal worker exit. A detached child is confined to
        # bwrap's PID namespace; the launcher is never allowed to outlive this operation.
        kill_tree()
        proc.wait()
        for reader in readers:
            reader.join(timeout=5)
        proc.stdout.close()
        proc.stderr.close()
    if overflow.is_set():
        raise SvwError(ErrorCode.OUTPUT_LIMIT, "probe subprocess exceeded its output limit")
    return subprocess.CompletedProcess(command.argv, proc.returncode,
                                       chunks[0].decode("utf-8", "replace"),
                                       chunks[1].decode("utf-8", "replace"))


def checked_text(spec, argv, *, timeout=60):
    """Metadata/config commands cannot silently fail while fixture checks claim READY."""
    result = run(spec, argv, timeout=timeout)
    value = result.stdout.strip() or result.stderr.strip()
    if result.returncode or not value:
        raise SvwError(ErrorCode.BACKEND_UNAVAILABLE,
                       f"probe metadata command failed (rc={result.returncode}): {argv[0]}")
    return value


def python_spec(*, sources=(), writable=(), cwd=None):
    return probe_spec(sources=sources, writable=writable, cwd=cwd,
                      python_venvs=(Path(sys.prefix),))
