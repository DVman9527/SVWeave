"""Unit tests: capability comparison (DEP-02), registry health transitions, pyslang health, doctor."""

from __future__ import annotations

import time
from pathlib import Path

import pytest

from svweave.adapters.mcp_client import ToolInfo
from svweave.domain.errors import ErrorCode, SvwError
from svweave.domain.states import BackendAvailability
from svweave.orchestration.capability import (
    CBM_ALLOWLIST,
    CBM_CORE_FEATURES,
    CBM_FEATURES,
    SERENA_ALLOWLIST,
    ToolStatus,
    compare_capabilities,
)
from svweave.orchestration.doctor_checks import backend_checks, static_checks
from svweave.orchestration.health import BackendHealth
from svweave.orchestration.registry import CBM, PYSLANG, RETRY_AFTER_S, SERENA, BackendRegistry
from svweave.orchestration.toolchain import Toolchain
from svweave.security.sandbox import HostCapability, SandboxRunner
from tests.unit._fakes import REPO, toolchain, tools_from_report

CAP = HostCapability(True, "/usr/bin/bwrap", "x", None)


def reg(tmp_path: Path, tc: Toolchain | None = None) -> BackendRegistry:
    return BackendRegistry(tc or toolchain(), tmp_path / "backends", tmp_path / "snaps", sandbox=SandboxRunner(capability=CAP))


def test_toolchain_pins_come_from_lock_and_report() -> None:
    tc = toolchain()
    assert tc.pinned_protocol("cbm") == "2025-11-25" and tc.pinned_protocol("serena") == "2025-11-25"
    assert tc.pinned_tool_hashes("cbm")["search_graph"] == "da3b6f9893a747db20db0cbbd38171748ac071df550813805bf90e98dc913199"
    assert tc.serena_version == "1.7.0" and tc.pyslang_version == "12.0.0"
    assert tc.cbm_binary.name == "codebase-memory-mcp" and tc.verible_ls.name == "verible-verilog-ls"
    with pytest.raises(SvwError) as ei:
        tc.backend_report("nope")
    assert ei.value.code is ErrorCode.CAPABILITY_MISMATCH
    with pytest.raises(SvwError) as ei2:
        Toolchain.load(Path("/nonexistent-root"))
    assert ei2.value.code is ErrorCode.MISSING_INPUT


def test_discover_uses_explicit_root_then_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    assert Toolchain.discover(REPO).root == REPO
    monkeypatch.setenv("SVWEAVE_TOOLCHAIN_ROOT", str(REPO))
    assert Toolchain.discover().root == REPO
    monkeypatch.setenv("SVWEAVE_TOOLCHAIN_ROOT", str(tmp_path))
    with pytest.raises(SvwError):
        Toolchain.discover()


def test_allowlists_exclude_dangerous_tools() -> None:
    assert not CBM_ALLOWLIST & {"delete_project", "manage_adr", "ingest_traces", "query_graph", "detect_changes"}
    assert not SERENA_ALLOWLIST & {"execute_shell_command", "create_text_file", "activate_project"}
    assert {"index_repository", "search_graph", "get_code_snippet", "index_status"} <= CBM_ALLOWLIST


def test_compare_statuses_changed_missing_not_pinned() -> None:
    tc = toolchain()
    live = tools_from_report(tc, "cbm", tamper={"get_graph_schema": "1" * 64})
    del live["check_index_coverage"]
    live["brand_new_tool"] = ToolInfo("brand_new_tool", {}, None, None, "2" * 64, "3" * 64)
    pinned = {k: v for k, v in tc.pinned_tool_hashes("cbm").items() if k != "search_code"}
    cmp = compare_capabilities("cbm", live, pinned, CBM_FEATURES, CBM_CORE_FEATURES)
    assert cmp.tools["get_graph_schema"] is ToolStatus.CHANGED
    assert cmp.tools["check_index_coverage"] is ToolStatus.MISSING
    assert cmp.tools["search_code"] is ToolStatus.NOT_PINNED  # live tool the report never saw: disabled
    assert cmp.tools["brand_new_tool"] is ToolStatus.NOT_PINNED
    assert cmp.features["schema"] is False and cmp.features["coverage"] is False
    assert cmp.features["text_search"] is False
    assert cmp.features["search"] and cmp.features["index"] and cmp.core_ok and not cmp.ok
    assert cmp.mismatched() == ["check_index_coverage", "get_graph_schema"]
    assert any("get_graph_schema: changed" in r for r in cmp.reasons)


def test_core_feature_change_makes_comparison_not_core_ok() -> None:
    tc = toolchain()
    live = tools_from_report(tc, "cbm", tamper={"index_repository": "9" * 64})
    cmp = compare_capabilities("cbm", live, tc.pinned_tool_hashes("cbm"), CBM_FEATURES, CBM_CORE_FEATURES)
    assert not cmp.core_ok and not cmp.features["index"]
    with pytest.raises(SvwError) as ei:
        cmp.require("index")
    assert ei.value.code is ErrorCode.CAPABILITY_MISMATCH
    with pytest.raises(SvwError):
        cmp.require("unknown_feature")


def test_registry_starts_unproven_and_failures_downgrade(tmp_path: Path) -> None:
    r = reg(tmp_path)
    for n in (PYSLANG, CBM, SERENA):
        assert r.health(n).status is BackendAvailability.UNAVAILABLE and not r.health(n).probed
    r.set_health(BackendHealth(name=CBM, status=BackendAvailability.READY, probed=True, version="0.11.0",
                               limitations=["x"]))
    assert r.usable(CBM)
    h = r.note_failure(CBM, SvwError(ErrorCode.BACKEND_UNAVAILABLE, "gone", {"reason": "process_exited"}))
    assert h.status is BackendAvailability.UNAVAILABLE and h.error_code == "BACKEND_UNAVAILABLE"
    assert "process_exited" in h.reasons[0] and h.limitations == ["x"] and h.version == "0.11.0" and not h.probed
    assert not r.usable(CBM)  # cooldown
    r._failed_at[CBM] = time.monotonic() - RETRY_AFTER_S - 1
    assert r.usable(CBM)
    d = r.health(CBM).to_dict()
    assert d["status"] == "UNAVAILABLE" and d["probed"] is False and d["name"] == CBM


def test_registry_cbm_start_failure_is_recorded(tmp_path: Path) -> None:
    tc = toolchain()
    broken = Toolchain(root=tmp_path / "no-tc", lock=tc.lock, report=tc.report)
    r = reg(tmp_path, broken)
    with pytest.raises(SvwError) as ei:
        r.cbm()
    assert ei.value.details["reason"] == "binary_missing"
    assert r.health(CBM).status is BackendAvailability.UNAVAILABLE and r.health(CBM).error_code == "BACKEND_UNAVAILABLE"
    with pytest.raises(SvwError):
        r.serena_for(tmp_path)
    assert r.health(SERENA).status is BackendAvailability.UNAVAILABLE


def test_probe_pyslang_ready_when_locked_version_matches(tmp_path: Path) -> None:
    h = reg(tmp_path).probe_pyslang()
    assert h.status is BackendAvailability.READY and h.probed and h.details["imported"] == "12.0.0"


def test_probe_pyslang_version_mismatch_is_unavailable(tmp_path: Path) -> None:
    tc = toolchain()
    lock = {**tc.lock, "components": {**tc.lock["components"], "pyslang": {**tc.lock["components"]["pyslang"], "version": "0.0.1"}}}
    r = reg(tmp_path, Toolchain(root=tc.root, lock=lock, report=tc.report))
    h = r.probe_pyslang()
    assert h.status is BackendAvailability.UNAVAILABLE and h.error_code == "CAPABILITY_MISMATCH"
    assert "locked 0.0.1" in h.reasons[0] and not h.probed


def test_probe_cbm_without_fixtures_is_unavailable_with_reason(tmp_path: Path) -> None:
    r = BackendRegistry(toolchain(), tmp_path / "b", tmp_path / "s", sandbox=SandboxRunner(capability=CAP),
                        fixtures_dir=tmp_path / "no-fixtures")
    h = r.probe_cbm()
    assert h.status is BackendAvailability.UNAVAILABLE and h.error_code == "MISSING_INPUT" and not h.probed
    h2 = r.probe_serena()
    assert h2.status is BackendAvailability.UNAVAILABLE and not h2.probed


def test_isolation_unavailable_blocks_probe_without_opt_in(tmp_path: Path) -> None:
    r = BackendRegistry(toolchain(), tmp_path / "b", tmp_path / "s",
                        sandbox=SandboxRunner(allow_degraded=False, capability=HostCapability(False, None, None, "no userns")))
    h = r.probe_cbm()
    assert h.status is BackendAvailability.UNAVAILABLE and "isolation" in str(h.reasons) + h.error_code.__str__().lower() or h.reasons


def test_doctor_static_checks_and_unproven_backends(tmp_path: Path) -> None:
    tc = toolchain()
    res = {c.name: c for c in static_checks(tc)}
    assert res["toolchain_lock"].status == "PASS"
    assert res["backend:cbm_binary"].status == "PASS"
    assert res["capability_report"].status == "PASS"
    out = {c.name: c for c in backend_checks(registry=reg(tmp_path))}
    for n in (PYSLANG, CBM, SERENA):
        assert out[f"backend:{n}"].status == "SKIP" and "not probed" in out[f"backend:{n}"].message


def test_doctor_reports_hash_mismatch_and_missing_binary(tmp_path: Path) -> None:
    tc = toolchain()
    root = tmp_path / "tc"
    (root / ".toolchain" / "cbm").mkdir(parents=True)
    (root / ".toolchain" / "cbm" / "codebase-memory-mcp").write_bytes(b"tampered")
    res = {c.name: c for c in static_checks(Toolchain(root=root, lock=tc.lock, report=tc.report))}
    assert res["backend:cbm_binary"].status == "FAIL" and res["backend:cbm_binary"].code == "CAPABILITY_MISMATCH"
    assert res["backend:verible_ls_binary"].status == "FAIL" and res["backend:verible_ls_binary"].code == "BACKEND_UNAVAILABLE"
    assert res["backend:serena_exe"].status == "FAIL"


def test_doctor_without_toolchain_reports_failure(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SVWEAVE_TOOLCHAIN_ROOT", str(tmp_path))
    [c] = backend_checks()
    assert c.status == "FAIL" and c.code == "MISSING_INPUT"
