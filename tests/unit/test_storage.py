from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any

import pytest

from svweave.domain.errors import ErrorCode, SvwError
from svweave.domain.ids import compute_build_id, sha256_hex
from svweave.domain.states import JobLifecycle
from svweave.manifest.resolver import resolve_profile
from svweave.snapshot.capture import capture_snapshot, snapshots_dir
from svweave.snapshot.models import SnapshotRecord
from svweave.storage.db import Database, plan_gc, run_gc
from svweave.storage.migrations import LATEST_VERSION, MIGRATIONS, current_version, migrate
from tests.conftest import Ws, profile

TABLES_13_2 = {
    "workspaces",
    "snapshots",
    "source_files",
    "builds",
    "jobs",
    "declarations",
    "references",
    "instances",
    "port_bindings",
    "assertions",
    "diagnostics",
    "backend_indexes",
    "crosswalks",
    "edit_transactions",
    "evidence",
}


@pytest.fixture
def db(tmp_path: Path) -> Database:
    d = Database(tmp_path / "state" / "svweave.db")
    d.upsert_workspace("t", {"project": "project"}, sha256_hex("policy"))
    return d


def make_snaps(ws: Ws, n: int) -> list[SnapshotRecord]:
    ws.config({"p": profile(source_inputs=[{"kind": "file", "path": "rtl/a.sv"}])})
    out = []
    for i in range(n):
        (ws.root / "rtl" / "a.sv").write_text(f"module a; // rev {i}\nendmodule\n")
        ctx = ws.ctx()
        out.append(capture_snapshot(ctx, resolve_profile(ctx, "p", {})))
    return out


def build_dict(snap_id: str, n: int, profile_name: str = "p") -> dict[str, Any]:
    cfg, tc = sha256_hex(f"cfg{n}"), sha256_hex("tc")
    return {
        "build_id": compute_build_id(snap_id, cfg, tc),
        "workspace_id": "t",
        "profile": profile_name,
        "snapshot_id": snap_id,
        "config_id": cfg,
        "toolchain_id": tc,
        "semantic_schema_version": "1",
        "completeness": "COMPLETE",
        "diagnostic_outcome": "NO_ERRORS",
    }


# --------------------------------------------------------------------------- migrations
def test_migrations_create_all_plan_13_2_tables_plus_published_pointer(db: Database) -> None:
    assert TABLES_13_2 | {"published_pointer"} <= db.tables()
    assert db.version == LATEST_VERSION == current_version(db.conn)
    assert [v for v, _ in MIGRATIONS] == sorted(v for v, _ in MIGRATIONS)
    cols = {r["name"] for r in db.conn.execute('PRAGMA table_info("references")')}
    assert {"reference_id", "target_id", "resolution_status"} <= cols
    idx = {r["name"] for r in db.conn.execute('PRAGMA index_list("references")')}
    assert "idx_references_target" in idx
    assert db.conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1


def test_migrate_is_idempotent_and_refuses_newer_schema(tmp_path: Path) -> None:
    p = tmp_path / "x.db"
    d = Database(p)
    assert migrate(d.conn) == LATEST_VERSION
    d.conn.execute(f"PRAGMA user_version = {LATEST_VERSION + 1}")
    d.close()
    with pytest.raises(SvwError) as ei:
        Database(p)
    assert ei.value.code == ErrorCode.CAPABILITY_MISMATCH


def test_failed_migration_rolls_back(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    from svweave.storage import migrations

    monkeypatch.setattr(migrations, "MIGRATIONS", [(1, "CREATE TABLE a(x);\nCREATE TABLE a(x);\n")])
    monkeypatch.setattr(migrations, "LATEST_VERSION", 1)
    conn = sqlite3.connect(str(tmp_path / "m.db"), isolation_level=None)
    with pytest.raises(sqlite3.OperationalError):
        migrations.migrate(conn)
    assert migrations.current_version(conn) == 0
    assert conn.execute("SELECT name FROM sqlite_master WHERE name='a'").fetchone() is None


def test_foreign_keys_and_check_constraints_enforced(db: Database) -> None:
    with pytest.raises(sqlite3.IntegrityError):
        db.stage_build({**build_dict("0" * 64, 1)})
    with pytest.raises(sqlite3.IntegrityError), db.transaction() as c:
        c.execute("INSERT INTO jobs(job_id, lifecycle) VALUES ('j','BOGUS')")
    with pytest.raises(sqlite3.IntegrityError), db.transaction() as c:
        c.execute("INSERT INTO evidence(evidence_id, kind, producer) VALUES ('e','MAYBE','p')")


def test_record_snapshot_and_source_files(ws: Ws, db: Database) -> None:
    (rec,) = make_snaps(ws, 1)
    db.record_snapshot(rec)
    db.record_snapshot(rec)  # idempotent
    assert db.conn.execute("SELECT COUNT(*) FROM snapshots").fetchone()[0] == 1
    rows = db.conn.execute("SELECT * FROM source_files").fetchall()
    assert [r["logical_path"] for r in rows] == ["rtl/a.sv", "rtl/b.sv"]  # + source-dir lookup
    assert rows[0]["bytes_hash"] == rec.files[0].sha256 and rows[0]["size"] == rec.files[0].size


# --------------------------------------------------------------------------- SNP-06
def test_SNP_06_crash_during_staging_keeps_previous_published_build(ws: Ws, db: Database) -> None:
    s1, s2 = make_snaps(ws, 2)
    db.record_snapshot(s1)
    db.record_snapshot(s2)
    good = build_dict(s1.snapshot_id, 1)
    db.stage_build(
        good, {"instances": [{"instance_id": "i1", "build_id": good["build_id"], "path": "top"}]}
    )
    ptr = db.published("t", "p")
    assert ptr is not None and ptr["build_id"] == good["build_id"]

    bad = build_dict(s2.snapshot_id, 2)
    job = db.create_job(bad["build_id"], s2.snapshot_id)
    # half a DB: first rows insert fine, then a crash (bad column) in the same transaction
    with pytest.raises(sqlite3.OperationalError):
        db.stage_build(
            bad,
            {
                "instances": [{"instance_id": "i9", "build_id": bad["build_id"], "path": "top"}],
                "declarations": [{"nonexistent_column": 1}],
            },
        )
    db.finish_job(job, JobLifecycle.FAILED, exit_code=1, error="extractor crashed")

    assert db.published("t", "p")["build_id"] == good["build_id"]  # type: ignore[index]
    assert (
        db.conn.execute(
            "SELECT COUNT(*) FROM builds WHERE build_id=?", (bad["build_id"],)
        ).fetchone()[0]
        == 0
    )
    assert (
        db.conn.execute("SELECT COUNT(*) FROM instances WHERE instance_id='i9'").fetchone()[0] == 0
    )
    assert (
        db.conn.execute("SELECT COUNT(*) FROM instances WHERE instance_id='i1'").fetchone()[0] == 1
    )
    j = db.job(job)
    assert j is not None and j["lifecycle"] == "FAILED" and j["error"] == "extractor crashed"
    assert j["finished_at"] is not None


def test_SNP_06_failed_validation_does_not_publish(ws: Ws, db: Database) -> None:
    s1, s2 = make_snaps(ws, 2)
    db.record_snapshot(s1)
    db.record_snapshot(s2)
    good = build_dict(s1.snapshot_id, 1)
    db.stage_build(good)

    def reject(conn: sqlite3.Connection, build_id: str) -> None:
        n = conn.execute("SELECT COUNT(*) FROM instances WHERE build_id=?", (build_id,)).fetchone()[
            0
        ]
        if n == 0:
            raise SvwError(ErrorCode.PARTIAL_ANALYSIS, "no instances extracted")

    bad = build_dict(s2.snapshot_id, 2)
    with pytest.raises(SvwError):
        db.stage_build(bad, validate=reject)
    assert db.published("t", "p")["build_id"] == good["build_id"]  # type: ignore[index]
    assert db.conn.execute("SELECT COUNT(*) FROM builds").fetchone()[0] == 1
    # same build, validation passes -> published pointer moves atomically
    db.stage_build(
        bad,
        {"instances": [{"instance_id": "i", "build_id": bad["build_id"], "path": "top"}]},
        validate=reject,
    )
    assert db.published("t", "p")["build_id"] == bad["build_id"]  # type: ignore[index]


def test_stage_without_publish_and_unknown_table_and_pointer_per_profile(
    ws: Ws, db: Database
) -> None:
    (s1,) = make_snaps(ws, 1)
    db.record_snapshot(s1)
    b = build_dict(s1.snapshot_id, 1)
    db.stage_build(b, publish=False)
    assert db.published("t", "p") is None
    with pytest.raises(SvwError):
        db.stage_build(build_dict(s1.snapshot_id, 2), {"x; DROP TABLE builds": [{"a": 1}]})
    db.stage_build(build_dict(s1.snapshot_id, 3, "other"))
    assert db.published("t", "other") is not None and db.published("t", "p") is None
    with pytest.raises(SvwError), db.transaction() as c:
        Database._publish(c, "t", "p", "f" * 64)


def test_job_identity_is_separate_from_build_id(ws: Ws, db: Database) -> None:
    (s1,) = make_snaps(ws, 1)
    db.record_snapshot(s1)
    bid = build_dict(s1.snapshot_id, 1)["build_id"]
    j1, j2 = db.create_job(bid, s1.snapshot_id), db.create_job(bid, s1.snapshot_id)
    assert j1 != j2 and bid not in (j1, j2)  # same build identity, two attempts
    db.finish_job(j1, JobLifecycle.TIMED_OUT)
    db.finish_job(j2, JobLifecycle.SUCCEEDED, 0)
    assert {db.job(j1)["lifecycle"], db.job(j2)["lifecycle"]} == {"TIMED_OUT", "SUCCEEDED"}  # type: ignore[index]
    assert db.job("nope") is None


# --------------------------------------------------------------------------- GC
def test_gc_keeps_last_three_and_dry_run_lists_without_deleting(ws: Ws, db: Database) -> None:
    snaps = make_snaps(ws, 5)
    for s in snaps:
        db.record_snapshot(s)
    sdir = snapshots_dir(ws.ctx())
    plan = run_gc(db, sdir, dry_run=True)
    assert plan.dry_run and plan.deleted == []
    assert plan.delete == [snaps[1].snapshot_id, snaps[0].snapshot_id]
    assert set(plan.keep) == {s.snapshot_id for s in snaps[2:]}
    assert all(p.is_dir() for p in sdir.iterdir())  # nothing removed
    assert (
        db.conn.execute("SELECT COUNT(*) FROM snapshots WHERE gc_state='DELETED'").fetchone()[0]
        == 0
    )
    real = run_gc(db, sdir, dry_run=False)
    assert real.deleted == plan.delete
    assert not (sdir / snaps[0].snapshot_id).exists() and (sdir / snaps[4].snapshot_id).is_dir()
    state = {r["snapshot_id"]: r["gc_state"] for r in db.conn.execute("SELECT * FROM snapshots")}
    assert state[snaps[0].snapshot_id] == "DELETED" and state[snaps[4].snapshot_id] == "PRESENT"
    assert run_gc(db, sdir, dry_run=False).delete == []  # idempotent


def test_gc_protects_active_jobs_pending_edits_evidence_and_published(ws: Ws, db: Database) -> None:
    snaps = make_snaps(ws, 8)
    for s in snaps:
        db.record_snapshot(s)
    ids = [s.snapshot_id for s in snaps]
    # 0: active job (direct)   1: active job via build   2: pending edit base
    # 3: pending edit candidate   4: evidence   5: terminal edit + finished job (NOT protected)
    # newest three (5,6,7) are kept as recent; so rotate: use 0-4 for protection, 5 unprotected
    # but 5 is within the newest three -> use keep_last=1 to make protections observable.
    db.create_job(None, ids[0], JobLifecycle.RUNNING)
    b = build_dict(ids[1], 1)
    db.stage_build(b, publish=False)
    db.create_job(b["build_id"], None, JobLifecycle.QUEUED)
    with db.transaction() as c:
        c.execute(
            "INSERT INTO edit_transactions(transaction_id, base_snapshot_id, "
            "candidate_snapshot_id, state) VALUES ('t1',?,?, 'AWAITING_APPROVAL')",
            (ids[2], ids[3]),
        )
        c.execute(
            "INSERT INTO edit_transactions(transaction_id, base_snapshot_id, state) "
            "VALUES ('t2',?, 'RECOVERY_REQUIRED')",
            (ids[5],),
        )
        c.execute(
            "INSERT INTO edit_transactions(transaction_id, base_snapshot_id, state) "
            "VALUES ('t3',?, 'ABORTED')",
            (ids[6],),
        )
        c.execute(
            "INSERT INTO evidence(evidence_id, kind, producer, snapshot_id) "
            "VALUES ('e1','RUNTIME_OBSERVED','sim',?)",
            (ids[4],),
        )
    done = db.create_job(None, ids[6], JobLifecycle.RUNNING)
    db.finish_job(done, JobLifecycle.SUCCEEDED, 0)
    plan = plan_gc(db, snapshots_dir(ws.ctx()), keep_last=1)
    reasons = {k: set(v) for k, v in plan.keep.items()}
    assert reasons[ids[7]] == {"recent_successful"}
    assert reasons[ids[0]] == {"active_job"} and reasons[ids[1]] == {"active_job"}
    assert reasons[ids[2]] == reasons[ids[3]] == reasons[ids[5]] == {"pending_edit"}
    assert reasons[ids[4]] == {"evidence"}
    assert plan.delete == [ids[6]]  # terminal transaction + finished job protect nothing
    # a published pointer protects its snapshot as well
    with db.transaction() as c:
        c.execute("UPDATE jobs SET lifecycle='SUCCEEDED'")
    db.stage_build(build_dict(ids[6], 9))
    again = plan_gc(db, snapshots_dir(ws.ctx()), keep_last=1)
    assert set(again.keep[ids[6]]) == {"published"}
    assert set(again.delete) == {ids[0], ids[1]}  # jobs finished -> no longer protected


def test_gc_reports_orphans_and_cleans_temp_dirs_only(ws: Ws, db: Database) -> None:
    snaps = make_snaps(ws, 1)
    db.record_snapshot(snaps[0])
    sdir = snapshots_dir(ws.ctx())
    (sdir / "deadbeef").mkdir()
    (sdir / ".tmp-abc").mkdir()
    (sdir / ".tmp-abc" / "f").write_text("x")
    plan = run_gc(db, sdir, dry_run=True)
    assert plan.orphan_dirs == ["deadbeef"] and plan.temp_dirs == [".tmp-abc"]
    assert plan.to_dict()["orphan_dirs"] == ["deadbeef"]
    run_gc(db, sdir, dry_run=False)
    assert (sdir / "deadbeef").is_dir()  # unknown dirs are never deleted automatically
    assert not (sdir / ".tmp-abc").exists()
    assert plan_gc(db, sdir / "nope").orphan_dirs == []
