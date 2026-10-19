"""INT-01, INT-03, API-05 (pin), DEP-02 (live), SEC-05 (real cbm) against the real pinned cbm."""

from __future__ import annotations

import os
import socket
from pathlib import Path

import pytest

from svweave.adapters.codebase_memory import CbmAdapter
from svweave.adapters.codebase_memory.models import IndexMapping, tree_fingerprint
from svweave.domain.errors import ErrorCode, SvwError
from svweave.domain.states import EvidenceKind
from svweave.orchestration.capability import CBM_CORE_FEATURES, CBM_FEATURES, compare_capabilities
from svweave.orchestration.toolchain import Toolchain
from svweave.security.sandbox import IsolationMode
from tests.integration.conftest import SID_A, SID_B

pytestmark = pytest.mark.integration


def test_cbm_runs_isolated_with_matching_capabilities(cbm: CbmAdapter, toolchain: Toolchain) -> None:
    assert cbm.isolation is IsolationMode.BWRAP and not cbm.warnings
    assert cbm.comparison is not None and cbm.comparison.ok, cbm.comparison
    assert cbm.init_info is not None
    assert cbm.init_info.protocol_version == toolchain.pinned_protocol("cbm") == "2025-11-25"
    assert cbm.init_info.server_version == "0.11.0"
    # the cbm HTTP UI (default ON upstream) must be off in our dedicated state
    with pytest.raises(OSError), socket.create_connection(("127.0.0.1", 9749), timeout=1):
        pass
    pid = cbm.pid
    assert pid is not None
    cmd = Path(f"/proc/{pid}/cmdline").read_bytes().split(b"\0")
    assert b"--unshare-net" in cmd and b"--die-with-parent" in cmd


def test_int01_index_snapshot_and_get_candidates_and_record_gaps(cbm: CbmAdapter, snapshots_root: Path) -> None:
    root = snapshots_root / SID_A
    before = tree_fingerprint(root)
    m = cbm.ensure_indexed(SID_A, root)
    assert tree_fingerprint(root) == before, "indexing must not modify the snapshot"
    assert m.nodes > 0 and m.edges > 0 and m.snapshot_id == SID_A
    assert m.content_fingerprint == before
    assert cbm.mapping(SID_A) == m

    for name in ("demo_lane", "demo_top"):
        r = cbm.search(SID_A, name)
        hits = [c for c in r.candidates if c.name == name]
        assert hits, f"{name} not returned"
        c = hits[0]
        assert c.file == f"demo/rtl/{name}.sv"
        assert c.evidence_kind == EvidenceKind.SOURCE_CANDIDATE.value
        assert c.backend == "codebase-memory-mcp" and c.snapshot_id == SID_A
        assert c.start_line and c.end_line and c.start_line <= c.end_line
        assert c.coverage["status"] == "no_recorded_issue"
    # source of the candidate
    snip = cbm.code_snippet(SID_A, cbm.search(SID_A, "demo_lane").candidates[0].upstream_qualified_name)
    assert "module demo_lane" in snip["source"]
    assert "file_path" not in snip and "qualified_name" not in snip  # no absolute host paths

    # ---- recorded gaps (actual M0 findings, surfaced as limitations, never hidden)
    assert "uvm/svw_demo_uvm_pkg.sv" in m.parse_unusable
    assert any(p == "demo/sva/demo_bind.sv" for p, _ in m.parse_partial)
    r = cbm.search(SID_A, "demo_lane")
    assert any("parse_unusable" in lim and "svw_demo_uvm_pkg.sv" in lim for lim in r.limitations)
    assert any("ports, generate blocks" in lim for lim in r.limitations)
    # classes of the UVM package are NOT in the graph (file is parse_unusable): honest absence
    assert not [c for c in cbm.search(SID_A, "svw_driver").candidates if c.name == "svw_driver"]
    # a hit inside the unusable file carries that coverage status
    pkg_hits = [c for c in cbm.search(SID_A, "run_phase").candidates if c.file == "uvm/svw_demo_uvm_pkg.sv"]
    assert pkg_hits and all(c.coverage["status"] == "parse_unusable" for c in pkg_hits)
    # cbm labels SV modules as Class: reported unchanged
    assert {c.upstream_label for c in cbm.search(SID_A, "demo_lane").candidates} >= {"Class"}


def test_int01_reindex_is_idempotent_and_reuses_verified_index(cbm: CbmAdapter, snapshots_root: Path) -> None:
    m1 = cbm.ensure_indexed(SID_A, snapshots_root / SID_A)
    m2 = cbm.ensure_indexed(SID_A, snapshots_root / SID_A)
    assert m1 == m2


def test_pagination_offsets(cbm: CbmAdapter, snapshots_root: Path) -> None:
    cbm.ensure_indexed(SID_A, snapshots_root / SID_A)
    full = cbm.search(SID_A, "demo_lane", limit=10)
    assert full.total >= 2
    p1 = cbm.search(SID_A, "demo_lane", limit=1, offset=0)
    p2 = cbm.search(SID_A, "demo_lane", limit=1, offset=1)
    assert p1.has_more and p1.candidates[0].upstream_qualified_name != p2.candidates[0].upstream_qualified_name


# ------------------------------------------------------------------------------- INT-03
def test_int03_unindexed_snapshot_is_refused_not_guessed(cbm: CbmAdapter) -> None:
    with pytest.raises(SvwError) as ei:
        cbm.search("c" * 64, "demo_lane")
    assert ei.value.code is ErrorCode.STALE_SNAPSHOT
    assert ei.value.details["reason"] == "snapshot_not_indexed"


def test_int03_two_snapshots_have_distinct_projects_and_never_mix(cbm: CbmAdapter, snapshots_root: Path) -> None:
    ma = cbm.ensure_indexed(SID_A, snapshots_root / SID_A)
    mb = cbm.ensure_indexed(SID_B, snapshots_root / SID_B)
    assert ma.project != mb.project
    # snapshot B has no UVM package: its results must not contain A's files
    files_b = {c.file for c in cbm.search(SID_B, "svw_demo_uvm_pkg").candidates}
    assert not any(f.startswith("uvm/") for f in files_b)
    # a qualified name from A's project cannot be used to read through B
    qn_a = cbm.search(SID_A, "demo_lane").candidates[0].upstream_qualified_name
    with pytest.raises(SvwError) as ei:
        cbm.code_snippet(SID_B, qn_a)
    assert ei.value.code is ErrorCode.STALE_SNAPSHOT and ei.value.details["reason"] == "foreign_project"


def test_int03_tampered_mapping_to_other_project_is_refused(cbm: CbmAdapter, snapshots_root: Path) -> None:
    ma = cbm.ensure_indexed(SID_A, snapshots_root / SID_A)
    mb = cbm.ensure_indexed(SID_B, snapshots_root / SID_B)
    bad = IndexMapping.from_json({**ma.to_json(), "project": mb.project})
    cbm.mappings.put(bad)
    try:
        with pytest.raises(SvwError) as ei:
            cbm.search(SID_A, "demo_lane")
        assert ei.value.code is ErrorCode.STALE_SNAPSHOT
        assert ei.value.details["reason"] == "index_snapshot_mismatch"
        assert "root_path" in ei.value.details["mismatch"]
    finally:
        cbm.mappings.put(ma)
    assert cbm.search(SID_A, "demo_lane").candidates


def test_int03_content_drift_detected_by_deep_check(cbm: CbmAdapter, snapshots_root: Path, tmp_path: Path) -> None:
    root = snapshots_root / SID_B
    m = cbm.ensure_indexed(SID_B, root)
    victim = root / "demo" / "rtl" / "demo_lane.sv"
    original = victim.read_bytes()
    try:
        victim.write_bytes(original + b"\n// drift\n")
        with pytest.raises(SvwError) as ei:
            cbm.verify_binding(SID_B, deep=True)
        assert "content_fingerprint" in ei.value.details["mismatch"]
        # ensure_indexed notices the changed fingerprint and rebuilds instead of trusting the name
        m2 = cbm.ensure_indexed(SID_B, root)
        assert m2.content_fingerprint != m.content_fingerprint
    finally:
        victim.write_bytes(original)
        cbm.ensure_indexed(SID_B, root)


def test_snapshot_outside_bound_snapshots_dir_is_refused(cbm: CbmAdapter, tmp_path: Path) -> None:
    other = tmp_path / "elsewhere"
    other.mkdir()
    with pytest.raises(SvwError) as ei:
        cbm.ensure_indexed("d" * 64, other)
    assert ei.value.code is ErrorCode.PATH_NOT_ALLOWED


# ------------------------------------------------------------------------------- allowlist, DEP-02, SEC-05
def test_dangerous_tools_are_unreachable_through_the_adapter(cbm: CbmAdapter) -> None:
    for tool in ("delete_project", "manage_adr", "ingest_traces", "query_graph", "detect_changes"):
        with pytest.raises(SvwError) as ei:
            cbm._call(tool, {"project": "x"}, "index")
        assert ei.value.code is ErrorCode.POLICY_DENIED
    # the real project still exists after those attempts
    assert cbm.search(SID_A, "demo_lane").candidates


def test_dep02_live_schema_vs_tampered_report_disables_feature(cbm: CbmAdapter, toolchain: Toolchain) -> None:
    hashes = dict(toolchain.pinned_tool_hashes("cbm"))
    ok = compare_capabilities("cbm", cbm.tool_infos, hashes, CBM_FEATURES, CBM_CORE_FEATURES)
    assert ok.ok
    hashes["search_graph"] = "0" * 64
    bad = compare_capabilities("cbm", cbm.tool_infos, hashes, CBM_FEATURES, CBM_CORE_FEATURES)
    assert not bad.features["search"] and bad.features["index"] and not bad.core_ok
    with pytest.raises(SvwError) as ei:
        bad.require("search")
    assert ei.value.code is ErrorCode.CAPABILITY_MISMATCH
    assert ei.value.details["mismatched_tools"] == ["search_graph"]


def test_sec05_real_cbm_cannot_see_original_source_outside_its_mounts(
    cbm: CbmAdapter, tmp_path: Path
) -> None:
    """Bypass the adapter's own path check: ask the REAL cbm to index a host directory that was
    never mounted into its sandbox. The OS view, not our validation, must stop it."""
    original = tmp_path / "original_source"
    original.mkdir()
    (original / "secret.sv").write_text("module secret_ip; endmodule\n")
    before = tree_fingerprint(original)
    with pytest.raises(SvwError) as ei:
        cbm._call("index_repository", {"repo_path": str(original), "mode": "fast"}, "index")
    assert ei.value.details["reason"] == "tool_error"
    # the backend itself reports that it cannot find / use the path (it does not exist in its view)
    assert "secret" not in ei.value.details["text"]
    assert not [c for c in cbm.search(SID_A, "secret_ip").candidates if c.name == "secret_ip"]
    assert tree_fingerprint(original) == before and sorted(os.listdir(original)) == ["secret.sv"]
