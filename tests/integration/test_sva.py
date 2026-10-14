"""SVA-01 .. SVA-03 and the stub boundary (plan 16.3, 16.5, 20.3)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from svweave.application import queries as Q
from svweave.application.runner import exit_code
from tests.integration.demo_support import demo_ws
from tests.integration.helpers import (
    build,
    dbq,
    golden,
    lang_ws,
    opts,
)

pytestmark = pytest.mark.integration


@pytest.mark.parametrize("lanes", [2, 4])
def test_SVA_01_bind_per_lane_keeps_declaration_and_targets_separate(
    tmp_path: Path, lanes: int
) -> None:
    w = demo_ws(tmp_path, lanes=lanes)
    env = build(w)
    bid = env.build_id
    assert bid and env.analysis_coverage is not None
    assert env.analysis_coverage.completeness == "COMPLETE"
    g = golden("demo_lanes")
    r = Q.assertions(opts(w), bid, "r", limit=500)
    by = {}
    for it in r.data["items"]:
        by.setdefault(it["entry_type"], []).append(it)
    # declarations (source) exist ONCE, independent of the number of lanes
    prop = [i for i in by["source"] if i["kind"] == "property_decl"]
    assert [p["name"] for p in prop] == ["p_stall_holds"]
    stmts = [i for i in by["source"] if i["kind"] == "assert"]
    assert len(stmts) == 1 and stmts[0]["label"] == g["assert"]["label"]
    assert stmts[0]["source"]["start"] == g["assert"]["start"]
    assert stmts[0]["clock"] == g["clock"] and stmts[0]["disable_iff"] == g["disable_iff"]
    # the bind directive: one source location, N elaborated targets
    (bind,) = by["bind"]
    assert bind["target_text"] == g["bind"]["target_text"]
    assert bind["source"]["file"].endswith("sva/demo_bind.sv")
    assert bind["source"]["start"] == g["bind"]["start"]
    targets = [
        (t["checker_instance"]["path"], t["target_instance"]["path"])
        for t in bind["elaborated_targets"]
    ]
    v = g["variants"][f"L{lanes}"]
    assert [c for c, _ in targets] == v["checker_paths"]
    assert [t for _, t in targets] == v["lane_paths"]
    # one elaborated assertion per lane, scoped to the bound checker instance
    el = by["elaborated"]
    assert [e["scope"] for e in el] == v["checker_paths"]
    assert {e["label"] for e in el} == {"ap_stall_holds"}
    assert len({e["assertion_id"] for e in el}) == lanes
    assert r.data["execution_status"] == "NOT_RUN"
    # scope filter: a single lane
    one = Q.assertions(
        opts(w), bid, "r", scope=v["lane_paths"][0] + ".u_stream_hold_sva", entry_type="elaborated"
    )
    assert one.data["total_matches"] == 1


def test_SVA_02_unknown_signal_and_unresolvable_bind_are_not_a_valid_inventory(
    tmp_path: Path,
) -> None:
    w = lang_ws(tmp_path, "sva02_unresolved", "sva02_top")
    env = build(w)
    bid = env.build_id
    assert bid and env.analysis_coverage is not None
    g = golden("sva02_unresolved")
    assert env.analysis_coverage.completeness == g["expect"]["completeness"]
    assert env.error is not None and exit_code(env) == 3
    d = Q.diagnostics(opts(w), bid, "r")
    codes = {x["code"]: x["location"] for x in d.data["items"] if x["is_error"]}
    for want in g["diagnostics"]:
        assert codes[want["code"]]["start"] == want["start"]
    a = Q.assertions(opts(w), bid, "r", limit=100)
    assert a.analysis_coverage is not None and a.analysis_coverage.completeness == "PARTIAL"
    assert a.outcome == "PARTIAL" and any("PARTIAL" in x for x in a.warnings)
    items = {(i["entry_type"], i["kind"]): i for i in a.data["items"]}
    asr = items[("elaborated", "assert")]
    assert asr["resolved"] is False and asr["errors"] == g["assertion"]["errors"]
    assert asr["source"]["start"] == g["assertion"]["start"]
    bind = items[("bind", "bind")]
    assert bind["resolved"] is False and bind["errors"] == g["bind"]["errors"]
    assert bind["elaborated_targets"] == [] and bind["source"]["start"] == g["bind"]["start"]
    un = dbq(
        w,
        'SELECT target_name FROM "references" WHERE build_id=? AND resolution_status=?',
        bid,
        "candidate_source",
    )
    assert {u["target_name"] for u in un} == {"missing_sig", "no_such_module"}


def test_SVA_03_slang_only_never_reports_execution_or_proof(tmp_path: Path) -> None:
    w = demo_ws(tmp_path)
    env = build(w)
    bid = env.build_id
    assert bid
    rows = dbq(w, "SELECT execution_status, metadata FROM assertions WHERE build_id=?", bid)
    assert rows and {r["execution_status"] for r in rows} == {"NOT_RUN"}
    blob = json.dumps([json.loads(r["metadata"]) for r in rows]).lower()
    for word in ("pass", "fail", "proved", "vacuous", "covered"):
        assert word not in blob
    a = Q.assertions(opts(w), bid, "r")
    assert {i["execution_status"] for i in a.data["items"]} == {"NOT_RUN"}
    assert "NOT_RUN" in " ".join(a.analysis_coverage.limitations if a.analysis_coverage else [])
    assert a.provenance.evidence_kind == "HDL_STATIC_RESOLVED"  # static; not RUNTIME_OBSERVED


def test_stub_boundary_marks_partial_and_names_the_stub(tmp_path: Path) -> None:
    w = lang_ws(tmp_path, "stub_boundary", "stub_top")
    env = build(w)
    bid = env.build_id
    assert bid and env.analysis_coverage is not None
    g = golden("stub_boundary")["stub"]
    assert env.analysis_coverage.completeness == g["completeness"]
    assert env.data["published"] is False
    assert any("stub/blackbox" in r for r in env.data["reasons"])
    h = Q.hierarchy(opts(w), bid, "r")
    stub = [i for i in h.data["items"] if i["is_stub"]]
    assert [(i["path"], i["definition"], i["stub_name"]) for i in stub] == [
        (g["instance"], g["definition"], g["stub_name"])
    ]
    c = Q.connections(opts(w), bid, g["instance"], "r")
    assert any("stub" in x for x in c.analysis_coverage.limitations)  # type: ignore[union-attr]
    gaps = {r["kind"] for r in dbq(w, "SELECT kind FROM coverage_gaps WHERE build_id=?", bid)}
    assert g["gap"] in gaps
