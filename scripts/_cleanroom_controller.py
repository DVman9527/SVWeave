"""DEP-01-R2: trusted HOST orchestration, not a backend isolation boundary.

Only the fixed preflight/probe controller is permitted here. It has no outer network
namespace; every native backend it invokes still requires the existing SandboxRunner.
"""
from __future__ import annotations

import contextlib
import os
import selectors
import signal
import subprocess
import time
from pathlib import Path
from typing import Any

from _cleanroom_preflight import PREFLIGHT_CODE

CONTRACT_REVISION = "DEP-01-R2-host-controller"


def validate_contract_identity(contract: dict[str, Any], current: dict[str, Any]) -> None:
    if (contract.get("revision") != CONTRACT_REVISION
            or current.get("dep01_contract_revision") != CONTRACT_REVISION):
        raise ValueError("current CI identity must explicitly select the R2 contract revision")
    for key in ("source_identity", "lock_sha256", "workflow_sha256"):
        if not contract.get(key) or contract[key] != current.get(key):
            raise ValueError(f"R2 contract differs from current CI identity: {key}")


def controller_env(room: Path) -> dict[str, str]:
    return {
        "PATH": "/usr/bin:/bin", "LANG": "C.UTF-8",
        "HOME": str(room / ".svweave/controller-home"),
        "PYTHONNOUSERSITE": "1", "PYTHONDONTWRITEBYTECODE": "1",
        "SVWEAVE_REQUIRE_BACKENDS": "1",
    }


def probe_argv(room: Path) -> list[str]:
    return [str(room / ".venv/bin/python"), "-I", str(room / "scripts/probe_backends.py"),
            "--out", str(room / "capability_report.json")]


def validate_probe_evidence(report: dict[str, Any], returncode: int) -> None:
    """Reject superficially matching summaries without successful isolation/source evidence."""
    if returncode != 0 or report.get("isolation") != "mandatory_bwrap":
        raise ValueError("fresh probe must succeed under mandatory bwrap")
    if (report.get("sandbox_host_capability", {}).get("available") is not True
            or report.get("required_backend_errors") != []):
        raise ValueError("fresh probe isolation/capability requirements failed")
    backends = report["backends"]
    if set(backends) != {"pyslang", "slang_cli", "cbm", "serena"}:
        raise ValueError("all four pinned backend probes are required")
    for backend in ("pyslang", "slang_cli"):
        manifest = backends[backend]["source_manifest_sha256"]
        if not manifest["before"] or manifest["before"] != manifest["after"]:
            raise ValueError(f"{backend} source immutability check failed")
    if (backends["cbm"].get("snapshot_unchanged_by_probe") is not True
            or backends["serena"].get("candidate_unchanged_by_probe") is not True):
        raise ValueError("MCP source immutability check failed")


def run_controller(argv: list[str], room: Path, timeout: float) -> subprocess.CompletedProcess[str]:
    """Bounded trusted controller launch; never an arbitrary native-command fallback.

    On failure kill the controller process group. Its mandatory bwrap children also use
    --die-with-parent and private PID namespaces; no detached native child is permitted.
    """
    if not room.is_absolute():
        raise ValueError("cleanroom controller requires an absolute fresh room")
    preflight = [str(room / ".venv/bin/python"), "-I", "-c", PREFLIGHT_CODE]
    if argv not in (preflight, probe_argv(room)):
        raise ValueError("only the fixed cleanroom preflight/probe controller may run on the host")
    env = controller_env(room)
    Path(env["HOME"]).mkdir(parents=True, exist_ok=True)
    proc = subprocess.Popen(argv, cwd=room, env=env, stdin=subprocess.DEVNULL,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, start_new_session=True)
    chunks = [bytearray(), bytearray()]
    streams = (proc.stdout, proc.stderr)
    selector = selectors.DefaultSelector()
    deadline = time.monotonic() + timeout
    drain_deadline = None

    def kill_tree():
        with contextlib.suppress(ProcessLookupError):
            os.killpg(proc.pid, signal.SIGKILL)

    try:
        for index, stream in enumerate(streams):
            os.set_blocking(stream.fileno(), False)
            selector.register(stream, selectors.EVENT_READ, index)
        while selector.get_map():
            now = time.monotonic()
            if now >= deadline:
                raise subprocess.TimeoutExpired(argv, timeout)
            if proc.poll() is not None:
                if drain_deadline is None:
                    kill_tree()
                    drain_deadline = now + 1.0
                elif now >= drain_deadline:
                    raise RuntimeError("cleanroom controller exited but descendant retained output pipes")
            for key, _ in selector.select(min(0.05, deadline - now)):
                block = os.read(key.fileobj.fileno(), 65536)
                if not block:
                    selector.unregister(key.fileobj)
                    continue
                chunk = chunks[key.data]
                if len(chunk) + len(block) > 4 * 1024 * 1024:
                    raise RuntimeError("cleanroom controller output exceeded 4 MiB per stream")
                chunk.extend(block)
        proc.wait(timeout=max(0, deadline - time.monotonic()))
    finally:
        selector.close()
        kill_tree()
        # No reader thread can retain a buffered-file lock: pipes are drained nonblocking.
        # A detached pipe holder cannot turn timeout/normal exit into an unbounded close.
        for stream in streams:
            stream.close()
        proc.wait(timeout=5)
    return subprocess.CompletedProcess(argv, proc.returncode,
                                       chunks[0].decode("utf-8", "replace"),
                                       chunks[1].decode("utf-8", "replace"))
