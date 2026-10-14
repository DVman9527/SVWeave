"""Write-ahead journal, per-file CAS apply and interrupted-write recovery (plan 12.5; EDT-07/08).

Filesystem facts, stated honestly: several file replacements are NOT one atomic transaction. We
get per-file atomic replace (temp file in the same directory, fsync, rename, directory fsync), a
cooperative workspace lock and a journal written BEFORE the first byte of the source changes.
Other editors can see the intermediate state; the analysis only reads immutable snapshots.

Recovery never overwrites bytes it did not write itself: a file is touched only when its current
hash equals the hash this transaction expects for that step; anything else is a conflict, the
human's version stays and the candidate version is written next to it for review.
"""

from __future__ import annotations

import json
import os
import stat
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from svweave.config.context import WorkspaceContext, snapshot_rel
from svweave.config.paths import is_hardlinked
from svweave.domain.errors import ErrorCode, SvwError
from svweave.domain.ids import sha256_hex
from svweave.security import safeio

JOURNAL_DIR = "journal"
RECOVERY_DIR = "recovery"
IN_PROGRESS = "in_progress"
COMPLETE = "complete"
RESOLVED = "resolved"


class SimulatedCrash(BaseException):
    """TEST-ONLY. Raised by the crash hook; derives from BaseException so that no handler in the
    commit path can 'clean up' (a real crash does not run cleanup either)."""


@dataclass
class JournalFile:
    logical_path: str
    before_sha256: str
    after_sha256: str
    status: str = "pending"  # pending | applied


@dataclass
class Journal:
    transaction_id: str
    base_snapshot_id: str
    patch_hash: str
    created_at: str
    status: str = IN_PROGRESS
    files: list[JournalFile] = field(default_factory=list)
    resolution: dict[str, Any] | None = None

    def to_json(self) -> dict[str, Any]:
        return {
            "schema": "svweave-journal-v1",
            "transaction_id": self.transaction_id,
            "base_snapshot_id": self.base_snapshot_id,
            "patch_hash": self.patch_hash,
            "created_at": self.created_at,
            "status": self.status,
            "files": [f.__dict__ for f in self.files],
            "resolution": self.resolution,
        }

    @classmethod
    def from_json(cls, d: dict[str, Any]) -> Journal:
        return cls(
            transaction_id=d["transaction_id"],
            base_snapshot_id=d["base_snapshot_id"],
            patch_hash=d["patch_hash"],
            created_at=d["created_at"],
            status=d["status"],
            files=[JournalFile(**f) for f in d["files"]],
            resolution=d.get("resolution"),
        )


def journal_dir(ctx: WorkspaceContext) -> Path:
    return ctx.state_dir / JOURNAL_DIR


def journal_rel(txn_id: str) -> str:
    return f"{JOURNAL_DIR}/{txn_id}.json"


def write_journal(ctx: WorkspaceContext, j: Journal) -> None:
    """Durable (fsync file + directory) atomic write of the journal."""
    safeio.ensure_private_dir(ctx.state_dir, JOURNAL_DIR)
    safeio.write_under(
        ctx.state_dir,
        journal_rel(j.transaction_id),
        (json.dumps(j.to_json(), indent=2, sort_keys=True) + "\n").encode(),
        0o600,
        durable=True,
    )


def read_journal(ctx: WorkspaceContext, txn_id: str) -> Journal | None:
    p = journal_dir(ctx) / f"{txn_id}.json"
    if not p.exists():
        return None
    try:
        return Journal.from_json(json.loads(safeio.read_regular(p, 1 << 22).decode("utf-8")))
    except (OSError, ValueError, KeyError, TypeError) as e:
        raise SvwError(
            ErrorCode.RECOVERY_REQUIRED,
            f"journal of {txn_id} is unreadable; manual recovery needed",
            {"reason": "journal_unreadable", "transaction_id": txn_id},
        ) from e


def unfinished_journals(ctx: WorkspaceContext) -> list[str]:
    """Transaction ids whose journal is not complete/resolved (or unreadable: fail closed)."""
    d = journal_dir(ctx)
    if not d.is_dir():
        return []
    out: list[str] = []
    for p in sorted(d.glob("*.json")):
        try:
            j = Journal.from_json(json.loads(safeio.read_regular(p, 1 << 22).decode("utf-8")))
            if j.status == IN_PROGRESS:
                out.append(j.transaction_id)
        except (OSError, ValueError, KeyError, TypeError, SvwError):
            out.append(p.stem)
    return out


def assert_no_recovery_pending(ctx: WorkspaceContext, blocking_txns: list[str]) -> None:
    """Every new prepare / commit is blocked while any journal is unfinished (EDT-07)."""
    pending = sorted(set(unfinished_journals(ctx)) | set(blocking_txns))
    if pending:
        raise SvwError(
            ErrorCode.RECOVERY_REQUIRED,
            "an interrupted edit commit exists; new edits are blocked until "
            "`svweave edit recover --transaction <id>` has resolved it: " + ", ".join(pending),
            {"transactions": pending},
        )


# --------------------------------------------------------------------------- file checks
def _lstat_regular(path: Path) -> os.stat_result:
    try:
        st = os.lstat(path)
    except FileNotFoundError as e:
        raise SvwError(
            ErrorCode.EDIT_CONFLICT, "target file disappeared", {"reason": "target_missing"}
        ) from e
    if stat.S_ISLNK(st.st_mode) or not stat.S_ISREG(st.st_mode):
        raise SvwError(
            ErrorCode.POLICY_DENIED,
            "target is a symlink or not a regular file",
            {"reason": "target_not_regular"},
        )
    if st.st_nlink > 1 or is_hardlinked(path):
        raise SvwError(
            ErrorCode.POLICY_DENIED,
            "target file is hardlinked; it can be changed through another name",
            {"reason": "target_hardlinked"},
        )
    return st


def current_sha(path: Path) -> str | None:
    """sha256 of a regular file, ``None`` if absent; a link/special file is a conflict marker."""
    try:
        st = os.lstat(path)
    except FileNotFoundError:
        return None
    if not stat.S_ISREG(st.st_mode):
        return "non-regular"
    return sha256_hex(safeio.read_regular(path))


def apply_files(
    ctx: WorkspaceContext,
    journal: Journal,
    payloads: dict[str, bytes],
    *,
    crash_after_files: int | None = None,
    on_progress: Callable[[Journal], None] | None = None,
) -> None:
    """Apply every journal entry: verify current == before, atomic durable replace, journal it.

    ``crash_after_files`` is a TEST-ONLY hook (keyword argument of the commit function; never read
    from configuration, environment or any MCP field).
    """
    applied = 0
    for jf in journal.files:
        if jf.status == "applied":
            applied += 1
            continue
        if crash_after_files is not None and applied >= crash_after_files:
            raise SimulatedCrash(f"simulated crash before file #{applied + 1}")
        path = ctx.to_abs(jf.logical_path)
        st = _lstat_regular(path)
        cur = current_sha(path)
        if cur != jf.before_sha256:
            raise SvwError(
                ErrorCode.EDIT_CONFLICT,
                f"{jf.logical_path} changed since the base snapshot; refusing to overwrite",
                {"reason": "expected_hash_mismatch", "path": jf.logical_path},
            )
        data = payloads[jf.logical_path]
        if sha256_hex(data) != jf.after_sha256:
            raise SvwError(
                ErrorCode.POLICY_DENIED,
                "candidate bytes differ from the validated after-hash",
                {"reason": "after_hash_mismatch", "path": jf.logical_path},
            )
        rel = path.relative_to(ctx.project_root).as_posix() if _inside(ctx, path) else None
        base = ctx.project_root if rel is not None else path.parent
        name = rel if rel is not None else path.name
        safeio.write_under(base, name, data, stat.S_IMODE(st.st_mode), durable=True)
        jf.status = "applied"
        write_journal(ctx, journal)
        if on_progress is not None:
            on_progress(journal)
        applied += 1


def _inside(ctx: WorkspaceContext, p: Path) -> bool:
    return ctx.project_root in p.parents


# --------------------------------------------------------------------------- recovery
@dataclass
class FileVerdict:
    logical_path: str
    verdict: str  # applied | not_applied | conflict
    current_sha256: str | None
    before_sha256: str
    after_sha256: str
    candidate_copy: str | None = None  # recovery path of the candidate version (conflicts)
    action: str | None = None

    def to_json(self) -> dict[str, Any]:
        return dict(self.__dict__)


def classify(ctx: WorkspaceContext, j: Journal) -> list[FileVerdict]:
    out: list[FileVerdict] = []
    for jf in j.files:
        cur = current_sha(ctx.to_abs(jf.logical_path))
        if cur == jf.after_sha256:
            v = "applied"
        elif cur == jf.before_sha256:
            v = "not_applied"
        else:
            v = "conflict"
        out.append(
            FileVerdict(jf.logical_path, v, cur, jf.before_sha256, jf.after_sha256)
        )
    return out


def preserve_candidate(
    ctx: WorkspaceContext, txn_id: str, logical: str, data: bytes
) -> str:
    """Write the candidate version next to (never over) the human's file under .svweave/recovery."""
    rel = f"{RECOVERY_DIR}/{txn_id}/{snapshot_rel(logical)}.candidate"
    parent = rel.rsplit("/", 1)[0]
    safeio.ensure_private_dir(ctx.state_dir, parent)
    safeio.write_under(ctx.state_dir, rel, data, 0o600, durable=True)
    return f".svweave/{rel}"
