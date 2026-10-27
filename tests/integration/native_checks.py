"""Native test premises, invoked ONLY by helpers.native_check inside mandatory outer bwrap.

All imports that can load or launch a backend happen after independent namespace / filesystem /
environment assertions. Explicit inner degraded behavior is a test subject, not a host opt-out.
"""
from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path
from typing import Any


def safety(source: Path, host_netns: str, host_pidns: str) -> dict[str, bool]:
    result = {
        "network_private": os.readlink("/proc/self/ns/net") != host_netns,
        "pid_private": os.readlink("/proc/self/ns/pid") != host_pidns,
        "secret_absent": "SVW_TEST_NATIVE_SECRET" not in os.environ,
    }
    # O_WRONLY without O_TRUNC: even a broken read-only boundary cannot damage this input.
    probe = next(source.rglob("*.sv"))
    try:
        fd = os.open(probe, os.O_WRONLY)
    except OSError:
        result["source_readonly"] = True
    else:
        os.close(fd)
        result["source_readonly"] = False
    assert all(result.values()), result
    return result


def ignored_diagnostics(source: Path) -> dict[str, int]:
    import pyslang
    from pyslang import driver as drv

    d = drv.Driver()
    d.addStandardArgs()
    assert d.parseCommandLine("slang t.sv --top top", drv.CommandLineOptions())
    assert d.processOptions() and d.parseAllSources()
    comp = d.createCompilation()
    ignored = [
        x for x in comp.getAllDiagnostics()
        if d.diagEngine.getSeverity(x.code, x.location) == pyslang.DiagnosticSeverity.Ignored
    ]
    return {"ignored": len(ignored)}


def token_processes(tokens: set[str]) -> list[int]:
    """Independent live-process evidence, not the cleanup implementation under test."""
    needles = {f"SVWEAVE_BACKEND_TOKEN={token}".encode() for token in tokens}
    result = []
    for entry in Path("/proc").glob("[0-9]*"):
        try:
            if (entry / "stat").read_text().rsplit(")", 1)[1].split()[0] == "Z":
                continue
            if needles.intersection((entry / "environ").read_bytes().split(b"\0")):
                result.append(int(entry.name))
        except (OSError, ValueError):
            continue
    return result


def degraded_cbm(source: Path, repo: Path, state: Path) -> dict[str, Any]:
    from svweave.adapters.mcp_client import McpSession
    from svweave.application.search import svw_search
    from svweave.domain.states import EvidenceKind
    from svweave.orchestration.registry import BackendRegistry
    from svweave.orchestration.toolchain import Toolchain
    from svweave.security.sandbox import HostCapability, SandboxRunner

    forced = SandboxRunner(
        allow_degraded=True, capability=HostCapability(False, None, None, "forced inside outer sandbox")
    )
    reg = BackendRegistry(Toolchain.load(repo), state / "backends", source.parent, sandbox=forced)
    tokens: set[str] = set()
    runtime: Path | None = None
    try:
        env = svw_search(snapshot_id=source.name, snapshot_root=source, query="demo_lane",
                         registry=reg, cursor_key=b"k" * 32)
        assert env.data["backend_used"] == "codebase-memory-mcp"
        assert env.data["degraded"] is True and "WITHOUT process isolation" in env.data["degraded_reason"]
        assert env.provenance.evidence_kind is EvidenceKind.SOURCE_CANDIDATE
        assert any(i["file"] == "demo/rtl/demo_lane.sv" for i in env.data["items"])
        ad = reg.cbm(source)
        assert ad.alive and ad.pid, "degraded env -i launch must still be tracked as alive"
        assert ad.isolation.value == "degraded_no_isolation" and ad.warnings
        assert isinstance(ad._session, McpSession)
        tokens.add(ad._session._client.token)
        assert token_processes(tokens), "the real backend must be observable before shutdown"
        assert ad._host_runtime is not None
        runtime = Path(ad._host_runtime)
        assert runtime.is_dir()
    finally:
        reg.shutdown()
    deadline = time.monotonic() + 10
    while token_processes(tokens) and time.monotonic() < deadline:
        time.sleep(0.1)
    assert token_processes(tokens) == [], "degraded cbm / daemon survived registry.shutdown()"
    assert runtime is not None and not runtime.exists(), "degraded runtime directory survived shutdown"
    return {"backend_used": env.data["backend_used"], "degraded": env.data["degraded"],
            "isolation": ad.isolation.value, "tokens": sorted(tokens),
            "processes_after_shutdown": [], "runtime_removed": True}


def degraded_slang(source: Path) -> dict[str, Any]:
    from svweave.adapters.slang.protocol import WorkerRequest
    from svweave.adapters.slang.runner import run_worker
    from svweave.security.sandbox import HostCapability, SandboxRunner

    no_bwrap = SandboxRunner(
        allow_degraded=True, capability=HostCapability(False, None, None, "control inside outer sandbox")
    )
    out = run_worker(
        WorkerRequest(snapshot_root=str(source), args=["--top", "t", "t.sv"]),
        timeout_s=60, memory_mib=4096, sandbox=no_bwrap,
    )
    assert out.kind == "ok", out.stderr_tail
    return {"kind": out.kind, "isolation": out.isolation,
            "isolation_warnings": list(out.isolation_warnings),
            "resource_usage": out.resource_usage, "stdout": out.stdout.decode()}


def main() -> None:
    name, source_arg, host_netns, host_pidns, *args = sys.argv[1:]
    source = Path(source_arg)
    evidence = safety(source, host_netns, host_pidns)
    if name == "ignored_diagnostics":
        result = ignored_diagnostics(source)
    elif name == "degraded_cbm":
        result = degraded_cbm(source, Path(args[0]), Path(args[1]))
    elif name == "degraded_slang":
        result = degraded_slang(source)
    else:
        raise ValueError(f"unknown native check: {name}")
    print(json.dumps({"safety": evidence, "result": result}))


if __name__ == "__main__":
    main()
