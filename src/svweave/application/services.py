"""One function per externally visible use case (plan 6.1, SVW-024).

The MCP tools (``gateway.tools``) and the CLI commands both call exactly these functions, so the
two surfaces cannot drift. Signature: ``fn(session, request_id, **arguments) -> Envelope``.
"""

from __future__ import annotations

import re
from typing import Any

from svweave.application import edit as edit_mod
from svweave.application import evidence as evidence_mod
from svweave.application import graph, queries
from svweave.application import impact as impact_mod
from svweave.application import status as status_mod
from svweave.application.search import svw_search
from svweave.application.session import Session
from svweave.domain.envelope import AnalysisCoverage, Envelope, ErrorInfo, Outcome
from svweave.domain.errors import ErrorCode, SvwError
from svweave.domain.ids import is_sha256_hex
from svweave.domain.states import Completeness, Freshness
from svweave.snapshot.capture import check_freshness, load_record, snapshots_dir
from svweave.storage.db import Database

JOB_LIMITATIONS = [
    "A finished job says the worker ran, not that the design is correct; read analysis_coverage "
    "and diagnostics of the resulting build.",
]


_CTRL = re.compile(r"[\x00-\x1f\x7f]")


def clean_text(**fields: str | None) -> None:
    """Shared by CLI and MCP: no control characters (newline included) in a text argument."""
    for name, value in fields.items():
        if value is not None and _CTRL.search(value):
            raise SvwError(
                ErrorCode.INVALID_CONFIG,
                f"{name} must not contain control characters",
                {"field": name},
            )


def clean_relpath(**fields: str | None) -> None:
    """Like :func:`clean_text`, and the value may not be absolute or contain `..` segments."""
    clean_text(**fields)
    for name, value in fields.items():
        if value is not None and (value.startswith("/") or ".." in value.split("/")):
            raise SvwError(
                ErrorCode.INVALID_CONFIG,
                f"{name} must be a relative path without '..'",
                {"field": name},
            )


def status(
    session: Session, request_id: str, *, profile: str | None = None, probe: bool = False
) -> Envelope:
    return status_mod.status(session, request_id, profile=profile, probe=probe)


_ACTIVE_LIFE = frozenset({"QUEUED", "RUNNING"})


def _job_error(data: dict[str, Any], res: dict[str, Any] | None) -> ErrorInfo | None:
    if res and isinstance(res.get("error"), dict):
        return ErrorInfo.model_validate(res["error"])
    life = data.get("lifecycle")
    if life == "TIMED_OUT":
        return ErrorInfo(code=ErrorCode.BUILD_TIMEOUT.value, message="the build job timed out")
    if life == "FAILED":
        text = str(data.get("error") or "the build job failed")
        code, _, msg = text.partition(": ")
        known = {c.value for c in ErrorCode}
        if code in known:
            return ErrorInfo(code=code, message=msg or text)
        return ErrorInfo(code=ErrorCode.INTERNAL.value, message=text)
    return None


def _job_envelope(
    session: Session, rid: str, data: dict[str, Any], warnings: list[str] | None = None
) -> Envelope:
    """Job control envelope. The top level mirrors the TRUE state: an active job has no analysis
    result yet (no coverage, outcome OK = the control call worked); a finished job carries the
    build's outcome, completeness and error; FAILED/TIMED_OUT are ERROR; CANCELLED is BLOCKED."""
    ctx = session.opts.load()
    warns = list(warnings or [])
    life = str(data.get("lifecycle"))
    res = data.get("result") if isinstance(data.get("result"), dict) else None
    limits = list(JOB_LIMITATIONS)
    coverage: AnalysisCoverage | None
    error = _job_error(data, res)
    freshness = Freshness.UNKNOWN
    if life in _ACTIVE_LIFE:
        outcome, coverage = Outcome.OK, None
        warns.append(f"job is {life}: no analysis result yet")
    elif life == "CANCELLED":
        outcome = Outcome.BLOCKED
        coverage = AnalysisCoverage(
            completeness=Completeness.BLOCKED, scope="job_control", basis="job_table", limitations=limits
        )
        warns.append("job was CANCELLED: no analysis result was produced")
    elif life in ("FAILED", "TIMED_OUT") or (res is not None and res.get("outcome") == "ERROR"):
        outcome = Outcome.ERROR
        coverage = AnalysisCoverage(
            completeness=Completeness.BLOCKED, scope="job_control", basis="job_table", limitations=limits
        )
    elif res is not None:
        outcome = Outcome(str(res.get("outcome") or "OK"))
        comp = Completeness(str(res.get("completeness") or "COMPLETE"))
        if comp is not Completeness.COMPLETE:
            limits.append(f"the build is {comp.value}: see error.details.reasons / svw_report")
        coverage = AnalysisCoverage(
            completeness=comp, scope="job_control", basis="job_table", limitations=limits
        )
        if res.get("freshness"):
            freshness = Freshness(str(res["freshness"]))
    else:  # SUCCEEDED but the result summary is not stored yet
        outcome, coverage = Outcome.OK, None
        warns.append("job finished but its result summary is not available yet")
    return Envelope(
        request_id=rid,
        outcome=outcome,
        workspace_id=ctx.workspace_id,
        snapshot_id=data.get("snapshot_id"),
        build_id=data.get("build_id"),
        profile=data.get("profile"),
        freshness=freshness,
        analysis_coverage=coverage,
        data=data,
        warnings=warns,
        error=error,
    )


def build(
    session: Session,
    request_id: str,
    *,
    profile: str,
    reuse: bool = True,
    expected_snapshot_id: str | None = None,
) -> Envelope:
    data = session.jobs.submit_build(
        profile, request_id, reuse=reuse, expected_snapshot_id=expected_snapshot_id
    )
    warns = []
    if data.get("already_running"):
        warns.append("a build of this profile is already running; returning that job")
    return _job_envelope(session, request_id, data, warns)


def job(session: Session, request_id: str, *, job_id: str, action: str = "status") -> Envelope:
    if action == "cancel":
        data = session.jobs.cancel(job_id)
    elif action == "status":
        data = session.jobs.status(job_id)
    else:
        raise SvwError(ErrorCode.INVALID_CONFIG, "action must be status or cancel")
    return _job_envelope(session, request_id, data)


def search(
    session: Session,
    request_id: str,
    *,
    snapshot_id: str,
    query: str,
    scope: str | None = None,
    limit: int | None = None,
    cursor: str | None = None,
) -> Envelope:
    clean_text(query=query)
    clean_relpath(scope=scope)
    if not is_sha256_hex(snapshot_id):
        raise SvwError(
            ErrorCode.INVALID_CONFIG,
            "snapshot_id must be 64 lowercase hex characters",
            {"field": "snapshot_id"},
        )
    ctx = session.opts.load()
    sdir = snapshots_dir(ctx) / snapshot_id
    with Database(ctx.state_dir / "svweave.db") as db:
        row = db.conn.execute(
            "SELECT gc_state FROM snapshots WHERE snapshot_id=?", (snapshot_id,)
        ).fetchone()
    if row is None or row["gc_state"] != "PRESENT" or not (sdir / "snapshot.json").is_file():
        raise SvwError(
            ErrorCode.MISSING_INPUT,
            f"snapshot {snapshot_id} not found",
            {"reason": "snapshot_missing", "snapshot_id": snapshot_id},
        )
    rec = load_record(sdir)
    env = svw_search(
        snapshot_id=snapshot_id,
        snapshot_root=sdir / "files",
        query=query,
        scope=scope,
        limit=limit if limit is not None else 100,
        cursor=cursor,
        registry=session.registry if session.use_graph else None,
        request_id=request_id,
        workspace_id=ctx.workspace_id,
        profile=rec.profile,
        cursor_key=session.cursor_key(),
    )
    try:
        env.freshness = (
            Freshness.CURRENT
            if check_freshness(ctx, rec).freshness == "CURRENT"
            else Freshness.STALE
        )
    except SvwError:
        env.freshness = Freshness.UNKNOWN
    if env.freshness is Freshness.STALE:
        env.warnings.append(
            "the workspace changed since this snapshot; results describe the snapshot"
        )
    return env


def symbol(
    session: Session,
    request_id: str,
    *,
    build_id: str,
    symbol_id: str | None = None,
    file: str | None = None,
    byte: int | None = None,
    name: str | None = None,
    limit: int | None = None,
    cursor: str | None = None,
) -> Envelope:
    clean_relpath(file=file)
    clean_text(name=name)
    return queries.symbol(
        session.opts,
        build_id,
        request_id,
        symbol_id=symbol_id,
        file=file,
        byte=byte,
        name=name,
        limit=limit,
        cursor=cursor,
    )


def references(
    session: Session,
    request_id: str,
    *,
    build_id: str,
    symbol_id: str,
    scope: str | None = None,
    limit: int | None = None,
    cursor: str | None = None,
) -> Envelope:
    clean_relpath(scope=scope)
    return queries.references(
        session.opts, build_id, symbol_id, request_id, scope=scope, limit=limit, cursor=cursor
    )


def impact(
    session: Session,
    request_id: str,
    *,
    build_id: str,
    symbol_id: str | None = None,
    file: str | None = None,
    byte: int | None = None,
    name: str | None = None,
    instance: str | None = None,
    depth: int = 3,
    limit: int | None = None,
) -> Envelope:
    clean_text(symbol_id=symbol_id, file=file, name=name, instance=instance)
    return impact_mod.impact(
        session.opts,
        build_id,
        request_id,
        symbol_id=symbol_id,
        file=file,
        byte=byte,
        name=name,
        instance=instance,
        depth=depth,
        limit=limit,
    )


def graph_export(
    session: Session,
    request_id: str,
    *,
    build_id: str,
    name: str | None = None,
    max_instances: int = graph.DEFAULT_MAX_INSTANCES,
    max_fanout: int = graph.DEFAULT_MAX_FANOUT,
) -> Envelope:
    clean_text(name=name)
    return graph.export_graph(
        session.opts,
        build_id,
        request_id,
        name=name,
        max_instances=max_instances,
        max_fanout=max_fanout,
    )


def hierarchy(
    session: Session,
    request_id: str,
    *,
    build_id: str,
    instance: str | None = None,
    top: str | None = None,
    depth: int | None = None,
    limit: int | None = None,
    cursor: str | None = None,
) -> Envelope:
    clean_text(instance=instance, top=top)
    return queries.hierarchy(
        session.opts,
        build_id,
        request_id,
        instance=instance,
        top=top,
        depth=depth,
        limit=limit,
        cursor=cursor,
    )


def connections(
    session: Session,
    request_id: str,
    *,
    build_id: str,
    instance: str,
    port: str | None = None,
    limit: int | None = None,
    cursor: str | None = None,
) -> Envelope:
    clean_text(instance=instance, port=port)
    return queries.connections(
        session.opts, build_id, instance, request_id, port=port, limit=limit, cursor=cursor
    )


def assertions(
    session: Session,
    request_id: str,
    *,
    build_id: str,
    scope: str | None = None,
    entry_type: str | None = None,
    limit: int | None = None,
    cursor: str | None = None,
) -> Envelope:
    clean_text(scope=scope)
    return queries.assertions(
        session.opts,
        build_id,
        request_id,
        scope=scope,
        entry_type=entry_type,
        limit=limit,
        cursor=cursor,
    )


def diagnostics(
    session: Session,
    request_id: str,
    *,
    build_id: str,
    limit: int | None = None,
    cursor: str | None = None,
) -> Envelope:
    return queries.diagnostics(session.opts, build_id, request_id, limit=limit, cursor=cursor)


def evidence(
    session: Session,
    request_id: str,
    *,
    build_id: str | None = None,
    evidence_id: str | None = None,
    evidence_type: str | None = None,
    limit: int | None = None,
    cursor: str | None = None,
) -> Envelope:
    return evidence_mod.get_evidence(
        session.opts,
        request_id,
        build_id=build_id,
        evidence_id=evidence_id,
        evidence_type=evidence_type,
        limit=limit,
        cursor=cursor,
    )


def edit(session: Session, request_id: str, *, action: str, **args: Any) -> Envelope:
    """``svw_edit``: prepare / validate / commit / abort. There is deliberately no approve action
    and no approval argument: approval is created only by the human CLI (plan 12.4)."""
    opts = session.opts
    if action == "prepare":
        return edit_mod.prepare(opts, request_id, args, registry=session.registry)
    if action == "validate":
        return edit_mod.validate(opts, request_id, transaction_id=args["transaction_id"])
    if action == "commit":
        return edit_mod.commit(
            opts,
            request_id,
            transaction_id=args["transaction_id"],
            expected_patch_hash=args["expected_patch_hash"],
            request_key=args["request_key"],
        )
    if action == "abort":
        return edit_mod.abort(opts, request_id, transaction_id=args["transaction_id"])
    raise SvwError(ErrorCode.INVALID_CONFIG, "unknown svw_edit action", {"action": action})
