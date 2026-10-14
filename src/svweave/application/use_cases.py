"""Use cases: manifest resolve, snapshot create/check, cache gc. Return envelopes."""

from __future__ import annotations

from svweave.application.options import WorkspaceOptions
from svweave.config.loader import policy_hash
from svweave.domain.envelope import AnalysisCoverage, Envelope, Outcome
from svweave.domain.errors import ErrorCode, SvwError
from svweave.domain.ids import is_sha256_hex
from svweave.domain.states import Completeness, Freshness
from svweave.manifest.resolver import resolve_profile, write_manifest
from svweave.snapshot.capture import capture_snapshot, check_freshness, load_record, snapshots_dir
from svweave.storage.db import Database, run_gc
from svweave.storage.lock import workspace_lock

DB_FILENAME = "svweave.db"


def _coverage(scope: str, basis: str) -> AnalysisCoverage:
    return AnalysisCoverage(
        completeness=Completeness.COMPLETE,
        scope=scope,
        basis=basis,
        unresolved_count=0,
        limitations=["No semantic analysis was performed; this is input resolution only."],
    )


def resolve_manifest(opts: WorkspaceOptions, profile: str | None, request_id: str) -> Envelope:
    ctx = opts.load()
    name, _ = ctx.profile(profile)
    nm = resolve_profile(ctx, name)
    path = write_manifest(ctx, nm)
    return Envelope(
        request_id=request_id,
        outcome=Outcome.OK,
        workspace_id=ctx.workspace_id,
        profile=name,
        freshness=Freshness.UNKNOWN,
        analysis_coverage=_coverage(name, "manifest_resolution"),
        data={
            "config_id": nm.config_id,
            "manifest_path": str(path.relative_to(ctx.project_root)),
            "cu_mode": nm.cu_mode,
            "units": [{"id": u.id, "sources": len(u.sources)} for u in nm.units],
            "sources": len(nm.all_source_paths()),
            "filelists": nm.filelists,
            "ignored_options": [i.model_dump(mode="json") for i in nm.ignored_options],
            "generated_inputs": [g.model_dump(mode="json") for g in nm.generated_inputs],
        },
        warnings=[
            f"generated input {g.path} has incomplete provenance"
            for g in nm.generated_inputs
            if g.provenance_incomplete
        ],
    )


def create_snapshot(opts: WorkspaceOptions, profile: str | None, request_id: str) -> Envelope:
    ctx = opts.load()
    name, _ = ctx.profile(profile)
    nm = resolve_profile(ctx, name)
    write_manifest(ctx, nm)
    with workspace_lock(ctx.state_dir):  # shared: GC must not run between capture and record
        rec = capture_snapshot(ctx, nm)
        with Database(ctx.state_dir / DB_FILENAME) as db:
            db.upsert_workspace(
                ctx.workspace_id,
                {r.id: ("external" if not r.internal else "project") for r in ctx.roots},
                policy_hash(ctx.policy),
            )
            db.record_snapshot(rec)
    return Envelope(
        request_id=request_id,
        outcome=Outcome.OK,
        workspace_id=ctx.workspace_id,
        snapshot_id=rec.snapshot_id,
        profile=name,
        freshness=Freshness.CURRENT,
        analysis_coverage=_coverage(name, "snapshot_capture"),
        data={
            "config_id": nm.config_id,
            "files": len(rec.files),
            "lookup_dirs": [
                {"dir": lk.logical_dir, "kind": lk.kind, "entries": len(lk.entries)}
                for lk in rec.lookup
            ],
            "attempts": rec.attempts,
            "snapshot_dir": str(
                (snapshots_dir(ctx) / rec.snapshot_id).relative_to(ctx.project_root)
            ),
        },
    )


def check_snapshot(opts: WorkspaceOptions, snapshot_id: str, request_id: str) -> Envelope:
    ctx = opts.load()
    if not is_sha256_hex(snapshot_id):
        raise SvwError(
            ErrorCode.INVALID_CONFIG,
            "snapshot id must be 64 lowercase hex chars",
            {"snapshot_id": snapshot_id},
        )
    sdir = snapshots_dir(ctx) / snapshot_id
    if not (sdir / "snapshot.json").is_file():
        raise SvwError(
            ErrorCode.MISSING_INPUT,
            f"snapshot {snapshot_id} not found",
            {"reason": "snapshot_missing", "snapshot_id": snapshot_id},
        )
    rec = load_record(sdir)
    res = check_freshness(ctx, rec)
    stale = res.freshness == "STALE"
    env = Envelope(
        request_id=request_id,
        outcome=Outcome.OK,
        workspace_id=ctx.workspace_id,
        snapshot_id=snapshot_id,
        profile=rec.profile,
        freshness=Freshness.STALE if stale else Freshness.CURRENT,
        analysis_coverage=_coverage(rec.profile, "snapshot_freshness"),
        data={
            "current_snapshot_id": res.current_snapshot_id,
            "changed": [c.model_dump() for c in res.changed],
        },
    )
    return env


def cache_gc(opts: WorkspaceOptions, dry_run: bool, request_id: str) -> Envelope:
    ctx = opts.load()
    with Database(ctx.state_dir / DB_FILENAME) as db:
        plan = run_gc(db, snapshots_dir(ctx), dry_run=dry_run)
    return Envelope(
        request_id=request_id,
        outcome=Outcome.OK,
        workspace_id=ctx.workspace_id,
        freshness=Freshness.UNKNOWN,
        analysis_coverage=_coverage("cache", "gc_plan"),
        data=plan.to_dict(),
    )
