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


def _fresh(ws: Ws, db: Database, bid: str) -> Freshness:
    return build_envelope(ws.ctx(), db, require_build(db, bid), "r", {}).freshness


def _setup_published(ws: Ws) -> tuple[Database, list[SnapshotRecord], str]:
    db = Database(ws.root / ".svweave" / "svweave.db")
    db.upsert_workspace("t", {"project": "project"}, sha256_hex("p"))
    snaps = make_snaps(ws, 3)
    for s in snaps:
        db.record_snapshot(s)
    old = staged(ws, db, snaps[2], 1, "COMPLETE", real=True)  # published, matches the files now
    assert _fresh(ws, db, old) == Freshness.CURRENT
    (ws.root / "rtl" / "a.sv").write_text("module a; // inputs changed\nendmodule\n")
    return db, snaps, old


def test_freshness_published_build_stays_stale_with_newer_unpublished_row(ws: Ws) -> None:
    """(a) a newer build row that is not published (stored with publish=False, i.e. the state of
    a build that is still running or was not promoted) must not demote the published build."""
    db, snaps, old = _setup_published(ws)
    b = {**build_dict(snaps[1].snapshot_id, 2), "completeness": "COMPLETE",
         "diagnostic_outcome": "NO_ERRORS"}
    assert db.stage_build(b, {"build_reports": [report(b["build_id"])]}, publish=False) is False
    assert db.published("t", "p")["build_id"] == old  # type: ignore[index]
    assert _fresh(ws, db, old) == Freshness.STALE


def test_freshness_failed_newer_build_never_published(ws: Ws) -> None:
    """(b) a PARTIAL newer build is never published: the old build stays the served one (STALE,
    published pointer unchanged); (d) the never-published build itself is HISTORICAL when
    queried explicitly, because another build is the served one."""
    from svweave.application.status import _builds_view

    db, snaps, old = _setup_published(ws)
    newer = staged(ws, db, snaps[1], 2, "PARTIAL")
    assert _fresh(ws, db, old) == Freshness.STALE
    assert _fresh(ws, db, newer) == Freshness.HISTORICAL
    view = _builds_view(ws.ctx(), db, ["p"])[0]
    assert view["current_build"]["build_id"] == old
    assert view["current_build"]["freshness"] == "STALE"
    assert view["latest_build"]["build_id"] == newer
    assert view["latest_build"]["freshness"] == "HISTORICAL"


def test_status_view_is_one_read_snapshot_while_a_build_publishes(
    ws: Ws, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A newer build publishes (on another connection) while _builds_view runs: current_build,
    its freshness and latest_build must all describe the SAME instant (the old build, STALE, no
    newer build yet), and the next view then shows the new state."""
    from svweave.application import status as st

    db, snaps, old = _setup_published(ws)
    real = st.freshness_of
    calls: list[str] = []

    def racing(ctx: object, d: Database, row: object) -> Freshness:
        if not calls:  # the first judgement triggers the concurrent publish
            other = Database(ws.root / ".svweave" / "svweave.db")
            try:
                nb = {**build_dict(snaps[1].snapshot_id, 2), "completeness": "COMPLETE",
                      "diagnostic_outcome": "NO_ERRORS"}
                assert other.stage_build(nb, {"build_reports": [report(nb["build_id"])]}) is True
                calls.append(str(nb["build_id"]))
            finally:
                other.close()
        return real(ctx, d, row)  # type: ignore[arg-type]

    monkeypatch.setattr(st, "freshness_of", racing)
    e = st._builds_view(ws.ctx(), db, ["p"])[0]
    assert calls, "the concurrent publish did not happen"
    assert e["current_build"]["build_id"] == old and e["current_build"]["freshness"] == "STALE"
    assert e["latest_build"] is None
    assert not db.conn.in_transaction, "the read transaction must be closed"
    monkeypatch.setattr(st, "freshness_of", real)
    e2 = st._builds_view(ws.ctx(), db, ["p"])[0]
    assert e2["current_build"]["build_id"] == calls[0] and e2["latest_build"] is None
    assert real(ws.ctx(), db, require_build(db, old)) == Freshness.HISTORICAL


def test_freshness_old_build_historical_once_newer_is_published(ws: Ws) -> None:
    """(c) the old build becomes HISTORICAL only when the pointer moved away from it."""
    db, snaps, old = _setup_published(ws)
    newer = staged(ws, db, snaps[1], 2, "COMPLETE")  # staged() publishes COMPLETE builds
    assert db.published("t", "p")["build_id"] == newer  # type: ignore[index]
    assert _fresh(ws, db, old) == Freshness.HISTORICAL


def test_freshness_unpublished_profile_serves_its_latest_build(ws: Ws) -> None:
    """With no published build at all the latest stored build (a first PARTIAL one) is STALE
    when its inputs changed; an earlier unpublished row is HISTORICAL."""
    db = Database(ws.root / ".svweave" / "svweave.db")
    db.upsert_workspace("t", {"project": "project"}, sha256_hex("p"))
    snaps = make_snaps(ws, 2)
    for s in snaps:
        db.record_snapshot(s)
    first = staged(ws, db, snaps[0], 1, "PARTIAL")
    second = staged(ws, db, snaps[1], 2, "PARTIAL")
    assert db.published("t", "p") is None
    assert _fresh(ws, db, first) == Freshness.HISTORICAL
    assert _fresh(ws, db, second) == Freshness.STALE


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


def test_SIZE02_envelope_limitations_are_capped_with_the_standing_lines_and_reasons_kept() -> None:
    from svweave.application.semantic_common import MAX_LIMITATIONS, cap_limitations

    short = [f"l{i}" for i in range(MAX_LIMITATIONS)]
    assert cap_limitations(short) == short  # at the cap: untouched
    many = ["standing-1", "standing-2", *[f"Blackbox boundary {i}" for i in range(6637)], "Partial analysis: x"]
    out = cap_limitations(many)
    assert len(out) == MAX_LIMITATIONS
    assert out[:2] == ["standing-1", "standing-2"] and out[-1] == "Partial analysis: x"
    assert "more limitation line(s) omitted" in out[-2] and str(len(many) - (MAX_LIMITATIONS - 2) - 1) in out[-2]
    assert len(json.dumps(out)) < 16_000  # nowhere near the old ~700 KB
