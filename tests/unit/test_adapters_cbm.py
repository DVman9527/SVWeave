"""Unit tests for the cbm adapter with scripted fake sessions (error branches). Real-backend
behaviour is covered by tests/integration/test_int_cbm.py."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from svweave.adapters.codebase_memory import CbmAdapter, IndexMapping
from svweave.adapters.codebase_memory import normalize as nz
from svweave.adapters.codebase_memory.models import MappingStore, tree_fingerprint
from svweave.adapters.mcp_client import ToolResult
from svweave.domain.errors import ErrorCode, SvwError
from svweave.orchestration.capability import CBM_CORE_FEATURES, CBM_FEATURES, compare_capabilities
from svweave.orchestration.toolchain import Toolchain
from svweave.security.sandbox import HostCapability, SandboxRunner
from tests.unit._fakes import FakeSession, jtext, toolchain, tools_from_report

SID = "1" * 64
PROJ = "proj-x"


def make(tmp_path: Path, tamper: dict[str, str] | None = None) -> tuple[CbmAdapter, FakeSession, Path]:
    tc = toolchain()
    snaps = tmp_path / "snaps"
    root = snaps / SID
    (root / "demo").mkdir(parents=True)
    (root / "demo" / "demo_lane.sv").write_text("module demo_lane; endmodule\n")
    fake = FakeSession(tools_from_report(tc, "cbm", tamper=tamper))
    ad = CbmAdapter(tc, tmp_path / "state", snaps, SandboxRunner(capability=HostCapability(True, "/usr/bin/bwrap", "x", None)),
                    verify_binary=False, poll_interval=0.0, index_timeout=0.3)
    ad._session = fake
    ad._bound_root = root.resolve()  # pretend this snapshot is already mounted (no restart)
    ad.comparison = compare_capabilities("cbm", fake.tools, tc.pinned_tool_hashes("cbm"), CBM_FEATURES, CBM_CORE_FEATURES)
    return ad, fake, root


def script_index(fake: FakeSession, root: Path, *, nodes: int = 5, edges: int = 7, root_path: str | None = None,
                 statuses: list[str] | None = None) -> None:
    seq = list(statuses or ["ready"])
    fake.handlers["index_repository"] = lambda a: jtext({
        "project": PROJ, "status": "indexed", "nodes": nodes, "edges": edges,
        "parse_unusable": {"files": [{"path": "uvm/p.sv", "whole_file": True}]},
        "parse_partial": {"files": [{"path": "demo/b.sv", "error_ranges": "7-7"}]}})

    def status(a: dict[str, Any]) -> str:
        st = seq.pop(0) if len(seq) > 1 else seq[0]
        return jtext({"project": a["project"], "status": st, "nodes": nodes, "edges": edges,
                      "root_path": root_path or str(root), "indexed_at": "2026-01-01T00:00:00Z"})

    fake.handlers["index_status"] = status


ROWS = {"cols": ["qn", "label", "file", "lines", "rank"], "total": 2, "has_more": False,
        "rows": [[f"{PROJ}.demo.demo_lane.demo_lane", "Class", "demo/demo_lane.sv", "2-17", -8.5],
                 [f"{PROJ}.demo.demo_lane", "Module", "demo/demo_lane.sv", "1-19", -3.5]]}


def test_ensure_indexed_records_mapping_and_persists_it(tmp_path: Path) -> None:
    ad, fake, root = make(tmp_path)
    script_index(fake, root)
    m = ad.ensure_indexed(SID, root)
    assert (m.snapshot_id, m.project, m.nodes, m.edges) == (SID, PROJ, 5, 7)
    assert m.content_fingerprint == tree_fingerprint(root)
    assert m.parse_unusable == ("uvm/p.sv",) and m.parse_partial == (("demo/b.sv", "7-7"),)
    call = [a for n, a in fake.calls if n == "index_repository"][0]
    assert call == {"repo_path": str(root.resolve()), "mode": "full"}  # nothing else is ever passed
    again = MappingStore(ad.paths.mapping_file).get(SID)
    assert again == m  # JSON file survives a restart


def test_ensure_indexed_reuses_verified_index_and_rebuilds_on_content_change(tmp_path: Path) -> None:
    ad, fake, root = make(tmp_path)
    script_index(fake, root)
    ad.ensure_indexed(SID, root)
    n_index = lambda: len([1 for n, _ in fake.calls if n == "index_repository"])  # noqa: E731
    ad.ensure_indexed(SID, root)
    assert n_index() == 1
    (root / "demo" / "demo_lane.sv").write_text("module changed; endmodule\n")
    ad.ensure_indexed(SID, root)
    assert n_index() == 2


def test_index_polling_is_bounded(tmp_path: Path) -> None:
    ad, fake, root = make(tmp_path)
    script_index(fake, root, statuses=["indexing", "indexing", "ready"])
    assert ad.ensure_indexed(SID, root).project == PROJ
    ad2, fake2, root2 = make(tmp_path / "b")
    script_index(fake2, root2, statuses=["indexing"])
    with pytest.raises(SvwError) as ei:
        ad2.ensure_indexed(SID, root2)
    assert ei.value.code is ErrorCode.BUILD_TIMEOUT and ei.value.details["reason"] == "index_timeout"
    ad3, fake3, root3 = make(tmp_path / "c")
    script_index(fake3, root3, statuses=["error"])
    with pytest.raises(SvwError) as ei2:
        ad3.ensure_indexed(SID, root3)
    assert ei2.value.details["reason"] == "index_failed"


def test_snapshot_root_must_be_inside_bound_dir_and_exist(tmp_path: Path) -> None:
    ad, fake, root = make(tmp_path)
    script_index(fake, root)
    with pytest.raises(SvwError) as ei:
        ad.ensure_indexed(SID, tmp_path)
    assert ei.value.code is ErrorCode.PATH_NOT_ALLOWED
    with pytest.raises(SvwError) as ei2:
        ad.ensure_indexed(SID, root / "nope")
    assert ei2.value.code is ErrorCode.MISSING_INPUT
    assert not fake.calls


@pytest.mark.parametrize(
    ("override", "expect"),
    [({"root_path": "/somewhere/else"}, "root_path"), ({"nodes": 99}, "graph_counts"),
     ({"status": "indexing"}, "status=indexing"), ({"project": "other"}, "project_name")],
)
def test_verify_binding_refuses_every_kind_of_mismatch(tmp_path: Path, override: dict[str, Any], expect: str) -> None:
    ad, fake, root = make(tmp_path)
    script_index(fake, root)
    ad.ensure_indexed(SID, root)
    good = json.loads(fake.handlers["index_status"]({"project": PROJ}))  # type: ignore[arg-type]
    fake.handlers["index_status"] = lambda a: jtext({**good, **override})
    fake.handlers["search_graph"] = lambda a: jtext(ROWS)
    with pytest.raises(SvwError) as ei:
        ad.search(SID, "demo_lane")
    assert ei.value.code is ErrorCode.STALE_SNAPSHOT
    assert expect in ei.value.details["mismatch"]
    assert not any(n == "search_graph" for n, _ in fake.calls), "must refuse BEFORE querying"


def test_project_vanished_upstream_is_stale_not_unavailable(tmp_path: Path) -> None:
    ad, fake, root = make(tmp_path)
    script_index(fake, root)
    ad.ensure_indexed(SID, root)
    fake.handlers["index_status"] = lambda a: jtext({"error": "project not found or not indexed"})
    with pytest.raises(SvwError) as ei:
        ad.verify_binding(SID)
    assert ei.value.code is ErrorCode.STALE_SNAPSHOT and ei.value.details["reason"] == "project_not_found"


def test_search_takes_project_from_mapping_only_and_normalizes(tmp_path: Path) -> None:
    ad, fake, root = make(tmp_path)
    script_index(fake, root)
    ad.ensure_indexed(SID, root)
    fake.handlers["search_graph"] = lambda a: jtext(ROWS)
    r = ad.search(SID, "demo_lane", file_pattern="demo/*", limit=7, offset=3)
    (name, args) = [c for c in fake.calls if c[0] == "search_graph"][0]
    assert args == {"project": PROJ, "query": "demo_lane", "limit": 7, "offset": 3, "format": "json",
                    "file_pattern": "demo/*"}
    c = r.candidates[0]
    assert (c.name, c.upstream_label, c.file, c.start_line, c.end_line) == ("demo_lane", "Class", "demo/demo_lane.sv", 2, 17)
    assert c.evidence_kind == "SOURCE_CANDIDATE" and c.backend == "codebase-memory-mcp"
    assert c.qualified_name == "demo.demo_lane.demo_lane" and not c.qualified_name.startswith(PROJ)
    assert c.coverage == {"status": "no_recorded_issue"}
    assert any("parse_unusable" in x and "uvm/p.sv" in x for x in r.limitations)
    assert any("parse_partial" in x and "demo/b.sv" in x for x in r.limitations)


def test_hits_from_a_foreign_project_or_with_unsafe_paths_are_dropped_and_reported(tmp_path: Path) -> None:
    ad, fake, root = make(tmp_path)
    script_index(fake, root)
    ad.ensure_indexed(SID, root)
    rows = {**ROWS, "rows": ROWS["rows"] + [
        ["other-proj.x.y", "Class", "x.sv", "1-2", -1.0],
        [f"{PROJ}.a.b", "Class", "/etc/passwd", "1-2", -1.0],
        [f"{PROJ}.a.c", "Class", "../../escape.sv", "1-2", -1.0],
    ]}
    fake.handlers["search_graph"] = lambda a: jtext(rows)
    r = ad.search(SID, "x")
    assert len(r.candidates) == 2
    assert len([w for w in r.warnings if w.startswith("dropped hit")]) == 3


def test_code_snippet_refuses_foreign_qualified_name(tmp_path: Path) -> None:
    ad, fake, root = make(tmp_path)
    script_index(fake, root)
    ad.ensure_indexed(SID, root)
    with pytest.raises(SvwError) as ei:
        ad.code_snippet(SID, "other-proj.demo.x")
    assert ei.value.code is ErrorCode.STALE_SNAPSHOT and ei.value.details["reason"] == "foreign_project"
    fake.handlers["get_code_snippet"] = lambda a: (
        "name: x\nqualified_name: proj-x.demo.x\nfile_path: /home/u/secret/x.sv\nstart_line: 1\nsource: |\n  module x;\n  endmodule\ncallers: 0\n")
    s = ad.code_snippet(SID, f"{PROJ}.demo.x")
    assert s["source"] == "module x;\nendmodule" and "file_path" not in s and "qualified_name" not in s


def test_allowlist_blocks_every_other_tool(tmp_path: Path) -> None:
    ad, fake, _ = make(tmp_path)
    for t in ("delete_project", "manage_adr", "ingest_traces", "query_graph", "trace_path", "detect_changes",
              "compare_graphs", "get_architecture", "get_file_outline", "made_up"):
        with pytest.raises(SvwError) as ei:
            ad._call(t, {}, "index")
        assert ei.value.code is ErrorCode.POLICY_DENIED
    assert not fake.calls


def test_dep02_changed_schema_disables_only_that_feature(tmp_path: Path) -> None:
    ad, fake, root = make(tmp_path, tamper={"search_graph": "0" * 64})
    assert ad.comparison is not None
    ad.comparison = compare_capabilities("cbm", fake.tools, toolchain().pinned_tool_hashes("cbm"),
                                         CBM_FEATURES, CBM_CORE_FEATURES)
    # tampering was applied to the LIVE tools; the pinned report is unchanged -> mismatch
    assert not ad.comparison.features["search"] and ad.comparison.features["index"]
    script_index(fake, root)
    ad.ensure_indexed(SID, root)  # indexing still works
    with pytest.raises(SvwError) as ei:
        ad.search(SID, "x")
    assert ei.value.code is ErrorCode.CAPABILITY_MISMATCH
    assert ei.value.details["feature"] == "search" and ei.value.details["mismatched_tools"] == ["search_graph"]
    assert not any(n == "search_graph" for n, _ in fake.calls)


def test_missing_tool_in_live_list_is_a_mismatch(tmp_path: Path) -> None:
    tc = toolchain()
    live = tools_from_report(tc, "cbm")
    del live["get_code_snippet"]
    cmp = compare_capabilities("cbm", live, tc.pinned_tool_hashes("cbm"), CBM_FEATURES, CBM_CORE_FEATURES)
    assert not cmp.features["snippet"] and cmp.core_ok and not cmp.ok
    assert cmp.tools["get_code_snippet"].value == "missing"


def test_tool_error_and_garbage_output_map_to_backend_unavailable(tmp_path: Path) -> None:
    ad, fake, root = make(tmp_path)
    script_index(fake, root)
    ad.ensure_indexed(SID, root)
    fake.handlers["search_graph"] = lambda a: ToolResult("search_graph", "boom /home/u/secret", True, None, 0.0)
    with pytest.raises(SvwError) as ei:
        ad.search(SID, "x")
    assert ei.value.details["reason"] == "tool_error" and "/home/u" not in ei.value.details["text"]
    fake.handlers["search_graph"] = lambda a: "not json at all"
    with pytest.raises(SvwError) as ei2:
        ad.search(SID, "x")
    assert ei2.value.details["reason"] == "bad_output"
    fake.handlers["search_graph"] = lambda a: jtext({"error": "something odd"})
    with pytest.raises(SvwError) as ei3:
        ad.search(SID, "x")
    assert ei3.value.details["reason"] == "tool_error"


def test_dead_backend_is_sticky_unavailable(tmp_path: Path) -> None:
    ad, fake, root = make(tmp_path)
    script_index(fake, root)
    ad.ensure_indexed(SID, root)
    fake.up = False
    for _ in range(2):
        with pytest.raises(SvwError) as ei:
            ad.search(SID, "x")
        assert ei.value.code is ErrorCode.BACKEND_UNAVAILABLE and ei.value.details["reason"] == "process_exited"
    assert not ad.alive and ad.describe()["failure"]


def test_upstream_failure_during_call_marks_adapter_failed(tmp_path: Path) -> None:
    ad, fake, root = make(tmp_path)
    script_index(fake, root)
    ad.ensure_indexed(SID, root)
    fake.raise_on_call = SvwError(ErrorCode.BACKEND_UNAVAILABLE, "pipe closed", {"reason": "process_exited"})
    with pytest.raises(SvwError):
        ad.search(SID, "x")
    fake.raise_on_call = None
    with pytest.raises(SvwError):
        ad.search(SID, "x")  # stays failed until the registry restarts it


def test_not_running_adapter_refuses(tmp_path: Path) -> None:
    ad, _, root = make(tmp_path)
    ad._session = None
    with pytest.raises(SvwError) as ei:
        ad.ensure_indexed(SID, root)
    assert ei.value.details["reason"] == "not_running"


def test_binary_hash_mismatch_blocks_start(tmp_path: Path) -> None:
    tc = toolchain()
    fake_root = tmp_path / "tc"
    (fake_root / ".toolchain" / "cbm").mkdir(parents=True)
    (fake_root / ".toolchain" / "cbm" / "codebase-memory-mcp").write_bytes(b"not the pinned binary")
    tc2 = Toolchain(root=fake_root, lock=tc.lock, report=tc.report)
    ad = CbmAdapter(tc2, tmp_path / "st", tmp_path / "snaps", SandboxRunner(capability=HostCapability(True, "/usr/bin/bwrap", "x", None)))
    with pytest.raises(SvwError) as ei:
        ad.start()
    assert ei.value.code is ErrorCode.CAPABILITY_MISMATCH and ei.value.details["reason"] == "binary_hash_mismatch"
    ad2 = CbmAdapter(Toolchain(root=tmp_path / "none", lock=tc.lock, report=tc.report), tmp_path / "st2",
                     tmp_path / "snaps", SandboxRunner(capability=HostCapability(True, "/usr/bin/bwrap", "x", None)))
    with pytest.raises(SvwError) as ei2:
        ad2.start()
    assert ei2.value.details["reason"] == "binary_missing"


def test_unusable_bwrap_without_degraded_opt_in_blocks_start(tmp_path: Path) -> None:
    tc = toolchain()
    if not tc.cbm_binary.is_file():
        pytest.skip("cbm not installed")
    ad = CbmAdapter(tc, tmp_path / "st", tmp_path / "snaps",
                    SandboxRunner(allow_degraded=False, capability=HostCapability(False, None, None, "no userns")))
    with pytest.raises(SvwError) as ei:
        ad.start()
    assert ei.value.code is ErrorCode.BACKEND_UNAVAILABLE and ei.value.details["isolation"] == "UNAVAILABLE"


# ------------------------------------------------------------------------------- pure helpers
def test_parse_helpers() -> None:
    assert nz.parse_lines("2-17") == (2, 17) and nz.parse_lines("5") == (5, 5)
    assert nz.parse_lines("") == (None, None) and nz.parse_lines(None) == (None, None)
    assert nz.safe_rel_path("a/b.sv") == "a/b.sv"
    for bad in ("/abs", "../x", "a//b", "a/../b", "", None, "a\x00b"):
        assert nz.safe_rel_path(bad) is None
    assert nz.parse_search_rows({"cols": ["a"], "rows": [[1], [2, 3]]}) == [{"a": 1}]
    assert nz.parse_search_rows({}) == []


def test_coverage_marks_partial_overlap_only_when_lines_overlap() -> None:
    m = IndexMapping(SID, "/r", PROJ, "f", 1, 1, None, "full", "0.11.0", "h",
                     parse_unusable=("u.sv",), parse_partial=(("p.sv", "7-7,20-22"),))
    assert nz.coverage_for(m, "u.sv", 1, 2)["status"] == "parse_unusable"
    assert nz.coverage_for(m, "p.sv", 1, 6) == {"status": "parse_partial", "error_ranges": "7-7,20-22", "overlaps_hit": False}
    assert nz.coverage_for(m, "p.sv", 5, 8)["overlaps_hit"] is True
    assert nz.coverage_for(m, "p.sv", None, None)["overlaps_hit"] is True
    assert nz.coverage_for(m, "ok.sv", 1, 2) == {"status": "no_recorded_issue"}
    many = IndexMapping(SID, "/r", PROJ, "f", 1, 1, None, "full", "0", "h",
                        parse_unusable=tuple(f"f{i}.sv" for i in range(30)))
    lim = nz.index_limitations(many, cap=5)
    assert len(lim) == 6 and "25 more files" in lim[-1]


def test_tree_fingerprint_is_deterministic_and_content_sensitive(tmp_path: Path) -> None:
    (tmp_path / "a").mkdir()
    (tmp_path / "a" / "x.sv").write_text("1")
    f1 = tree_fingerprint(tmp_path)
    assert tree_fingerprint(tmp_path) == f1
    (tmp_path / "a" / "x.sv").write_text("2")
    assert tree_fingerprint(tmp_path) != f1
    (tmp_path / "a" / "x.sv").write_text("1")
    assert tree_fingerprint(tmp_path) == f1
    (tmp_path / "link").symlink_to("a")
    assert tree_fingerprint(tmp_path) != f1


def test_mapping_store_tolerates_corrupt_file(tmp_path: Path) -> None:
    p = tmp_path / "m.json"
    p.write_text("{not json")
    s = MappingStore(p)
    assert s.all() == {}
    m = IndexMapping(SID, "/r", PROJ, "f", 1, 1, None, "full", "0.11.0", "h")
    s.put(m)
    assert MappingStore(p).get(SID) == m
    s.drop(SID)
    assert MappingStore(p).get(SID) is None
