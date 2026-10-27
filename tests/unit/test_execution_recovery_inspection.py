"""Read-only recovery must not create databases/WAL files or claim remote termination."""
from __future__ import annotations

import hashlib
import os
from pathlib import Path

import pytest

from svweave.execution.ledger import ExecutionLedger
from svweave.execution.models import AttemptRecord, Ownership
from svweave.execution.recovery import inspect_recovery
from svweave.storage.db import Database


def record(**updates: object) -> AttemptRecord:
    values: dict[str, object] = dict(attempt_id="a", job_id="job", snapshot_id="snapshot",
                                   execution_fingerprint="0" * 64, owner_host="host",
                                   owner_boot_id="boot", owner_pid=1, owner_start_identity="start",
                                   submission_started=True,
                                   ownership=Ownership(attempt_id="a", nonce="n", cluster="c", uid=os.getuid(),
                                                       job_id="123", step_id="4", submitted_at="time"))
    values.update(updates)
    return AttemptRecord.model_validate(values)


def files(root: Path) -> dict[str, str]:
    return {str(p.relative_to(root)): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in root.rglob("*") if p.is_file() and not p.is_symlink()}


def seed(root: Path, *, ledger: bool = True, confirmed: bool = False) -> None:
    with Database(root / "svweave.db") as db:
        db.add_execution_attempt_ref(record())
    if ledger:
        directory = root / "execution"
        directory.mkdir(mode=0o700)
        ExecutionLedger(directory).save(record(cleanup_confirmed=confirmed))


def test_SRUN_10_inspection_does_not_create_state_or_database(tmp_path: Path) -> None:
    state = tmp_path / "absent"
    report = inspect_recovery(state)
    assert not state.exists()
    assert report["database_status"] == "ABSENT"
    assert not report["remote_terminal_verified"] and not report["scheduler_queried"]
    assert report["attempts"] == []
    assert "not proof" in report["limitations"][-1]


@pytest.mark.parametrize("confirmed", [False, True])
def test_SRUN_10_inspection_records_are_not_fresh_terminal_proof(tmp_path: Path, confirmed: bool) -> None:
    seed(tmp_path, confirmed=confirmed)
    before = files(tmp_path)
    report = inspect_recovery(tmp_path)
    assert report["database_status"] == "READ_ONLY_SNAPSHOT"
    assert report["workspace_blocked"] is (not confirmed)
    assert report["attempts"][0]["recorded_scheduler_target"] == "123.4"
    assert report["attempts"][0]["cleanup_confirmed_recorded"] is confirmed
    assert report["attempts"][0]["scheduler_state"] == "UNKNOWN"
    assert not report["remote_terminal_verified"]
    assert files(tmp_path) == before
    assert not (tmp_path / "svweave.db-wal").exists()
    assert not (tmp_path / "svweave.db-shm").exists()


@pytest.mark.parametrize("kind", ["missing", "corrupt", "symlink", "fifo"])
def test_SRUN_10_inspection_unknown_evidence_stays_blocked(tmp_path: Path, kind: str) -> None:
    seed(tmp_path, ledger=False)
    if kind != "missing":
        directory = tmp_path / "execution"
        directory.mkdir(mode=0o700)
        path = directory / "a.json"
        if kind == "corrupt":
            path.write_text("bad")
            path.chmod(0o600)
        elif kind == "symlink":
            path.symlink_to(tmp_path / "svweave.db")
        else:
            os.mkfifo(path, 0o600)
    report = inspect_recovery(tmp_path)
    assert report["workspace_blocked"]
    assert report["attempts"][0]["ledger_status"] == "MISSING"
    assert not report["remote_terminal_verified"]


def test_SRUN_10_inspection_never_ignores_or_creates_wal(tmp_path: Path) -> None:
    seed(tmp_path)
    wal = tmp_path / "svweave.db-wal"
    wal.write_bytes(b"pending or invalid WAL is not safe to ignore")
    before = files(tmp_path)
    report = inspect_recovery(tmp_path)
    assert report["database_status"] == "BUSY_OR_UNCHECKED_JOURNAL"
    assert report["workspace_blocked"] and report["pin_scope"] == "ALL"
    assert files(tmp_path) == before
    assert not (tmp_path / "svweave.db-shm").exists()


def test_SRUN_10_inspection_limits_entries_and_database_references(tmp_path: Path) -> None:
    with Database(tmp_path / "svweave.db") as db:
        db.add_execution_attempt_ref(record())
        db.add_execution_attempt_ref(record(attempt_id="b", ownership=None))
    report = inspect_recovery(tmp_path, limit=1)
    assert report["database_status"] == "LIMIT_EXCEEDED"
    assert report["workspace_blocked"] and report["pin_scope"] == "ALL"
    directory = tmp_path / "execution"
    directory.mkdir(mode=0o700)
    (directory / "one.tmp").write_text("")
    (directory / "two.tmp").write_text("")
    assert "ledger_limit_exceeded" in inspect_recovery(tmp_path, limit=1)["issues"]


def test_SRUN_10_inspection_refuses_symlink_state_root(tmp_path: Path) -> None:
    actual = tmp_path / "actual"
    actual.mkdir()
    seed(actual)
    linked = tmp_path / "linked"
    linked.symlink_to(actual, target_is_directory=True)
    before = files(actual)
    report = inspect_recovery(linked)
    assert report["database_status"] == "UNREADABLE_OR_UNSAFE" and report["workspace_blocked"]
    assert files(actual) == before


def test_SRUN_13_recovery_output_is_bounded_and_truncation_pins_all(tmp_path: Path, monkeypatch) -> None:
    from svweave.domain.ids import canonical_json
    from svweave.execution import recovery

    references = {f"a{n}": ("j" * 256, "s" * 256, "0" * 64) for n in range(1000)}
    monkeypatch.setattr(recovery, "_references", lambda *_: (references, "READ_ONLY_SNAPSHOT"))
    report = recovery.inspect_recovery(tmp_path, limit=1000)
    assert len(canonical_json(report).encode()) <= 64 * 1024
    assert report["truncated"] and report["workspace_blocked"]
    assert report["pin_scope"] == "ALL"
    assert "response_limit_exceeded" in report["issues"]


def test_SRUN_10_legacy_database_change_is_not_reported_clean(tmp_path: Path, monkeypatch) -> None:
    import sqlite3

    from svweave.execution import recovery

    path = tmp_path / "svweave.db"
    real_connect = sqlite3.connect
    with real_connect(path) as writer:
        writer.execute("PRAGMA user_version=4")
    class Cursor:
        def __init__(self, wrapped) -> None:
            self.wrapped = wrapped
        def fetchone(self):
            result = self.wrapped.fetchone()
            with real_connect(path) as writer:
                writer.execute("CREATE TABLE execution_attempt_refs(attempt_id TEXT,job_id TEXT,snapshot_id TEXT,execution_fingerprint TEXT)")
                writer.execute("INSERT INTO execution_attempt_refs VALUES ('a','job','snapshot',?)", ("0" * 64,))
                writer.execute("PRAGMA user_version=5")
            return result
    class Connection:
        def __init__(self, wrapped) -> None:
            self.wrapped = wrapped
        def __getattr__(self, name):
            return getattr(self.wrapped, name)
        def execute(self, sql, *args):
            cursor = self.wrapped.execute(sql, *args)
            return Cursor(cursor) if "sqlite_master" in sql else cursor
    monkeypatch.setattr(recovery.sqlite3, "connect", lambda *a, **kw: Connection(real_connect(*a, **kw)))
    report = recovery.inspect_recovery(tmp_path)
    assert report["database_status"] == "CHANGED_DURING_INSPECTION"
    assert report["workspace_blocked"] and report["pin_scope"] == "ALL"
