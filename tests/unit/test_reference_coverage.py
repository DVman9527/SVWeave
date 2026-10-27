"""HDL-COV-01/02: coverage is separate from build success; no backend is used here."""

from __future__ import annotations

import json
from typing import Any

import pytest

from svweave.adapters.slang.ingest import ingest
from svweave.application import impact, queries, rename
from svweave.application.options import WorkspaceOptions
from svweave.application.semantic_common import build_envelope, require_build
from svweave.domain.envelope import Envelope, Outcome, Pagination, ReferenceCoverage
from svweave.domain.ids import sha256_hex
from svweave.domain.states import Completeness, Freshness
from svweave.storage.db import Database
from svweave.storage.semantic import dumps
from tests.conftest import Ws
from tests.unit.test_semantic_envelope import report
from tests.unit.test_slang_ingest import BID, decl, dto, loc, ref, run, snap, unsup
from tests.unit.test_storage import build_dict, make_snaps


def _coverage(env: Envelope) -> ReferenceCoverage:
    assert env.analysis_coverage is not None
    cov = env.analysis_coverage.reference_coverage
    assert cov is not None
    assert cov.absence_proof_supported is False
    return cov


def _stage(ws: Ws, *, reference_count: int = 2) -> tuple[str, str]:
    """Synthetic DTO/storage fixture, not a parser or semantic acceptance test."""
    (snap,) = make_snaps(ws, 1)
    b = build_dict(snap.snapshot_id, 1)
    result = dto(
        declarations=[decl(0, "a", loc=loc("rtl/a.sv", 7, 8))],
        references=[ref(loc=loc("rtl/a.sv", i, i + 1)) for i in range(reference_count)],
    )
    ing = ingest(result, snap, b["build_id"], "j", ["a"])
    with Database(ws.root / ".svweave" / "svweave.db") as db:
        db.upsert_workspace("t", {"project": "project"}, sha256_hex("p"))
        db.record_snapshot(snap)
        db.stage_build(b, {**ing.rows, "build_reports": [report(
            b["build_id"], stats=dumps(ing.summary), unresolved_count=0, reasons="[]",
            limitations=dumps(ing.limitations),
        )]})
    return b["build_id"], ing.rows["declarations"][0]["symbol_id"]


def test_HDL_COV_01_complete_build_has_known_feature_and_scope_omissions() -> None:
    ing = run(dto(references=[ref()]))
    assert ing.completeness == Completeness.COMPLETE and len(ing.rows["references"]) == 1
    cov = ReferenceCoverage.model_validate(ing.summary["reference_coverage"])
    assert cov.status == "PARTIAL" and cov.absence_proof_supported is False
    assert cov.features["packed_dimension_expressions"] == "UNSUPPORTED"
    assert cov.features["unpacked_dimension_expressions"] == "UNSUPPORTED"
    assert cov.features["subroutine_return_type_names"] == "UNSUPPORTED"
    assert cov.features["class_scope_prefix_names"] == "UNSUPPORTED"
    assert cov.scopes["inactive_branches"] == "NOT_SEARCHED"
    assert cov.scopes["uninstantiated_generic_bodies"] == "NOT_SEARCHED"
    assert cov.scopes["unloaded_library_members"] == "NOT_SEARCHED"
    assert cov.scopes["call_only_specialization_bodies"] == "NOT_SEARCHED"
    assert cov.observations is not None and cov.observations.scope_gaps == {}
    assert "unsearched_source_scopes" in cov.blockers  # zero observed gaps is not full coverage


@pytest.mark.parametrize("scope", ["project", "external"])
def test_HDL_COV_02_unsupported_and_dropped_origins_remain_distinct(scope: str) -> None:
    ing = run(dto(
        unsupported_nodes=[unsup(scope, 2)],
        dropped_refs=[{"scope": scope, "count": 3, "reason": "unrecordable"}],
    ), "a.sv", "@lib/x.sv")
    assert ing.completeness == ("PARTIAL" if scope == "project" else "COMPLETE")
    cov = ReferenceCoverage.model_validate(ing.summary["reference_coverage"])
    assert cov.observations is not None
    assert getattr(cov.observations, f"unsupported_nodes_{scope}") == 2
    assert getattr(cov.observations, f"dropped_references_{scope}") == 3
    assert f"unsupported_{scope}_nodes" in cov.blockers
    assert f"dropped_{scope}_references" in cov.blockers
    assert cov.absence_proof_supported is False


def test_HDL_COV_02_external_extractor_failure_keeps_origin_and_blocking_verdict() -> None:
    ing = run(dto(unsupported_nodes=[unsup("external", 1, "extractor_exception:TypeError")]),
              "a.sv", "@lib/x.sv")
    assert ing.completeness == "PARTIAL"  # external extraction exceptions still fail closed
    cov = ReferenceCoverage.model_validate(ing.summary["reference_coverage"])
    assert cov.observations is not None
    assert cov.observations.unsupported_nodes_external == 1
    assert cov.observations.unsupported_nodes_project == 0


def test_HDL_COV_02_dependency_inventory_gaps_are_in_rows_and_coverage() -> None:
    ing = ingest(dto(), snap("a.sv"), BID, "j", ["m"], extra_gaps=[{
        "kind": "library_member_not_loaded", "name": "lib/unused.sv", "detail": "not read",
    }])
    assert ing.completeness == "COMPLETE"
    assert ing.summary["gaps"] == len(ing.rows["coverage_gaps"]) == 1
    cov = ReferenceCoverage.model_validate(ing.summary["reference_coverage"])
    assert cov.observations is not None
    assert cov.observations.scope_gaps == {"library_member_not_loaded": 1}
    assert "observed_scope_gaps" in cov.blockers


@pytest.mark.parametrize("stats", [{}, {"reference_coverage": {"contract_version": "2"}}, []])
def test_HDL_COV_01_historical_missing_or_unknown_metadata_is_unknown(
    ws: Ws, stats: Any,
) -> None:
    bid, _ = _stage(ws)
    with Database(ws.root / ".svweave" / "svweave.db") as db:
        db.conn.execute("UPDATE build_reports SET stats=? WHERE build_id=?", (dumps(stats), bid))
        env = build_envelope(ws.ctx(), db, require_build(db, bid), "r", {},
                             freshness=Freshness.CURRENT, reference_query="references")
    assert env.outcome == Outcome.OK
    cov = _coverage(env)
    assert cov.status == "UNKNOWN" and cov.observations is None
    assert "coverage_metadata_unavailable" in cov.blockers
    old = env.model_dump(mode="json")
    del old["analysis_coverage"]["reference_coverage"]
    assert Envelope.model_validate(old).schema_version == "1.0"  # existing payloads still parse


@pytest.mark.parametrize("completeness", ["COMPLETE", "PARTIAL", "BLOCKED"])
@pytest.mark.parametrize("freshness", list(Freshness))
def test_HDL_COV_02_stale_partial_unknown_and_truncated_never_authorize_absence(
    ws: Ws, completeness: str, freshness: Freshness,
) -> None:
    bid, _ = _stage(ws)
    with Database(ws.root / ".svweave" / "svweave.db") as db:
        db.conn.execute("UPDATE builds SET completeness=? WHERE build_id=?", (completeness, bid))
        env = build_envelope(
            ws.ctx(), db, require_build(db, bid), "r", {"items": []}, freshness=freshness,
            reference_query="references", pagination=Pagination(limit=1, truncated=True),
        )
    cov = _coverage(env)
    assert cov.result_truncated and "result_truncated" in cov.blockers
    if completeness != "COMPLETE":
        assert f"build_{completeness.lower()}" in cov.blockers
    if freshness != Freshness.CURRENT:
        assert f"freshness_{freshness.value.lower()}" in cov.blockers


def test_HDL_COV_01_query_keeps_positive_edges_empty_scope_and_final_page_are_not_absence(
    ws: Ws, monkeypatch: pytest.MonkeyPatch,
) -> None:
    bid, sid = _stage(ws)
    monkeypatch.setattr(queries._S, "fresh", lambda self: Freshness.CURRENT)
    opts = WorkspaceOptions(root=ws.root)
    first = queries.references(opts, bid, sid, "r", limit=1)
    assert first.outcome == Outcome.OK and first.data["total_matches"] == 2
    assert len(first.data["items"]) == 1 and first.data["items"][0]["resolution_status"] == "resolved_static"
    assert _coverage(first).query == "references" and _coverage(first).result_truncated
    last = queries.references(opts, bid, sid, "r", limit=1, cursor=first.pagination.next_cursor)
    assert len(last.data["items"]) == 1 and not last.pagination.truncated
    assert "previous_pages_not_in_result" in _coverage(last).blockers
    empty = queries.references(opts, bid, sid, "r", scope="not-searched/")
    assert empty.data["items"] == []
    assert empty.data["searched_scope"]["scope"] == "not-searched/"
    assert _coverage(empty).searched_scope == "not-searched/"
    assert "query_scope_restricted" in _coverage(empty).blockers
    assert any("no references found in the searched scope" in w for w in empty.warnings)


def test_HDL_COV_02_oversize_item_cannot_leave_coverage_untruncated(
    ws: Ws, monkeypatch: pytest.MonkeyPatch,
) -> None:
    bid, sid = _stage(ws)
    monkeypatch.setattr(queries._S, "fresh", lambda self: Freshness.CURRENT)
    env = queries.references(WorkspaceOptions(root=ws.root), bid, sid, "r")
    assert not _coverage(env).result_truncated
    env.data["items"] = [{"truncated_item": True, "id": "x", "size_bytes": 999999}]
    queries.flag_oversize(env)
    assert env.pagination.truncated and _coverage(env).result_truncated
    assert "result_truncated" in _coverage(env).blockers


def test_HDL_COV_02_impact_depth_and_sample_limits_are_explicit(
    ws: Ws, monkeypatch: pytest.MonkeyPatch,
) -> None:
    bid, sid = _stage(ws)
    monkeypatch.setattr(queries._S, "fresh", lambda self: Freshness.CURRENT)
    env = impact.impact(WorkspaceOptions(root=ws.root), bid, "r", symbol_id=sid, limit=1, depth=1)
    assert env.outcome == Outcome.OK and env.data["references_summary"]["total"] == 2
    assert len(env.data["references"]) == 1
    cov = _coverage(env)
    assert cov.query == "impact" and cov.depth_limit == 1 and cov.result_truncated
    assert "bounded_hierarchy_depth" in cov.blockers


def test_HDL_COV_01_rename_candidate_is_not_a_complete_reference_claim(
    ws: Ws, monkeypatch: pytest.MonkeyPatch,
) -> None:
    bid, sid = _stage(ws, reference_count=0)
    monkeypatch.setattr(queries._S, "fresh", lambda self: Freshness.CURRENT)
    monkeypatch.setattr(rename, "MAX_DIFF_BYTES", 0)
    env = rename.rename_preview(WorkspaceOptions(root=ws.root), bid, "r", symbol_id=sid,
                                new_name="renamed", limit=1)
    assert _coverage(env).query == "rename_preview"
    assert "known_reference_omissions" in _coverage(env).blockers
    assert env.data["sites_total"] == 1  # positive candidate sites are retained
    assert env.data["diffs_truncated"] and env.pagination.truncated
    assert _coverage(env).result_truncated
    assert json.loads(env.to_json())["analysis_coverage"]["reference_coverage"]["absence_proof_supported"] is False
