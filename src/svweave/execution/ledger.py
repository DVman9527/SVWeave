"""Durable local attempt ledger; missing/corrupt evidence never releases remote input pins."""

from __future__ import annotations

import contextlib
import fcntl
import json
import os
import sqlite3
import stat
import uuid
from pathlib import Path

from svweave.domain.errors import ErrorCode, SvwError
from svweave.execution.models import AttemptRecord

MAX_RECORD_BYTES = 64 * 1024


class ExecutionLedger:
    """Operator-selected private local directory, not shared backend storage.

    Opening each component without following symlinks avoids redirection through a parent.
    Atomic replace plus directory fsync makes each persisted state an entire record.
    """

    def __init__(self, root: Path) -> None:
        if not root.is_absolute() or ".." in root.parts:
            raise ValueError("ledger path must be canonical and absolute")
        self.root = root

    def _open(self) -> int:
        fd = os.open("/", os.O_RDONLY | os.O_DIRECTORY)
        try:
            for part in self.root.parts[1:]:
                next_fd = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
                os.close(fd)
                fd = next_fd
            info = os.fstat(fd)
            if info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) & 0o077:
                raise ValueError("ledger directory must be private and owned by current UID")
            return fd
        except BaseException:
            os.close(fd)
            raise

    @staticmethod
    def _name(attempt_id: str) -> str:
        if not attempt_id or len(attempt_id) > 128 or any(
            c not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-"
            for c in attempt_id
        ):
            raise ValueError("invalid attempt id")
        return attempt_id + ".json"

    def save(self, record: AttemptRecord) -> None:
        data = record.model_dump_json().encode()
        if len(data) > MAX_RECORD_BYTES:
            raise ValueError("execution record exceeds limit")
        fd = self._open()
        temporary = ".tmp-" + uuid.uuid4().hex
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            try:
                previous = self.read(record.attempt_id)
            except FileNotFoundError:
                previous = None
            if previous is not None:
                immutable = ("attempt_id", "job_id", "snapshot_id", "execution_fingerprint",
                             "owner_host", "owner_boot_id", "owner_pid", "owner_start_identity")
                if any(getattr(previous, key) != getattr(record, key) for key in immutable):
                    raise ValueError("execution attempt identity is immutable")
                if previous.ownership is not None and previous.ownership != record.ownership:
                    raise ValueError("execution ownership cannot be replaced")
                for key in ("submission_started", "cleanup_confirmed", "cancel_requested"):
                    if getattr(previous, key) and not getattr(record, key):
                        raise ValueError("execution safety state cannot regress")
            out = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=fd)
            with os.fdopen(out, "wb") as stream:
                stream.write(data)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, self._name(record.attempt_id), src_dir_fd=fd, dst_dir_fd=fd)
            os.fsync(fd)
        finally:
            with contextlib.suppress(FileNotFoundError):
                os.unlink(temporary, dir_fd=fd)
            os.close(fd)

    def read(self, attempt_id: str) -> AttemptRecord:
        fd = self._open()
        try:
            src = os.open(self._name(attempt_id), os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=fd)
            with os.fdopen(src, "rb") as stream:
                info = os.fstat(stream.fileno())
                if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
                    raise ValueError("unsafe execution record")
                data = stream.read(MAX_RECORD_BYTES + 1)
            if len(data) > MAX_RECORD_BYTES:
                raise ValueError("execution record exceeds limit")
            record = AttemptRecord.model_validate_json(data)
            if record.attempt_id != attempt_id:
                raise ValueError("execution record identity mismatch")
            return record
        finally:
            os.close(fd)

    def pinned_snapshots(self) -> set[str]:
        try:
            fd = self._open()
            try:
                names = os.listdir(fd)
            finally:
                os.close(fd)
            pins = set()
            for name in names:
                if name.endswith(".json"):
                    record = self.read(name[:-5])
                    if record.pins_inputs:
                        pins.add(record.snapshot_id)
            return pins
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            raise SvwError(
                ErrorCode.RECOVERY_REQUIRED,
                "execution ledger cannot establish safe garbage collection",
                {"reason": "execution_ledger_unreadable"},
            ) from exc



def _references(state_dir: Path, conn: sqlite3.Connection | None) -> list[tuple[str, str, str, str]]:
    own_connection = None
    if conn is None:
        path = state_dir / "svweave.db"
        if not path.exists() and not path.is_symlink():
            return []
        if path.is_symlink() or not path.is_file():
            raise ValueError("unsafe execution reference database")
        own_connection = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)
        conn = own_connection
    try:
        present = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='execution_attempt_refs'"
        ).fetchone()
        if present is None:
            # Historical schemas had no enabled Slurm launch path. A missing table in
            # the schema that promises references is corruption, not an empty ledger.
            if int(conn.execute("PRAGMA user_version").fetchone()[0]) >= 5:
                raise ValueError("execution reference table missing")
            return []
        rows = conn.execute(
            "SELECT attempt_id,job_id,snapshot_id,execution_fingerprint FROM execution_attempt_refs"
        ).fetchall()
        result = []
        for row in rows:
            values = tuple(row)
            if len(values) != 4 or any(not isinstance(value, str) or not value for value in values):
                raise ValueError("malformed execution reference")
            ExecutionLedger._name(values[0])
            if len(values[3]) != 64 or any(c not in "0123456789abcdef" for c in values[3]):
                raise ValueError("malformed execution fingerprint")
            result.append((values[0], values[1], values[2], values[3]))
        return result
    finally:
        if own_connection is not None:
            own_connection.close()


def _reconciliation_state(
    state_dir: Path, conn: sqlite3.Connection | None,
) -> tuple[set[str] | None, set[str]]:
    """None pins every snapshot; '*' blocks every job when evidence is unreadable."""
    try:
        refs = _references(state_dir, conn)
        root = state_dir / "execution"
        ledger = ExecutionLedger(root)
        records: dict[str, AttemptRecord] = {}
        if root.exists() or root.is_symlink():
            fd = ledger._open()
            try:
                names = os.listdir(fd)
            finally:
                os.close(fd)
            for name in names:
                if name.endswith(".json"):
                    record = ledger.read(name[:-5])
                    records[record.attempt_id] = record
        pins: set[str] = set()
        jobs: set[str] = set()
        for attempt_id, job_id, snapshot_id, fingerprint in refs:
            candidate = records.get(attempt_id)
            if candidate is None or (
                candidate.job_id, candidate.snapshot_id, candidate.execution_fingerprint
            ) != (job_id, snapshot_id, fingerprint) or not candidate.cleanup_confirmed:
                pins.add(snapshot_id)
                jobs.add(job_id)
                if candidate is not None:
                    pins.add(candidate.snapshot_id)
                    jobs.add(candidate.job_id)
        # Legacy/unreferenced submitted records remain barriers as well. Neither DB
        # terminal state nor local PID/manager ownership proves remote termination.
        for record in records.values():
            if record.pins_inputs:
                pins.add(record.snapshot_id)
                jobs.add(record.job_id)
        return pins, jobs
    except (SvwError, ValueError, OSError, sqlite3.Error):
        return None, {"*"}


def pinned_snapshots(state_dir: Path, conn: sqlite3.Connection | None = None) -> set[str] | None:
    return _reconciliation_state(state_dir, conn)[0]


def job_requires_reconciliation(
    state_dir: Path, job_id: str, conn: sqlite3.Connection | None = None,
) -> bool:
    jobs = _reconciliation_state(state_dir, conn)[1]
    return "*" in jobs or job_id in jobs


def workspace_requires_reconciliation(
    state_dir: Path, conn: sqlite3.Connection | None = None,
) -> bool:
    return bool(_reconciliation_state(state_dir, conn)[1])


def ensure_reconciled(state_dir: Path, conn: sqlite3.Connection | None = None) -> None:
    if workspace_requires_reconciliation(state_dir, conn):
        raise SvwError(
            ErrorCode.RECOVERY_REQUIRED,
            "unresolved remote execution prevents new workspace submissions",
            {"reason": "execution_reconciliation_required", "submitted": False},
        )
