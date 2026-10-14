"""Health (READY only after a real fixture query), INT-04 (backend killed -> DEGRADED/UNAVAILABLE +
marked text fallback), doctor backend checks. Real cbm / Serena / pyslang."""

from __future__ import annotations

import os
import signal
import time
from collections.abc import Iterator

import pytest

from svweave.application.search import svw_search
from svweave.domain.envelope import Outcome
from svweave.domain.states import BackendAvailability, EvidenceKind
from svweave.orchestration.doctor_checks import backend_checks
from svweave.orchestration.registry import CBM, PYSLANG, SERENA, BackendRegistry
from svweave.orchestration.toolchain import Toolchain
from svweave.security.sandbox import SandboxRunner
from tests.integration.conftest import SID_A, copy_fixture_dirs

pytestmark = pytest.mark.integration


@pytest.fixture(scope="module")
def registry(
    toolchain: Toolchain, sandbox: SandboxRunner, tmp_path_factory: pytest.TempPathFactory
) -> Iterator[BackendRegistry]:
    base = tmp_path_factory.mktemp("ws")
    snaps = base / "snapshots"
    copy_fixture_dirs(snaps / SID_A)
    reg = BackendRegistry(toolchain, base / "backends", snaps, sandbox=sandbox)
    try:
        yield reg
    finally:
        reg.shutdown()


def test_unprobed_backends_are_never_ready(registry: BackendRegistry) -> None:
    for name in (PYSLANG, CBM, SERENA):
        h = registry.health(name)
        assert h.status is BackendAvailability.UNAVAILABLE and not h.probed
        assert "not probed" in h.reasons[0]


def test_pyslang_health_ready_only_when_version_equals_lock(registry: BackendRegistry) -> None:
    h = registry.probe_pyslang()
    assert h.status is BackendAvailability.READY and h.probed
    assert h.version == registry.toolchain.pyslang_version == h.details["imported"]


def test_cbm_probe_is_degraded_with_recorded_extraction_gaps(registry: BackendRegistry) -> None:
    h = registry.probe_cbm()
    assert h.status is BackendAvailability.DEGRADED, h
    assert h.isolation == "bwrap" and h.protocol_version == "2025-11-25" and h.version == "0.11.0"
    assert h.details["fixture_hits"] == {"demo_lane": True, "demo_top": True}
    assert "uvm/svw_demo_uvm_pkg.sv" in h.details["parse_unusable"]
    assert any("parse_unusable" in r for r in h.reasons)
    assert all(h.features.values())
    assert any("ports, generate blocks" in lim for lim in h.limitations)


def test_serena_probe_ready_for_the_declared_subset(registry: BackendRegistry) -> None:
    h = registry.probe_serena()
    assert h.status is BackendAvailability.READY and h.probed, h
    assert h.details["task_route"] == "exact_range"
    assert all(h.details["checks"].values())
    assert any("task symbols" in lim for lim in h.limitations)
    assert any("Function" in lim for lim in h.limitations)


def test_doctor_backend_checks_static_and_unproven(registry: BackendRegistry) -> None:
    res = {c.name: c for c in backend_checks(registry=registry)}
    assert res["toolchain_lock"].status == "PASS"
    assert res["backend:cbm_binary"].status == "PASS" and res["backend:verible_ls_binary"].status == "PASS"
    assert res["isolation:bwrap"].status == "PASS"
    # health lines come from the probes above
    assert res[f"backend:{CBM}"].status == "WARN" and res[f"backend:{SERENA}"].status == "PASS"
    assert res[f"backend:{PYSLANG}"].status == "PASS"


def test_int04_killed_cbm_degrades_health_and_search_falls_back_to_marked_text(
    registry: BackendRegistry,
) -> None:
    snap = registry.snapshots_root / SID_A
    ok = svw_search(snapshot_id=SID_A, snapshot_root=snap, query="demo_lane", registry=registry, cursor_key=b"k" * 32)
    assert ok.data["backend_used"] == "codebase-memory-mcp" and ok.data["degraded"] is False
    assert ok.provenance.backend == "codebase-memory-mcp"
    assert ok.provenance.evidence_kind is EvidenceKind.SOURCE_CANDIDATE
    assert ok.analysis_coverage is not None and ok.analysis_coverage.limitations
    assert any(i["file"] == "demo/rtl/demo_lane.sv" for i in ok.data["items"])
    assert ok.snapshot_id == SID_A

    ad = registry.cbm(snap)
    assert ad.pid
    os.kill(ad.pid, signal.SIGKILL)
    deadline = time.monotonic() + 15
    while ad.alive and time.monotonic() < deadline:
        time.sleep(0.2)

    env = svw_search(snapshot_id=SID_A, snapshot_root=snap, query="demo_lane", registry=registry, cursor_key=b"k" * 32)
    assert env.data["backend_used"] == "text_fallback" and env.data["degraded"] is True
    assert env.outcome is Outcome.PARTIAL
    assert env.provenance.backend == "text_fallback"
    assert env.provenance.evidence_kind is EvidenceKind.SOURCE_CANDIDATE
    assert any(w.startswith("DEGRADED") for w in env.warnings)
    assert any("BACKEND_UNAVAILABLE" in w for w in env.warnings)
    assert {i["file"] for i in env.data["items"]} >= {"demo/rtl/demo_lane.sv", "demo/rtl/demo_top.sv"}
    h = registry.health(CBM)
    assert h.status is BackendAvailability.UNAVAILABLE and h.error_code == "BACKEND_UNAVAILABLE"
    # a successful text search must not make the graph backend look healthy
    assert registry.health(CBM).status is BackendAvailability.UNAVAILABLE
    # inside the cooldown the dead backend is not even tried again
    assert not registry.usable(CBM)

    # after the cooldown the registry restarts cbm and the graph path works again
    registry._failed_at[CBM] -= 60
    again = svw_search(snapshot_id=SID_A, snapshot_root=snap, query="demo_top", registry=registry, cursor_key=b"k" * 32)
    assert again.data["backend_used"] == "codebase-memory-mcp" and again.data["degraded"] is False
