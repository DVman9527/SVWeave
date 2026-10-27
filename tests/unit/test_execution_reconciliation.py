"""SRUN-10/17: database references survive file-ledger loss; no scheduler is contacted."""
from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from svweave.domain.errors import SvwError
from svweave.execution.ledger import (
    ExecutionLedger,
    ensure_reconciled,
    job_requires_reconciliation,
    pinned_snapshots,
    workspace_requires_reconciliation,
)
from svweave.execution.models import AttemptRecord
from svweave.storage.db import Database, plan_gc
from svweave.storage.migrations import MIGRATIONS, migrate


def attempt(**changes: object) -> AttemptRecord:
    fields: dict[str, object] = dict(attempt_id="attempt", job_id="job", snapshot_id="snapshot", execution_fingerprint="0" * 64, owner_host="host", owner_boot_id="boot", owner_pid=123, owner_start_identity="start", submission_started=True)
    fields.update(changes)
    return AttemptRecord.model_validate(fields)


def test_SRUN_10_reference_survives_missing_directory_and_record(tmp_path: Path) -> None:
    with Database(tmp_path / "svweave.db") as db:
        db.add_execution_attempt_ref(attempt())
        assert workspace_requires_reconciliation(tmp_path, db.conn)
        assert workspace_requires_reconciliation(tmp_path)  # read-only automatic DB discovery
        assert pinned_snapshots(tmp_path) == {"snapshot"}
        root = tmp_path / "execution"
        root.mkdir(mode=0o700)
        ledger = ExecutionLedger(root)
        ledger.save(attempt())
        (root / "attempt.json").unlink()
        assert pinned_snapshots(tmp_path, db.conn) == {"snapshot"}
        with pytest.raises(SvwError, match="unresolved"):
            ensure_reconciled(tmp_path, db.conn)


def test_SRUN_10_reference_is_committed_idempotent_and_immutable(tmp_path: Path) -> None:
    with Database(tmp_path / "svweave.db") as db:
        db.add_execution_attempt_ref(attempt())
        db.add_execution_attempt_ref(attempt())
        with sqlite3.connect(db.path) as other:
            assert other.execute("SELECT COUNT(*) FROM execution_attempt_refs").fetchone()[0] == 1
        with pytest.raises(SvwError):
            db.add_execution_attempt_ref(attempt(snapshot_id="different"))
        for sql in (
            "DELETE FROM execution_attempt_refs",
            "UPDATE execution_attempt_refs SET snapshot_id='different'",
            "INSERT OR REPLACE INTO execution_attempt_refs VALUES ('attempt','job','different','x','today')",
        ):
            with pytest.raises(sqlite3.IntegrityError):
                db.conn.execute(sql)


def test_SRUN_17_corrupt_and_mismatched_referenced_ledger_fail_closed(tmp_path: Path) -> None:
    with Database(tmp_path / "svweave.db") as db:
        db.add_execution_attempt_ref(attempt())
        root = tmp_path / "execution"
        root.mkdir(mode=0o700)
        ledger = ExecutionLedger(root)
        ledger.save(attempt(snapshot_id="other", cleanup_confirmed=True))
        assert pinned_snapshots(tmp_path, db.conn) == {"snapshot", "other"}
        assert workspace_requires_reconciliation(tmp_path, db.conn)
        (root / "attempt.json").write_text("corrupt")
        assert pinned_snapshots(tmp_path, db.conn) is None
        assert workspace_requires_reconciliation(tmp_path, db.conn)


def test_SRUN_10_valid_confirmed_evidence_does_not_remove_reference(tmp_path: Path) -> None:
    with Database(tmp_path / "svweave.db") as db:
        db.add_execution_attempt_ref(attempt())
        root = tmp_path / "execution"
        root.mkdir(mode=0o700)
        ExecutionLedger(root).save(attempt(cleanup_confirmed=True))
        assert not workspace_requires_reconciliation(tmp_path, db.conn)
        assert pinned_snapshots(tmp_path, db.conn) == set()
        assert db.conn.execute("SELECT COUNT(*) FROM execution_attempt_refs").fetchone()[0] == 1
        (root / "attempt.json").unlink()
        assert workspace_requires_reconciliation(tmp_path, db.conn)


def test_SRUN_10_unreferenced_submission_blocks_workspace(tmp_path: Path) -> None:
    root = tmp_path / "execution"
    root.mkdir(mode=0o700)
    ExecutionLedger(root).save(attempt())
    assert workspace_requires_reconciliation(tmp_path)
    assert job_requires_reconciliation(tmp_path, "job")
    with pytest.raises(SvwError):
        ensure_reconciled(tmp_path)


def test_SRUN_10_reference_before_submission_is_already_a_barrier(tmp_path: Path) -> None:
    with Database(tmp_path / "svweave.db") as db:
        initial = attempt(submission_started=False)
        db.add_execution_attempt_ref(initial)
        root = tmp_path / "execution"
        root.mkdir(mode=0o700)
        ExecutionLedger(root).save(initial)
        assert workspace_requires_reconciliation(tmp_path, db.conn)
        assert pinned_snapshots(tmp_path, db.conn) == {"snapshot"}


def test_SRUN_17_gc_preserves_reference_when_file_ledger_missing(tmp_path: Path) -> None:
    with Database(tmp_path / "svweave.db") as db:
        db.upsert_workspace("ws", {}, "hash")
        db.conn.execute("INSERT INTO snapshots (snapshot_id,workspace_id,profile,config_id,inventory_hash,capture_metadata,created_at,seq) VALUES ('snapshot','ws','p','cfg','inv','{}','today',1)")
        db.add_execution_attempt_ref(attempt())
        plan = plan_gc(db, tmp_path / "snapshots", keep_last=0)
        assert "execution_cleanup_unconfirmed" in plan.keep["snapshot"]
        assert "snapshot" not in plan.delete


def test_SRUN_22_v4_to_v5_append_only_migration(tmp_path: Path) -> None:
    from svweave.storage.migrations import _split

    conn = sqlite3.connect(tmp_path / "old.db", isolation_level=None)
    for version, sql in MIGRATIONS[:4]:
        for statement in _split(sql):
            conn.execute(statement)
        conn.execute(f"PRAGMA user_version={version}")
    assert conn.execute("PRAGMA user_version").fetchone()[0] == 4
    assert migrate(conn) == 5
    assert conn.execute("SELECT COUNT(*) FROM execution_attempt_refs").fetchone()[0] == 0
    conn.close()


@pytest.mark.parametrize("lifecycle", ["RUNNING", "FAILED", "CANCELLED", "TIMED_OUT", "SUCCEEDED"])
def test_SRUN_10_terminal_job_and_current_owner_do_not_clear_barrier(tmp_path: Path, lifecycle: str) -> None:
    import os

    with Database(tmp_path / "svweave.db") as db:
        current = attempt(owner_pid=os.getpid())
        db.add_execution_attempt_ref(current)
        root = tmp_path / "execution"
        root.mkdir(mode=0o700)
        ExecutionLedger(root).save(current)
        db.conn.execute("INSERT INTO jobs(job_id,lifecycle) VALUES (?,?)", (current.job_id, lifecycle))
        assert workspace_requires_reconciliation(tmp_path, db.conn)
        with pytest.raises(SvwError):
            ensure_reconciled(tmp_path, db.conn)


def test_SRUN_10_missing_promised_reference_table_is_uncertain(tmp_path: Path) -> None:
    with Database(tmp_path / "svweave.db") as db:
        db.conn.execute("DROP TABLE execution_attempt_refs")
        assert workspace_requires_reconciliation(tmp_path, db.conn)
        assert pinned_snapshots(tmp_path, db.conn) is None
