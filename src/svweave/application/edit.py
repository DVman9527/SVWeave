"""Controlled edit loop use cases (plan 12, 14.4, 15.1; milestone M4).

prepare -> validate -> approve (human CLI only) -> commit -> post-apply verification; abort and
recover. The MCP tool ``svw_edit`` and the CLI both call exactly these functions. Rules that are
enforced here and nowhere else may be bypassed:

* the ORIGINAL workspace is written only by :func:`commit`, only after a valid trusted approval,
  under the exclusive workspace lock, file by file with compare-and-set on the before-hash and a
  write-ahead journal (plan 12.5);
* every other step edits the CANDIDATE directory ``.svweave/candidates/<txn>/`` (a real byte copy
  of the base snapshot) or reads immutable snapshots;
* fail closed: unknown safety state means refuse, never "apply anyway".
"""

from __future__ import annotations

import sqlite3
from collections import Counter
from collections.abc import Callable, Iterator
from contextlib import AbstractContextManager, contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from svweave.adapters.serena.guard import tree_hashes
from svweave.application.build import DB_FILENAME, run_build, run_build_on_record
from svweave.application.options import WorkspaceOptions
from svweave.application.semantic_common import require_build
from svweave.application.toolchain import compute_toolchain
from svweave.config.context import WorkspaceContext, snapshot_rel
from svweave.config.loader import policy_hash
from svweave.config.trusted import ensure_outside_tree
from svweave.domain.envelope import (
    AnalysisCoverage,
    Envelope,
    ErrorInfo,
    Outcome,
    Provenance,
)
from svweave.domain.errors import ErrorCode, SvwError
from svweave.domain.ids import canonical_hash
from svweave.domain.states import Completeness, DiagnosticOutcome, Freshness
from svweave.domain.states import TransactionState as S
from svweave.editing import approval as appr
from svweave.editing import journal as jr
from svweave.editing.candidate import (
    candidate_dir,
    create_candidate,
    remove_candidate,
    tree_hash,
    tree_state,
)
from svweave.editing.candidate_edit import SerenaFactory, SymbolEditor, apply_edits
from svweave.editing.candsnap import derive_candidate_snapshot
from svweave.editing.diffing import (
    DIFF_DISPLAY_LIMIT,
    FileChange,
    build_file_change,
    combined_diff,
    compare_trees,
    diagnostic_control_lines,
    has_unsafe_control,
    is_text,
    patch_hash,
    patch_record,
)
from svweave.editing.models import PrepareRequest
from svweave.editing.plan import (
    declared_files,
    denied,
    edit_policy_state,
    path_allowed,
    recheck_edit_policy,
    require_editing_enabled,
    resolve_edits,
    slang_scope_escape,
)
from svweave.editing.store import Txn, TxnStore, new_txn_id, now_iso, valid_txn_id
from svweave.manifest.resolver import resolve_profile
from svweave.snapshot.capture import capture_snapshot, check_freshness, load_record, snapshots_dir
from svweave.snapshot.models import SnapshotRecord
from svweave.storage.db import Database
from svweave.storage.lock import WorkspaceBusy, workspace_lock

__all__ = [
    "Confirmation",
    "abort",
    "approve",
    "commit",
    "prepare",
    "recover",
    "show",
    "validate",
]

TXN_TTL_S = 24 * 3600
NOT_RUN = {"eda_compile": "NOT_RUN", "simulation": "NOT_RUN", "formal": "NOT_RUN"}
LIMITATIONS = [
    "Validation is static slang analysis of the candidate only. EDA compile, simulation and "
    "formal runs were NOT_RUN; a passing static check says nothing about runtime behaviour.",
]


# ------------------------------------------------------------------------------ helpers
@contextmanager
def _session(opts: WorkspaceOptions) -> Iterator[tuple[WorkspaceContext, Database, TxnStore]]:
    ctx = opts.load()
    with Database(ctx.state_dir / DB_FILENAME) as db:
        yield ctx, db, TxnStore(db)


def _utcnow() -> datetime:
    return datetime.now(UTC)


def _require_id(txn_id: str) -> None:
    if not valid_txn_id(txn_id):
        raise SvwError(
            ErrorCode.INVALID_CONFIG, "bad transaction id", {"field": "transaction_id"}
        )


def _txn_data(txn: Txn) -> dict[str, Any]:
    r = txn.record
    return {
        "transaction_id": txn.transaction_id,
        "state": txn.state.value,
        "patch_hash": txn.patch_hash,
        "base_build_id": txn.base_build_id,
        "base_snapshot_id": txn.base_snapshot_id,
        "candidate_snapshot_id": txn.candidate_snapshot_id,
        "profiles": r.get("profiles", []),
        "files": r.get("files", []),
        "edits": r.get("edits", []),
        "edit_paths": sorted({e["edit_path"] for e in r.get("edits", [])}),
        "expires_at": r.get("expires_at"),
        "auto_committable": r.get("validation", {}).get("auto_committable"),
        "not_run": NOT_RUN,
    }


def _env(
    ctx: WorkspaceContext,
    request_id: str,
    txn: Txn,
    data: dict[str, Any],
    *,
    outcome: Outcome = Outcome.OK,
    warnings: list[str] | None = None,
    error: ErrorInfo | None = None,
    completeness: Completeness = Completeness.COMPLETE,
    freshness: Freshness = Freshness.UNKNOWN,
) -> Envelope:
    return Envelope(
        request_id=request_id,
        outcome=outcome,
        workspace_id=ctx.workspace_id,
        snapshot_id=txn.base_snapshot_id,
        build_id=txn.base_build_id,
        profile=txn.profile,
        freshness=freshness,
        analysis_coverage=AnalysisCoverage(
            completeness=completeness,
            scope="edit_transaction",
            basis="static_slang_only",
            limitations=list(LIMITATIONS),
        ),
        provenance=Provenance(backend="svweave-edit-gateway"),
        data={**_txn_data(txn), **data},
        warnings=warnings or [],
        error=error,
    )


def _fail(store: TxnStore, txn: Txn, to: S, err: SvwError) -> SvwError:
    """Record an abandoned transaction with its reason; returns the error to raise."""
    if txn.state not in {S.APPLIED, S.POST_APPLY_VERIFIED} and to in _legal(txn.state):
        store.update(
            txn.transaction_id,
            lambda r: r.__setitem__("error", {"code": err.code.value, "message": err.message[:300]}),
            to=to,
            note=err.code.value,
        )
    return err


def _legal(state: S) -> frozenset[S]:
    from svweave.editing.machine import LEGAL

    return LEGAL[state]


def _check_expiry(store: TxnStore, txn: Txn) -> Txn:
    exp = txn.record.get("expires_at")
    if (
        exp
        and datetime.fromisoformat(exp) <= _utcnow()
        and S.EXPIRED in _legal(txn.state)
    ):
        store.update(txn.transaction_id, to=S.EXPIRED, note="transaction ttl")
        raise SvwError(
            ErrorCode.APPROVAL_EXPIRED,
            "the edit transaction expired; prepare it again",
            {"reason": "transaction_expired", "transaction_id": txn.transaction_id},
        )
    return txn


def _base_rec(ctx: WorkspaceContext, sid: str) -> tuple[SnapshotRecord, Path]:
    d = snapshots_dir(ctx) / sid
    if not (d / "snapshot.json").is_file():
        raise SvwError(
            ErrorCode.STALE_SNAPSHOT,
            "the base snapshot is no longer available",
            {"reason": "snapshot_missing", "snapshot_id": sid},
        )
    return load_record(d), d


def _stale(ctx: WorkspaceContext, rec: SnapshotRecord) -> SvwError | None:
    res = check_freshness(ctx, rec)
    if res.freshness == "CURRENT":
        return None
    return SvwError(
        ErrorCode.STALE_SNAPSHOT,
        "the workspace changed since the base snapshot (sources or dependencies)",
        {
            "reason": "workspace_changed",
            "snapshot_id": rec.snapshot_id,
            "changed": [c.model_dump() for c in res.changed[:20]],
        },
    )


# ------------------------------------------------------------------------------ freezing
@dataclass
class Frozen:
    tree: dict[str, str]
    tree_hash: str
    failures: list[dict[str, Any]] = field(default_factory=list)
    changes: list[FileChange] = field(default_factory=list)
    patch: dict[str, Any] = field(default_factory=dict)
    patch_hash: str = ""
    changed_lines: int = 0

    def after_bytes(self) -> dict[str, bytes]:
        return {c.logical_path: c.after_bytes for c in self.changes}


def freeze_candidate(
    ctx: WorkspaceContext, record: dict[str, Any], base_dir: Path, cand_dir: Path, base_sid: str
) -> Frozen:
    """Hash the candidate tree and diff it against the base snapshot (EDT-06, EDT-10)."""
    base_tree = tree_hashes(base_dir / "files")
    cand_tree = tree_state(cand_dir, frozenset(base_tree))
    cmp = compare_trees(base_tree, cand_tree, cand_dir)
    fz = Frozen(tree=cand_tree, tree_hash=tree_hash(cand_tree))
    declared: dict[str, str] = {snapshot_rel(lp): lp for lp in record.get("files", [])}

    def fail(reason: str, **kw: Any) -> None:
        fz.failures.append({"reason": reason, **kw})

    for rel in cmp.changes.added:
        fail("file_added", path=rel, detail="creating files is not supported (EDT-10)")
    for rel in cmp.changes.removed:
        fail("file_removed", path=rel, detail="deleting / renaming files is not supported (EDT-10)")
    for rel in cmp.irregular:
        fail("symlink_or_special_file", path=rel)
    for rel in cmp.hardlinked:
        fail("hardlinked_file", path=rel)
    for rel in cmp.binary:
        fail("binary_file", path=rel)
    skip = set(cmp.irregular) | set(cmp.binary)
    for rel in cmp.changes.modified:
        if rel not in declared:
            fail("undeclared_file_changed", path=rel, detail="not part of the prepared edits")
            continue
        logical = declared[rel]
        if not path_allowed(ctx, logical):
            fail("path_not_allowed", path=logical)
        if rel in skip:
            continue
        bp = base_dir / "files" / rel
        if not is_text(bp.read_bytes()):
            fail("binary_file", path=rel)
            continue
        fz.changes.append(build_file_change(logical, rel, bp, cand_dir / rel))
    for c in fz.changes:
        hits = diagnostic_control_lines(c.diff)
        if hits:
            fail("diagnostic_control_change", path=c.logical_path, lines=hits[:5],
                 detail="a candidate may not add or alter diagnostic-control directives")
    for c in fz.changes:
        if any(
            has_unsafe_control(ln[1:])
            for ln in c.diff.splitlines(keepends=True)
            if ln.startswith("+") and not ln.startswith("+++")
        ):
            fail("control_character_in_edit", path=c.logical_path,
                 detail="added text holds a terminal control character, DEL or a bidi override")
    fz.changed_lines = sum(c.added_lines + c.removed_lines for c in fz.changes)
    ed = ctx.manifest.editing
    if len(fz.changes) > ed.max_files_per_patch:
        fail("too_many_files", count=len(fz.changes), limit=ed.max_files_per_patch)
    if fz.changed_lines > ed.max_changed_lines:
        fail("too_many_changed_lines", count=fz.changed_lines, limit=ed.max_changed_lines)
    if not fz.changes and not any(f["reason"] != "empty_patch" for f in fz.failures):
        fail("empty_patch", detail="the candidate does not differ from the base")
    fz.patch = patch_record(base_sid, fz.changes)
    fz.patch_hash = patch_hash(fz.patch)
    return fz


def _profiles_binding(txn: Txn, ctx: WorkspaceContext) -> appr.Binding:
    assert txn.patch_hash is not None and txn.base_snapshot_id is not None
    return appr.Binding(
        transaction_id=txn.transaction_id,
        patch_hash=txn.patch_hash,
        base_snapshot_id=txn.base_snapshot_id,
        profiles=tuple(txn.record.get("profiles", [])),
        policy_hash=policy_hash(ctx.policy),
        config_ids=tuple(sorted(txn.record.get("config_ids", {}).items())),
        edit_policy_hash=canonical_hash(edit_policy_state(ctx, list(txn.record.get("files", [])))),
    )


def _check_trusted_location(ctx: WorkspaceContext) -> None:
    """F9: approvals live OUTSIDE the project tree, in a 0700 directory."""
    ensure_outside_tree(ctx.trusted_dir, ctx.manifest_dir, ctx.project_root)


# ------------------------------------------------------------------------------ prepare
def _default_serena(
    ctx: WorkspaceContext, registry: Any | None
) -> tuple[SerenaFactory | None, str | None]:
    """Serena factory from the backend registry, or (None, reason) when the capability is absent."""
    from svweave.orchestration.registry import BackendRegistry

    reg = registry
    own = False
    if reg is None:
        try:
            reg = BackendRegistry.for_workspace(ctx)
            own = True
        except SvwError as e:
            return None, f"{e.code.value}: {e.message}"
    if not reg.toolchain.serena_exe.is_file():
        return None, "Serena is not installed in the toolchain"

    @contextmanager
    def factory(candidate: Path) -> Iterator[SymbolEditor]:
        try:
            yield reg.serena_for(candidate)
        finally:
            reg.stop_serena()
            if own:
                reg.shutdown()

    return factory, None


def prepare(
    opts: WorkspaceOptions,
    request_id: str,
    request: PrepareRequest | dict[str, Any],
    *,
    registry: Any | None = None,
    serena_factory: Callable[[Path], AbstractContextManager[SymbolEditor]] | None = None,
    use_serena: bool = True,
) -> Envelope:
    """Create the candidate. ``use_serena=False`` models an absent Serena capability: function
    edits then go through the labelled ``exact_range`` path."""
    try:
        req = (
            request
            if isinstance(request, PrepareRequest)
            else PrepareRequest.model_validate(request)
        )
    except ValidationError as e:
        raise SvwError(
            ErrorCode.INVALID_CONFIG,
            "invalid edit request",
            {"errors": [{"path": "/".join(map(str, x["loc"])), "type": x["type"]} for x in e.errors()[:10]]},
        ) from e
    with _session(opts) as (ctx, db, store):
        require_editing_enabled(ctx)
        phash = req.payload_hash()
        same_key = store.all_by_request_key(req.request_key)
        for t in same_key:  # ABORTED transactions keep their key (F7)
            if t.payload_hash != phash:
                raise SvwError(
                    ErrorCode.IDEMPOTENCY_CONFLICT,
                    "this request_key was already used with a different payload",
                    {"transaction_id": t.transaction_id},
                )
        prior = store.by_request_key(req.request_key)
        if prior is not None:
            return _env(
                ctx, request_id, prior, {"idempotent_replay": True},
                warnings=["same request_key and payload: returning the existing transaction"],
            )
        jr.assert_no_recovery_pending(ctx, [t.transaction_id for t in store.blocking()])

        build = require_build(db, req.base_build_id)
        if build["snapshot_id"] != req.snapshot_id:
            raise SvwError(
                ErrorCode.STALE_SNAPSHOT,
                "snapshot_id does not belong to base_build_id",
                {"reason": "snapshot_build_mismatch"},
            )
        if build["completeness"] != Completeness.COMPLETE.value:
            raise SvwError(
                ErrorCode.PARTIAL_ANALYSIS,
                "the base build is not COMPLETE; editing needs a COMPLETE, CURRENT build",
                {"completeness": build["completeness"], "reason": "base_not_complete"},
            )
        rec, rec_dir = _base_rec(ctx, req.snapshot_id)
        stale = _stale(ctx, rec)
        if stale is not None:
            raise stale
        profile = str(build["profile"])
        nm = resolve_profile(ctx, profile)
        tc_id, _, _ = compute_toolchain(ctx.project_root)
        if nm.config_id != build["config_id"] or tc_id != build["toolchain_id"]:
            raise SvwError(
                ErrorCode.STALE_SNAPSHOT,
                "configuration or toolchain changed since the base build",
                {"reason": "config_or_toolchain_changed"},
            )
        # the configured validate_profiles are the mandatory floor; a request may only ADD (F4)
        profiles = list(dict.fromkeys(
            [profile, *ctx.manifest.editing.validate_profiles, *req.validate_profiles]
        ))
        for p in profiles:
            ctx.profile(p)  # INVALID_CONFIG for an unknown profile

        resolved, _base_bytes = resolve_edits(ctx, db, req.base_build_id, rec, req.edits)

        factory = serena_factory
        serena_note: str | None = None
        if factory is None and any(
            e.kind == "symbol_replacement" and e.decl_kind == "function" for e in resolved
        ):
            if use_serena:
                factory, serena_note = _default_serena(ctx, registry)
            else:
                serena_note = "disabled by the caller"


        txn_id = new_txn_id()
        record: dict[str, Any] = {
            "request": req.model_dump(mode="json", exclude={"request_key"}),
            "profiles": profiles,
            "files": declared_files(resolved),
            "edits": [e.to_record() for e in resolved],
            "policy_hash": policy_hash(ctx.policy),
            "toolchain_id": tc_id,
            "expires_at": (_utcnow() + timedelta(seconds=TXN_TTL_S)).isoformat(timespec="seconds"),
            "serena_unavailable": serena_note,
        }
        try:
            txn = store.create(
                txn_id,
                base_snapshot_id=req.snapshot_id,
                base_build_id=req.base_build_id,
                profile=profile,
                request_key=req.request_key,
                payload_hash=phash,
                record=record,
            )
        except sqlite3.IntegrityError:  # lost a race on the idempotency key
            again = store.by_request_key(req.request_key)
            if again is not None and again.payload_hash == phash:
                return _env(ctx, request_id, again, {"idempotent_replay": True})
            raise SvwError(
                ErrorCode.IDEMPOTENCY_CONFLICT,
                "this request_key was already used with a different payload",
            ) from None

        cdir = candidate_dir(ctx, txn_id)
        try:
            create_candidate(rec_dir / "files", cdir)
            apply_edits(cdir, resolved, factory)
            fz = freeze_candidate(ctx, {"files": record["files"]}, rec_dir, cdir, req.snapshot_id)
            if fz.failures:
                raise denied(
                    "candidate_rejected",
                    "the candidate violates the edit policy: "
                    + ", ".join(sorted({f["reason"] for f in fz.failures})),
                    failures=fz.failures[:20],
                )
        except BaseException as e:
            remove_candidate(cdir)
            err = e if isinstance(e, SvwError) else SvwError(
                ErrorCode.INTERNAL, f"prepare failed ({type(e).__name__})"
            )
            _fail(store, txn, S.ABORTED, err)  # also for KeyboardInterrupt: auditable abort
            raise
        txn = store.update(
            txn_id,
            lambda r: r.update(
                edits=[e.to_record() for e in resolved],
                candidate={"dir": f".svweave/candidates/{txn_id}", "tree_hash": fz.tree_hash},
            ),
            to=S.CANDIDATE_READY,
        )
        data = {
            "candidate_dir": f".svweave/candidates/{txn_id}",
            "next": f"svweave edit validate --transaction {txn_id}",
        }
        warns = [
            "candidate prepared; the original workspace was NOT modified",
        ]
        if serena_note:
            warns.append(f"Serena capability unavailable ({serena_note}); function edits use exact_range")
        return _env(ctx, request_id, txn, data, warnings=warns)


# ------------------------------------------------------------------------------ validate
def _build_facts(db: Database, build_id: str) -> dict[str, Any]:
    row = db.conn.execute(
        "SELECT completeness, diagnostic_outcome, snapshot_id, config_id FROM builds "
        "WHERE build_id=?",
        (build_id,),
    ).fetchone()
    if row is None:
        return {"build_id": build_id, "completeness": "BLOCKED", "diagnostic_outcome": "NOT_AVAILABLE"}
    diags = db.conn.execute(
        "SELECT severity, code, message, file, suppression FROM diagnostics WHERE build_id=?",
        (build_id,),
    ).fetchall()
    warnings: Counter[tuple[str, str, str]] = Counter()
    errors = 0
    for d in diags:
        if d["severity"] == "error":
            errors += 1
        elif d["severity"] == "warning" and d["suppression"] is None:  # no waiver => counts
            warnings[(str(d["code"]), str(d["file"]), str(d["message"]))] += 1
    return {
        "build_id": build_id,
        "snapshot_id": row["snapshot_id"],
        "config_id": row["config_id"],
        "completeness": row["completeness"],
        "diagnostic_outcome": row["diagnostic_outcome"],
        "errors": errors,
        "warnings": sum(warnings.values()),
        "_warning_keys": warnings,
    }


def _clean(f: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in f.items() if not k.startswith("_")}


def _is_clean(f: dict[str, Any]) -> bool:
    return bool(
        f["completeness"] == Completeness.COMPLETE.value
        and f["diagnostic_outcome"] == DiagnosticOutcome.NO_ERRORS.value
    )


def _run_profile_validation(
    opts: WorkspaceOptions,
    ctx: WorkspaceContext,
    db: Database,
    txn: Txn,
    profile: str,
    changed: dict[str, bytes],
    request_id: str,
    failures: list[dict[str, Any]],
) -> dict[str, Any]:
    """Baseline + candidate static build of one profile; appends failures; returns the facts."""
    out: dict[str, Any] = {"profile": profile}
    base_env = run_build(opts, profile, request_id)
    if base_env.build_id is None or base_env.error and base_env.outcome in (Outcome.ERROR, Outcome.BLOCKED):
        failures.append({"reason": "baseline_build_failed", "profile": profile,
                         "error": base_env.error.code if base_env.error else None})
        out["baseline"] = {"build_id": base_env.build_id, "completeness": "BLOCKED"}
        return out
    base = _build_facts(db, base_env.build_id)
    out["baseline"] = _clean(base)
    if profile == txn.profile and (
        base_env.build_id != txn.base_build_id or base_env.snapshot_id != txn.base_snapshot_id
    ):
        raise SvwError(
            ErrorCode.STALE_SNAPSHOT,
            "the baseline of the base profile is no longer the prepared base build",
            {"reason": "base_build_changed"},
        )
    if not _is_clean(base):
        failures.append({
            "reason": "baseline_not_clean", "profile": profile,
            "completeness": base["completeness"], "diagnostic_outcome": base["diagnostic_outcome"],
            "detail": "baseline must be COMPLETE + NO_ERRORS; the diff is returned for review "
            "but is not auto-committable (plan 12.4)",
        })
    brec, _ = _base_rec(ctx, base_env.snapshot_id or "")
    crec = derive_candidate_snapshot(ctx, brec, changed)
    if crec is None:
        failures.append({"reason": "profile_does_not_cover_edit", "profile": profile})
        return out
    cand_env = run_build_on_record(opts, profile, crec, request_id, publish=False)
    out["candidate_snapshot_id"] = crec.snapshot_id
    if cand_env.build_id is None or (cand_env.error and cand_env.outcome in (Outcome.ERROR, Outcome.BLOCKED)):
        failures.append({"reason": "candidate_build_failed", "profile": profile,
                         "error": cand_env.error.code if cand_env.error else None})
        out["candidate"] = {"build_id": cand_env.build_id, "completeness": "BLOCKED"}
        return out
    cand = _build_facts(db, cand_env.build_id)
    out["candidate"] = _clean(cand)
    if not _is_clean(cand):
        failures.append({
            "reason": "candidate_not_clean", "profile": profile,
            "completeness": cand["completeness"], "diagnostic_outcome": cand["diagnostic_outcome"],
            "errors": cand.get("errors"),
        })
    new = cand["_warning_keys"] - base["_warning_keys"]
    gone = base["_warning_keys"] - cand["_warning_keys"]
    out["diagnostics_delta"] = {
        "errors": [base.get("errors"), cand.get("errors")],
        "warnings": [base.get("warnings"), cand.get("warnings")],
        "new_warnings": [{"code": k[0], "file": k[1], "message": k[2][:200], "count": n}
                         for k, n in sorted(new.items())][:20],
        "resolved_warnings": sum(gone.values()),
    }
    if new:
        failures.append({"reason": "new_warnings", "profile": profile,
                         "count": sum(new.values()),
                         "detail": "candidate introduces non-waived warnings"})
    all_edits = txn.record.get("edits", [])
    targets = []
    for e in all_edits:
        if e.get("kind") == "slang_symbol" and e.get("resolved_symbol"):
            # where the replacement must sit in CANDIDATE coordinates: earlier edits of the same
            # file shift it by (replacement length - replaced length)
            shift = sum(
                o["replacement_bytes"] - (o["end_byte"] - o["start_byte"])
                for o in all_edits
                if o["logical_path"] == e["logical_path"] and o["start_byte"] < e["start_byte"]
            )
            targets.append({**e["resolved_symbol"],
                            "new_start": e["start_byte"] + shift,
                            "new_end": e["start_byte"] + shift + e["replacement_bytes"]})
    if targets:  # ADR-013: the replacement must stay inside the declaration it replaced
        problems = slang_scope_escape(db, base_env.build_id, cand_env.build_id, targets)
        if problems:
            failures.append({"reason": "slang_symbol_scope_escape", "profile": profile,
                             "problems": problems[:10],
                             "detail": "the candidate changed declarations outside the targeted "
                             "task / function, or the target lost its name, kind or constructor flag"})
    out["candidate_build_id"] = cand_env.build_id
    out["baseline_build_id"] = base_env.build_id
    out["baseline_snapshot_id"] = base_env.snapshot_id
    return out


_POLICY_REASONS = frozenset({
    "file_added", "file_removed", "symlink_or_special_file", "hardlinked_file", "binary_file",
    "undeclared_file_changed", "path_not_allowed", "too_many_files", "too_many_changed_lines",
    "empty_patch", "candidate_modified_after_prepare", "control_character_in_edit",
    "slang_symbol_scope_escape",
})


def validate(opts: WorkspaceOptions, request_id: str, *, transaction_id: str) -> Envelope:
    _require_id(transaction_id)
    with _session(opts) as (ctx, db, store):
        require_editing_enabled(ctx)
        txn = _check_expiry(store, store.get(transaction_id))
        if txn.state is not S.CANDIDATE_READY:
            stored = txn.record.get("validation")
            if stored is not None:
                return _validation_envelope(ctx, request_id, txn, stored, replay=True)
            raise SvwError(
                ErrorCode.INVALID_CONFIG,
                f"transaction is {txn.state.value}; validate needs CANDIDATE_READY",
                {"state": txn.state.value},
            )
        txn = store.update(transaction_id, expect=S.CANDIDATE_READY, to=S.VALIDATING)
        try:
            stored = _validate_body(opts, ctx, db, store, txn, request_id)
        except SvwError as e:
            if e.code is ErrorCode.STALE_SNAPSHOT:
                raise _fail(store, store.get(transaction_id), S.CONFLICT, e) from None
            raise _fail(store, store.get(transaction_id), S.VALIDATION_FAILED, e) from None
        except BaseException:
            cur = store.get(transaction_id)
            if cur.state is S.VALIDATING:
                store.update(transaction_id, to=S.VALIDATION_FAILED, note="unexpected error")
            raise
        return _validation_envelope(ctx, request_id, store.get(transaction_id), stored, replay=False)


def _validate_body(
    opts: WorkspaceOptions,
    ctx: WorkspaceContext,
    db: Database,
    store: TxnStore,
    txn: Txn,
    request_id: str,
) -> dict[str, Any]:
    assert txn.base_snapshot_id is not None
    rec, rec_dir = _base_rec(ctx, txn.base_snapshot_id)
    stale = _stale(ctx, rec)
    if stale is not None:
        raise stale
    cdir = candidate_dir(ctx, txn.transaction_id)
    if not cdir.is_dir():
        raise SvwError(ErrorCode.MISSING_INPUT, "candidate directory is gone",
                       {"reason": "candidate_missing"})
    fz = freeze_candidate(ctx, txn.record, rec_dir, cdir, txn.base_snapshot_id)
    failures = list(fz.failures)
    prepared_tree = txn.record.get("candidate", {}).get("tree_hash")
    if prepared_tree != fz.tree_hash:
        failures.insert(0, {
            "reason": "candidate_modified_after_prepare",
            "detail": "the candidate differs from the tree frozen at prepare time",
        })
    profiles_out: list[dict[str, Any]] = []
    if not any(f["reason"] in _POLICY_REASONS for f in failures):
        changed = fz.after_bytes()
        for p in txn.record["profiles"]:
            profiles_out.append(
                _run_profile_validation(opts, ctx, db, txn, p, changed, request_id, failures)
            )
    baseline_partial = any(f["reason"] == "baseline_not_clean" for f in failures)
    ok = not failures
    diff_text = combined_diff(fz.changes)
    truncated = len(diff_text.encode()) > DIFF_DISPLAY_LIMIT
    stored: dict[str, Any] = {
        "ok": ok,
        "auto_committable": ok and not baseline_partial,
        "baseline_partial": baseline_partial,
        "failures": failures,
        "patch_hash": fz.patch_hash,
        "tree_hash": fz.tree_hash,
        "files": [
            {
                "logical_path": c.logical_path,
                "before_sha256": c.before_sha256,
                "after_sha256": c.after_sha256,
                "added_lines": c.added_lines,
                "removed_lines": c.removed_lines,
            }
            for c in fz.changes
        ],
        "changed_lines": fz.changed_lines,
        "diff": diff_text.encode()[:DIFF_DISPLAY_LIMIT].decode("utf-8", "ignore"),
        "diff_truncated": truncated,
        "profiles": profiles_out,
        "toolchain_id": txn.record.get("toolchain_id"),
        "policy_hash": policy_hash(ctx.policy),
        "validated_at": now_iso(),
    }
    cand_sid = next((p.get("candidate_snapshot_id") for p in profiles_out
                     if p["profile"] == txn.profile), None)

    def mut(r: dict[str, Any]) -> None:
        r["validation"] = stored
        r["profile_builds"] = {
            p["profile"]: {
                k: p.get(k)
                for k in ("baseline_build_id", "baseline_snapshot_id", "candidate_build_id",
                          "candidate_snapshot_id")
            }
            for p in profiles_out
        }
        r["candidate"] = {**r.get("candidate", {}), "validated_tree_hash": fz.tree_hash}
        r["config_ids"] = {
            p["profile"]: p["baseline"]["config_id"]
            for p in profiles_out
            if p.get("baseline", {}).get("config_id")
        }

    if ok:
        store.update(txn.transaction_id, mut, to=S.VALIDATED, patch_hash=fz.patch_hash,
                     candidate_snapshot_id=cand_sid)
        store.update(txn.transaction_id, to=S.AWAITING_APPROVAL)
    else:
        store.update(
            txn.transaction_id, mut, to=S.VALIDATION_FAILED, patch_hash=fz.patch_hash,
            candidate_snapshot_id=cand_sid, note="; ".join(sorted({f["reason"] for f in failures})),
        )
    return stored


def _validation_envelope(
    ctx: WorkspaceContext, request_id: str, txn: Txn, v: dict[str, Any], *, replay: bool
) -> Envelope:
    data = {
        "validation": {k: v[k] for k in v if k != "diff"},
        "diff": v["diff"],
        "diff_truncated": v["diff_truncated"],
        "patch_hash": v["patch_hash"],
        "files_changed": v["files"],
        "diagnostics_delta": {p["profile"]: p.get("diagnostics_delta") for p in v["profiles"]},
        "idempotent_replay": replay,
    }
    if v["ok"]:
        data["approval_command"] = (
            f"svweave edit approve --transaction {txn.transaction_id} --patch-hash {v['patch_hash']}"
        )
        return _env(ctx, request_id, txn, data, warnings=[
            "validated statically only; EDA compile / simulation / formal are NOT_RUN",
        ])
    reasons = {f["reason"] for f in v["failures"]}
    policy = bool(reasons & _POLICY_REASONS)
    code = ErrorCode.POLICY_DENIED if policy else ErrorCode.PARTIAL_ANALYSIS
    return _env(
        ctx, request_id, txn, data,
        outcome=Outcome.ERROR,
        completeness=Completeness.PARTIAL,
        warnings=["the diff is kept for review, but this candidate cannot be committed"],
        error=ErrorInfo(
            code=code.value,
            message="candidate validation failed: " + ", ".join(sorted(reasons)),
            details={"failures": v["failures"][:20], "auto_committable": False},
        ),
    )


# ------------------------------------------------------------------------------ approve
@dataclass(frozen=True)
class Confirmation:
    """What the human typed and how it was obtained (``tty`` or the unqualified ``test_token``)."""

    typed: str
    method: str


def approve(
    opts: WorkspaceOptions,
    request_id: str,
    *,
    transaction_id: str,
    patch_hash: str,
    confirm: Callable[[dict[str, Any]], Confirmation],
    ttl_s: int = appr.DEFAULT_TTL_S,
) -> Envelope:
    """Human approval (CLI only; there is no MCP tool for this). ``confirm`` shows the diff and
    returns what the human typed: the first 12 characters of the patch hash."""
    _require_id(transaction_id)
    with _session(opts) as (ctx, db, store):
        require_editing_enabled(ctx)
        _check_trusted_location(ctx)
        txn = _check_expiry(store, store.get(transaction_id))
        if txn.state not in (S.AWAITING_APPROVAL, S.APPROVED):
            raise SvwError(
                ErrorCode.APPROVAL_REQUIRED,
                f"transaction is {txn.state.value}; only a validated transaction can be approved",
                {"state": txn.state.value},
            )
        if txn.patch_hash != patch_hash:
            raise SvwError(
                ErrorCode.APPROVAL_REQUIRED,
                "--patch-hash does not match the validated patch of this transaction",
                {"reason": "patch_hash_mismatch"},
            )
        # re-freeze NOW: the human must approve the bytes that will be written
        assert txn.base_snapshot_id is not None
        _, rec_dir = _base_rec(ctx, txn.base_snapshot_id)
        fz = freeze_candidate(ctx, txn.record, rec_dir, candidate_dir(ctx, transaction_id),
                              txn.base_snapshot_id)
        if fz.failures or fz.patch_hash != txn.patch_hash:
            raise SvwError(
                ErrorCode.APPROVAL_REQUIRED,
                "the candidate changed after validation; approval refused (EDT-04)",
                {"reason": "candidate_changed_after_validation"},
            )
        v = txn.record["validation"]
        got = confirm({
            "transaction_id": transaction_id,
            "patch_hash": txn.patch_hash,
            "diff": combined_diff(fz.changes),
            "files": v["files"],
            "profiles": txn.record["profiles"],
            "edit_paths": sorted({e["edit_path"] for e in txn.record["edits"]}),
            "resolved_symbols": [
                e["resolved_symbol"] for e in txn.record["edits"] if e.get("resolved_symbol")
            ],
            "limitations": LIMITATIONS,
            "baseline_partial": v.get("baseline_partial", False),
        })
        if got.method != "tty" and not ctx.policy.allow_test_approvals:
            raise SvwError(
                ErrorCode.APPROVAL_REQUIRED,
                "this approval path is not a human at a terminal and the trusted policy does not "
                "set allow_test_approvals",
                {"reason": "test_approvals_not_allowed"},
            )
        if got.typed.strip() != txn.patch_hash[:12]:
            raise SvwError(
                ErrorCode.APPROVAL_REQUIRED,
                "confirmation does not match the first 12 characters of the patch hash",
                {"reason": "confirmation_mismatch"},
            )
        if not v.get("auto_committable"):
            raise denied("not_auto_committable", "this transaction is not auto-committable")
        rec = appr.write_approval(
            ctx.trusted_dir, _profiles_binding(txn, ctx), method=got.method, ttl_s=ttl_s
        )
        if txn.state is S.AWAITING_APPROVAL:
            txn = store.update(
                transaction_id,
                lambda r: r.__setitem__("approval", {k: rec[k] for k in
                                                     ("approved_at", "expires_at", "method")}),
                to=S.APPROVED,
            )
        else:
            txn = store.update(
                transaction_id,
                lambda r: r.__setitem__("approval", {k: rec[k] for k in
                                                     ("approved_at", "expires_at", "method")}),
            )
        warns = []
        if got.method != "tty":
            warns.append(
                f"approval method {got.method!r} is a NON-QUALIFIED test harness path, not a "
                "human at a terminal"
            )
        return _env(ctx, request_id, txn, {
            "approval": {"expires_at": rec["expires_at"], "method": rec["method"],
                         "qualified": rec["method"] == "tty"},
            "next": f"svweave edit commit --transaction {transaction_id} --patch-hash "
                    f"{txn.patch_hash} --request-key <key>",
        }, warnings=warns)


# ------------------------------------------------------------------------------ commit
def commit(
    opts: WorkspaceOptions,
    request_id: str,
    *,
    transaction_id: str,
    expected_patch_hash: str,
    request_key: str,
    crash_after_files: int | None = None,
) -> Envelope:
    """Apply an approved, validated transaction. ``crash_after_files`` is TEST-ONLY: keyword
    argument of this function, not reachable from configuration, environment or MCP."""
    _require_id(transaction_id)
    with _session(opts) as (ctx, db, store):
        require_editing_enabled(ctx)
        _check_trusted_location(ctx)
        txn = store.get(transaction_id)
        for other in store.commit_key_owner(request_key):  # keys are scoped per workspace (F7)
            if other.transaction_id != transaction_id:
                raise SvwError(
                    ErrorCode.IDEMPOTENCY_CONFLICT,
                    "this commit request_key was already used for another transaction",
                    {"transaction_id": other.transaction_id},
                )
        prior = txn.record.get("commit")
        if (
            prior is not None
            and prior.get("request_key") == request_key
            and prior.get("expected_patch_hash") != expected_patch_hash
        ):
            raise SvwError(
                ErrorCode.IDEMPOTENCY_CONFLICT,
                "this request_key was already used with a different payload",
                {"transaction_id": transaction_id},
            )
        if txn.state in (S.APPLIED, S.POST_APPLY_VERIFIED) and prior is not None:
            if expected_patch_hash != txn.patch_hash:
                raise SvwError(
                    ErrorCode.IDEMPOTENCY_CONFLICT,
                    "transaction already applied with a different patch hash",
                    {"transaction_id": transaction_id},
                )
            if txn.state is S.APPLIED:  # writes are done; finish the verification only
                return _post_apply(opts, ctx, db, store, txn, request_id, replay=True)
            return _commit_envelope(ctx, request_id, txn, prior["result"], replay=True)
        if txn.state is S.CONFLICT:
            raise SvwError(ErrorCode.EDIT_CONFLICT, "transaction is in CONFLICT; prepare a new one",
                           {"reason": "transaction_conflict"})
        if txn.state in (S.COMMITTING, S.RECOVERY_REQUIRED):
            jr.assert_no_recovery_pending(ctx, [txn.transaction_id])
        if txn.state in (S.ABORTED, S.EXPIRED):
            raise denied("transaction_closed", f"transaction is {txn.state.value}")
        if txn.state is S.VALIDATION_FAILED:
            raise denied("validation_failed", "the candidate failed validation and cannot be committed")
        if txn.state not in (S.AWAITING_APPROVAL, S.APPROVED):
            raise SvwError(
                ErrorCode.APPROVAL_REQUIRED,
                f"transaction is {txn.state.value}: not validated and approved",
                {"reason": "not_validated", "state": txn.state.value},
            )
        txn = _check_expiry(store, txn)
        if expected_patch_hash != txn.patch_hash:
            raise SvwError(
                ErrorCode.APPROVAL_REQUIRED,
                "expected_patch_hash is not the validated patch hash of this transaction",
                {"reason": "patch_hash_mismatch"},
            )
        assert txn.base_snapshot_id is not None and txn.patch_hash is not None
        rec, rec_dir = _base_rec(ctx, txn.base_snapshot_id)
        # 0. the CURRENT editing policy (it may have changed since approval) is checked first, so a
        # policy change is reported as such and not as a changed candidate (F10, A4)
        recheck_edit_policy(ctx, list(txn.record.get("files", [])),
                            list(txn.record.get("profiles", [])))
        # 1. the bytes about to be written must be the bytes that were validated and approved
        fz = freeze_candidate(ctx, txn.record, rec_dir, candidate_dir(ctx, transaction_id),
                              txn.base_snapshot_id)
        if (
            fz.failures
            or fz.patch_hash != txn.patch_hash
            or fz.tree_hash != txn.record.get("candidate", {}).get("validated_tree_hash")
        ):
            raise SvwError(
                ErrorCode.APPROVAL_REQUIRED,
                "the candidate changed after validation: the approval no longer applies (EDT-04)",
                {"reason": "candidate_changed_after_validation"},
            )
        # 2. trusted approval (missing / forged / expired / bound to something else -> refuse)
        binding = _profiles_binding(txn, ctx)
        appr_rec = appr.verify_approval(
            ctx.trusted_dir, binding, allow_test_approvals=ctx.policy.allow_test_approvals
        )
        # 3. exclusive workspace lock (cooperative), then the TOCTOU re-check
        try:
            with workspace_lock(ctx.state_dir, exclusive=True, blocking=False):
                txn = _commit_locked(ctx, db, store, txn, rec, fz, request_key, expected_patch_hash,
                                     crash_after_files, str(appr_rec["method"]))
        except WorkspaceBusy as e:
            raise SvwError(
                ErrorCode.WORKSPACE_UNSTABLE,
                "the workspace is locked by another SVWeave operation; retry",
                {"reason": "workspace_busy"},
            ) from e
        return _post_apply(opts, ctx, db, store, txn, request_id, replay=False)


def check_inputs_unchanged(
    ctx: WorkspaceContext, txn: Txn, tolerate: dict[str, str] | None = None
) -> None:
    """Toolchain, the resolved config of every validate profile and every relevant input equal
    what was validated (raises EDIT_CONFLICT otherwise). ``tolerate`` maps logical path ->
    after-hash: a file that already sits at that hash is this transaction's own write, not a
    foreign change (transaction-aware inventory for recovery, A3)."""
    tc_id, _, _ = compute_toolchain(ctx.project_root)
    if tc_id != txn.record.get("toolchain_id"):
        raise SvwError(
            ErrorCode.EDIT_CONFLICT, "the toolchain changed since validation",
            {"reason": "toolchain_changed"})
    # the resolved configuration of every validate profile is unchanged (F1)
    for prof, want in sorted(txn.record.get("config_ids", {}).items()):
        try:
            now_id = resolve_profile(ctx, prof).config_id
        except SvwError as e:
            raise SvwError(
                ErrorCode.EDIT_CONFLICT, "a validate profile no longer resolves",
                {"reason": "config_unresolvable", "profile": prof, "cause": e.code.value}) from None
        if now_id != want:
            raise SvwError(
                ErrorCode.EDIT_CONFLICT,
                "the configuration of a validate profile changed since validation; nothing "
                "was written",
                {"reason": "config_changed", "profile": prof})
    # every relevant input still equals what was validated: the base snapshot (sources, headers,
    # include/library search space) and the baseline snapshot of every validate profile
    sids = {txn.base_snapshot_id, *(
        v.get("baseline_snapshot_id") for v in txn.record.get("profile_builds", {}).values()
    )}
    own = tolerate or {}
    for sid in sorted(s for s in sids if s):
        r, _ = _base_rec(ctx, sid)
        stale = _stale(ctx, r)
        if stale is None:
            continue
        changed = stale.details.get("changed", [])
        foreign = [
            c for c in changed
            if not (
                c["change"] == "modified" and c["path"] in own
                and jr.current_sha(ctx.to_abs(c["path"])) == own[c["path"]]
            )
        ]
        if foreign or not changed:
            raise SvwError(
                ErrorCode.EDIT_CONFLICT,
                "the workspace changed since validation (a source or dependency was edited); "
                "nothing was written",
                {"reason": "workspace_changed", **stale.details, "changed": foreign or changed})


def _conflict(store: TxnStore, txn: Txn, err: SvwError) -> SvwError:
    return _fail(store, store.get(txn.transaction_id), S.CONFLICT, err)


def _commit_locked(
    ctx: WorkspaceContext,
    db: Database,
    store: TxnStore,
    txn: Txn,
    base_rec: SnapshotRecord,
    fz: Frozen,
    request_key: str,
    expected_patch_hash: str,
    crash_after_files: int | None,
    appr_method: str,
) -> Txn:
    jr.assert_no_recovery_pending(ctx, [t.transaction_id for t in store.blocking()])
    try:
        check_inputs_unchanged(ctx, txn)
    except SvwError as e:
        raise _conflict(store, txn, e) from None
    files = [
        jr.JournalFile(c.logical_path, c.before_sha256, c.after_sha256)
        for c in sorted(fz.changes, key=lambda c: c.logical_path)
    ]
    payloads = {c.logical_path: c.after_bytes for c in fz.changes}
    # per-file CAS precondition BEFORE the journal exists (nothing to recover yet)
    try:
        for jf in files:
            p = ctx.to_abs(jf.logical_path)
            jr._lstat_regular(p)
            if jr.current_sha(p) != jf.before_sha256:
                raise SvwError(
                    ErrorCode.EDIT_CONFLICT,
                    f"{jf.logical_path} changed since the base snapshot; refusing to overwrite",
                    {"reason": "expected_hash_mismatch", "path": jf.logical_path},
                )
            if not path_allowed(ctx, jf.logical_path):
                raise denied("path_not_allowed", "path not allowed", path=jf.logical_path)
    except SvwError as e:
        raise _conflict(store, txn, e) from None
    journal = jr.Journal(
        transaction_id=txn.transaction_id,
        base_snapshot_id=base_rec.snapshot_id,
        patch_hash=fz.patch_hash,
        created_at=now_iso(),
        files=files,
    )
    # everything the recovery path needs is recorded BEFORE the journal exists
    if txn.state is S.AWAITING_APPROVAL:
        txn = store.update(txn.transaction_id, to=S.APPROVED, note="approval verified at commit")
    txn = store.update(
        txn.transaction_id,
        lambda r: r.__setitem__("commit", {
            "request_key": request_key, "expected_patch_hash": expected_patch_hash,
            "journal": jr.journal_rel(txn.transaction_id), "result": None,
            "approval_method": appr_method,
        }),
    )
    jr.write_journal(ctx, journal)  # write-ahead: BEFORE the first source byte changes
    txn = store.update(txn.transaction_id, to=S.COMMITTING)
    try:
        jr.apply_files(ctx, journal, payloads, crash_after_files=crash_after_files)
    except jr.SimulatedCrash:
        raise  # a real crash cleans nothing up: journal in_progress, state COMMITTING
    except SvwError as e:
        done = sum(1 for f in journal.files if f.status == "applied")
        if done == 0:
            journal.status = jr.RESOLVED
            journal.resolution = {"outcome": "nothing_applied", "error": e.code.value}
            jr.write_journal(ctx, journal)
            store.update(txn.transaction_id, to=S.RECOVERY_REQUIRED, note="apply failed early")
            store.update(txn.transaction_id, to=S.CONFLICT, note="nothing applied")
            raise
        store.update(txn.transaction_id, to=S.RECOVERY_REQUIRED, note=e.code.value)
        raise SvwError(
            ErrorCode.RECOVERY_REQUIRED,
            f"commit stopped after {done}/{len(journal.files)} files ({e.code.value}); run "
            "`svweave edit recover`",
            {"transaction_id": txn.transaction_id, "cause": e.code.value, "applied": done},
        ) from e
    journal.status = jr.COMPLETE
    jr.write_journal(ctx, journal)
    return store.update(txn.transaction_id, to=S.APPLIED)


def _post_apply(
    opts: WorkspaceOptions,
    ctx: WorkspaceContext,
    db: Database,
    store: TxnStore,
    txn: Txn,
    request_id: str,
    *,
    replay: bool,
) -> Envelope:
    """Verify what was written, then (only then) publish and mark POST_APPLY_VERIFIED (F2).

    Order: bytes on disk == the validated after-hashes; re-snapshot == the validated candidate
    snapshot (no build is started before that identity check); the build of that snapshot must be
    COMPLETE + NO_ERRORS; only then is it published. Any failure leaves the transaction APPLIED
    with ``post_apply_status: FAILED`` - the source IS already written, so this is reported
    loudly and never as verified."""
    v = txn.record["validation"]
    expected = {f["logical_path"]: f["after_sha256"] for f in v["files"]}
    failures: list[dict[str, Any]] = []
    mismatches = [lp for lp, h in expected.items() if jr.current_sha(ctx.to_abs(lp)) != h]
    if mismatches:
        failures.append({"reason": "post_apply_bytes_differ", "paths": mismatches})
    pb = txn.record.get("profile_builds", {})
    verified: dict[str, Any] = {}
    if not mismatches:
        for prof in txn.record["profiles"]:
            nm = resolve_profile(ctx, prof)
            snap = capture_snapshot(ctx, nm)
            want = pb.get(prof, {})
            entry: dict[str, Any] = {
                "snapshot_id": snap.snapshot_id,
                "snapshot_equals_candidate": snap.snapshot_id == want.get("candidate_snapshot_id"),
                "config_id_unchanged": nm.config_id == txn.record.get("config_ids", {}).get(prof),
                "build_id": None,
            }
            verified[prof] = entry
            if not entry["snapshot_equals_candidate"] or not entry["config_id_unchanged"]:
                failures.append({"reason": "post_apply_identity_differs", "profile": prof})
                continue
            env = run_build_on_record(opts, prof, snap, request_id, publish=False)
            row = _build_facts(db, env.build_id) if env.build_id else {}
            entry.update(
                build_id=env.build_id,
                completeness=row.get("completeness"),
                diagnostic_outcome=row.get("diagnostic_outcome"),
            )
            if not row or not _is_clean(row):
                failures.append({
                    "reason": "post_apply_build_not_clean", "profile": prof,
                    "completeness": row.get("completeness"),
                    "diagnostic_outcome": row.get("diagnostic_outcome"),
                })
                continue
            old = want.get("baseline_build_id")
            db.publish_build(str(env.build_id))
            from svweave.application.semantic_common import freshness_of

            entry["old_build_id"] = old
            entry["old_build_freshness"] = (
                freshness_of(ctx, db, require_build(db, old)).value if old else None
            )
    if failures:
        def mut_fail(r: dict[str, Any]) -> None:
            r.setdefault("commit", {})["post_apply"] = {
                "status": "FAILED", "reasons": failures, "at": now_iso()}
        store.update(txn.transaction_id, mut_fail)
        identity = any(
            f["reason"] in ("post_apply_bytes_differ", "post_apply_identity_differs") for f in failures
        )
        raise SvwError(
            ErrorCode.EDIT_CONFLICT if identity else ErrorCode.PARTIAL_ANALYSIS,
            "THE SOURCE FILES WERE ALREADY WRITTEN but post-apply verification FAILED; the "
            "transaction stays APPLIED and is NOT verified",
            {"state": txn.state.value, "post_apply_status": "FAILED", "reasons": failures,
             "transaction_id": txn.transaction_id},
        )
    result = {
        "patch_hash": txn.patch_hash,
        "files_changed": v["files"],
        "static_diagnostics_delta": {p["profile"]: p.get("diagnostics_delta") for p in v["profiles"]},
        "post_apply": verified,
        "post_apply_status": "VERIFIED",
        "approval_method": (txn.record.get("commit") or {}).get("approval_method"),
        "approval_qualified": (txn.record.get("commit") or {}).get("approval_method") == "tty",
        "edit_paths": sorted({e["edit_path"] for e in txn.record["edits"]}),
        "not_run": NOT_RUN,
        "verified_at": now_iso(),
    }
    if txn.state is S.APPLIED:
        def mut(r: dict[str, Any]) -> None:
            r["commit"]["result"] = result
            r["commit"]["post_apply"] = {"status": "VERIFIED", "at": result["verified_at"]}
        txn = store.update(txn.transaction_id, mut, to=S.POST_APPLY_VERIFIED)
        remove_candidate(candidate_dir(ctx, txn.transaction_id))
    return _commit_envelope(ctx, request_id, txn, result, replay=replay)


def _commit_envelope(
    ctx: WorkspaceContext, request_id: str, txn: Txn, result: dict[str, Any], *, replay: bool
) -> Envelope:
    return _env(
        ctx, request_id, txn, {**result, "idempotent_replay": replay},
        freshness=Freshness.CURRENT,
        warnings=["applied after trusted approval; EDA compile / simulation / formal are NOT_RUN"],
    )


# ------------------------------------------------------------------------------ abort
def abort(opts: WorkspaceOptions, request_id: str, *, transaction_id: str) -> Envelope:
    """Clean the candidate, keep the audit record. Never touches the workspace."""
    _require_id(transaction_id)
    with _session(opts) as (ctx, db, store):
        try:
            with workspace_lock(ctx.state_dir, exclusive=True, blocking=False):
                return _abort_locked(ctx, store, request_id, transaction_id)
        except WorkspaceBusy as e:
            raise SvwError(
                ErrorCode.EDIT_CONFLICT,
                "a commit (or another workspace operation) holds the workspace lock; abort refused",
                {"reason": "workspace_busy", "transaction_id": transaction_id},
            ) from e


def _abort_locked(
    ctx: WorkspaceContext, store: TxnStore, request_id: str, transaction_id: str
) -> Envelope:
    txn = store.get(transaction_id)
    if (
        txn.state in (S.COMMITTING, S.RECOVERY_REQUIRED, S.APPLIED)
        or transaction_id in jr.unfinished_journals(ctx)
    ):
        raise SvwError(
            ErrorCode.EDIT_CONFLICT,
            "the transaction is being committed, was interrupted or is already applied; abort is "
            "refused (use `svweave edit recover` for an interrupted commit)",
            {"reason": "commit_in_progress_or_applied", "state": txn.state.value,
             "transaction_id": transaction_id},
        )
    note = "candidate removed; workspace untouched"
    if S.ABORTED in _legal(txn.state):
        txn = store.update(transaction_id, to=S.ABORTED, note="aborted by request")
    elif txn.state is S.POST_APPLY_VERIFIED:
        note = "already applied: applied changes are NOT reverted; only the candidate was removed"
    remove_candidate(candidate_dir(ctx, transaction_id))
    return _env(ctx, request_id, txn, {"note": note},
                warnings=[note] if "NOT reverted" in note else [])


def show(opts: WorkspaceOptions, request_id: str, *, transaction_id: str) -> Envelope:
    _require_id(transaction_id)
    with _session(opts) as (ctx, _db, store):
        txn = store.get(transaction_id)
        return _env(ctx, request_id, txn, {"audit": txn.record.get("audit", [])})


# ------------------------------------------------------------------------------ recover
def recover(
    opts: WorkspaceOptions,
    request_id: str,
    *,
    transaction_id: str,
    mode: str | None = None,
) -> Envelope:
    """Resolve an interrupted commit (plan 12.5, EDT-07/08). ``mode`` is None (classify; clear the
    block only if every file is consistent), ``roll_forward``, ``rollback`` or ``accept_current``.
    Bytes are only ever replaced when they still equal what this transaction expects."""
    _require_id(transaction_id)
    if mode not in (None, "roll_forward", "rollback", "accept_current"):
        raise SvwError(ErrorCode.INVALID_CONFIG, "bad recover mode", {"mode": str(mode)})
    with _session(opts) as (ctx, db, store):
        txn = store.get(transaction_id)
        j = jr.read_journal(ctx, transaction_id)
        if j is None:
            raise SvwError(ErrorCode.MISSING_INPUT, "no journal for this transaction",
                           {"reason": "journal_missing"})
        if txn.state not in (S.COMMITTING, S.RECOVERY_REQUIRED, S.APPROVED) and (
            j.status != jr.IN_PROGRESS
        ):
            return _env(ctx, request_id, txn, {"recovery": {"needed": False,
                                                             "journal_status": j.status}})
        try:
            with workspace_lock(ctx.state_dir, exclusive=True, blocking=False):
                env, txn = _recover_locked(ctx, store, txn, j, request_id, mode)
        except WorkspaceBusy as e:
            raise SvwError(ErrorCode.WORKSPACE_UNSTABLE, "workspace is locked; retry",
                           {"reason": "workspace_busy"}) from e
        if txn.state is S.APPLIED and env.data["recovery"]["block_cleared"]:
            # rolled forward / found fully applied: finish with post-apply verification (F3)
            post = _post_apply(opts, ctx, db, store, txn, request_id, replay=False)
            post.data["recovery"] = env.data["recovery"]
            return post
        return env


def _candidate_bytes(ctx: WorkspaceContext, txn_id: str, logical: str, want: str) -> bytes | None:
    from svweave.domain.ids import sha256_hex
    from svweave.security import safeio

    try:  # the candidate tree is backend-writable: no link in ANY component may be followed
        data = safeio.read_regular_under(candidate_dir(ctx, txn_id), snapshot_rel(logical))
    except (OSError, SvwError):
        return None
    return data if sha256_hex(data) == want else None


def _before_bytes(ctx: WorkspaceContext, j: jr.Journal, logical: str, want: str) -> bytes | None:
    from svweave.domain.ids import sha256_hex
    from svweave.security import safeio

    p = snapshots_dir(ctx) / j.base_snapshot_id / "files" / snapshot_rel(logical)
    try:
        data = safeio.read_regular(p)
    except (OSError, SvwError):
        return None
    return data if sha256_hex(data) == want else None


def _write_one(ctx: WorkspaceContext, logical: str, data: bytes, expect_current: str) -> None:
    import os
    import stat as st

    from svweave.security import safeio

    path = ctx.to_abs(logical)
    if jr.current_sha(path) != expect_current:
        raise SvwError(ErrorCode.EDIT_CONFLICT, "file changed during recovery",
                       {"reason": "changed_during_recovery", "path": logical})
    mode = st.S_IMODE(os.lstat(path).st_mode)
    jr._lstat_regular(path)
    inside = ctx.project_root in path.parents
    base = ctx.project_root if inside else path.parent
    name = path.relative_to(ctx.project_root).as_posix() if inside else path.name
    safeio.write_under(base, name, data, mode, durable=True)


def _roll_forward_gates(ctx: WorkspaceContext, txn: Txn, j: jr.Journal) -> None:
    """Everything a normal commit re-checks, before recovery writes a single candidate byte (A3):
    editing permission, trusted location, current policy, a valid approval bound to the live
    policy / profiles / configs, unchanged toolchain / config / dependencies (files already at
    their after-hash are this transaction's own writes) and a candidate that still equals the
    approved patch."""
    require_editing_enabled(ctx)
    _check_trusted_location(ctx)
    if txn.patch_hash is None or txn.base_snapshot_id is None or j.patch_hash != txn.patch_hash:
        raise SvwError(ErrorCode.APPROVAL_REQUIRED, "journal and transaction disagree on the patch",
                       {"reason": "journal_patch_mismatch"})
    recheck_edit_policy(ctx, [f.logical_path for f in j.files],
                        list(txn.record.get("profiles", [])))
    # the approval must have been valid when the commit began (the journal is written only after
    # the approval check); every binding field is checked against the LIVE state
    began = datetime.fromisoformat(j.created_at)
    appr.verify_approval(
        ctx.trusted_dir, _profiles_binding(txn, ctx),
        allow_test_approvals=ctx.policy.allow_test_approvals, now=began,
    )
    check_inputs_unchanged(ctx, txn, {f.logical_path: f.after_sha256 for f in j.files})
    _, rec_dir = _base_rec(ctx, txn.base_snapshot_id)
    fz = freeze_candidate(ctx, txn.record, rec_dir, candidate_dir(ctx, txn.transaction_id),
                          txn.base_snapshot_id)
    after = {c.logical_path: c.after_sha256 for c in fz.changes}
    if (
        fz.failures
        or fz.patch_hash != txn.patch_hash
        or after != {f.logical_path: f.after_sha256 for f in j.files}
    ):
        raise SvwError(
            ErrorCode.APPROVAL_REQUIRED,
            "the candidate no longer equals the approved patch; roll-forward refused (EDT-04)",
            {"reason": "candidate_changed_after_validation"},
        )


def _recover_locked(
    ctx: WorkspaceContext,
    store: TxnStore,
    txn: Txn,
    j: jr.Journal,
    request_id: str,
    mode: str | None,
) -> tuple[Envelope, Txn]:
    if txn.state not in (S.COMMITTING, S.RECOVERY_REQUIRED, S.APPROVED):
        # the state already moved on but the journal was left unfinished by a crash in between:
        # close the journal only; the transaction state is authoritative
        j.status = jr.RESOLVED
        j.resolution = {"outcome": "state_already_final", "state": txn.state.value}
        jr.write_journal(ctx, j)
        return _env(ctx, request_id, txn, {"recovery": {
            "block_cleared": True, "mode": mode, "files": [], "notes": [
                f"journal closed; transaction already {txn.state.value}"], "next": None}}), txn
    verdicts = jr.classify(ctx, j)
    applied = [v for v in verdicts if v.verdict == "applied"]
    not_applied = [v for v in verdicts if v.verdict == "not_applied"]
    conflicts = [v for v in verdicts if v.verdict == "conflict"]
    # the human's version is NEVER overwritten: keep the candidate version next to it
    for v in conflicts + (not_applied if mode == "accept_current" else []):
        data = _candidate_bytes(ctx, txn.transaction_id, v.logical_path, v.after_sha256)
        if data is not None:
            v.candidate_copy = jr.preserve_candidate(ctx, txn.transaction_id, v.logical_path, data)
        v.action = "kept_current_version"
    if txn.state in (S.COMMITTING, S.APPROVED):
        # APPROVED + journal = a crash between the journal write and COMMITTING (COMMITTING-
        # equivalent); the state is moved BEFORE any file is touched
        txn = store.update(txn.transaction_id, to=S.RECOVERY_REQUIRED, note="recovery started")
    resolution: dict[str, Any] | None = None
    new_state: S | None = None
    notes: list[str] = []

    if mode == "roll_forward":
        if conflicts:
            notes.append("roll_forward refused: conflicts exist (the human's changes are kept)")
        else:
            _roll_forward_gates(ctx, txn, j)  # A3: same gates as a normal commit
            for v in not_applied:
                data = _candidate_bytes(ctx, txn.transaction_id, v.logical_path, v.after_sha256)
                if data is None:
                    raise SvwError(ErrorCode.MISSING_INPUT, "candidate bytes unavailable",
                                   {"reason": "candidate_missing", "path": v.logical_path})
                _write_one(ctx, v.logical_path, data, v.before_sha256)
                v.verdict, v.action = "applied", "rolled_forward"
            resolution, new_state = {"outcome": "rolled_forward"}, S.APPLIED
    elif mode == "rollback":
        if conflicts:
            notes.append("rollback refused: conflicts exist (the human's changes are kept)")
        else:
            for v in applied:
                data = _before_bytes(ctx, j, v.logical_path, v.before_sha256)
                if data is None:
                    raise SvwError(ErrorCode.MISSING_INPUT, "original bytes unavailable",
                                   {"reason": "base_missing", "path": v.logical_path})
                _write_one(ctx, v.logical_path, data, v.after_sha256)  # CAS: only if == after
                v.verdict, v.action = "not_applied", "rolled_back"
            resolution, new_state = {"outcome": "rolled_back"}, S.ABORTED
    elif mode == "accept_current":
        resolution = {"outcome": "accepted_current",
                      "files": {v.logical_path: v.verdict for v in verdicts}}
        new_state = S.CONFLICT
    else:
        if not conflicts and not not_applied:
            resolution, new_state = {"outcome": "all_applied"}, S.APPLIED
        elif not conflicts and not applied:
            resolution, new_state = {"outcome": "none_applied"}, S.ABORTED
        else:
            notes.append(
                "files are in a mixed or conflicting state: choose --roll-forward (no conflicts), "
                "--rollback (no conflicts) or --accept-current (keep what is on disk)"
            )
    cleared = resolution is not None
    if cleared:
        assert new_state is not None
        txn = store.update(txn.transaction_id, to=new_state, note=f"recovery {resolution}")
        j.status = jr.RESOLVED  # only after the state update succeeded
        j.resolution = resolution
        jr.write_journal(ctx, j)
    return _env(
        ctx, request_id, txn,
        {"recovery": {
            "block_cleared": cleared,
            "mode": mode,
            "files": [v.to_json() for v in verdicts],
            "notes": notes,
            "next": "svweave edit commit (same request key) finishes post-apply verification"
            if new_state is S.APPLIED else None,
        }},
        outcome=Outcome.OK if cleared else Outcome.PARTIAL,
        error=None if cleared else ErrorInfo(
            code=ErrorCode.RECOVERY_REQUIRED.value,
            message="recovery is not complete: new edits stay blocked",
            details={"files": [v.to_json() for v in verdicts], "notes": notes},
        ),
    ), txn
