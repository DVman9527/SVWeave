"""Bounded local recovery inspection; never reconcile, cancel, submit, migrate or unpin.

SQLite is opened through an already verified read-only fd with mode=ro/immutable=1.
WAL/SHM presence is uncertainty, not permission to ignore pending WAL transactions.
That conservative branch avoids SQLite creating auxiliary files during inspection.
"""
from __future__ import annotations

import os
import re
import sqlite3
import stat
import time
from pathlib import Path
from typing import Any

from svweave.domain.ids import canonical_json
from svweave.execution.ledger import ExecutionLedger
from svweave.execution.models import AttemptRecord

_ID = re.compile(r"[A-Za-z0-9_.-]{1,256}\Z")
_HASH = re.compile(r"[0-9a-f]{64}\Z")


def _open_directory(path: Path) -> int:
    if not path.is_absolute() or ".." in path.parts:
        raise ValueError("noncanonical state directory")
    fd = os.open("/", os.O_RDONLY | os.O_DIRECTORY)
    try:
        for part in path.parts[1:]:
            child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            os.close(fd)
            fd = child
        return fd
    except BaseException:
        os.close(fd)
        raise


def _present(fd: int, name: str) -> bool:
    try:
        os.stat(name, dir_fd=fd, follow_symlinks=False)
        return True
    except FileNotFoundError:
        return False


def _references(state: Path, limit: int) -> tuple[dict[str, tuple[str, str, str]], str]:
    try:
        rootfd = _open_directory(state)
    except FileNotFoundError:
        return {}, "ABSENT"
    try:
        if any(_present(rootfd, "svweave.db" + suffix) for suffix in ("-wal", "-shm", "-journal")):
            return {}, "BUSY_OR_UNCHECKED_JOURNAL"
        try:
            dbfd = os.open("svweave.db", os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=rootfd)
        except FileNotFoundError:
            return {}, "ABSENT"
        try:
            before = os.fstat(dbfd)
            if not stat.S_ISREG(before.st_mode):
                raise ValueError("reference database is not regular")
            connection = sqlite3.connect(f"file:/proc/self/fd/{dbfd}?mode=ro&immutable=1", uri=True)
            try:
                connection.setlimit(sqlite3.SQLITE_LIMIT_LENGTH, 64 * 1024)
                start = time.monotonic()
                connection.set_progress_handler(lambda: int(time.monotonic() - start > 0.25), 1000)
                version = int(connection.execute("PRAGMA user_version").fetchone()[0])
                table = connection.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='execution_attempt_refs'").fetchone()
                read_status = "READ_ONLY_SNAPSHOT"
                if table is None:
                    if version >= 5:
                        raise ValueError("promised reference table absent")
                    read_status = "LEGACY_NO_REFERENCES"
                    rows = []
                else:
                    rows = connection.execute(
                        "SELECT attempt_id,job_id,snapshot_id,execution_fingerprint FROM execution_attempt_refs ORDER BY attempt_id LIMIT ?",
                        (limit + 1,),
                    ).fetchall()
                if len(rows) > limit:
                    return {}, "LIMIT_EXCEEDED"
                references = {}
                for attempt, job, snapshot, fingerprint in rows:
                    if (any(not isinstance(v, str) or not _ID.fullmatch(v) for v in (attempt, job, snapshot))
                            or not isinstance(fingerprint, str) or not _HASH.fullmatch(fingerprint)):
                        raise ValueError("invalid reference identity")
                    ExecutionLedger._name(attempt)
                    references[attempt] = (job, snapshot, fingerprint)
            finally:
                connection.close()
            after = os.fstat(dbfd)
            named = os.stat("svweave.db", dir_fd=rootfd, follow_symlinks=False)
            if (before.st_ino, before.st_size, before.st_mtime_ns, before.st_ctime_ns) != (
                    after.st_ino, after.st_size, after.st_mtime_ns, after.st_ctime_ns
            ) or named.st_ino != before.st_ino or any(
                _present(rootfd, "svweave.db" + suffix) for suffix in ("-wal", "-shm", "-journal")
            ):
                return {}, "CHANGED_DURING_INSPECTION"
            return references, read_status
        finally:
            os.close(dbfd)
    finally:
        os.close(rootfd)


def inspect_recovery(state_dir: Path, *, limit: int = 200) -> dict[str, Any]:
    """Return local recorded targets only; absence is never proof of remote termination."""
    if type(limit) is not int or not 1 <= limit <= 1000:
        raise ValueError("inspection limit must be an integer from 1 to 1000")
    issues: list[str] = []
    references: dict[str, tuple[str, str, str]] = {}
    records: dict[str, AttemptRecord] = {}
    try:
        references, database_status = _references(state_dir, limit)
    except (OSError, ValueError, sqlite3.Error):
        database_status = "UNREADABLE_OR_UNSAFE"
    if database_status not in ("ABSENT", "LEGACY_NO_REFERENCES", "READ_ONLY_SNAPSHOT"):
        issues.append("reference_database_uncertain")
    root = state_dir / "execution"
    try:
        directory = ExecutionLedger(root)
        fd = directory._open()
        try:
            names = []
            with os.scandir(fd) as entries:
                for scanned, entry in enumerate(entries):
                    if scanned >= limit:
                        issues.append("ledger_limit_exceeded")
                        break
                    if entry.name.endswith(".json"):
                        names.append(entry.name)
                        if len(names) > limit:
                            issues.append("ledger_limit_exceeded")
                            break
            for name in sorted(names[:limit]):
                try:
                    loaded = directory.read(name[:-5])
                    if not _ID.fullmatch(loaded.job_id) or not _ID.fullmatch(loaded.snapshot_id):
                        raise ValueError("unsafe recorded identity")
                    records[loaded.attempt_id] = loaded
                except (OSError, ValueError):
                    issues.append("ledger_record_unreadable")
        finally:
            os.close(fd)
    except FileNotFoundError:
        pass
    except (OSError, ValueError):
        issues.append("ledger_directory_unreadable")
    attempts = []
    pins: set[str] = set()
    identities = sorted(set(references) | set(records))
    if len(identities) > limit:
        issues.append("attempt_limit_exceeded")
    for attempt_id in identities[:limit]:
        reference, record = references.get(attempt_id), records.get(attempt_id)
        matches = bool(reference and record and reference == (
            record.job_id, record.snapshot_id, record.execution_fingerprint))
        unresolved = bool((reference and (not matches or not record or not record.cleanup_confirmed))
                          or (record and record.pins_inputs))
        if unresolved:
            if reference:
                pins.add(reference[1])
            if record:
                pins.add(record.snapshot_id)
        attempts.append({
            "attempt_id": attempt_id,
            "job_id": reference[0] if reference else record.job_id if record else None,
            "snapshot_id": reference[1] if reference else record.snapshot_id if record else None,
            "durable_reference_present": reference is not None,
            "ledger_status": "MISSING" if record is None else "IDENTITY_MISMATCH" if reference and not matches else "PRESENT",
            "phase_recorded": record.phase.value if record else "UNKNOWN",
            "cleanup_confirmed_recorded": record.cleanup_confirmed if record else None,
            "requires_reconciliation": unresolved,
            "recorded_scheduler_target": record.ownership.target if record and record.ownership else None,
            "allocation_owned_recorded": record.ownership.allocation_owned if record and record.ownership else None,
            "scheduler_state": "UNKNOWN",
        })
    uncertain = bool(issues)
    report: dict[str, Any] = {
        "schema_version": "1", "read_only": True, "submitted": False,
        "scheduler_queried": False, "remote_terminal_verified": False,
        "database_status": database_status,
        "workspace_blocked": uncertain or bool(pins),
        "pin_scope": "ALL" if uncertain else "RECORDED_SNAPSHOTS",
        "pinned_snapshot_ids": sorted(pins), "attempts": attempts,
        "issues": sorted(set(issues)), "limit": limit,
        "truncated": any("limit_exceeded" in issue for issue in issues),
        "limitations": [
            "Local records only; no scheduler query, cancellation, submission, migration or unpin was performed.",
            "Recorded IDs and cleanup flags are not current cancellation authority or proof of remote termination.",
            "No recorded attempts is not proof that no remote jobs exist.",
        ],
    }

    # Leave ample envelope/redaction overhead under the public 128 KiB response cap.
    # Truncation is uncertainty: never turn an incomplete inventory into "nothing live".
    if len(canonical_json(report).encode()) > 64 * 1024:
        report["truncated"] = True
        report["workspace_blocked"] = True
        report["pin_scope"] = "ALL"
        report["pinned_snapshot_ids"] = []
        report["issues"] = sorted(set(report["issues"]) | {"response_limit_exceeded"})
        while report["attempts"] and len(canonical_json(report).encode()) > 64 * 1024:
            report["attempts"].pop()
    return report
