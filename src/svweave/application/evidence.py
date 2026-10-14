"""Evidence store: import of external evidence manifests and read access (plan 13.3, 16.2, 16.3).

Rules enforced here (tests UVM-04, REL-01, SVA-03):

* an imported record is checked against ``schemas/evidence.schema.json`` (the pydantic model),
  artifact paths must lie inside the trusted policy roots and their sha256 must match;
* ``trust_class`` is forced to ``EXTERNAL_DECLARED`` unless the producer is a trusted runner (the
  set is empty in v1); a hash only proves the record is unchanged, never that it is true;
* a record is attached to a build only when snapshot/build/profile can be shown to correspond;
  otherwise it is stored as ``unlinked`` and is never reported as a result of any build;
* ``evidence_id`` is the content hash of the immutable record;
* static analysis evidence (``HDL_STATIC_RESOLVED``) is recorded automatically per build; EDA
  compile, simulation, formal and SVA execution stay ``NOT_RUN`` unless imported evidence exists,
  and results of different profiles / seeds / runs are listed separately, never merged.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from svweave.application.build import DB_FILENAME
from svweave.application.keys import cursor_key
from svweave.application.options import WorkspaceOptions
from svweave.application.queries import fit_items, flag_oversize
from svweave.application.semantic_common import build_envelope, require_build
from svweave.config.context import WorkspaceContext
from svweave.domain.cursor import (
    MAX_RESPONSE_BYTES,
    check_limit,
    decode_cursor,
    encode_cursor,
    query_hash,
)
from svweave.domain.envelope import (
    AnalysisCoverage,
    Envelope,
    Outcome,
    Pagination,
    Provenance,
)
from svweave.domain.errors import ErrorCode, SvwError
from svweave.domain.ids import canonical_hash, is_sha256_hex
from svweave.domain.states import Completeness, EvidenceKind, Freshness
from svweave.storage.db import Database

HEX64 = r"^[0-9a-f]{64}$"
MAX_MANIFEST_BYTES = 1 << 20
MAX_RECORDS = 200
EVIDENCE_TYPES = ("static", "eda_compile", "simulation", "formal", "coverage")
IMPORT_TYPES = ("eda_compile", "simulation", "formal", "coverage")
#: Producers whose records may keep a RUNTIME_OBSERVED trust class. None exist in v1.
TRUSTED_RUNNERS: frozenset[str] = frozenset()
STATIC_PRODUCER = "svweave.slang"

LIMITATIONS = [
    "An evidence hash proves the record is unchanged, not that its content is true.",
    "Results of different profiles, seeds and runs are listed separately and never merged.",
]

_PATH_SEP = "/"


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Artifact(_Strict):
    path: str = Field(min_length=1, max_length=1024)
    sha256: str = Field(pattern=HEX64)


class EvidenceManifest(_Strict):
    """External evidence record (plan 16.2)."""

    schema_version: Literal["1.0"]
    kind: Literal["eda_compile", "simulation", "formal", "coverage"]
    producer: str = Field(min_length=1, max_length=128)
    trust_class: Literal["EXTERNAL_DECLARED", "RUNTIME_OBSERVED"] = "EXTERNAL_DECLARED"
    profile: str = Field(min_length=1, max_length=64)
    snapshot_id: str | None = Field(default=None, pattern=HEX64)
    build_id: str | None = Field(default=None, pattern=HEX64)
    test_name: str | None = Field(default=None, max_length=256)
    seed: str | None = Field(default=None, max_length=64)
    run_id: str | None = Field(default=None, max_length=128)
    tool_version: str | None = Field(default=None, max_length=128)
    result: Literal["NOT_RUN", "BLOCKED", "PASS", "FAIL"]
    artifacts: list[Artifact] = Field(default_factory=list, max_length=1000)


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def _open_db(ctx: WorkspaceContext) -> Database:
    return Database(ctx.state_dir / DB_FILENAME)


# ----------------------------------------------------------------------------------- import
def _sha256_of(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


MAX_ARTIFACT_BYTES = 64 * 1024  # total JSON size of one record's stored artifact list


def _check_artifacts(
    ctx: WorkspaceContext, rec: EvidenceManifest, base: Path
) -> list[dict[str, str]]:
    out: list[dict[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for a in rec.artifacts:
        lexical, real = ctx.path_policy.check(a.path, base)  # PATH_NOT_ALLOWED on escape
        if (str(lexical), a.sha256) in seen:  # the same artifact listed twice is stored once
            continue
        seen.add((str(lexical), a.sha256))
        if not real.is_file():
            raise SvwError(
                ErrorCode.MISSING_INPUT,
                f"artifact not found: {a.path}",
                {"reason": "artifact_missing", "path": a.path},
            )
        got = _sha256_of(real)
        if got != a.sha256:
            raise SvwError(
                ErrorCode.INVALID_CONFIG,
                f"artifact sha256 mismatch for {a.path}",
                {
                    "reason": "artifact_hash_mismatch",
                    "path": a.path,
                    "expected": a.sha256,
                    "actual": got,
                },
            )
        try:
            shown = lexical.relative_to(ctx.project_root).as_posix()
        except ValueError:
            shown = str(lexical)
        out.append({"path": shown, "sha256": a.sha256})
    if len(json.dumps(out)) > MAX_ARTIFACT_BYTES:
        raise SvwError(
            ErrorCode.INVALID_CONFIG,
            "artifact list of one evidence record is too large",
            {"reason": "artifacts_too_large", "max_bytes": MAX_ARTIFACT_BYTES, "count": len(out)},
        )
    return out


def _effective_record(rec: EvidenceManifest, artifacts: list[dict[str, str]]) -> dict[str, Any]:
    trust = (
        rec.trust_class
        if rec.producer in TRUSTED_RUNNERS and rec.trust_class == "RUNTIME_OBSERVED"
        else EvidenceKind.EXTERNAL_DECLARED.value
    )
    return {
        "schema_version": rec.schema_version,
        "evidence_type": rec.kind,
        "producer": rec.producer,
        "trust_class": trust,
        "profile": rec.profile,
        "snapshot_id": rec.snapshot_id,
        "build_id": rec.build_id,
        "test_name": rec.test_name,
        "seed": rec.seed,
        "run_id": rec.run_id,
        "tool_version": rec.tool_version,
        "result": rec.result,
        "artifacts": artifacts,
    }


def evidence_id_of(record: dict[str, Any]) -> str:
    return canonical_hash({"kind": "evidence", "record": record})


def _link(db: Database, rec: dict[str, Any]) -> tuple[str, str | None, str | None, str | None]:
    """-> (link_state, reason, build_id, snapshot_id). Only provable correspondence links."""
    bid, sid = rec["build_id"], rec["snapshot_id"]
    if bid:
        b = db.conn.execute("SELECT * FROM builds WHERE build_id=?", (bid,)).fetchone()
        if b is None:
            return "unlinked", "build_not_found", None, None
        if sid and sid != b["snapshot_id"]:
            return "unlinked", "snapshot_does_not_match_build", None, None
        if rec["profile"] != b["profile"]:
            return "unlinked", "profile_does_not_match_build", None, None
        return "linked_build", None, b["build_id"], b["snapshot_id"]
    if sid:
        s = db.conn.execute(
            "SELECT snapshot_id FROM snapshots WHERE snapshot_id=?", (sid,)
        ).fetchone()
        if s is None:
            return "unlinked", "snapshot_not_found", None, None
        return "linked_snapshot", None, None, sid
    return "unlinked", "no_source_linkage", None, None


def import_manifest(opts: WorkspaceOptions, manifest_file: Path, request_id: str) -> Envelope:
    ctx = opts.load()
    path = Path(manifest_file)
    try:
        raw = path.read_bytes()
    except OSError as e:
        raise SvwError(
            ErrorCode.MISSING_INPUT,
            f"cannot read evidence manifest: {e.strerror}",
            {"reason": "manifest_unreadable"},
        ) from e
    if len(raw) > MAX_MANIFEST_BYTES:
        raise SvwError(
            ErrorCode.INVALID_CONFIG,
            "evidence manifest too large",
            {"max_bytes": MAX_MANIFEST_BYTES},
        )
    try:
        doc = json.loads(raw)
    except ValueError as e:
        raise SvwError(
            ErrorCode.INVALID_CONFIG, "evidence manifest is not valid JSON", {"reason": "bad_json"}
        ) from e
    items = doc if isinstance(doc, list) else [doc]
    if not items or len(items) > MAX_RECORDS:
        raise SvwError(ErrorCode.INVALID_CONFIG, f"expected 1..{MAX_RECORDS} evidence records")
    parsed: list[EvidenceManifest] = []
    for n, item in enumerate(items):
        try:
            parsed.append(EvidenceManifest.model_validate(item))
        except ValidationError as e:
            raise SvwError(
                ErrorCode.INVALID_CONFIG,
                f"evidence record {n} violates the evidence schema",
                {
                    "record": n,
                    "errors": [
                        {"loc": ".".join(str(x) for x in err["loc"]), "type": err["type"]}
                        for err in e.errors()[:20]
                    ],
                },
            ) from e
    effective: list[dict[str, Any]] = []
    warnings: list[str] = []
    for n, rec in enumerate(parsed):
        arts = _check_artifacts(ctx, rec, path.resolve().parent)
        eff = _effective_record(rec, arts)
        if eff["trust_class"] != rec.trust_class:
            warnings.append(
                f"record {n}: trust_class {rec.trust_class} is not available to producer "
                f"{rec.producer!r}; stored as EXTERNAL_DECLARED"
            )
        effective.append(eff)

    results: list[dict[str, Any]] = []
    with _open_db(ctx) as db, db.transaction() as c:
        for n, eff in enumerate(effective):
            eid = evidence_id_of(eff)
            state, reason, bid, sid = _link(db, eff)
            existing = c.execute(
                "SELECT link_state FROM evidence WHERE evidence_id=?", (eid,)
            ).fetchone()
            status = "imported"
            if existing is not None:
                status = "already_present"
                if existing["link_state"] == "unlinked" and state != "unlinked":
                    c.execute(
                        "UPDATE evidence SET build_id=?, snapshot_id=?, link_state=?, link_reason=? "
                        "WHERE evidence_id=?",
                        (bid, sid, state, reason, eid),
                    )
                    status = "relinked"
            else:
                c.execute(
                    "INSERT INTO evidence(evidence_id, kind, producer, build_id, snapshot_id, "
                    "run_identity, artifacts, trust_class, outcome, evidence_type, profile, "
                    "test_name, seed, run_id, tool_version, link_state, link_reason, "
                    "claimed_snapshot_id, claimed_build_id, record, imported_at) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        eid,
                        eff["trust_class"],
                        eff["producer"],
                        bid,
                        sid,
                        eff["run_id"],
                        json.dumps(eff["artifacts"]),
                        eff["trust_class"],
                        eff["result"],
                        eff["evidence_type"],
                        eff["profile"],
                        eff["test_name"],
                        eff["seed"],
                        eff["run_id"],
                        eff["tool_version"],
                        state,
                        reason,
                        eff["snapshot_id"],
                        eff["build_id"],
                        json.dumps(eff, sort_keys=True),
                        _now(),
                    ),
                )
            if state == "unlinked":
                warnings.append(
                    f"record {n} ({eid[:12]}) is stored UNLINKED ({reason}); it is not attached "
                    "to any build and is never shown as a result of the current build"
                )
            results.append(
                {
                    "evidence_id": eid,
                    "status": status,
                    "link_state": state,
                    "link_reason": reason,
                    "trust_class": eff["trust_class"],
                    "result": eff["result"],
                    "evidence_type": eff["evidence_type"],
                    "build_id": bid,
                    "snapshot_id": sid,
                }
            )
    return Envelope(
        request_id=request_id,
        outcome=Outcome.OK,
        workspace_id=ctx.workspace_id,
        freshness=Freshness.UNKNOWN,
        analysis_coverage=AnalysisCoverage(
            completeness=Completeness.COMPLETE,
            scope="evidence_import",
            basis="schema_hash_path_link_checks",
            limitations=list(LIMITATIONS),
        ),
        provenance=Provenance(
            backend="evidence_store", evidence_kind=EvidenceKind.EXTERNAL_DECLARED
        ),
        data={
            "records": results,
            "unlinked": sum(1 for r in results if r["link_state"] == "unlinked"),
        },
        warnings=warnings,
    )


# ----------------------------------------------------------------------------------- static
def static_record(build: sqlite3.Row) -> tuple[str, dict[str, Any]]:
    rec = {
        "schema_version": "1.0",
        "evidence_type": "static",
        "producer": STATIC_PRODUCER,
        "trust_class": EvidenceKind.HDL_STATIC_RESOLVED.value,
        "profile": build["profile"],
        "snapshot_id": build["snapshot_id"],
        "build_id": build["build_id"],
        "toolchain_id": build["toolchain_id"],
        "completeness": build["completeness"],
        "diagnostic_outcome": build["diagnostic_outcome"],
        "result": None,
        "artifacts": [],
    }
    return evidence_id_of(rec), rec


def ensure_static(db: Database, build: sqlite3.Row) -> str:
    """Record (idempotently) the HDL_STATIC_RESOLVED evidence of a build; returns its id."""
    eid, rec = static_record(build)
    with db.transaction() as c:
        c.execute(
            "INSERT OR IGNORE INTO evidence(evidence_id, kind, producer, build_id, snapshot_id, "
            "run_identity, artifacts, trust_class, outcome, evidence_type, profile, link_state, "
            "record, imported_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                eid,
                EvidenceKind.HDL_STATIC_RESOLVED.value,
                STATIC_PRODUCER,
                build["build_id"],
                build["snapshot_id"],
                None,
                "[]",
                EvidenceKind.HDL_STATIC_RESOLVED.value,
                build["diagnostic_outcome"],
                "static",
                build["profile"],
                "linked_build",
                json.dumps(rec, sort_keys=True),
                _now(),
            ),
        )
    return eid


def record_static_for(opts: WorkspaceOptions, build_id: str | None) -> None:
    """Best-effort hook after a build (never fails the build)."""
    if not build_id or not is_sha256_hex(build_id):
        return
    try:
        ctx = opts.load()
        with _open_db(ctx) as db:
            row = db.conn.execute("SELECT * FROM builds WHERE build_id=?", (build_id,)).fetchone()
            if row is not None:
                ensure_static(db, row)
    except (SvwError, sqlite3.Error):
        return


# ----------------------------------------------------------------------------------- read
def _dto(r: sqlite3.Row) -> dict[str, Any]:
    return {
        "evidence_id": r["evidence_id"],
        "evidence_type": r["evidence_type"],
        "evidence_kind": r["kind"],
        "trust_class": r["trust_class"],
        "producer": r["producer"],
        "profile": r["profile"],
        "build_id": r["build_id"],
        "snapshot_id": r["snapshot_id"],
        "test_name": r["test_name"],
        "seed": r["seed"],
        "run_id": r["run_id"],
        "tool_version": r["tool_version"],
        "result": r["outcome"] if r["evidence_type"] != "static" else None,
        "diagnostic_outcome": r["outcome"] if r["evidence_type"] == "static" else None,
        "link_state": r["link_state"],
        "link_reason": r["link_reason"],
        "artifacts": json.loads(r["artifacts"] or "[]"),
        "imported_at": r["imported_at"],
    }


def _ext_state(rows: list[sqlite3.Row], etypes: tuple[str, ...]) -> dict[str, Any]:
    sel = [r for r in rows if r["evidence_type"] in etypes]
    if not sel:
        return {"state": "NOT_RUN", "records": 0, "by_result": {}, "runs": []}
    by: dict[str, int] = {}
    for r in sel:
        by[r["outcome"]] = by.get(r["outcome"], 0) + 1
    return {
        "state": "EXTERNAL_DECLARED",
        "records": len(sel),
        "by_result": by,
        "runs": [
            {k: r[k] for k in ("evidence_id", "profile", "test_name", "seed", "run_id")}
            | {"result": r["outcome"], "evidence_type": r["evidence_type"]}
            for r in sel[:50]
        ],
    }


def states_for_build(db: Database, build: sqlite3.Row) -> dict[str, Any]:
    """Static / EDA / simulation / formal / SVA states, kept apart (REL-01, SVA-03)."""
    rows = db.conn.execute(
        "SELECT * FROM evidence WHERE build_id=? AND link_state='linked_build' "
        "ORDER BY imported_at, evidence_id",
        (build["build_id"],),
    ).fetchall()
    sva = _ext_state(rows, ("simulation", "formal"))
    return {
        "static_analysis": {
            "state": "HDL_STATIC_RESOLVED",
            "completeness": build["completeness"],
            "diagnostic_outcome": build["diagnostic_outcome"],
        },
        "eda_compile": _ext_state(rows, ("eda_compile",)),
        "simulation": _ext_state(rows, ("simulation",)),
        "formal": _ext_state(rows, ("formal",)),
        "coverage": _ext_state(rows, ("coverage",)),
        "sva_execution": {
            **sva,
            "note": "SVWeave statically lists assertions only; execution/proof status exists "
            "only as externally declared evidence.",
        },
    }


def unlinked_count(db: Database) -> int:
    return int(
        db.conn.execute("SELECT COUNT(*) FROM evidence WHERE link_state='unlinked'").fetchone()[0]
    )


def get_evidence(
    opts: WorkspaceOptions,
    request_id: str,
    *,
    build_id: str | None = None,
    evidence_id: str | None = None,
    evidence_type: str | None = None,
    limit: int | None = None,
    cursor: str | None = None,
) -> Envelope:
    if (build_id is None) == (evidence_id is None):
        raise SvwError(ErrorCode.INVALID_CONFIG, "give exactly one of build_id / evidence_id")
    if evidence_type is not None and evidence_type not in EVIDENCE_TYPES:
        raise SvwError(ErrorCode.INVALID_CONFIG, f"type must be one of {list(EVIDENCE_TYPES)}")
    ctx = opts.load()
    with _open_db(ctx) as db:
        if evidence_id is not None:
            if not is_sha256_hex(evidence_id):
                raise SvwError(ErrorCode.INVALID_CONFIG, "evidence_id must be 64 hex chars")
            r = db.conn.execute(
                "SELECT * FROM evidence WHERE evidence_id=?", (evidence_id,)
            ).fetchone()
            if r is None:
                raise SvwError(
                    ErrorCode.MISSING_INPUT,
                    f"unknown evidence {evidence_id}",
                    {"reason": "evidence_missing"},
                )
            skel = _envelope(
                ctx, request_id, r["build_id"], r["snapshot_id"], r["profile"],
                {"items": [], "total_matches": 1}, Pagination(),
            )
            budget = MAX_RESPONSE_BYTES - len(skel.to_json(indent=None).encode("utf-8")) - 1024
            return flag_oversize(
                _envelope(
                    ctx,
                    request_id,
                    r["build_id"],
                    r["snapshot_id"],
                    r["profile"],
                    {"items": fit_items([_dto(r)], budget), "total_matches": 1},
                    Pagination(),
                )
            )
        assert build_id is not None
        build = require_build(db, build_id)
        lim = check_limit(limit)
        key = cursor_key(ctx)
        qh = query_hash("svw_evidence", {"type": evidence_type})
        offset = decode_cursor(key, cursor, build_id, qh, "imported") if cursor else 0
        where, args = "build_id=? AND link_state='linked_build'", [build_id]
        if evidence_type:
            where += " AND evidence_type=?"
            args.append(evidence_type)
        rows = db.conn.execute(
            f"SELECT * FROM evidence WHERE {where} ORDER BY imported_at, evidence_id", args
        ).fetchall()
        all_items = [_dto(r) for r in rows]
        if evidence_type in (None, "static") and not any(
            d["evidence_type"] == "static" for d in all_items
        ):  # read tools never write: show the derivable static record without storing it
            eid, rec = static_record(build)
            all_items.insert(
                0,
                {
                    "evidence_id": eid,
                    "evidence_type": "static",
                    "evidence_kind": EvidenceKind.HDL_STATIC_RESOLVED.value,
                    "trust_class": EvidenceKind.HDL_STATIC_RESOLVED.value,
                    "producer": STATIC_PRODUCER,
                    "profile": build["profile"],
                    "build_id": build_id,
                    "snapshot_id": build["snapshot_id"],
                    "test_name": None,
                    "seed": None,
                    "run_id": None,
                    "tool_version": None,
                    "result": None,
                    "diagnostic_outcome": build["diagnostic_outcome"],
                    "link_state": "linked_build",
                    "link_reason": None,
                    "artifacts": [],
                    "imported_at": None,
                },
            )
        total = len(all_items)
        states = states_for_build(db, build)
        skeleton = build_envelope(
            ctx, db, build, request_id,
            {"items": [], "total_matches": total, "states": states,
             "unlinked_records_in_store": unlinked_count(db)},
            pagination=Pagination(limit=lim, next_cursor="x" * 120, truncated=True),
            extra_limits=LIMITATIONS,
        )
        budget = MAX_RESPONSE_BYTES - len(skeleton.to_json(indent=None).encode("utf-8")) - 1024
        items = fit_items(all_items[offset : offset + lim], budget)
        new_off = offset + len(items)
        more = new_off < total
        pag = Pagination(
            limit=lim,
            truncated=more,
            next_cursor=encode_cursor(key, build_id, qh, "imported", new_off) if more else None,
        )
        data = {
            "items": items,
            "total_matches": total,
            "states": states,
            "unlinked_records_in_store": unlinked_count(db),
        }
        env = build_envelope(
            ctx, db, build, request_id, data, pagination=pag, extra_limits=LIMITATIONS
        )
        env.provenance = Provenance(backend="evidence_store", evidence_kind=None)
        return flag_oversize(env)


def _envelope(
    ctx: WorkspaceContext,
    rid: str,
    build_id: str | None,
    snapshot_id: str | None,
    profile: str | None,
    data: dict[str, Any],
    pag: Pagination,
) -> Envelope:
    return Envelope(
        request_id=rid,
        outcome=Outcome.OK,
        workspace_id=ctx.workspace_id,
        snapshot_id=snapshot_id,
        build_id=build_id,
        profile=profile,
        freshness=Freshness.UNKNOWN,
        analysis_coverage=AnalysisCoverage(
            completeness=Completeness.COMPLETE,
            scope="evidence_record",
            basis="evidence_store",
            limitations=list(LIMITATIONS),
        ),
        provenance=Provenance(backend="evidence_store", evidence_kind=None),
        data=data,
        pagination=pag,
    )
