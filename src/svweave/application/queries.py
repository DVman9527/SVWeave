"""Semantic query services (plan 14.3). Every result is the common envelope (plan 14.5):

* bound to an immutable ``build_id`` (+ its snapshot, profile, toolchain);
* ``freshness`` is computed against the workspace by the M1 snapshot check;
* ``analysis_coverage`` carries completeness, scope, basis, unresolved count and limitations,
  so a partial or stale answer is never silently presented as complete;
* ``provenance`` is ``HDL_STATIC_RESOLVED`` from slang;
* pagination: limit default 100 / max 500, opaque cursor bound to build + query + sort +
  offset, and a 128 KiB response cap that truncates (``truncated=true`` + cursor) instead of
  producing invalid JSON.

Positions: spans are zero-based half-open byte offsets into the ORIGINAL file bytes;
``line``/``col`` are 1-based display values with ``position_encoding`` "utf-8" (byte columns).
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from svweave.application.build import DB_FILENAME
from svweave.application.keys import cursor_key
from svweave.application.options import WorkspaceOptions
from svweave.application.semantic_common import build_envelope, freshness_of, require_build
from svweave.config.context import WorkspaceContext
from svweave.domain.cursor import (
    MAX_RESPONSE_BYTES,
    check_limit,
    decode_cursor,
    encode_cursor,
    query_hash,
)
from svweave.domain.envelope import Envelope, ErrorInfo, Outcome, Pagination
from svweave.domain.errors import ErrorCode, SvwError
from svweave.domain.ids import is_sha256_hex
from svweave.domain.locations import Encoding, LineIndex, byte_to_lsp
from svweave.domain.states import Freshness
from svweave.snapshot.capture import snapshots_dir
from svweave.snapshot.models import SnapFile
from svweave.storage.db import Database
from svweave.storage.semantic import loads

LOCATION_CONVENTION = {
    "span": "zero-based half-open byte offsets [start,end) into the original file bytes",
    "line_col": "1-based, column counts UTF-8 bytes",
    "position_encoding": "utf-8",
}
MAX_DEPTH = 1000
SYMBOL_PAGE_DEFAULT = 50
MAX_SPECIALIZATIONS = 20


# ---------------------------------------------------------------------------------- session
class _S:
    def __init__(self, ctx: WorkspaceContext, db: Database, build: sqlite3.Row, key: bytes) -> None:
        self.ctx, self.db, self.build, self.key = ctx, db, build, key
        self._fresh: Freshness | None = None

    def fresh(self) -> Freshness:
        """Freshness is computed ONCE per query (it is the expensive part of an envelope)."""
        if self._fresh is None:
            self._fresh = freshness_of(self.ctx, self.db, self.build)
        return self._fresh

    @property
    def bid(self) -> str:
        return str(self.build["build_id"])

    def rows(self, sql: str, *args: Any) -> list[sqlite3.Row]:
        return self.db.conn.execute(sql, args).fetchall()

    def one(self, sql: str, *args: Any) -> sqlite3.Row | None:
        r: sqlite3.Row | None = self.db.conn.execute(sql, args).fetchone()
        return r

    def count(self, sql: str, *args: Any) -> int:
        return int(self.db.conn.execute(sql, args).fetchone()[0])


def _cursor_key(ctx: WorkspaceContext) -> bytes:
    """Workspace secret in the trusted dir (outside the repo), shared with svw_search."""
    return cursor_key(ctx)


@contextmanager
def _session(opts: WorkspaceOptions, build_id: str) -> Iterator[_S]:
    ctx = opts.load()
    with Database(ctx.state_dir / DB_FILENAME) as db:
        build = require_build(db, build_id)
        yield _S(ctx, db, build, _cursor_key(ctx))


# ---------------------------------------------------------------------------------- DTOs
def _decl_dto(r: sqlite3.Row) -> dict[str, Any]:
    macro = loads(r["macro"])
    return {
        "symbol_id": r["symbol_id"],
        "declaration_id": r["declaration_id"],
        "kind": r["kind"],
        "name": r["name"],
        "qualified_name": r["qualified_name"],
        "scope": r["scope"],
        "type": r["type"],
        "compilation_unit": r["compilation_unit"],
        "library": r["library"],
        "location": loads(r["loc"]),
        "full_span": None
        if r["full_start"] is None
        else {"start": r["full_start"], "end": r["full_end"]},
        "macro_origin": macro,
        "attrs": loads(r["attrs"]) or {},
    }


def _ref_dto(r: sqlite3.Row, qn: dict[str, tuple[str, str]]) -> dict[str, Any]:
    cont = qn.get(r["container_id"]) if r["container_id"] else None
    return {
        "reference_id": r["reference_id"],
        "kind": r["kind"],
        "resolution_status": r["resolution_status"],
        "reason": r["unresolved_reason"],
        "target_declaration_id": r["target_id"],
        "target_name": r["target_name"],
        "location": loads(r["loc"]),
        "container": None
        if cont is None
        else {"qualified_name": cont[0], "kind": cont[1], "declaration_id": r["container_id"]},
        "context": loads(r["context"]) or {},
    }


def _inst_dto(r: sqlite3.Row, depth: int | None = None) -> dict[str, Any]:
    d = {
        "instance_id": r["instance_id"],
        "path": r["path"],
        "hierarchical_path": r["hier_path"],
        "path_components": loads(r["components"]),
        "parent_id": r["parent_id"],
        "definition": r["definition"],
        "definition_declaration_id": r["definition_declaration_id"],
        "specialization": r["specialization"],
        "parameters": loads(r["parameters"]) or [],
        "generate_context": loads(r["generate_context"]) or [],
        "is_top": bool(r["is_top"]),
        "is_interface": bool(r["is_interface"]),
        "is_stub": bool(r["is_stub"]),
        "stub_name": r["stub_name"],
        "blackbox_candidate": bool(r["blackbox_candidate"]),
        "location": loads(r["loc"]),
        "bound_from": loads(r["bind"]),
    }
    if depth is not None:
        d["depth"] = depth
    return d


def _qualified(s: _S, ids: set[str]) -> dict[str, tuple[str, str]]:
    if not ids:
        return {}
    out: dict[str, tuple[str, str]] = {}
    ids_l = sorted(ids)
    for i in range(0, len(ids_l), 500):
        chunk = ids_l[i : i + 500]
        q = ",".join("?" for _ in chunk)
        for r in s.rows(
            f"SELECT declaration_id, qualified_name, kind FROM declarations "
            f"WHERE build_id=? AND declaration_id IN ({q})",
            s.bid,
            *chunk,
        ):
            out[r["declaration_id"]] = (r["qualified_name"], r["kind"])
    return out


# ---------------------------------------------------------------------------------- paging
def _page(
    s: _S,
    request_id: str,
    tool: str,
    params: dict[str, Any],
    sort: str,
    limit: int | None,
    cursor: str | None,
    total: int,
    fetch: Callable[[int, int], list[sqlite3.Row]],
    render: Callable[[list[sqlite3.Row]], list[dict[str, Any]]],
    extra: dict[str, Any],
    *,
    extra_limits: list[str] | None = None,
    warnings: list[str] | None = None,
) -> Envelope:
    lim = check_limit(limit)
    qh = query_hash(tool, params)
    offset = decode_cursor(s.key, cursor, s.bid, qh, sort) if cursor else 0
    warns = list(warnings or [])
    rows = fetch(offset, lim)
    items = render(rows)
    skeleton = build_envelope(
        s.ctx,
        s.db,
        s.build,
        request_id,
        {**extra, "items": [], "total_matches": total},
        pagination=Pagination(limit=lim, next_cursor="x" * 120, truncated=True),
        warnings=warns + ["x" * 160],
        extra_limits=extra_limits,
        freshness=s.fresh(),
    )
    budget = MAX_RESPONSE_BYTES - len(skeleton.to_json(indent=None).encode("utf-8")) - 1024
    kept = fit_items(items, budget)
    consumed = len(kept)
    new_offset = offset + consumed
    more = new_offset < total
    nxt = encode_cursor(s.key, s.bid, qh, sort, new_offset) if more else None
    if offset > 0 and offset >= total:
        warns.append(
            f"page is empty because the cursor offset ({offset}) is at or beyond the end of the "
            f"{total} matching item(s); this is not a statement about other queries"
        )
    if more:
        warns.append(
            f"truncated: showing {consumed} of {total} matching items; use next_cursor for the "
            "rest, items not on this page must not be assumed absent"
        )
    return flag_oversize(
        build_envelope(
            s.ctx,
            s.db,
            s.build,
            request_id,
            {**extra, "items": kept, "total_matches": total, "returned": consumed, "offset": offset},
            pagination=Pagination(limit=lim, next_cursor=nxt, truncated=more),
            warnings=warns,
            extra_limits=extra_limits,
            freshness=s.fresh(),
        )
    )


def fit_items(items: list[dict[str, Any]], budget: int) -> list[dict[str, Any]]:
    """Largest prefix of ``items`` within ``budget`` bytes of JSON. The first item is always
    kept, but when it alone exceeds the budget it is replaced by a small placeholder, so a
    response can never exceed the cap (shared by every paged tool)."""
    kept: list[dict[str, Any]] = []
    used = 0
    for it in items:
        sz = len(json.dumps(it, ensure_ascii=False, separators=(",", ":")).encode("utf-8")) + 1
        if kept and used + sz > budget:
            break
        if not kept and sz > budget:
            it = {
                "truncated_item": True,
                "note": "item exceeds the response cap",
                "reason": "item_exceeds_response_cap",
                "size_bytes": sz,
                "id": _item_id(it),
            }
            sz = 260
        kept.append(it)
        used += sz
    return kept


def flag_oversize(env: Envelope, key: str = "items") -> Envelope:
    """Make item truncation explicit at the ENVELOPE level (B9). An oversized item is replaced by a
    placeholder; without this the last item could vanish with ``truncated=false`` and no cursor.
    Sets ``pagination.truncated``, lists the omitted items and their reason, downgrades OK to
    PARTIAL, and answers OUTPUT_LIMIT when not a single real item fits."""
    items = env.data.get(key)
    if not isinstance(items, list):
        return env
    ph = [i for i in items if isinstance(i, dict) and i.get("truncated_item")]
    if not ph:
        return env
    env.pagination.truncated = True
    env.data["oversize_items"] = [
        {"id": i.get("id"), "reason": "item_exceeds_response_cap", "size_bytes": i.get("size_bytes")}
        for i in ph
    ]
    env.warnings.append(
        f"{len(ph)} item(s) exceed the {MAX_RESPONSE_BYTES}-byte response cap and are replaced by "
        "placeholders: their content is NOT in this result (see data.oversize_items)"
    )
    if env.outcome is Outcome.OK:
        env.outcome = Outcome.PARTIAL
    if len(ph) == len(items) and env.error is None:
        env.error = ErrorInfo(
            code=ErrorCode.OUTPUT_LIMIT.value,
            message="every item of this page exceeds the response cap; no item content could be "
            "returned inline",
            details={
                "cap_bytes": MAX_RESPONSE_BYTES,
                "items": env.data["oversize_items"],
                "hint": "fetch the item by its id with a narrower dedicated query (svw_symbol by "
                "symbol_id, svw_references with scope); there is no inline path above the cap",
            },
        )
    return env


def _item_id(it: dict[str, Any]) -> str | None:
    for k in (
        "evidence_id",
        "reference_id",
        "declaration_id",
        "instance_id",
        "assertion_id",
        "symbol_id",
    ):
        if k in it:
            return str(it[k])
    return None


# ---------------------------------------------------------------------------------- symbol
def _decl_rows_by_location(s: _S, file: str, byte: int) -> list[sqlite3.Row]:
    ids: dict[str, sqlite3.Row] = {}
    for r in s.rows(
        "SELECT * FROM declarations WHERE build_id=? AND file=? AND span_start<=? AND ?<span_end",
        s.bid,
        file,
        byte,
        byte,
    ):
        ids[r["declaration_id"]] = r
    refs = s.rows(
        'SELECT target_id FROM "references" WHERE build_id=? AND file=? AND span_start<=? '
        "AND ?<span_end AND target_id IS NOT NULL",
        s.bid,
        file,
        byte,
        byte,
    )
    for ref in refs:
        if ref["target_id"] not in ids:
            d = s.one(
                "SELECT * FROM declarations WHERE build_id=? AND declaration_id=?",
                s.bid,
                ref["target_id"],
            )
            if d is not None:
                ids[d["declaration_id"]] = d
    return list(ids.values())


def symbol(
    opts: WorkspaceOptions,
    build_id: str,
    request_id: str,
    *,
    symbol_id: str | None = None,
    file: str | None = None,
    byte: int | None = None,
    name: str | None = None,
    limit: int | None = None,
    cursor: str | None = None,
) -> Envelope:
    """Declaration lookup by ``symbol_id`` OR ``file``+``byte`` (OR a name). Several candidates
    are returned together with ``AMBIGUOUS_SYMBOL`` instead of picking one."""
    modes = [symbol_id is not None, file is not None or byte is not None, name is not None]
    if sum(modes) != 1:
        raise SvwError(
            ErrorCode.INVALID_CONFIG,
            "give exactly one of: symbol_id, file+byte, name",
            {"fields": ["symbol_id", "file+byte", "name"]},
        )
    if (file is None) != (byte is None) and name is None and symbol_id is None:
        raise SvwError(ErrorCode.INVALID_CONFIG, "file and byte must be given together")
    if byte is not None and (not isinstance(byte, int) or byte < 0):
        raise SvwError(ErrorCode.INVALID_CONFIG, "byte must be a non-negative integer")
    with _session(opts, build_id) as s:
        if symbol_id is not None:
            rows = s.rows(
                "SELECT * FROM declarations WHERE build_id=? AND symbol_id=?", s.bid, symbol_id
            )
            if not rows:
                raise SvwError(
                    ErrorCode.MISSING_INPUT,
                    f"unknown symbol {symbol_id}",
                    {"reason": "symbol_missing"},
                )
        elif file is not None and byte is not None:
            rows = _decl_rows_by_location(s, file, byte)
        else:
            rows = s.rows(
                "SELECT * FROM declarations WHERE build_id=? AND (qualified_name=? OR name=?) "
                "ORDER BY file, span_start",
                s.bid,
                name,
                name,
            )
        lim = SYMBOL_PAGE_DEFAULT if limit is None else check_limit(limit)
        qh = query_hash(
            "svw_symbol", {"symbol_id": symbol_id, "file": file, "byte": byte, "name": name}
        )
        offset = decode_cursor(s.key, cursor, s.bid, qh, "location") if cursor else 0
        cands = [_decl_dto(r) for r in rows[offset : offset + lim]]
        for c in cands:
            if c["kind"] == "class":
                sp = s.rows(
                    "SELECT text, parameters, base_class FROM specializations WHERE build_id=? "
                    "AND class_declaration_id=? ORDER BY text",
                    s.bid,
                    c["declaration_id"],
                )
                c["specialization_count"] = len(sp)
                c["specializations_truncated"] = len(sp) > MAX_SPECIALIZATIONS
                sp = sp[:MAX_SPECIALIZATIONS]
                c["specializations"] = [
                    {
                        "text": x["text"],
                        "parameters": loads(x["parameters"]),
                        "base_class": x["base_class"],
                    }
                    for x in sp
                ]
        budget = MAX_RESPONSE_BYTES - 16_384
        kept = fit_items(cands, budget)
        new_off = offset + len(kept)
        more = new_off < len(rows)
        cands = kept
        data: dict[str, Any] = {
            "candidates": cands,
            "candidate_count": len(rows),
            "returned": len(cands),
            "offset": offset,
            "location_convention": LOCATION_CONVENTION,
        }
        pag = Pagination(
            limit=lim,
            truncated=more,
            next_cursor=encode_cursor(s.key, s.bid, qh, "location", new_off) if more else None,
        )
        warns: list[str] = []
        if more:
            warns.append(
                f"truncated: showing {len(cands)} of {len(rows)} candidates; use next_cursor for "
                "the rest, candidates not on this page must not be assumed absent"
            )
        if any(c.get("specializations_truncated") for c in cands):
            warns.append(
                f"class specializations are bounded to {MAX_SPECIALIZATIONS} per class "
                "(specialization_count gives the full number)"
            )
        if not rows:
            warns.append(
                "no declaration or resolved reference covers this input; that is not "
                "proof the identifier does not exist in other builds or inactive code"
            )
        err = None
        outcome = None
        if len(rows) == 1 and cands and not cands[0].get("truncated_item"):
            data["symbol"] = cands[0]
        elif len(rows) > 1:
            err = ErrorInfo(
                code=ErrorCode.AMBIGUOUS_SYMBOL.value,
                message=f"{len(rows)} scoped candidates; choose one by symbol_id "
                "(no candidate was selected automatically)",
                details={
                    "candidates": [
                        {
                            "symbol_id": c["symbol_id"],
                            "qualified_name": c["qualified_name"],
                            "kind": c["kind"],
                            "location": c["location"],
                        }
                        for c in cands
                        if "symbol_id" in c
                    ],
                    "candidate_count": len(rows),
                    "next_cursor": pag.next_cursor,
                },
            )
            outcome = Outcome.ERROR
        env = build_envelope(
            s.ctx,
            s.db,
            s.build,
            request_id,
            data,
            pagination=pag,
            warnings=warns,
            error=err,
            outcome=outcome,
            freshness=s.fresh(),
        )
        return flag_oversize(env, "candidates")


# ---------------------------------------------------------------------------------- references
def _gap_limits(s: _S) -> list[str]:
    rows = s.rows(
        "SELECT kind, COUNT(*) n FROM coverage_gaps WHERE build_id=? GROUP BY kind", s.bid
    )
    msgs = {
        "generic_class_not_specialized": "{n} generic class(es) never specialized: references "
        "inside their bodies are not extracted",
        "definition_not_elaborated": "{n} module/interface definition(s) not elaborated under the "
        "tops: references inside them are not extracted (inactive_for_profile)",
        "library_member_not_loaded": "{n} library member(s) never loaded: not analysed",
        "stub_boundary": "{n} stub/blackbox boundary instance(s): internals unknown",
        "empty_module_body": "{n} instance(s) of modules with an empty body (possible blackbox)",
    }
    return [msgs[r["kind"]].format(n=r["n"]) for r in rows if r["kind"] in msgs]


def references(
    opts: WorkspaceOptions,
    build_id: str,
    symbol_id: str,
    request_id: str,
    *,
    scope: str | None = None,
    limit: int | None = None,
    cursor: str | None = None,
) -> Envelope:
    """Resolved and unresolved occurrences of one declaration. ``scope`` is an optional logical
    path prefix. The result states what was searched; an empty page is not 'no references'."""
    with _session(opts, build_id) as s:
        d = s.one("SELECT * FROM declarations WHERE build_id=? AND symbol_id=?", s.bid, symbol_id)
        if d is None:
            raise SvwError(
                ErrorCode.MISSING_INPUT, f"unknown symbol {symbol_id}", {"reason": "symbol_missing"}
            )
        did = d["declaration_id"]
        # exact (case-sensitive, wildcard-free) prefix matching: SV paths and names are
        # case-sensitive, LIKE is not (M3)
        scope_sql = " AND substr(file, 1, ?) = ?" if scope else ""
        # resolved edges + unresolved names equal to this name + virtual calls whose static
        # target is an overridden method (this symbol may be the runtime target)
        base = (
            'FROM "references" WHERE build_id=? AND ('
            "target_id=? OR (resolution_status='candidate_source' AND target_name=?) "
            "OR (resolution_status='dynamic_unresolved' AND instr(context, ?) > 0))" + scope_sql
        )
        args: list[Any] = [s.bid, did, d["name"], did] + ([len(scope), scope] if scope else [])
        total = s.count("SELECT COUNT(*) " + base, *args)

        def fetch(offset: int, lim: int) -> list[sqlite3.Row]:
            return s.rows(
                "SELECT * " + base + " ORDER BY file, span_start, ord LIMIT ? OFFSET ?",
                *args,
                lim,
                offset,
            )

        def render(rows: list[sqlite3.Row]) -> list[dict[str, Any]]:
            qn = _qualified(s, {r["container_id"] for r in rows if r["container_id"]})
            out = []
            for r in rows:
                it = _ref_dto(r, qn)
                if r["target_id"] != did:
                    it["via"] = (
                        "unresolved_name_match"
                        if r["resolution_status"] == "candidate_source"
                        else "virtual_dispatch_candidate"
                    )
                out.append(it)
            return out

        rep_row = s.one("SELECT scope FROM build_reports WHERE build_id=?", s.bid)
        searched = {
            "scope": scope or "all analysed source files",
            "tops": rep_row["scope"] if rep_row else None,
            "kind": "occurrences visited by slang in elaborated code of the selected tops",
            "target_declaration": _decl_dto(d),
        }
        extra = {
            "searched_scope": searched,
            "resolution_basis": "slang static name resolution (HDL_STATIC_RESOLVED); "
            "virtual calls are dynamic_unresolved",
            "location_convention": LOCATION_CONVENTION,
        }
        warns = []
        if total == 0:
            warns.append(
                "no references found in the searched scope; see analysis_coverage for what "
                "was not searched before concluding the symbol is unused"
            )
        return _page(
            s,
            request_id,
            "references",
            {"symbol_id": symbol_id, "scope": scope},
            "file,span",
            limit,
            cursor,
            total,
            fetch,
            render,
            extra,
            extra_limits=_gap_limits(s),
            warnings=warns,
        )


# ---------------------------------------------------------------------------------- hierarchy
def _resolve_instance(s: _S, ref: str) -> sqlite3.Row:
    if is_sha256_hex(ref):
        r = s.one("SELECT * FROM instances WHERE build_id=? AND instance_id=?", s.bid, ref)
        if r is not None:
            return r
    cleaned = ref.strip()
    rows = s.rows(
        "SELECT * FROM instances WHERE build_id=? AND (path=? OR hier_path=? OR TRIM(path)=? "
        "OR TRIM(hier_path)=?) ORDER BY ord",
        s.bid,
        ref,
        ref,
        cleaned,
        cleaned,
    )
    if not rows:
        raise SvwError(
            ErrorCode.MISSING_INPUT,
            f"unknown instance {ref!r}",
            {"reason": "instance_missing", "instance": ref},
        )
    if len(rows) > 1:
        raise SvwError(
            ErrorCode.AMBIGUOUS_SYMBOL,
            f"{len(rows)} instances match {ref!r}",
            {"candidates": [{"instance_id": r["instance_id"], "path": r["path"]} for r in rows]},
        )
    return rows[0]


def hierarchy(
    opts: WorkspaceOptions,
    build_id: str,
    request_id: str,
    *,
    instance: str | None = None,
    top: str | None = None,
    depth: int | None = None,
    limit: int | None = None,
    cursor: str | None = None,
) -> Envelope:
    """Instance tree (pre-order) below an instance, a top, or all tops. ``depth`` = levels below
    the root (0 = the root only)."""
    if depth is not None and (not isinstance(depth, int) or depth < 0 or depth > MAX_DEPTH):
        raise SvwError(ErrorCode.INVALID_CONFIG, f"depth must be in 0..{MAX_DEPTH}")
    if instance is not None and top is not None:
        raise SvwError(ErrorCode.INVALID_CONFIG, "give instance OR top, not both")
    with _session(opts, build_id) as s:
        if instance is not None:
            roots = [_resolve_instance(s, instance)["instance_id"]]
        elif top is not None:
            rr = s.rows(
                "SELECT instance_id FROM instances WHERE build_id=? AND is_top=1 AND "
                "(path=? OR definition=?) ORDER BY ord",
                s.bid,
                top,
                top,
            )
            if not rr:
                raise SvwError(
                    ErrorCode.MISSING_INPUT,
                    f"no top instance {top!r}",
                    {"reason": "top_missing", "top": top},
                )
            roots = [r["instance_id"] for r in rr]
        else:
            roots = [
                r["instance_id"]
                for r in s.rows(
                    "SELECT instance_id FROM instances WHERE build_id=? AND is_top=1 ORDER BY ord",
                    s.bid,
                )
            ]
        dmax = MAX_DEPTH if depth is None else depth
        q = ",".join("?" for _ in roots) or "NULL"
        cte = (
            "WITH RECURSIVE t(iid, d) AS (SELECT instance_id, 0 FROM instances WHERE build_id=? "
            f"AND instance_id IN ({q}) UNION ALL SELECT i.instance_id, t.d+1 FROM instances i "
            "JOIN t ON i.parent_id=t.iid WHERE i.build_id=? AND t.d < ?) "
        )
        cargs: list[Any] = [s.bid, *roots, s.bid, dmax]
        total = s.count(cte + "SELECT COUNT(*) FROM t", *cargs)

        def fetch(offset: int, lim: int) -> list[sqlite3.Row]:
            return s.rows(
                cte + "SELECT i.*, t.d AS depth FROM t JOIN instances i ON i.instance_id=t.iid "
                "AND i.build_id=? ORDER BY i.ord LIMIT ? OFFSET ?",
                *cargs,
                s.bid,
                lim,
                offset,
            )

        def render(rows: list[sqlite3.Row]) -> list[dict[str, Any]]:
            return [_inst_dto(r, r["depth"]) for r in rows]

        extra = {
            "roots": roots,
            "depth": depth,
            "location_convention": LOCATION_CONVENTION,
            "path_note": "paths are display strings; identity is instance_id "
            "(built from path components, never from splitting the string on '.')",
        }
        return _page(
            s,
            request_id,
            "hierarchy",
            {"instance": instance, "top": top, "depth": depth},
            "preorder",
            limit,
            cursor,
            total,
            fetch,
            render,
            extra,
            extra_limits=_gap_limits(s),
        )


# ---------------------------------------------------------------------------------- connections
def _binding_dto(r: sqlite3.Row, inst_path: dict[str, str]) -> dict[str, Any]:
    return {
        "formal_port": r["formal_port"],
        "port_kind": r["port_kind"],
        "direction": r["direction"],
        "port_type": (loads(r["types"]) or {}).get("port"),
        "port_declaration_id": r["port_declaration_id"],
        "association_style": r["style"],
        "actual": {
            "text": r["actual_expression"],
            "kind": r["actual_kind"],
            "type": (loads(r["types"]) or {}).get("actual"),
            "bit_width": r["bit_width"],
            "is_constant": bool(r["is_constant"]),
            "implicit": bool(r["implicit_actual"]),
            "location": loads(r["actual_loc"]),
            "parts": loads(r["parts"]) or [],
        },
        "referenced_symbols": loads(r["referenced_symbols"]) or [],
        "interface_instance": None
        if r["interface_instance_id"] is None
        else {
            "instance_id": r["interface_instance_id"],
            "path": inst_path.get(r["interface_instance_id"]),
        },
        "modport": r["modport"],
        "supported": bool(r["supported"]),
        "note": r["note"],
    }


def connections(
    opts: WorkspaceOptions,
    build_id: str,
    instance: str,
    request_id: str,
    *,
    port: str | None = None,
    limit: int | None = None,
    cursor: str | None = None,
) -> Envelope:
    """Direct formal/actual associations of one instance (never collapses a concatenation into a
    single signal; direction is the port's declared direction, not a driver/sink analysis)."""
    with _session(opts, build_id) as s:
        inst = _resolve_instance(s, instance)
        iid = inst["instance_id"]
        where = "build_id=? AND instance_id=?" + (" AND formal_port=?" if port else "")
        args: list[Any] = [s.bid, iid] + ([port] if port else [])
        total = s.count("SELECT COUNT(*) FROM port_bindings WHERE " + where, *args)
        if port and total == 0:
            raise SvwError(
                ErrorCode.MISSING_INPUT,
                f"instance has no port {port!r}",
                {"reason": "port_missing", "port": port},
            )
        paths = {
            r["instance_id"]: r["path"]
            for r in s.rows("SELECT instance_id, path FROM instances WHERE build_id=?", s.bid)
        }

        def fetch(offset: int, lim: int) -> list[sqlite3.Row]:
            return s.rows(
                "SELECT * FROM port_bindings WHERE " + where + " ORDER BY ord LIMIT ? OFFSET ?",
                *args,
                lim,
                offset,
            )

        def render(rows: list[sqlite3.Row]) -> list[dict[str, Any]]:
            return [_binding_dto(r, paths) for r in rows]

        extra = {
            "instance": _inst_dto(inst),
            "location_convention": LOCATION_CONVENTION,
            "note": "direct associations only; clocking blocks, tran/alias, force/release and "
            "cross-hierarchy continuous assignments are outside the v1 guarantee",
        }
        lim_extra = _gap_limits(s)
        if inst["is_stub"]:
            lim_extra.append(
                f"instance is an explicit stub ({inst['stub_name']}): connectivity "
                "beyond the boundary is unknown"
            )
        return _page(
            s,
            request_id,
            "connections",
            {"instance": iid, "port": port},
            "port-order",
            limit,
            cursor,
            total,
            fetch,
            render,
            extra,
            extra_limits=lim_extra,
        )


# ---------------------------------------------------------------------------------- assertions
def assertions(
    opts: WorkspaceOptions,
    build_id: str,
    request_id: str,
    *,
    scope: str | None = None,
    entry_type: str | None = None,
    limit: int | None = None,
    cursor: str | None = None,
) -> Envelope:
    """SVA inventory: source declarations/statements, bind directives and per-instance
    elaborated entries, kept separate. Execution/proof status is always NOT_RUN."""
    if entry_type not in (None, "source", "bind", "elaborated"):
        raise SvwError(ErrorCode.INVALID_CONFIG, "entry_type must be source|bind|elaborated")
    with _session(opts, build_id) as s:
        where = "a.build_id=?"
        args: list[Any] = [s.bid]
        if entry_type:
            where += " AND a.entry_type=?"
            args.append(entry_type)
        if scope:
            inst = _resolve_instance(s, scope)
            pref = inst["path"]
            where += " AND (a.entry_type<>'elaborated' OR i.path=? OR substr(i.path, 1, ?) = ?)"
            args += [pref, len(pref) + 1, pref + "."]
        base = (
            "FROM assertions a LEFT JOIN instances i ON i.build_id=a.build_id AND i.instance_id=a.instance_id WHERE "
            + where
        )
        total = s.count("SELECT COUNT(*) " + base, *args)
        order = (
            "ORDER BY CASE a.entry_type WHEN 'source' THEN 0 WHEN 'bind' THEN 1 ELSE 2 END, "
            "a.file, a.span_start, a.ord"
        )
        paths = {
            r["instance_id"]: r["path"]
            for r in s.rows("SELECT instance_id, path FROM instances WHERE build_id=?", s.bid)
        }

        def fetch(offset: int, lim: int) -> list[sqlite3.Row]:
            return s.rows(
                "SELECT a.*, i.path AS inst_path " + base + " " + order + " LIMIT ? OFFSET ?",
                *args,
                lim,
                offset,
            )

        def render(rows: list[sqlite3.Row]) -> list[dict[str, Any]]:
            out = []
            for r in rows:
                m = loads(r["metadata"]) or {}
                it = {
                    "assertion_id": r["assertion_id"],
                    "entry_type": r["entry_type"],
                    "kind": r["kind"],
                    "name": r["name"],
                    "scope": r["inst_path"],
                    "instance_id": r["instance_id"],
                    "source": m.get("source"),
                    "execution_status": r["execution_status"],
                    "resolved": m.get("resolved"),
                    "errors": m.get("errors", []),
                }
                for k in (
                    "style",
                    "label",
                    "property",
                    "property_declaration_id",
                    "clock",
                    "disable_iff",
                    "target_text",
                    "instantiation_text",
                    "declaration_id",
                ):
                    if k in m:
                        it[k] = m[k]
                if "elaborated" in m:
                    it["elaborated_targets"] = [
                        {
                            "checker_instance": {
                                "instance_id": e["checker_instance_id"],
                                "path": paths.get(e["checker_instance_id"]),
                            },
                            "target_instance": None
                            if e["target_instance_id"] is None
                            else {
                                "instance_id": e["target_instance_id"],
                                "path": paths.get(e["target_instance_id"]),
                            },
                        }
                        for e in m["elaborated"]
                    ]
                out.append(it)
            return out

        extra = {
            "location_convention": LOCATION_CONVENTION,
            "execution_status": "NOT_RUN",
            "note": "static inventory only: no property pass/fail, vacuity, attempts or proof "
            "result exists in this evidence (slang only parses and elaborates)",
        }
        return _page(
            s,
            request_id,
            "assertions",
            {"scope": scope, "entry_type": entry_type},
            "entry-file-span",
            limit,
            cursor,
            total,
            fetch,
            render,
            extra,
            extra_limits=["SVA execution and proof status are NOT_RUN."],
        )


# ---------------------------------------------------------------------------------- diagnostics
def diagnostics(
    opts: WorkspaceOptions,
    build_id: str,
    request_id: str,
    *,
    limit: int | None = None,
    cursor: str | None = None,
) -> Envelope:
    with _session(opts, build_id) as s:
        total = s.count("SELECT COUNT(*) FROM diagnostics WHERE build_id=?", s.bid)

        def fetch(offset: int, lim: int) -> list[sqlite3.Row]:
            return s.rows(
                "SELECT * FROM diagnostics WHERE build_id=? ORDER BY diagnostic_id "
                "LIMIT ? OFFSET ?",
                s.bid,
                lim,
                offset,
            )

        def render(rows: list[sqlite3.Row]) -> list[dict[str, Any]]:
            out = []
            for r in rows:
                loc = loads(r["locations"]) or {}
                out.append(
                    {
                        "severity": r["severity"],
                        "code": r["code"],
                        "message": r["message"],
                        "is_error": bool(r["is_error"]),
                        "location": loc.get("primary"),
                        "ranges": loc.get("ranges", []),
                        "macro_notes": loc.get("macro_notes", []),
                        "option": loc.get("option"),
                    }
                )
            return out

        extra = {
            "diagnostic_outcome": s.build["diagnostic_outcome"],
            "location_convention": LOCATION_CONVENTION,
        }
        return _page(
            s, request_id, "diagnostics", {}, "emission", limit, cursor, total, fetch, render, extra
        )


# ---------------------------------------------------------------------------------- LSP helper
def lsp_positions(
    opts: WorkspaceOptions,
    build_id: str,
    file: str,
    start: int,
    end: int,
    encoding: Encoding = "utf-16",
) -> dict[str, Any]:
    """Convert a canonical byte span of a snapshot file to LSP (line0, character) positions in
    ``encoding`` (LOC-01). Reads the ORIGINAL bytes captured in the build's snapshot."""
    with _session(opts, build_id) as s:
        snap_dir = snapshots_dir(s.ctx) / s.build["snapshot_id"]
        rec = s.one(
            "SELECT 1 FROM source_files WHERE snapshot_id=? AND logical_path=?",
            s.build["snapshot_id"],
            file,
        )
        if rec is None:
            raise SvwError(
                ErrorCode.MISSING_INPUT,
                f"{file!r} is not in the build's snapshot",
                {"reason": "file_not_in_snapshot"},
            )
        from svweave.snapshot.capture import load_record

        sf: SnapFile = next(f for f in load_record(snap_dir).files if f.logical_path == file)
        data = Path(snap_dir / sf.snapshot_path).read_bytes()
        idx = LineIndex(data)
        return {
            "file": file,
            "encoding": encoding,
            "start": dict(
                zip(("line", "character"), byte_to_lsp(idx, start, encoding), strict=True)
            ),
            "end": dict(zip(("line", "character"), byte_to_lsp(idx, end, encoding), strict=True)),
        }
