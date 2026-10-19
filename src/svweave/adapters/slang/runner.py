"""Launch and supervise the HDL worker process (plan 7.1, 14.6, API-03).

* argument array, no shell; ``cwd`` = snapshot root; python ``-I`` (isolated: the snapshot
  directory can never shadow a Python module);
* its own session/process group, so a timeout, a cancel or a crash can reap the WHOLE tree
  with ``killpg``; stragglers are killed even after a normal exit;
* ``RLIMIT_AS`` from ``analysis.max_worker_memory_mib``; core dumps disabled;
* a native crash, a signal or a non-zero exit yields a structured ``WorkerOutcome`` and never
  an exception into the gateway;
* peak RSS and CPU time come from the worker's own last stderr line (``SVW_RUSAGE``): ``wait4`` on the
  launcher only sees the bwrap process. When the line is absent (killed, crashed) the launcher's
  ``wait4`` rusage is used and ``rusage_source`` says ``launcher`` (a lower bound);
* the worker runs INSIDE the bwrap sandbox of ``security/sandbox.py`` (plan 6.4, 23.1): it sees
  only a read-only system, the Python runtime / venv / svweave sources, the immutable snapshot
  and the adapter's group files (all read-only), a private tmpfs ``/tmp``, and no network. An
  absolute ``include "/etc/passwd"`` or a path into the original repository is therefore
  file-not-found at read time, not merely detected afterwards. Without bwrap the build fails
  closed (``BACKEND_UNAVAILABLE``) unless the operator opts into the degraded mode with
  ``SVWEAVE_ALLOW_DEGRADED_WORKER=1`` (or ``allow_degraded=True``); the outcome then carries
  ``isolation = "degraded_no_isolation"`` and a warning, which callers must surface.
"""

from __future__ import annotations

import contextlib
import os
import resource
import signal
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

import svweave
from svweave.adapters.slang.protocol import WorkerRequest, parse_rusage_line
from svweave.domain.errors import ErrorCode, SvwError
from svweave.security.sandbox import IsolationMode, SandboxRunner, SandboxSpec

ALLOW_DEGRADED_ENV = "SVWEAVE_ALLOW_DEGRADED_WORKER"

OutcomeKind = Literal[
    "ok", "timeout", "cancelled", "crashed", "exited", "resource_limit", "bad_output"
]

STDERR_KEEP = 256 * 1024
_MEMORY_MARKERS = (b"MemoryError", b"std::bad_alloc", b"Cannot allocate memory")


@dataclass
class WorkerOutcome:
    kind: OutcomeKind
    exit_code: int | None = None
    signal: int | None = None
    stdout: bytes = b""
    stderr_tail: str = ""
    wall_s: float = 0.0
    peak_rss_kib: int | None = None
    user_cpu_s: float | None = None
    sys_cpu_s: float | None = None
    error_code: ErrorCode | None = None
    message: str = ""
    resource_usage: dict[str, Any] = field(default_factory=dict)
    #: ``bwrap`` or ``degraded_no_isolation``; callers must surface the degraded value.
    isolation: str = IsolationMode.BWRAP.value
    isolation_warnings: tuple[str, ...] = ()

    @property
    def ok(self) -> bool:
        return self.kind == "ok"


class WorkerHandle:
    """Cancellation handle: ``cancel()`` kills the worker tree; safe from any thread."""

    def __init__(self) -> None:
        self._cancel = threading.Event()
        self.pgid: int | None = None

    def cancel(self) -> None:
        self._cancel.set()
        kill_group(self.pgid)

    @property
    def cancelled(self) -> bool:
        return self._cancel.is_set()


def kill_group(pgid: int | None) -> None:
    if pgid is None:
        return
    with contextlib.suppress(ProcessLookupError, PermissionError):
        os.killpg(pgid, signal.SIGKILL)


def _limits(memory_mib: int) -> Any:
    def apply() -> None:
        limit = int(memory_mib) * 1024 * 1024
        resource.setrlimit(resource.RLIMIT_AS, (limit, limit))
        resource.setrlimit(resource.RLIMIT_CORE, (0, 0))

    return apply


def worker_env() -> dict[str, str]:
    """Minimal environment: nothing from the caller's environment leaks into the worker."""
    return {
        "PATH": "/usr/bin:/bin",
        "LANG": "C.UTF-8",
        "LC_ALL": "C.UTF-8",
        "PYTHONDONTWRITEBYTECODE": "1",
        "PYTHONUNBUFFERED": "1",
    }


def worker_sandbox_spec(req: WorkerRequest, python: str | None = None) -> SandboxSpec:
    """What the worker may see: read-only runtime + venv + svweave sources + snapshot + the
    adapter's group files; a private tmpfs ``/tmp`` is added by the sandbox; no network."""
    exe = Path(python or sys.executable)
    ro: list[Path] = []

    def add(p: Path) -> None:
        p = Path(os.path.realpath(p))
        # /usr (and so /usr/local) is part of the minimal sandbox system already
        if p not in ro and not (p == Path("/usr") or Path("/usr") in p.parents):
            ro.append(p)

    add(Path(sys.base_prefix))  # the interpreter's stdlib when it lives outside /usr
    add(Path(sys.prefix))  # the venv: bin/python, pyvenv.cfg, site-packages (pyslang, pydantic)
    add(exe.parent)
    add(Path(svweave.__file__).resolve().parent.parent)  # editable installs: the src tree
    add(Path(req.snapshot_root))
    for g in req.group_files:
        add(Path(g.path))
    return SandboxSpec(ro_paths=tuple(ro), chdir=Path(req.snapshot_root), env=worker_env())


def run_worker(
    req: WorkerRequest,
    *,
    timeout_s: float,
    memory_mib: int,
    handle: WorkerHandle | None = None,
    python: str | None = None,
    test_fault: str | None = None,
    sandbox: SandboxRunner | None = None,
    allow_degraded: bool | None = None,
) -> WorkerOutcome:
    """Run one build in a sandboxed child process. Never raises for worker failures."""
    if test_fault is not None:
        req = req.model_copy(update={"test_fault": test_fault})
    handle = handle or WorkerHandle()
    payload = req.model_dump_json().encode("utf-8")
    inner = [python or sys.executable, "-I", "-m", "svweave.adapters.slang.worker"]
    if allow_degraded is None:
        allow_degraded = os.environ.get(ALLOW_DEGRADED_ENV) == "1"
    try:
        runner = sandbox or SandboxRunner(allow_degraded=allow_degraded)
        wrapped = runner.wrap(worker_sandbox_spec(req, python), inner)
    except SvwError as e:  # fail closed: no bwrap and no explicit degraded mode
        return WorkerOutcome(
            kind="exited",
            error_code=e.code,
            message=f"worker not started: {e.message} ({e.details.get('reason', '')})"[:500],
            isolation="UNAVAILABLE",
        )
    cmd = wrapped.argv
    t0 = time.monotonic()
    try:
        proc = subprocess.Popen(  # noqa: S603 - argument array, no shell
            cmd,
            cwd=req.snapshot_root,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env={"PATH": "/usr/bin:/bin"},
            start_new_session=True,
            preexec_fn=_limits(memory_mib),  # noqa: PLW1509
            close_fds=True,
        )
    except OSError as e:
        return WorkerOutcome(
            kind="exited",
            error_code=ErrorCode.BACKEND_UNAVAILABLE,
            message=f"cannot start worker: {e}",
        )
    pid = proc.pid
    handle.pgid = pid  # start_new_session: pgid == pid
    if handle.cancelled:  # cancelled between request and start
        kill_group(pid)

    out_chunks: list[bytes] = []
    err_buf = bytearray()

    def feed() -> None:
        try:
            assert proc.stdin is not None
            proc.stdin.write(payload)
            proc.stdin.close()
        except (BrokenPipeError, OSError):
            pass

    def drain_out() -> None:
        assert proc.stdout is not None
        while True:
            chunk = proc.stdout.read(1 << 20)
            if not chunk:
                break
            out_chunks.append(chunk)

    def drain_err() -> None:
        assert proc.stderr is not None
        while True:
            chunk = proc.stderr.read(1 << 16)
            if not chunk:
                break
            err_buf.extend(chunk)
            if len(err_buf) > 2 * STDERR_KEEP:
                del err_buf[: len(err_buf) - STDERR_KEEP]

    threads = [threading.Thread(target=f, daemon=True) for f in (feed, drain_out, drain_err)]
    for t in threads:
        t.start()

    timed_out = False
    status: int | None = None
    rusage: Any = None
    deadline = t0 + timeout_s
    while True:
        try:
            wpid, st, ru = os.wait4(pid, os.WNOHANG)
        except ChildProcessError:
            break
        if wpid == pid:
            status, rusage = st, ru
            break
        if handle.cancelled:
            kill_group(pid)
        elif time.monotonic() >= deadline:
            timed_out = True
            kill_group(pid)
        time.sleep(0.01)
    proc.returncode = 0  # reaped here; keep Popen from waiting again
    # Reap descendants no matter how the worker ended (leaves no orphans, API-03).
    kill_group(pid)
    for t in threads:
        t.join(timeout=5)
    for f in (proc.stdout, proc.stderr, proc.stdin):
        if f is not None:
            with contextlib.suppress(OSError, ValueError):
                f.close()
    wall = time.monotonic() - t0
    stdout = b"".join(out_chunks)
    err_bytes, self_usage = parse_rusage_line(bytes(err_buf)[-STDERR_KEEP:])
    out = WorkerOutcome(
        kind="ok",
        isolation=wrapped.isolation.value,
        isolation_warnings=wrapped.warnings,
        stdout=stdout,
        stderr_tail=err_bytes.decode("utf-8", "replace"),
        wall_s=round(wall, 3),
    )
    rusage_source = "launcher"
    if rusage is not None:
        out.peak_rss_kib = int(rusage.ru_maxrss)  # KiB on Linux
        out.user_cpu_s = round(rusage.ru_utime, 3)
        out.sys_cpu_s = round(rusage.ru_stime, 3)
    if self_usage is not None:  # the worker's own figures; the launcher's cover only bwrap itself
        out.peak_rss_kib = int(self_usage["maxrss_kib"])
        out.user_cpu_s = round(self_usage["user_s"], 3)
        out.sys_cpu_s = round(self_usage["sys_s"], 3)
        rusage_source = "worker"
    out.resource_usage = {
        "wall_s": out.wall_s,
        "peak_rss_kib": out.peak_rss_kib,
        "user_cpu_s": out.user_cpu_s,
        "sys_cpu_s": out.sys_cpu_s,
        "rusage_source": rusage_source,
        "memory_limit_mib": memory_mib,
        "timeout_s": timeout_s,
        "isolation": wrapped.isolation.value,
        "isolation_warnings": list(wrapped.warnings),
    }
    if status is not None:
        if os.WIFSIGNALED(status):
            out.signal = os.WTERMSIG(status)
        elif os.WIFEXITED(status):
            out.exit_code = os.WEXITSTATUS(status)
            if wrapped.isolation is IsolationMode.BWRAP and 128 < out.exit_code <= 128 + 64:
                # bwrap reports a signalled sandbox child as exit status 128 + signal
                out.signal, out.exit_code = out.exit_code - 128, None
    if handle.cancelled and not (out.signal is None and out.exit_code == 0):
        out.kind = "cancelled"
        out.message = "build cancelled; worker process group killed"
    elif timed_out:
        out.kind = "timeout"
        out.error_code = ErrorCode.BUILD_TIMEOUT
        out.message = f"build exceeded {timeout_s:g}s; worker process group killed"
    elif out.signal is not None:
        name = signal.Signals(out.signal).name
        if out.signal == signal.SIGKILL or any(m in err_bytes for m in _MEMORY_MARKERS):
            out.kind = "resource_limit"
            out.error_code = ErrorCode.RESOURCE_LIMIT
            out.message = f"worker killed by {name} (memory limit {memory_mib} MiB?)"
        else:
            out.kind = "crashed"
            out.error_code = ErrorCode.BACKEND_UNAVAILABLE
            out.message = f"worker crashed with signal {name} ({out.signal})"
    elif out.exit_code != 0:
        if any(m in err_bytes for m in _MEMORY_MARKERS):
            out.kind = "resource_limit"
            out.error_code = ErrorCode.RESOURCE_LIMIT
            out.message = f"worker ran out of memory (limit {memory_mib} MiB)"
        else:
            out.kind = "exited"
            out.error_code = ErrorCode.BACKEND_UNAVAILABLE
            out.message = f"worker exited with code {out.exit_code}"
    elif not stdout.strip():
        out.kind = "bad_output"
        out.error_code = ErrorCode.BACKEND_UNAVAILABLE
        out.message = "worker produced no result"
    return out
