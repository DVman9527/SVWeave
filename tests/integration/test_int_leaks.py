"""No backend process (cbm server, its daemon child, Serena, Verible) outlives its adapter, in
bwrap mode, in degraded mode and after registry probes. Runs without module-scoped backends."""

from __future__ import annotations

import os
import subprocess
import time
from pathlib import Path

import pytest

from svweave.adapters.codebase_memory import CbmAdapter
from svweave.adapters.mcp_client import TOKEN_ENV, McpClient, McpLaunch
from svweave.adapters.serena import SerenaAdapter
from svweave.application.search import svw_search
from svweave.domain.states import EvidenceKind
from svweave.orchestration.registry import CBM, BackendRegistry
from svweave.orchestration.toolchain import Toolchain
from svweave.security.sandbox import HostCapability, SandboxRunner
from tests.integration.conftest import SID_A, copy_fixture_dirs, skip_or_fail

pytestmark = pytest.mark.integration
KEY = b"k" * 32


#: Per-client SVWEAVE_BACKEND_TOKENs of every McpClient THIS test process created. Only processes whose
#: environment carries one of these are "ours"; an unrelated host process that merely mentions
#: codebase-memory-mcp/serena/verible in its argv (an editor, a reviewer's prompt) is never counted.
_OWN_TOKENS: set[str] = set()


@pytest.fixture(autouse=True)
def _record_client_tokens(monkeypatch: pytest.MonkeyPatch) -> None:
    orig = McpClient.__init__

    def init(self: McpClient, launch: McpLaunch) -> None:
        orig(self, launch)
        _OWN_TOKENS.add(self.token)

    monkeypatch.setattr(McpClient, "__init__", init)


def backend_procs() -> list[int]:
    """Live processes (server, daemon children, setsid-detached ones) started by this test process:
    identified by the per-client token in /proc/<pid>/environ, never by their command line."""
    needles = [f"{TOKEN_ENV}={t}".encode() for t in _OWN_TOKENS]
    if not needles:
        return []
    out = []
    for d in os.listdir("/proc"):
        if not d.isdigit() or int(d) == os.getpid():
            continue
        try:
            env = Path(f"/proc/{d}/environ").read_bytes().split(b"\0")
        except OSError:
            continue
        if any(n in env for n in needles):
            out.append(int(d))
    return out


def test_unrelated_process_mentioning_backend_names_is_not_counted() -> None:
    """C1 regression: a foreign process whose argv contains the backend names must be ignored."""
    p = subprocess.Popen(["sleep", "30", "codebase-memory-mcp", "serena", "verible-verilog-ls"])
    try:
        assert p.pid not in backend_procs()
    finally:
        p.kill()
        p.wait()


def wait_gone(deadline_s: float = 10.0) -> list[int]:
    end = time.monotonic() + deadline_s
    while backend_procs() and time.monotonic() < end:
        time.sleep(0.2)
    return backend_procs()


def test_no_cbm_process_outlives_its_adapter_in_bwrap_mode(
    toolchain: Toolchain, sandbox: SandboxRunner, snapshots_root: Path, tmp_path: Path
) -> None:
    assert not backend_procs(), f"fixture: stray backend processes before the test: {backend_procs()}"
    ad = CbmAdapter(toolchain, tmp_path / "st", snapshots_root, sandbox)
    ad.start(snapshots_root / SID_A)
    ad.ensure_indexed(SID_A, snapshots_root / SID_A)
    assert ad.pid and backend_procs()
    ad.stop()
    assert wait_gone() == [], "cbm (or its --cbm-daemon-internal child) survived adapter.stop()"


def test_f4_degraded_mode_runs_for_real_marks_results_degraded_and_leaves_nothing_behind(
    toolchain: Toolchain, snapshots_root: Path, tmp_path: Path
) -> None:
    if not toolchain.cbm_binary.is_file():
        skip_or_fail("cbm binary not installed")
    assert not backend_procs()
    forced = SandboxRunner(allow_degraded=True, capability=HostCapability(False, None, None, "forced for test"))
    reg = BackendRegistry(toolchain, tmp_path / "backends", snapshots_root, sandbox=forced)
    try:
        env = svw_search(snapshot_id=SID_A, snapshot_root=snapshots_root / SID_A, query="demo_lane",
                         registry=reg, cursor_key=KEY)
        assert env.data["backend_used"] == "codebase-memory-mcp"
        assert env.data["degraded"] is True and "WITHOUT process isolation" in env.data["degraded_reason"]
        assert env.provenance.evidence_kind is EvidenceKind.SOURCE_CANDIDATE
        assert any(i["file"] == "demo/rtl/demo_lane.sv" for i in env.data["items"])
        ad = reg.cbm(snapshots_root / SID_A)
        assert ad.alive and ad.pid, "degraded (env -i) launch must still be tracked as alive"
        assert ad.isolation.value == "degraded_no_isolation" and ad.warnings
    finally:
        reg.shutdown()
    assert wait_gone() == [], "degraded-mode cbm / daemon survived registry.shutdown()"


def test_registry_probe_leaves_no_process_behind(toolchain: Toolchain, sandbox: SandboxRunner, tmp_path: Path) -> None:
    assert not backend_procs()
    snaps = tmp_path / "snaps"
    snaps.mkdir()
    reg = BackendRegistry(toolchain, tmp_path / "b", snaps, sandbox=sandbox)
    try:
        h = reg.probe_cbm()
        assert h.status.value in ("READY", "DEGRADED") and h.name == CBM
        assert wait_gone() == [], "probe_cbm left a backend running"
        h2 = reg.probe_serena()
        assert h2.status.value == "READY"
        assert wait_gone() == [], "probe_serena left a backend running"
    finally:
        reg.shutdown()


def test_serena_no_process_outlives_adapter_and_python_bind_is_narrow(
    toolchain: Toolchain, sandbox: SandboxRunner, tmp_path: Path
) -> None:
    cand = copy_fixture_dirs(tmp_path / "cand", ("demo",))
    ad = SerenaAdapter(toolchain, tmp_path / "st", cand, sandbox)
    spec = ad._spec({})
    real = Path(os.path.realpath(toolchain.serena_venv / "bin" / "python"))
    assert real.parents[1] not in [Path(p) for p in spec.ro_paths] or str(real).startswith("/usr/bin")
    ad.start()
    try:
        assert ad.find_symbol(cand, "demo_lane", "demo/rtl/demo_lane.sv")
        # host-only logs / ctx are not writable from inside
        p = sandbox.run_once(spec, ["/bin/sh", "-c", f"touch {ad.paths.logs}/x; touch {ad.paths.ctx_dir}/x"])
        assert p.returncode != 0 and not (ad.paths.logs / "x").exists() and not (ad.paths.ctx_dir / "x").exists()
    finally:
        ad.stop()
    assert wait_gone() == []
