"""SQLite access, repository operations, staged publication (plan 9.6) and GC."""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from svweave.domain.errors import ErrorCode, SvwError
from svweave.domain.ids import new_job_id
from svweave.domain.states import (
    ACTIVE_JOB_STATES,
    TERMINAL_TRANSACTION_STATES,
    Completeness,
    JobLifecycle,
)
from svweave.snapshot.models import SnapshotRecord
from svweave.storage.lock import WorkspaceBusy, init_lock, workspace_lock
from svweave.storage.migrations import migrate

KEEP_LAST_SUCCESSFUL = 3


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


class Database:
    def __init__(self, path: Path) -> None:
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(path), isolation_level=None)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys = ON")
        # Opening a fresh database switches the journal mode and migrates; both fail immediately
        # with "database is locked" when several processes/threads open it at once, so setup is
        # serialised with an advisory lock (steady-state opens only pay a PRAGMA read).
        with init_lock(path):
            self.conn.execute("PRAGMA journal_mode = WAL")
            self.version = migrate(self.conn)

    def close(self) -> None:
        self.conn.close()

    def __enter__(self) -> Database:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        """BEGIN IMMEDIATE ... COMMIT; any exception rolls everything back."""
        self.conn.execute("BEGIN IMMEDIATE")
        try:
            yield self.conn
        except BaseException:
            self.conn.execute("ROLLBACK")
            raise
        else:
            self.conn.execute("COMMIT")

    def tables(self) -> set[str]:
        rows = self.conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
        return {r["name"] for r in rows}

    # ---------------------------------------------------------------- workspace / snapshot
    def upsert_workspace(
        self, workspace_id: str, root_mapping: dict[str, str], policy_hash: str
    ) -> None:
        with self.transaction() as c:
            c.execute(
                "INSERT INTO workspaces(workspace_id, root_mapping, policy_hash, created_at) "
                "VALUES (?,?,?,?) ON CONFLICT(workspace_id) DO UPDATE SET "
                "root_mapping=excluded.root_mapping, policy_hash=excluded.policy_hash",
                (workspace_id, json.dumps(root_mapping, sort_keys=True), policy_hash, _now()),
            )

    def add_retention_ref(self, snapshot_id: str, ref: str) -> None:
        """Pin a snapshot for an external reason (audit, release...). GC never deletes it."""
        with self.transaction() as c:
            row = c.execute(
                "SELECT retention_refs FROM snapshots WHERE snapshot_id=?", (snapshot_id,)
            ).fetchone()
            if row is None:
                raise SvwError(ErrorCode.MISSING_INPUT, f"unknown snapshot {snapshot_id}")
            refs = json.loads(row["retention_refs"] or "[]")
            if ref not in refs:
                refs.append(ref)
            c.execute(
                "UPDATE snapshots SET retention_refs=? WHERE snapshot_id=?",
                (json.dumps(refs), snapshot_id),
            )

    def record_snapshot(self, rec: SnapshotRecord) -> None:
        """Insert snapshot + source_files atomically (idempotent for an existing id)."""
        with self.transaction() as c:
            exists = c.execute(
                "SELECT gc_state FROM snapshots WHERE snapshot_id=?", (rec.snapshot_id,)
            ).fetchone()
            seq = c.execute("SELECT COALESCE(MAX(seq),0)+1 FROM snapshots").fetchone()[0]
            if exists is not None:
                # identical content captured again: it is the newest capture now (GC orders by seq)
                c.execute(
                    "UPDATE snapshots SET gc_state='PRESENT', seq=? WHERE snapshot_id=?",
                    (seq, rec.snapshot_id),
                )
                return
            c.execute(
                "INSERT INTO snapshots(snapshot_id, workspace_id, profile, config_id, "
                "inventory_hash, capture_metadata, created_at, seq) VALUES (?,?,?,?,?,?,?,?)",
                (
                    rec.snapshot_id,
                    rec.workspace_id,
                    rec.profile,
                    rec.config_id,
                    rec.snapshot_id,
                    json.dumps({"attempts": rec.attempts}),
                    rec.created_at,
                    seq,
                ),
            )
            c.executemany(
                "INSERT INTO source_files(snapshot_id, logical_path, bytes_hash, size, origin, "
                "role, ord, symlink_target) VALUES (?,?,?,?,?,?,?,?)",
                [
                    (
                        rec.snapshot_id,
                        f.logical_path,
                        f.sha256,
                        f.size,
                        f.original_path,
                        f.role,
                        i,
                        f.symlink_target,
                    )
                    for i, f in enumerate(rec.files)
                ],
            )

    # ---------------------------------------------------------------- jobs
    def create_job(
        self,
        build_id: str | None,
        snapshot_id: str | None,
        lifecycle: JobLifecycle = JobLifecycle.RUNNING,
        job_id: str | None = None,
    ) -> str:
        jid = job_id or new_job_id()
        with self.transaction() as c:
            c.execute(
                "INSERT INTO jobs(job_id, build_id, snapshot_id, lifecycle, started_at) "
                "VALUES (?,?,?,?,?)",
                (jid, build_id, snapshot_id, lifecycle.value, _now()),
            )
        return jid

    def start_job(
        self,
        job_id: str,
        build_id: str | None,
        snapshot_id: str | None,
        lifecycle: JobLifecycle = JobLifecycle.RUNNING,
    ) -> str:
        """Move a pre-registered (QUEUED) job to RUNNING, or create it when unknown."""
        with self.transaction() as c:
            cur = c.execute(
                "UPDATE jobs SET build_id=?, snapshot_id=?, lifecycle=?, started_at=? "
                "WHERE job_id=?",
                (build_id, snapshot_id, lifecycle.value, _now(), job_id),
            )
            if cur.rowcount == 0:
                c.execute(
                    "INSERT INTO jobs(job_id, build_id, snapshot_id, lifecycle, started_at) "
                    "VALUES (?,?,?,?,?)",
                    (job_id, build_id, snapshot_id, lifecycle.value, _now()),
                )
        return job_id

    def finish_job(
        self,
        job_id: str,
        lifecycle: JobLifecycle,
        exit_code: int | None = None,
        error: str | None = None,
    ) -> None:
        with self.transaction() as c:
            c.execute(
                "UPDATE jobs SET lifecycle=?, finished_at=?, exit_code=?, error=? WHERE job_id=?",
                (lifecycle.value, _now(), exit_code, error, job_id),
            )

    def job(self, job_id: str) -> sqlite3.Row | None:
        row: sqlite3.Row | None = self.conn.execute(
            "SELECT * FROM jobs WHERE job_id=?", (job_id,)
        ).fetchone()
        return row

    # ---------------------------------------------------------------- staged publication
    def stage_build(
        self,
        build: dict[str, Any],
        rows: dict[str, list[dict[str, Any]]] | None = None,
        *,
        validate: Callable[[sqlite3.Connection, str], None] | None = None,
        publish: bool = True,
    ) -> bool:
        """Write a build and its rows in ONE transaction; publish the pointer last.

        If anything raises (row insert, `validate`, crash simulation), the transaction rolls
        back: no partial build rows and the previous published pointer is untouched (SNP-06).
        `build` needs: build_id, workspace_id, profile, snapshot_id, config_id, toolchain_id,
        semantic_schema_version, completeness, diagnostic_outcome.

        Returns True when the published pointer moved. Only COMPLETE builds are ever published
        (plan 9.6 / 25.1): PARTIAL and BLOCKED builds are stored and addressable by build_id but
        never become the "current" build.
        """
        for table in rows or {}:
            if not table.replace("_", "").isalnum():
                raise SvwError(ErrorCode.INVALID_CONFIG, f"bad table name {table!r}")
        with self.transaction() as c:
            c.execute(
                "INSERT INTO builds(build_id, workspace_id, profile, snapshot_id, config_id, "
                "toolchain_id, extractor_id, semantic_schema_version, completeness, "
                "diagnostic_outcome, created_at) VALUES "
                "(:build_id,:workspace_id,:profile,:snapshot_id,:config_id,:toolchain_id,"
                ":extractor_id,:semantic_schema_version,:completeness,:diagnostic_outcome,:created_at)",
                {"extractor_id": None, "created_at": _now(), **build},
            )
            for table, table_rows in (rows or {}).items():
                for r in table_rows:
                    cols = ",".join(f'"{k}"' for k in r)
                    marks = ",".join("?" for _ in r)
                    c.execute(f'INSERT INTO "{table}"({cols}) VALUES ({marks})', tuple(r.values()))
            if validate is not None:
                validate(c, build["build_id"])
            if publish and build["completeness"] == Completeness.COMPLETE.value:
                self._publish(c, build["workspace_id"], build["profile"], build["build_id"])
                return True
            return False

    @staticmethod
    def _publish(c: sqlite3.Connection, workspace_id: str, profile: str, build_id: str) -> None:
        row = c.execute(
            "SELECT snapshot_id, completeness FROM builds WHERE build_id=?", (build_id,)
        ).fetchone()
        if row is None:
            raise SvwError(ErrorCode.INVALID_CONFIG, f"cannot publish unknown build {build_id}")
        if row["completeness"] != Completeness.COMPLETE.value:
            raise SvwError(
                ErrorCode.PARTIAL_ANALYSIS,
                f"build {build_id} is {row['completeness']}; only COMPLETE builds are published",
                {"build_id": build_id, "completeness": row["completeness"]},
            )
        c.execute(
            "INSERT INTO published_pointer(workspace_id, profile, build_id, snapshot_id, "
            "published_at) VALUES (?,?,?,?,?) ON CONFLICT(workspace_id, profile) DO UPDATE SET "
            "build_id=excluded.build_id, snapshot_id=excluded.snapshot_id, "
            "published_at=excluded.published_at",
            (workspace_id, profile, build_id, row["snapshot_id"], _now()),
        )

    def publish_build(self, build_id: str) -> None:
        """Move the published pointer to an existing COMPLETE build (M4 post-apply)."""
        with self.transaction() as c:
            b = c.execute(
                "SELECT workspace_id, profile FROM builds WHERE build_id=?", (build_id,)
            ).fetchone()
            if b is None:
                raise SvwError(ErrorCode.MISSING_INPUT, f"unknown build {build_id}")
            self._publish(c, b["workspace_id"], b["profile"], build_id)

    def published(self, workspace_id: str, profile: str) -> sqlite3.Row | None:
        row: sqlite3.Row | None = self.conn.execute(
            "SELECT * FROM published_pointer WHERE workspace_id=? AND profile=?",
            (workspace_id, profile),
        ).fetchone()
        return row


# ------------------------------------------------------------------------------ GC
@dataclass
class GcPlan:
    keep: dict[str, list[str]] = field(default_factory=dict)  # snapshot_id -> reasons
    delete: list[str] = field(default_factory=list)
    orphan_dirs: list[str] = field(default_factory=list)  # on disk, unknown to DB (never deleted)
    temp_dirs: list[str] = field(default_factory=list)  # leftover .tmp-* capture dirs
    skipped_reason: str | None = None  # e.g. "workspace_busy": a live capture/commit holds the lock
    deleted: list[str] = field(default_factory=list)
    dry_run: bool = True

    def to_dict(self) -> dict[str, Any]:
        return {
            "dry_run": self.dry_run,
            "keep": self.keep,
            "delete": self.delete,
            "deleted": self.deleted,
            "orphan_dirs": self.orphan_dirs,
            "temp_dirs": self.temp_dirs,
            "skipped_reason": self.skipped_reason,
        }


def _record_snapshots(raw: str | None) -> list[str] | None:
    """Snapshot ids named by an open transaction's ``record.profile_builds``. An unreadable record
    cannot be proven to name nothing: ``None`` makes ``plan_gc`` keep everything (fail closed)."""
    try:
        rec = json.loads(raw or "{}")
        builds = rec.get("profile_builds") or {}
        return [
            v[k]
            for v in builds.values()
            for k in ("baseline_snapshot_id", "candidate_snapshot_id")
            if isinstance(v, dict) and isinstance(v.get(k), str) and v[k]
        ]
    except (ValueError, AttributeError, TypeError):
        return None


def plan_gc(db: Database, snapshots_dir: Path, keep_last: int = KEEP_LAST_SUCCESSFUL) -> GcPlan:
    """Decide what GC may delete (plan 9.6). Pure read; no filesystem or DB mutation."""
    c = db.conn
    plan = GcPlan()
    present = [
        r["snapshot_id"]
        for r in c.execute(
            "SELECT snapshot_id FROM snapshots WHERE gc_state='PRESENT' ORDER BY seq DESC"
        )
    ]

    def keep(sid: str, why: str) -> None:
        plan.keep.setdefault(sid, []).append(why)

    for sid in present[:keep_last]:
        keep(sid, "recent_successful")
    active = ",".join(f"'{s.value}'" for s in ACTIVE_JOB_STATES)
    for r in c.execute(
        f"SELECT DISTINCT COALESCE(j.snapshot_id, b.snapshot_id) AS sid FROM jobs j "
        f"LEFT JOIN builds b ON b.build_id=j.build_id WHERE j.lifecycle IN ({active})"
    ):
        if r["sid"]:
            keep(r["sid"], "active_job")
    terminal = ",".join(f"'{s.value}'" for s in TERMINAL_TRANSACTION_STATES)
    for r in c.execute(
        f"SELECT base_snapshot_id, candidate_snapshot_id, record FROM edit_transactions "
        f"WHERE state NOT IN ({terminal})"
    ):
        for sid in (r["base_snapshot_id"], r["candidate_snapshot_id"]):
            if sid:
                keep(sid, "pending_edit")
        # every other validate profile's baseline / candidate snapshot (record.profile_builds)
        named = _record_snapshots(r["record"])
        if named is None:
            for sid in present:
                keep(sid, "pending_edit")
        else:
            for sid in named:
                keep(sid, "pending_edit")
    for r in c.execute("SELECT DISTINCT snapshot_id FROM evidence WHERE snapshot_id IS NOT NULL"):
        keep(r["snapshot_id"], "evidence")
    # evidence that names only a build is still evidence for that build's snapshot
    for r in c.execute(
        "SELECT DISTINCT b.snapshot_id AS sid FROM evidence e JOIN builds b "
        "ON b.build_id=e.build_id WHERE e.build_id IS NOT NULL"
    ):
        keep(r["sid"], "evidence")
    for r in c.execute("SELECT snapshot_id, retention_refs FROM snapshots"):
        try:
            refs = json.loads(r["retention_refs"] or "[]")
        except json.JSONDecodeError:
            refs = ["<unparseable retention_refs>"]  # fail closed: keep
        if refs:
            keep(r["snapshot_id"], "retention_ref")
    for r in c.execute("SELECT DISTINCT snapshot_id FROM published_pointer"):
        keep(r["snapshot_id"], "published")
    for sid in present:
        if sid not in plan.keep:
            plan.delete.append(sid)
    known = {r["snapshot_id"] for r in c.execute("SELECT snapshot_id FROM snapshots")}
    if snapshots_dir.is_dir():
        for p in sorted(snapshots_dir.iterdir()):
            if p.name.startswith(".tmp-"):
                plan.temp_dirs.append(p.name)
            elif p.is_dir() and p.name not in known:
                plan.orphan_dirs.append(p.name)
    return plan


def run_gc(
    db: Database,
    snapshots_dir: Path,
    *,
    dry_run: bool = True,
    keep_last: int = KEEP_LAST_SUCCESSFUL,
) -> GcPlan:
    from svweave.snapshot.capture import remove_snapshot_dir

    plan = plan_gc(db, snapshots_dir, keep_last)
    plan.dry_run = dry_run
    if dry_run:
        return plan
    try:
        # Exclusive + non-blocking: while any capture (shared holder) or edit commit is live,
        # nothing is deleted, including its .tmp-* directory.
        with workspace_lock(snapshots_dir.parent, exclusive=True, blocking=False):
            # the first plan was computed WITHOUT the lock: a prepare / build may have started
            # since, so the deletion plan that counts is the one made while holding it (A5)
            plan = plan_gc(db, snapshots_dir, keep_last)
            plan.dry_run = False
            for sid in plan.delete:
                remove_snapshot_dir(snapshots_dir / sid)
                with db.transaction() as c:
                    c.execute("UPDATE snapshots SET gc_state='DELETED' WHERE snapshot_id=?", (sid,))
                plan.deleted.append(sid)
            for name in plan.temp_dirs:
                remove_snapshot_dir(snapshots_dir / name)
    except WorkspaceBusy:
        plan.skipped_reason = "workspace_busy"
    return plan
