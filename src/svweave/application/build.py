"""Build use case: manifest -> snapshot -> identity -> worker job -> staged, validated publish
(plan 8.4, 9.5, 9.6, 10.2, 24.1).

State dimensions stay separate: the *job* (QUEUED/RUNNING/SUCCEEDED/FAILED/CANCELLED/
TIMED_OUT) says whether the worker ran; *completeness* (COMPLETE/PARTIAL/BLOCKED) and the
diagnostic outcome describe the analysis. A job that succeeded on a design with errors is
SUCCEEDED + PARTIAL + HAS_ERRORS (SEM-07). Only COMPLETE builds move the published pointer.
"""

from __future__ import annotations

import json
import threading
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from svweave.adapters.slang.args import build_invocation
from svweave.adapters.slang.deps import check_dependencies
from svweave.adapters.slang.ingest import Ingested, ingest
from svweave.adapters.slang.protocol import ExtractResult, WorkerRequest
from svweave.adapters.slang.runner import WorkerHandle, WorkerOutcome
from svweave.application.options import WorkspaceOptions
from svweave.application.semantic_common import BASIS, freshness_of, require_build
from svweave.application.toolchain import compute_toolchain
from svweave.config.context import WorkspaceContext
from svweave.config.loader import policy_hash
from svweave.domain.envelope import AnalysisCoverage, Envelope, ErrorInfo, Outcome, Provenance
from svweave.domain.errors import ErrorCode, SvwError
from svweave.domain.ids import SEMANTIC_SCHEMA_VERSION, compute_build_id, new_job_id, sha256_hex
from svweave.domain.states import (
    Completeness,
    DiagnosticOutcome,
    EvidenceKind,
    Freshness,
    JobLifecycle,
)
from svweave.editing.plan import require_local_editing
from svweave.execution.ledger import ensure_reconciled
from svweave.execution.local import run_worker
from svweave.execution.slurm import ensure_available
from svweave.execution.slurm import run_worker as run_slurm_worker
from svweave.manifest.models import NormalizedManifest
from svweave.manifest.resolver import resolve_profile, write_manifest
from svweave.snapshot.capture import capture_snapshot, snapshots_dir
from svweave.snapshot.models import SnapshotRecord
from svweave.storage.db import Database
from svweave.storage.lock import WorkspaceBusy, build_lock, workspace_lock
from svweave.storage.semantic import dumps

DB_FILENAME = "svweave.db"

# ---------------------------------------------------------------------------- job registry
_REGISTRY: dict[str, WorkerHandle] = {}
_REGISTRY_LOCK = threading.Lock()


def cancel_job(job_id: str) -> bool:
    """Kill the worker process tree of a running job (API-03). False if it is not running."""
    with _REGISTRY_LOCK:
        h = _REGISTRY.get(job_id)
    if h is None:
        return False
    h.cancel()
    return True


# ---------------------------------------------------------------------------- run artifacts
#: every build leaves these files (plan 24.1); a stage that never ran leaves an explicit
#: ``not_available`` document instead of a missing file (M6)
ARTIFACTS = (
    "request.json",
    "normalized_manifest.json",
    "argument_ledger.json",
    "snapshot_inventory.json",
    "toolchain_manifest.json",
    "backend_capabilities.json",
    "diagnostics.json",
    "dependency_report.json",
    "unsupported_nodes.json",
    "semantic_summary.json",
    "resource_usage.json",
    "result.json",
)


class _Run:
    """`.svweave/runs/<job-id>/` artifacts (plan 24.1). No source text is ever written."""

    def __init__(
        self, ctx: WorkspaceContext, job_id: str, request_id: str, build_id: str | None = None
    ) -> None:
        self.dir = ctx.state_dir / "runs" / job_id
        self.dir.mkdir(parents=True, exist_ok=True)
        self.job_id = job_id
        self.request_id = request_id
        self.build_id: str | None = build_id
        for name in ARTIFACTS:
            self.write(
                name,
                {
                    "status": "not_available",
                    "reason": "the stage that produces this artifact did not run (yet)",
                },
            )
        self.event("build", "created")

    def write(self, name: str, obj: Any) -> None:
        (self.dir / name).write_text(
            json.dumps(obj, indent=2, sort_keys=True, ensure_ascii=False, default=str) + "\n",
            encoding="utf-8",
        )

    def event(
        self,
        component: str,
        event: str,
        *,
        outcome: str = "ok",
        duration_s: float | None = None,
        error_code: str | None = None,
    ) -> None:
        rec = {
            "ts": datetime.now(UTC).isoformat(timespec="milliseconds"),
            "request_id": self.request_id,
            "job_id": self.job_id,
            "build_id": self.build_id,
            "component": component,
            "event": event,
            "duration_s": duration_s,
            "outcome": outcome,
            "error_code": error_code,
        }
        with open(self.dir / "events.jsonl", "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, sort_keys=True) + "\n")


def _rel(ctx: WorkspaceContext, p: Path) -> str:
    try:
        return p.relative_to(ctx.project_root).as_posix()
    except ValueError:
        return str(p)


def require_execution_fingerprint(ctx: WorkspaceContext, expected: str | None) -> None:
    """A queued request must never silently migrate to a different execution context."""
    if expected is not None and ctx.execution.fingerprint != expected:
        raise SvwError(
            ErrorCode.POLICY_DENIED,
            "execution context changed after the build request; submit a new request",
            {"reason": "execution_context_changed", "submitted": False},
        )


# ---------------------------------------------------------------------------- entry point
def run_build(
    opts: WorkspaceOptions,
    profile: str | None,
    request_id: str,
    *,
    reuse: bool = True,
    handle: WorkerHandle | None = None,
    on_job: Callable[[str, WorkerHandle], None] | None = None,
    test_fault: str | None = None,
    job_id: str | None = None,
    expected_snapshot_id: str | None = None,
    expected_execution_fingerprint: str | None = None,
) -> Envelope:
    """Build (or reuse) the semantic result of a profile. At most ONE build runs per workspace
    (exclusive non-blocking build lock, shared by the CLI and the MCP job manager). With
    ``expected_snapshot_id`` the build stops with STALE_SNAPSHOT when the captured snapshot differs.
    ``test_fault`` is a TEST-ONLY hook that is never read from configuration."""
    ctx = opts.load()
    require_execution_fingerprint(ctx, expected_execution_fingerprint)
    request_execution = ctx.execution.fingerprint
    ensure_reconciled(ctx.state_dir)
    if ctx.execution.provider == "srun":
        ensure_available(ctx.execution)
    try:
        with build_lock(ctx.state_dir):
            return _run_build_locked(
                opts,
                profile,
                request_id,
                reuse,
                handle,
                on_job,
                test_fault,
                job_id,
                expected_snapshot_id,
                request_execution,
            )
    except WorkspaceBusy as e:
        raise SvwError(
            ErrorCode.RESOURCE_LIMIT,
            "another build is already running in this workspace; only one build runs at a time",
            {"reason": "build_in_progress"},
        ) from e


def _run_build_locked(
    opts: WorkspaceOptions,
    profile: str | None,
    request_id: str,
    reuse: bool,
    handle: WorkerHandle | None,
    on_job: Callable[[str, WorkerHandle], None] | None,
    test_fault: str | None,
    job_id: str | None,
    expected_snapshot_id: str | None,
    expected_execution_fingerprint: str,
) -> Envelope:
    ctx = opts.load()
    require_execution_fingerprint(ctx, expected_execution_fingerprint)
    ensure_reconciled(ctx.state_dir)
    if ctx.execution.provider == "srun":
        ensure_available(ctx.execution)
    name, _ = ctx.profile(profile)
    nm = resolve_profile(ctx, name)
    write_manifest(ctx, nm)
    with workspace_lock(ctx.state_dir):
        rec = capture_snapshot(ctx, nm)
        with Database(ctx.state_dir / DB_FILENAME) as db:
            db.upsert_workspace(
                ctx.workspace_id,
                {r.id: ("external" if not r.internal else "project") for r in ctx.roots},
                policy_hash(ctx.policy),
            )
            db.record_snapshot(rec)
            if expected_snapshot_id is not None and rec.snapshot_id != expected_snapshot_id:
                raise SvwError(
                    ErrorCode.STALE_SNAPSHOT,
                    "workspace no longer matches expected_snapshot_id; no build was run",
                    {
                        "expected_snapshot_id": expected_snapshot_id,
                        "current_snapshot_id": rec.snapshot_id,
                    },
                )
            return _build_snapshot(
                ctx,
                db,
                nm,
                rec,
                request_id,
                reuse,
                handle or WorkerHandle(),
                on_job,
                test_fault,
                job_id,
            )


def run_build_on_record(
    opts: WorkspaceOptions,
    profile: str | None,
    rec: SnapshotRecord,
    request_id: str,
    *,
    publish: bool = True,
    reuse: bool = True,
) -> Envelope:
    """Build an ALREADY captured snapshot record (M4 edit candidates: ``publish=False`` keeps the
    candidate build from moving the published pointer). The record's snapshot directory must exist
    under ``.svweave/snapshots``; the manifest is resolved against the current workspace."""
    ctx = opts.load()
    require_local_editing(ctx)
    ensure_reconciled(ctx.state_dir)
    try:
        with build_lock(ctx.state_dir):  # one build at a time, like run_build
            name, _ = ctx.profile(profile)
            nm = resolve_profile(ctx, name)
            write_manifest(ctx, nm)
            with workspace_lock(ctx.state_dir), Database(ctx.state_dir / DB_FILENAME) as db:
                db.upsert_workspace(
                    ctx.workspace_id,
                    {r.id: ("external" if not r.internal else "project") for r in ctx.roots},
                    policy_hash(ctx.policy),
                )
                db.record_snapshot(rec)
                return _build_snapshot(
                    ctx, db, nm, rec, request_id, reuse, WorkerHandle(), None, None, None, publish
                )
    except WorkspaceBusy as e:
        raise SvwError(
            ErrorCode.RESOURCE_LIMIT,
            "another build is already running in this workspace; only one build runs at a time",
            {"reason": "build_in_progress"},
        ) from e


def _build_snapshot(
    ctx: WorkspaceContext,
    db: Database,
    nm: NormalizedManifest,
    rec: SnapshotRecord,
    request_id: str,
    reuse: bool,
    handle: WorkerHandle,
    on_job: Callable[[str, WorkerHandle], None] | None,
    test_fault: str | None,
    pre_job_id: str | None = None,
    publish: bool = True,
) -> Envelope:
    if ctx.execution.provider == "srun":
        ensure_available(ctx.execution)  # before any cache lookup, including direct internal calls
    tc_id, tc_parts, lock_path = compute_toolchain(ctx.project_root)
    build_id = compute_build_id(
        rec.snapshot_id, nm.config_id, tc_id,
        execution_fingerprint=(ctx.execution.fingerprint if ctx.execution.provider != "local" else None),
    )
    existing = db.conn.execute("SELECT * FROM builds WHERE build_id=?", (build_id,)).fetchone()
    if existing is not None and reuse and existing["completeness"] == Completeness.COMPLETE.value:
        if publish:
            _publish_reused(ctx, db, existing, nm.profile)
        return _stored_envelope(
            ctx, db, build_id, request_id, reused=True, job_id=None, profile=nm.profile
        )

    job_id = pre_job_id or new_job_id()
    db.start_job(job_id, build_id, rec.snapshot_id)
    run = _Run(ctx, job_id, request_id, build_id)
    handle_registered = False
    try:
        with _REGISTRY_LOCK:
            _REGISTRY[job_id] = handle
        handle_registered = True
        if on_job is not None:
            on_job(job_id, handle)
        return _run_job(
            ctx,
            db,
            nm,
            rec,
            build_id,
            tc_id,
            tc_parts,
            lock_path,
            run,
            request_id,
            handle,
            test_fault,
            publish,
        )
    except BaseException as e:
        run.event("build", "aborted", outcome="error", error_code=type(e).__name__)
        run.write(
            "result.json",
            {
                "status": "aborted",
                "error": type(e).__name__,
                "code": getattr(getattr(e, "code", None), "value", None),
                "message": str(e)[:500],
            },
        )
        db.finish_job(job_id, JobLifecycle.FAILED, error=f"{type(e).__name__}: {e}"[:500])
        raise
    finally:
        if handle_registered:
            with _REGISTRY_LOCK:
                _REGISTRY.pop(job_id, None)


def _publish_reused(ctx: WorkspaceContext, db: Database, build: Any, profile: str) -> None:
    """A reused COMPLETE build must also move the published pointer when the caller asked to
    publish (B8) - but only after the full (strict) freshness check, so a build whose inputs no
    longer match the workspace is never published. The pointer is the REQUESTING profile's: a
    second profile with identical inputs reuses the first profile's build_id."""
    pub = db.published(build["workspace_id"], profile)
    if pub is not None and pub["build_id"] == build["build_id"]:
        return
    if freshness_of(ctx, db, build, strict=True) is Freshness.CURRENT:
        db.publish_build(build["build_id"], profile)


def _run_job(
    ctx: WorkspaceContext,
    db: Database,
    nm: NormalizedManifest,
    rec: SnapshotRecord,
    build_id: str,
    tc_id: str,
    tc_parts: dict[str, Any],
    lock_path: Path,
    run: _Run,
    request_id: str,
    handle: WorkerHandle,
    test_fault: str | None,
    publish: bool = True,
) -> Envelope:
    job_id = run.job_id
    files_root = (snapshots_dir(ctx) / rec.snapshot_id / "files").resolve()
    inv = build_invocation(nm, files_root, run.dir / "groups")
    analysis = ctx.manifest.analysis
    req = WorkerRequest(
        snapshot_root=str(files_root),
        args=inv.args,
        group_files=inv.group_files,
        tops=list(nm.top_modules),
    )
    run.write(
        "request.json",
        {
            "job_id": job_id,
            "request_id": request_id,
            "build_id": build_id,
            "worker_request": req.model_dump(mode="json", exclude={"test_fault"}),
            "timeout_s": analysis.max_build_seconds,
            "memory_mib": analysis.max_worker_memory_mib,
        },
    )
    run.write("normalized_manifest.json", nm.model_dump(mode="json"))
    run.write(
        "argument_ledger.json",
        {
            "ledger": [e.model_dump(mode="json") for e in nm.ledger],
            "slang_args": inv.args,
            "group_files": inv.ledger,
        },
    )
    run.write("snapshot_inventory.json", rec.model_dump(mode="json"))
    run.write(
        "toolchain_manifest.json",
        {"toolchain_id": tc_id, "parts": tc_parts, "lock_file": str(lock_path.name)},
    )
    run.write(
        "backend_capabilities.json",
        {
            "backend": "slang",
            "via": "pyslang",
            "pyslang_version": tc_parts["pyslang"]["version"],
            "adr": "ADR-003",
        },
    )
    run.event("worker", "start")
    worker = run_worker if ctx.execution.provider == "local" else run_slurm_worker
    execution_args: dict[str, Any] = {}
    if ctx.execution.provider == "srun":
        execution_args = {
            "execution": ctx.execution, "policy": ctx.policy.execution,
            "run_dir": run.dir, "job_id": job_id, "snapshot_id": rec.snapshot_id,
            "toolchain_id": tc_id,
        }
    outcome = worker(
        req,
        timeout_s=float(analysis.max_build_seconds),
        memory_mib=analysis.max_worker_memory_mib,
        handle=handle,
        test_fault=test_fault,
        **execution_args,
    )
    run.event(
        "worker",
        "finish",
        outcome=outcome.kind,
        duration_s=outcome.wall_s,
        error_code=outcome.error_code.value if outcome.error_code else None,
    )
    run.write("resource_usage.json", outcome.resource_usage)
    if not outcome.ok:
        return _failed_job(ctx, db, run, build_id, rec, outcome)

    try:
        raw = json.loads(outcome.stdout.decode("utf-8"))
        result = ExtractResult.model_validate(raw).model_dump()
    except (ValueError, ValidationError) as e:
        outcome.kind = "bad_output"
        outcome.error_code = ErrorCode.CAPABILITY_MISMATCH
        outcome.message = f"worker result does not match the extraction schema: {type(e).__name__}"
        return _failed_job(ctx, db, run, build_id, rec, outcome)

    adapter_files = [g.path for g in inv.group_files]
    dep = check_dependencies(result["dependencies"], rec, adapter_files)
    run.write("dependency_report.json", dep)
    run.write("unsupported_nodes.json", result["unsupported_nodes"])
    if dep["ok"]:
        run.write(
            "diagnostics.json",
            result["diagnostics"] + [{"driver_message": m} for m in result["driver_messages"]],
        )
    else:  # BLOCKED: diagnostics may quote text of a file outside the snapshot; keep none (A2)
        run.write("diagnostics.json", [{"withheld": "dependency leak: build BLOCKED"}])

    build_row = {
        "build_id": build_id,
        "workspace_id": ctx.workspace_id,
        "profile": nm.profile,
        "snapshot_id": rec.snapshot_id,
        "config_id": nm.config_id,
        "toolchain_id": tc_id,
        "extractor_id": result["extractor_version"],
        "semantic_schema_version": SEMANTIC_SCHEMA_VERSION,
    }
    source_map = {f.logical_path: {"bytes_hash": f.sha256, "size": f.size} for f in rec.files}
    had_complete = _is_complete(db, build_id)
    if not dep["ok"]:
        if had_complete:
            raise SvwError(
                ErrorCode.INTERNAL,
                "a COMPLETE build of identical inputs now reads files outside its snapshot",
                {"build_id": build_id, "violations": dep["violations"]},
            )
        return _blocked(ctx, db, run, build_row, rec, result, dep, outcome, nm, source_map)

    gaps_extra = [
        {
            "kind": "library_member_not_loaded",
            "name": rel,
            "detail": "library member present in the "
            "search path but never read by slang (not instantiated); its contents are not analysed",
        }
        for rel in dep["library_members_not_loaded"]
    ]
    ing = ingest(
        result, rec, build_id, job_id, list(nm.top_modules), stubs=nm.stubs,
        extra_gaps=gaps_extra,
    )
    if had_complete:
        return _verified_rerun(ctx, db, run, request_id, build_id, ing, outcome)
    if gaps_extra:
        ing.limitations.append(
            f"{len(gaps_extra)} library member(s) were never loaded; their references are not covered."
        )
    row = {
        **build_row,
        "completeness": ing.completeness.value,
        "diagnostic_outcome": ing.diagnostic_outcome.value,
    }
    rows = dict(ing.rows)
    rows["build_reports"] = [_report_row(build_id, job_id, result, ing, dep, source_map, nm)]
    # a re-run of an identical (non-COMPLETE) build replaces the old rows; the pointer never
    # referenced it because only COMPLETE builds are published
    _purge_build(db, build_id)
    published = db.stage_build(row, rows, validate=_validate_counts(ing), publish=publish)
    db.finish_job(job_id, JobLifecycle.SUCCEEDED, exit_code=outcome.exit_code or 0)
    run.event("build", "published" if published else "stored", outcome=ing.completeness.value)
    run.write(
        "semantic_summary.json",
        {
            **ing.summary,
            "completeness": ing.completeness.value,
            "diagnostic_outcome": ing.diagnostic_outcome.value,
            "reasons": ing.reasons,
            "published": published,
        },
    )
    env = _stored_envelope(
        ctx,
        db,
        build_id,
        request_id,
        reused=False,
        job_id=job_id,
        published=published,
        resource=outcome.resource_usage,
        run=run,
    )
    _surface_isolation(env, outcome)
    run.write("result.json", env.model_dump(mode="json"))
    return env


def _surface_isolation(env: Envelope, outcome: WorkerOutcome) -> Envelope:
    """Degraded worker isolation must reach the caller (never silently dropped)."""
    env.data["worker_isolation"] = outcome.isolation
    if outcome.isolation != "bwrap" or outcome.isolation_warnings:
        env.warnings.extend(
            f"worker isolation: {w}"
            for w in (outcome.isolation_warnings or (f"mode is {outcome.isolation}",))
        )
    return env


def _is_complete(db: Database, build_id: str) -> bool:
    r = db.conn.execute("SELECT completeness FROM builds WHERE build_id=?", (build_id,)).fetchone()
    return bool(r and r["completeness"] == Completeness.COMPLETE.value)


_VOLATILE_COLS = frozenset({"build_id", "job_id"})


def _norm_cell(v: Any) -> Any:
    return int(v) if isinstance(v, bool) else v


def _table_digest(rows: list[dict[str, Any]]) -> str:
    """Order-independent sha256 over normalized rows (volatile ids dropped, bool -> int)."""
    canon = sorted(
        json.dumps(
            {k: _norm_cell(v) for k, v in r.items() if k not in _VOLATILE_COLS},
            sort_keys=True,
            ensure_ascii=False,
            default=str,
        )
        for r in rows
    )
    return sha256_hex("\n".join(canon))


def _stored_digests(db: Database, build_id: str, ing: Ingested) -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    for table, fresh in ing.rows.items():
        if table == "build_reports":
            continue
        cols = [c for c in (fresh[0] if fresh else {}) if c not in _VOLATILE_COLS]
        if cols:
            sel = ",".join(f'"{c}"' for c in cols)
            stored = [
                dict(r)
                for r in db.conn.execute(
                    f'SELECT {sel} FROM "{table}" WHERE build_id=?', (build_id,)
                ).fetchall()
            ]
        else:
            n = db.conn.execute(
                f'SELECT COUNT(*) FROM "{table}" WHERE build_id=?', (build_id,)
            ).fetchone()[0]
            stored = [{} for _ in range(n)]
        out[table] = {"rows": len(stored), "sha256": _table_digest(stored)}
    return out


def _verified_rerun(
    ctx: WorkspaceContext,
    db: Database,
    run: _Run,
    request_id: str,
    build_id: str,
    ing: Ingested,
    outcome: WorkerOutcome,
) -> Envelope:
    """`--no-reuse` on an existing COMPLETE build: the build is immutable, so the fresh run is
    only compared with it (determinism check); nothing is overwritten or re-published. The
    comparison covers the canonical normalized rows of EVERY semantic table (declarations,
    references, instances, ports, assertions, diagnostics, gaps, ...), the completeness and the
    diagnostic outcome - not just their counts."""
    stored = _stored_digests(db, build_id, ing)
    fresh = {
        t: {"rows": len(r), "sha256": _table_digest(r)}
        for t, r in ing.rows.items()
        if t != "build_reports"
    }
    diff: dict[str, Any] = {
        t: {"stored": stored.get(t), "fresh": fresh[t]} for t in fresh if stored.get(t) != fresh[t]
    }
    b = require_build(db, build_id)
    if b["completeness"] != ing.completeness.value:
        diff["completeness"] = {"stored": b["completeness"], "fresh": ing.completeness.value}
    if b["diagnostic_outcome"] != ing.diagnostic_outcome.value:
        diff["diagnostic_outcome"] = {
            "stored": b["diagnostic_outcome"],
            "fresh": ing.diagnostic_outcome.value,
        }
    if diff:
        run.event("build", "verified_rerun_mismatch", outcome="error")
        raise SvwError(
            ErrorCode.INTERNAL,
            "rebuild of identical inputs gave a different result (non-deterministic extraction); "
            "the stored immutable build was NOT replaced",
            {"build_id": build_id, "differences": diff, "stored_build_replaced": False},
        )
    digest = sha256_hex(json.dumps(fresh, sort_keys=True))
    db.finish_job(run.job_id, JobLifecycle.SUCCEEDED, exit_code=outcome.exit_code or 0)
    run.event("build", "verified_rerun", outcome="COMPLETE")
    env = _stored_envelope(
        ctx,
        db,
        build_id,
        request_id,
        reused=False,
        job_id=run.job_id,
        resource=outcome.resource_usage,
        run=run,
    )
    env.data["verified_rerun"] = True
    env.data["semantic_result_sha256"] = digest
    _surface_isolation(env, outcome)
    run.write("result.json", env.model_dump(mode="json"))
    return env


def _validate_counts(ing: Ingested) -> Callable[[Any, str], None]:
    """Staging validation: what was inserted must equal what was ingested (no half builds)."""

    def check(conn: Any, build_id: str) -> None:
        for table in ("declarations", "references", "instances", "port_bindings", "assertions"):
            want = len(ing.rows[table])
            got = conn.execute(
                f'SELECT COUNT(*) FROM "{table}" WHERE build_id=?', (build_id,)
            ).fetchone()[0]
            if got != want:
                raise RuntimeError(f"staging validation failed for {table}: {got} != {want}")

    return check


def _purge_build(db: Database, build_id: str) -> None:
    with db.transaction() as c:
        row = c.execute("SELECT completeness FROM builds WHERE build_id=?", (build_id,)).fetchone()
        if row is None:
            return
        if row["completeness"] == Completeness.COMPLETE.value:
            return  # never destroy a COMPLETE build
        for t in (
            "declarations",
            '"references"',
            "instances",
            "port_bindings",
            "assertions",
            "diagnostics",
            "specializations",
            "inactive_regions",
            "coverage_gaps",
            "unsupported_nodes",
            "build_reports",
        ):
            c.execute(f"DELETE FROM {t} WHERE build_id=?", (build_id,))
        c.execute("DELETE FROM builds WHERE build_id=?", (build_id,))


def _report_row(
    build_id: str,
    job_id: str,
    result: dict[str, Any],
    ing: Ingested,
    dep: dict[str, Any],
    source_map: dict[str, Any],
    nm: NormalizedManifest,
) -> dict[str, Any]:
    return {
        "build_id": build_id,
        "job_id": job_id,
        "extractor_version": result["extractor_version"],
        "pyslang_version": result["pyslang_version"],
        "scope": ing.scope,
        "tops": dumps(list(nm.top_modules)),
        "unresolved_count": ing.unresolved_count,
        "dynamic_count": ing.dynamic_count,
        "reasons": dumps(ing.reasons),
        "limitations": dumps(ing.limitations),
        "stats": dumps(
            {**result["stats"], **ing.summary, "semantic_deviations": list(nm.semantic_deviations)}
        ),
        "dependency_report": dumps(dep),
        "source_map": dumps(source_map),
    }


def _blocked(
    ctx: WorkspaceContext,
    db: Database,
    run: _Run,
    build_row: dict[str, Any],
    rec: SnapshotRecord,
    result: dict[str, Any],
    dep: dict[str, Any],
    outcome: WorkerOutcome,
    nm: NormalizedManifest,
    source_map: dict[str, Any],
) -> Envelope:
    """Dependency leak: store the verdict, withhold every semantic row, never publish."""
    build_id, job_id = build_row["build_id"], run.job_id
    row = {
        **build_row,
        "completeness": Completeness.BLOCKED.value,
        "diagnostic_outcome": DiagnosticOutcome.NOT_AVAILABLE.value,
    }
    reasons = [f"dependency leak: {v['reason']}: {v['path']}" for v in dep["violations"]]
    rows = {
        "build_reports": [
            {
                "build_id": build_id,
                "job_id": job_id,
                "extractor_version": result["extractor_version"],
                "pyslang_version": result["pyslang_version"],
                "scope": ",".join(nm.top_modules),
                "tops": dumps(list(nm.top_modules)),
                "unresolved_count": 0,
                "dynamic_count": 0,
                "reasons": dumps(reasons),
                "limitations": dumps(["Results withheld: dependency leak."]),
                "stats": dumps(
                    {**result["stats"], "semantic_deviations": list(nm.semantic_deviations)}
                ),
                "dependency_report": dumps(dep),
                "source_map": dumps(source_map),
            }
        ]
    }
    _purge_build(db, build_id)
    db.stage_build(row, rows)
    db.finish_job(job_id, JobLifecycle.SUCCEEDED, exit_code=0)
    run.event("build", "blocked", outcome="BLOCKED", error_code=ErrorCode.PATH_NOT_ALLOWED.value)
    env = Envelope(
        request_id=run.request_id,
        outcome=Outcome.BLOCKED,
        workspace_id=ctx.workspace_id,
        snapshot_id=rec.snapshot_id,
        build_id=build_id,
        profile=nm.profile,
        freshness=Freshness.CURRENT,
        analysis_coverage=AnalysisCoverage(
            completeness=Completeness.BLOCKED, scope=None, basis=BASIS, limitations=reasons
        ),
        provenance=Provenance(
            backend="slang",
            toolchain_id=build_row["toolchain_id"],
            evidence_kind=EvidenceKind.UNRESOLVED,
        ),
        data={
            "job_id": job_id,
            "lifecycle": JobLifecycle.SUCCEEDED.value,
            "violations": dep["violations"],
            "published": False,
            "run_dir": _rel(ctx, run.dir),
        },
        error=ErrorInfo(
            code=ErrorCode.PATH_NOT_ALLOWED.value,
            message="the build read files outside the snapshot inventory; "
            "analysis is BLOCKED and was not published",
            details={"violations": dep["violations"]},
        ),
    )
    _surface_isolation(env, outcome)
    run.write("result.json", env.model_dump(mode="json"))
    return env


def _failed_job(
    ctx: WorkspaceContext,
    db: Database,
    run: _Run,
    build_id: str,
    rec: SnapshotRecord,
    outcome: WorkerOutcome,
) -> Envelope:
    lifecycle = {
        "timeout": JobLifecycle.TIMED_OUT,
        "cancelled": JobLifecycle.CANCELLED,
    }.get(outcome.kind, JobLifecycle.FAILED)
    code = outcome.error_code or ErrorCode.BACKEND_UNAVAILABLE
    detail = {
        "exit_code": outcome.exit_code,
        "signal": outcome.signal,
        "kind": outcome.kind,
        "wall_s": outcome.wall_s,
        "peak_rss_kib": outcome.peak_rss_kib,
    }
    exit_val = (
        outcome.exit_code
        if outcome.exit_code is not None
        else (-outcome.signal if outcome.signal is not None else None)
    )
    db.finish_job(
        run.job_id, lifecycle, exit_code=exit_val, error=f"{code.value}: {outcome.message}"[:500]
    )
    run.event("build", lifecycle.value, outcome=outcome.kind, error_code=code.value)
    env = Envelope(
        request_id=run.request_id,
        outcome=Outcome.ERROR,
        workspace_id=ctx.workspace_id,
        snapshot_id=rec.snapshot_id,
        build_id=build_id,
        profile=rec.profile,
        freshness=Freshness.CURRENT,
        analysis_coverage=AnalysisCoverage(
            completeness=Completeness.BLOCKED,
            basis=BASIS,
            limitations=["No result: " + outcome.message],
        ),
        data={
            "job_id": run.job_id,
            "lifecycle": lifecycle.value,
            "published": False,
            "run_dir": _rel(ctx, run.dir),
            **detail,
            "stderr_tail": outcome.stderr_tail[-2000:],
        },
        error=ErrorInfo(code=code.value, message=outcome.message, details=detail),
    )
    _surface_isolation(env, outcome)
    run.write("result.json", env.model_dump(mode="json"))
    return env


def _stored_envelope(
    ctx: WorkspaceContext,
    db: Database,
    build_id: str,
    request_id: str,
    *,
    reused: bool,
    job_id: str | None,
    published: bool | None = None,
    resource: dict[str, Any] | None = None,
    run: _Run | None = None,
    profile: str | None = None,
) -> Envelope:
    from svweave.application.semantic_common import build_envelope, report_of

    b = require_build(db, build_id)
    rep = report_of(db, build_id)
    prof = profile or b["profile"]
    if published is None:
        pub = db.published(b["workspace_id"], prof)
        published = bool(pub and pub["build_id"] == build_id)
    stats = rep.get("stats") or {}
    data: dict[str, Any] = {
        "job_id": job_id,
        "reused": reused,
        "published": published,
        "config_id": b["config_id"],
        "toolchain_id": b["toolchain_id"],
        "completeness": b["completeness"],
        "diagnostic_outcome": b["diagnostic_outcome"],
        "job_lifecycle": JobLifecycle.SUCCEEDED.value if job_id else None,
        "reasons": rep["reasons"],
        "semantic_deviations": list(stats.get("semantic_deviations") or []),
        "counts": {
            k: stats.get(k)
            for k in (
                "declarations",
                "references",
                "instances",
                "port_bindings",
                "assertions",
                "diagnostics",
                "errors",
                "warnings",
                "specializations",
                "unresolved",
                "dynamic_unresolved",
            )
            if k in stats
        },
        "resource_usage": resource,
        "run_dir": _rel(ctx, run.dir) if run else None,
    }
    partial = b["completeness"] != Completeness.COMPLETE.value
    err = None
    if partial:
        err = ErrorInfo(
            code=ErrorCode.PARTIAL_ANALYSIS.value,
            message="build finished but the analysis is not COMPLETE: " + "; ".join(rep["reasons"]),
            details={"reasons": rep["reasons"], "diagnostic_outcome": b["diagnostic_outcome"]},
        )
    env = build_envelope(ctx, db, b, request_id, data, error=err)
    env.profile = prof  # the profile that asked, not the first profile that built these inputs
    return env


def build_freshness(ctx: WorkspaceContext, db: Database, build_id: str) -> Freshness:
    return freshness_of(ctx, db, require_build(db, build_id))
