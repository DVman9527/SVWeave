"""Shared helpers for the build use case and the query services: build lookup, freshness,
analysis coverage and the common result envelope (plan 14.5)."""

from __future__ import annotations

import json
import os
import sqlite3
import time
from pathlib import Path
from typing import Any

from svweave.config.context import WorkspaceContext
from svweave.domain.envelope import (
    AnalysisCoverage,
    Envelope,
    ErrorInfo,
    Outcome,
    Pagination,
    Provenance,
)
from svweave.domain.errors import ErrorCode, SvwError
from svweave.domain.ids import is_sha256_hex, sha256_hex
from svweave.domain.states import Completeness, EvidenceKind, Freshness
from svweave.snapshot.capture import check_freshness, load_record, snapshots_dir, walk_dirs
from svweave.snapshot.models import SnapshotRecord
from svweave.storage.db import Database
from svweave.storage.semantic import loads

BASIS = "qualified_static_subset"


def require_build(db: Database, build_id: str) -> sqlite3.Row:
    if not is_sha256_hex(build_id):
        raise SvwError(
            ErrorCode.INVALID_CONFIG,
            "build id must be 64 lowercase hex characters",
            {"build_id": build_id},
        )
    row: sqlite3.Row | None = db.conn.execute(
        "SELECT * FROM builds WHERE build_id=?", (build_id,)
    ).fetchone()
    if row is None:
        raise SvwError(
            ErrorCode.MISSING_INPUT,
            f"unknown build {build_id}",
            {"reason": "build_missing", "build_id": build_id},
        )
    return row


def inputs_unchanged(ctx: WorkspaceContext, build: sqlite3.Row) -> bool:
    """The build's config_id and toolchain_id equal what the workspace resolves to NOW. A change
    that only touches ``svweave.yaml`` (defines, parameters, include order, CU mode...) leaves the
    snapshot identical but makes the build describe a different configuration (M2)."""
    from svweave.application.toolchain import compute_toolchain
    from svweave.manifest.resolver import resolve_profile

    try:
        nm = resolve_profile(ctx, build["profile"])
        tc_id = compute_toolchain(ctx.project_root)[0]
    except SvwError:
        return False  # the profile no longer resolves / the toolchain changed
    return bool(nm.config_id == build["config_id"] and tc_id == build["toolchain_id"])


# ------------------------------------------------------------------------------ freshness
# Read-path fast path. The authoritative check (check_freshness: re-enumerate the inventory and
# lookup search space and HASH every file, plus re-resolving the manifest) costs seconds on a
# 100k-line design. A query only needs to know whether anything it depends on changed since the
# last full check, so the full verdict is cached per (workspace, build) together with a stat
# fingerprint of everything that check reads. Same fingerprint -> same verdict; any difference
# -> full check again. Commit/publish paths do NOT use this module's fast path (they call
# check_freshness / byte comparisons directly, plan 9.6); ``strict=True`` forces the full check.
#
# LIMITATION (documented in docs/limitations.md): an edit that preserves size, mtime_ns, ino AND
# ctime_ns of a file is invisible to the read-path shortcut; ctime cannot be set from user space,
# so this needs root or a clock rollback.
_VERDICTS: dict[tuple[str, str], tuple[str, bool | None]] = {}
_RECORDS: dict[tuple[str, str], SnapshotRecord] = {}


def _sig(path: str) -> tuple[Any, ...]:
    try:
        st = os.stat(path)
    except OSError:
        return (path, None)
    return (path, st.st_size, st.st_mtime_ns, st.st_ino, st.st_ctime_ns)


def _dir_sigs(ctx: WorkspaceContext, root: str, recursive: bool) -> list[tuple[Any, ...]]:
    """Stat signatures of a lookup directory (and its sub-directories when searched
    recursively): adding/removing/renaming an entry changes the directory's mtime/ctime. Uses the
    snapshot's bounded traversal (each real directory once, root exclusions, trusted symlinks), so
    a symlink cycle cannot blow up and excluded trees do not matter. A traversal the capture rules
    refuse yields an always-different signature, i.e. the full check runs (and reports)."""
    if not recursive:
        return [_sig(root)]
    try:
        return [_sig(str(d)) for d in walk_dirs(ctx, Path(root))]
    except (SvwError, OSError):
        return [("<untraversable>", root, time.monotonic_ns())]


def input_fingerprint(ctx: WorkspaceContext, build: sqlite3.Row, rec: SnapshotRecord) -> str:
    """Digest of every input of the full freshness verdict (stat-level for files)."""
    from importlib import metadata

    from svweave.application import toolchain as tc

    prof = ctx.manifest.profiles.get(build["profile"])
    env_names = sorted(getattr(prof, "environment_allowlist", []) or [])
    try:
        pyslang_v = metadata.version("pyslang")
    except metadata.PackageNotFoundError:
        pyslang_v = None
    try:
        lock = tc.find_lock(ctx.project_root)
        lock_sig = _sig(str(lock))
    except SvwError:
        lock_sig = ("<no lock>",)
    doc = {
        # the loaded config objects (manifest, local mapping, trusted policy effect, roots)
        "manifest": ctx.manifest.model_dump_json(),
        "local": ctx.local.model_dump_json() if ctx.local else None,
        "policy": ctx.policy.model_dump_json(),
        "roots": [[r.id, str(r.abs), r.internal] for r in ctx.roots],
        "env": [[n, os.environ.get(n)] for n in env_names],
        "toolchain": [tc.EXTRACTOR_VERSION, tc.extractor_source_hash(), pyslang_v, lock_sig],
        "files": [_sig(f.original_path) for f in rec.files],
        "lookup": [
            [lk.kind, lk.logical_dir, _dir_sigs(ctx, lk.original_dir, lk.recursive)] for lk in rec.lookup
        ],
    }
    return sha256_hex(json.dumps(doc, sort_keys=True, default=str))


def _inputs_current_full(
    ctx: WorkspaceContext, build: sqlite3.Row, rec: SnapshotRecord
) -> bool | None:
    try:
        res = check_freshness(ctx, rec)
    except SvwError:
        return None
    return bool(res.freshness == "CURRENT" and inputs_unchanged(ctx, build))


def freshness_of(
    ctx: WorkspaceContext, db: Database, build: sqlite3.Row, *, strict: bool = False
) -> Freshness:
    """CURRENT: snapshot equals the workspace AND the config/toolchain identities still match.
    Otherwise STALE if this is the latest build of the profile, HISTORICAL if a newer build of the
    same profile exists (explicit old-build query). ``strict`` skips the stat fast path."""
    sid = build["snapshot_id"]
    sdir = snapshots_dir(ctx) / sid
    snap_row = db.conn.execute(
        "SELECT gc_state FROM snapshots WHERE snapshot_id=?", (sid,)
    ).fetchone()
    if (
        snap_row is None
        or snap_row["gc_state"] == "DELETED"
        or not (sdir / "snapshot.json").is_file()
    ):
        return Freshness.HISTORICAL
    key = (str(ctx.state_dir), str(build["build_id"]))
    rec = _RECORDS.get((key[0], sid))
    if rec is None:  # a snapshot directory is immutable: its record can be cached
        rec = load_record(sdir)
        _RECORDS[(key[0], sid)] = rec
    if strict:
        current = _inputs_current_full(ctx, build, rec)
    else:
        fp = input_fingerprint(ctx, build, rec)  # taken BEFORE the check, so a race re-checks
        hit = _VERDICTS.get(key)
        if hit is not None and hit[0] == fp:
            current = hit[1]
        else:
            current = _inputs_current_full(ctx, build, rec)
            _VERDICTS[key] = (fp, current)
    if current is None:
        return Freshness.UNKNOWN
    if current:
        return Freshness.CURRENT
    latest = db.conn.execute(
        "SELECT build_id FROM builds WHERE workspace_id=? AND profile=? "
        "ORDER BY created_at DESC, rowid DESC LIMIT 1",
        (build["workspace_id"], build["profile"]),
    ).fetchone()
    return (
        Freshness.STALE
        if latest and latest["build_id"] == build["build_id"]
        else Freshness.HISTORICAL
    )


def report_of(db: Database, build_id: str) -> dict[str, Any]:
    r = db.conn.execute("SELECT * FROM build_reports WHERE build_id=?", (build_id,)).fetchone()
    if r is None:
        return {
            "scope": None,
            "unresolved_count": 0,
            "limitations": [],
            "reasons": [],
            "dynamic_count": 0,
        }
    return {
        "scope": r["scope"],
        "unresolved_count": r["unresolved_count"],
        "dynamic_count": r["dynamic_count"],
        "limitations": loads(r["limitations"]) or [],
        "reasons": loads(r["reasons"]) or [],
        "tops": loads(r["tops"]) or [],
        "stats": loads(r["stats"]) or {},
        "job_id": r["job_id"],
    }


def coverage_of(
    db: Database, build: sqlite3.Row, extra_limits: list[str] | None = None
) -> AnalysisCoverage:
    rep = report_of(db, build["build_id"])
    limits = list(rep["limitations"]) + list(extra_limits or [])
    comp = Completeness(build["completeness"])
    if comp != Completeness.COMPLETE and rep["reasons"]:
        limits.append("Partial analysis: " + "; ".join(rep["reasons"]))
    return AnalysisCoverage(
        completeness=comp,
        scope=rep["scope"],
        basis=BASIS,
        unresolved_count=rep["unresolved_count"],
        limitations=limits,
    )


def provenance_of(build: sqlite3.Row) -> Provenance:
    return Provenance(
        backend="slang",
        toolchain_id=build["toolchain_id"],
        evidence_kind=EvidenceKind.HDL_STATIC_RESOLVED,
    )


def build_envelope(
    ctx: WorkspaceContext,
    db: Database,
    build: sqlite3.Row,
    request_id: str,
    data: dict[str, Any],
    *,
    pagination: Pagination | None = None,
    warnings: list[str] | None = None,
    extra_limits: list[str] | None = None,
    outcome: Outcome | None = None,
    error: ErrorInfo | None = None,
    freshness: Freshness | None = None,
) -> Envelope:
    comp = Completeness(build["completeness"])
    if outcome is None:
        outcome = {
            Completeness.COMPLETE: Outcome.OK,
            Completeness.PARTIAL: Outcome.PARTIAL,
            Completeness.BLOCKED: Outcome.BLOCKED,
        }[comp]
    warns = list(warnings or [])
    fr = freshness if freshness is not None else freshness_of(ctx, db, build)
    if fr != Freshness.CURRENT:
        warns.append(
            f"freshness is {fr.value}: the workspace changed since this build's snapshot; "
            "results describe the snapshot, not the current files"
        )
    if comp == Completeness.PARTIAL:
        warns.append("analysis is PARTIAL: absence from this result is not proof of absence")
    return Envelope(
        request_id=request_id,
        outcome=outcome,
        workspace_id=build["workspace_id"],
        snapshot_id=build["snapshot_id"],
        build_id=build["build_id"],
        profile=build["profile"],
        freshness=fr,
        analysis_coverage=coverage_of(db, build, extra_limits),
        provenance=provenance_of(build),
        data=data,
        pagination=pagination or Pagination(),
        warnings=warns,
        error=error,
    )
