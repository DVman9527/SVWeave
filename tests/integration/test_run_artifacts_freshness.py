"""Run artifacts (plan 24.1), job/analysis state separation (9.5) and freshness (9.6)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from svweave.application import queries as Q
from tests.integration.helpers import build, dbq, lang_ws, opts

pytestmark = pytest.mark.integration

REQUIRED = {
    "request.json",
    "normalized_manifest.json",
    "argument_ledger.json",
    "snapshot_inventory.json",
    "toolchain_manifest.json",
    "backend_capabilities.json",
    "diagnostics.json",
    "dependency_report.json",
    "unsupported_nodes.json",
    "semantic_summary.json",
    "resource_usage.json",
    "events.jsonl",
    "result.json",
}


def test_run_directory_has_every_plan_24_1_artifact_and_no_source_text(tmp_path: Path) -> None:
    w = lang_ws(tmp_path, "sem07_errors", "err_top")
    env = build(w)
    run_dir = w.root / env.data["run_dir"]
    assert run_dir.parent.name == "runs" and run_dir.name == env.data["job_id"]
    assert {p.name for p in run_dir.iterdir()} >= REQUIRED
    events = [json.loads(line) for line in (run_dir / "events.jsonl").read_text().splitlines()]
    assert events and {
        "ts",
        "request_id",
        "job_id",
        "build_id",
        "component",
        "event",
        "duration_s",
        "outcome",
        "error_code",
    } <= set(events[0])
    assert {e["job_id"] for e in events} == {env.data["job_id"]} and {
        e["build_id"] for e in events
    } == {env.build_id}
    assert [e["event"] for e in events][:3] == ["created", "start", "finish"]
    res = json.loads((run_dir / "result.json").read_text())
    assert res["build_id"] == env.build_id and res["error"]["code"] == "PARTIAL_ANALYSIS"
    usage = json.loads((run_dir / "resource_usage.json").read_text())
    assert (
        usage["peak_rss_kib"] > 1000 and usage["memory_limit_mib"] == 8192 and usage["wall_s"] > 0
    )
    tc = json.loads((run_dir / "toolchain_manifest.json").read_text())
    assert (
        tc["toolchain_id"] == env.data["toolchain_id"]
        and tc["parts"]["pyslang"]["version"] == "12.0.0"
    )
    assert json.loads((run_dir / "dependency_report.json").read_text())["ok"] is True
    summary = json.loads((run_dir / "semantic_summary.json").read_text())
    assert (
        summary["completeness"] == "PARTIAL"
        and summary["errors"] == 3
        and summary["published"] is False
    )
    req = json.loads((run_dir / "request.json").read_text())
    assert "test_fault" not in req["worker_request"]
    # no source text in any artifact (identifiers from the sources must not appear verbatim as code)
    blob = "".join(p.read_text(errors="replace") for p in run_dir.iterdir() if p.is_file())
    for needle in ("assign x = 8'h01", "logic [7:0 broken_decl", "initial n = s"):
        assert needle not in blob
    # job lifecycle and analysis completeness are separate dimensions
    job = dbq(w, "SELECT * FROM jobs")[0]
    assert job["lifecycle"] == "SUCCEEDED" and env.analysis_coverage.completeness == "PARTIAL"  # type: ignore[union-attr]


def test_partial_build_is_stored_addressable_and_rebuilt_not_published(tmp_path: Path) -> None:
    w = lang_ws(tmp_path, "sem07_errors", "err_top")
    e1 = build(w)
    e2 = build(w)  # only COMPLETE builds are reused: this re-runs the worker
    assert e1.build_id == e2.build_id and e2.data["reused"] is False
    assert len(dbq(w, "SELECT 1 FROM jobs WHERE build_id=?", e1.build_id)) == 2
    assert len(dbq(w, "SELECT 1 FROM builds")) == 1
    assert dbq(w, "SELECT 1 FROM published_pointer") == []
    d = Q.diagnostics(opts(w), e1.build_id, "r")  # a PARTIAL build is still queryable by id
    assert d.outcome == "PARTIAL" and d.data["total_matches"] == 3


def test_freshness_current_stale_historical(tmp_path: Path) -> None:
    w = lang_ws(tmp_path, "sem01_scopes", "sem01_top")
    e1 = build(w)
    assert e1.freshness == "CURRENT"
    assert Q.hierarchy(opts(w), e1.build_id, "r").freshness == "CURRENT"  # type: ignore[arg-type]
    (w.root / "pkg_a.sv").write_text((w.root / "pkg_a.sv").read_text() + "// edit\n")
    stale = Q.hierarchy(opts(w), e1.build_id, "r")  # type: ignore[arg-type]
    assert stale.freshness == "STALE" and any("STALE" in x for x in stale.warnings)
    e2 = build(w)
    assert e2.freshness == "CURRENT" and e2.build_id != e1.build_id
    hist = Q.hierarchy(opts(w), e1.build_id, "r")  # type: ignore[arg-type]
    assert hist.freshness == "HISTORICAL"
    # the new complete build moved the published pointer; the old one stays addressable
    assert dbq(w, "SELECT build_id FROM published_pointer")[0]["build_id"] == e2.build_id
    # a new header lookup candidate also makes it stale (M1 snapshot rule is used, not mtime)
    (w.root / "pkg_a.sv").write_text((w.root / "pkg_a.sv").read_text().replace("// edit\n", ""))
    assert Q.hierarchy(opts(w), e2.build_id, "r").freshness == "STALE"  # type: ignore[arg-type]
    assert Q.hierarchy(opts(w), e1.build_id, "r").freshness == "CURRENT"  # type: ignore[arg-type]


def test_no_reuse_on_a_complete_build_is_a_verified_rerun_not_an_overwrite(tmp_path: Path) -> None:
    w = lang_ws(tmp_path, "sem01_scopes", "sem01_top")
    e1 = build(w)
    before = dbq(w, "SELECT COUNT(*) n FROM declarations")[0]["n"]
    e2 = build(w, reuse=False)
    assert e2.build_id == e1.build_id and e2.data["reused"] is False and e2.data["verified_rerun"] is True
    assert e2.data["job_id"] != e1.data["job_id"]  # a new attempt with its own record ...
    assert len(dbq(w, "SELECT 1 FROM jobs WHERE build_id=?", e1.build_id)) == 2
    assert dbq(w, "SELECT COUNT(*) n FROM declarations")[0]["n"] == before  # ... same immutable build
    assert dbq(w, "SELECT build_id FROM published_pointer")[0]["build_id"] == e1.build_id
