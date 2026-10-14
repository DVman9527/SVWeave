"""Common query envelope (plan 14.5): states, provenance, freshness, coverage and paging."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from svweave.application.semantic_common import build_envelope, report_of, require_build
from svweave.domain.cursor import check_limit
from svweave.domain.envelope import Envelope, Outcome, Pagination
from svweave.domain.errors import ErrorCode, SvwError
from svweave.domain.ids import sha256_hex
from svweave.domain.states import EvidenceKind, Freshness
from svweave.snapshot.models import SnapshotRecord
from svweave.storage.db import Database
from svweave.storage.semantic import dumps
from tests.conftest import Ws
from tests.unit.test_storage import build_dict, make_snaps


def report(bid: str, **kw: object) -> dict[str, object]:
    base = {
        "build_id": bid,
        "job_id": "j",
        "extractor_version": "x",
        "pyslang_version": "12",
        "scope": "top",
        "tops": dumps(["top"]),
        "unresolved_count": 2,
        "dynamic_count": 0,
        "reasons": dumps(["2 unresolved/unsupported reference(s)"]),
        "limitations": dumps(["static only"]),
        "stats": "{}",
        "dependency_report": "{}",
        "source_map": "{}",
    }
    base.update(kw)
    return base


def staged(
    ws: Ws, db: Database, rec: SnapshotRecord, n: int, completeness: str, real: bool = False
) -> str:
    from svweave.application.toolchain import compute_toolchain
    from svweave.manifest.resolver import resolve_profile

    ids = (
        {
            "config_id": resolve_profile(ws.ctx(), "p").config_id,
            "toolchain_id": compute_toolchain(ws.root)[0],
        }
        if real
        else {}
    )
    b = {
        **build_dict(rec.snapshot_id, n),
        **ids,
        "completeness": completeness,
        "diagnostic_outcome": "HAS_ERRORS" if completeness != "COMPLETE" else "NO_ERRORS",
    }
    db.stage_build(b, {"build_reports": [report(b["build_id"])]})
    return str(b["build_id"])


def build_row_tc(db: Database, bid: str) -> str:
    return str(db.conn.execute("SELECT toolchain_id FROM builds WHERE build_id=?", (bid,)).fetchone()[0])


def test_envelope_states_provenance_and_freshness(ws: Ws, tmp_path: Path) -> None:
    db = Database(ws.root / ".svweave" / "svweave.db")
    db.upsert_workspace("t", {"project": "project"}, sha256_hex("p"))
    s1, s2 = make_snaps(ws, 2)
    db.record_snapshot(s1)
    db.record_snapshot(s2)
    ctx = ws.ctx()
    b_part = staged(ws, db, s1, 1, "PARTIAL")  # s1 is NOT the workspace state any more
    b_comp = staged(ws, db, s2, 2, "COMPLETE", real=True)  # s2 is the current workspace content
    env = build_envelope(ctx, db, require_build(db, b_comp), "r", {"items": []})
    assert env.outcome == Outcome.OK and env.freshness == Freshness.CURRENT
    assert (
        env.provenance.backend == "slang"
        and env.provenance.evidence_kind == EvidenceKind.HDL_STATIC_RESOLVED
    )
    assert env.provenance.toolchain_id == build_row_tc(db, b_comp)
    assert env.analysis_coverage is not None and env.analysis_coverage.scope == "top"
    assert (
        env.analysis_coverage.basis == "qualified_static_subset"
        and env.analysis_coverage.unresolved_count == 2
    )
    assert env.workspace_id == "t" and env.snapshot_id == s2.snapshot_id and env.build_id == b_comp
    # an older build of the same profile: its snapshot differs from the files now -> HISTORICAL,
    # and PARTIAL is surfaced as outcome, coverage limitations and a warning (never a bare OK)
    old = build_envelope(ctx, db, require_build(db, b_part), "r", {})
    assert old.freshness == Freshness.HISTORICAL and old.outcome == Outcome.PARTIAL
    assert any("PARTIAL" in w for w in old.warnings) and any(
        "HISTORICAL" in w for w in old.warnings
    )
    assert old.analysis_coverage is not None
    assert "2 unresolved" in " ".join(old.analysis_coverage.limitations)
    # editing the workspace makes the latest build STALE
    (ws.root / "rtl" / "a.sv").write_text("module a; // later\nendmodule\n")
    assert build_envelope(ctx, db, require_build(db, b_comp), "r", {}).freshness == Freshness.STALE
    # BLOCKED is never OK
    db.conn.execute("UPDATE builds SET completeness='BLOCKED' WHERE build_id=?", (b_part,))
    assert build_envelope(ctx, db, require_build(db, b_part), "r", {}).outcome == Outcome.BLOCKED


def test_envelope_json_roundtrip_and_pagination_defaults(ws: Ws) -> None:
    db = Database(ws.root / ".svweave" / "svweave.db")
    db.upsert_workspace("t", {"project": "project"}, sha256_hex("p"))
    (s1,) = make_snaps(ws, 1)
    db.record_snapshot(s1)
    b = staged(ws, db, s1, 1, "COMPLETE")
    env = build_envelope(
        ws.ctx(),
        db,
        require_build(db, b),
        "req-1",
        {"items": [1]},
        pagination=Pagination(limit=5, next_cursor="c", truncated=True),
    )
    again = Envelope.model_validate(json.loads(env.to_json(indent=None)))
    assert again == env and again.pagination.truncated and again.pagination.next_cursor == "c"
    assert report_of(db, b)["scope"] == "top" and report_of(db, "0" * 64)["limitations"] == []
    with pytest.raises(SvwError) as ei:
        require_build(db, "zz")
    assert ei.value.code == ErrorCode.INVALID_CONFIG
    with pytest.raises(SvwError) as ei2:
        require_build(db, "0" * 64)
    assert ei2.value.code == ErrorCode.MISSING_INPUT
    assert check_limit(None) == 100
