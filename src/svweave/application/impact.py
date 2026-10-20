"""Static impact of changing one thing in one build (``svweave query impact`` / ``svw_impact``).

Answers "what does this touch?" from the stored, build-bound semantic data only; nothing is
guessed and nothing is run:

* a **module / interface** (definition): every instance of it, the definitions that instantiate it
  (``dependents``, transitively up to ``depth`` levels) and the ones it instantiates
  (``dependencies``), plus the source references to it;
* a **port**: every instance port connection that binds it (actual expression, signals, file:line)
  and the dependents of the module that owns it;
* any other declaration (net, variable, parameter, ...): its resolved references and the port
  connections that reference it, and the dependents of the enclosing module;
* an **instance**: its ancestors, what lies below it and its own port connections.

Everything inherits the build's limits (the envelope says PARTIAL, STALE, ...): an instance,
reference or connection missing from the answer is not proof that it does not exist. Counts are
counts, not a risk score. Dependents are derived from the elaborated instance tree, so a module
that is never instantiated under the build's tops (inactive generate, never loaded library
member) is invisible here.
"""

from __future__ import annotations

import json
import sqlite3
from collections import defaultdict
from typing import Any

from svweave.application.options import WorkspaceOptions
from svweave.application.queries import (
    _S,
    LOCATION_CONVENTION,
    _decl_dto,
    _decl_rows_by_location,
    _gap_limits,
    _inst_dto,
    _qualified,
    _resolve_instance,
    _session,
)
from svweave.application.semantic_common import build_envelope
from svweave.domain.cursor import MAX_RESPONSE_BYTES, check_limit
from svweave.domain.envelope import Envelope, ErrorInfo, Outcome
from svweave.domain.errors import ErrorCode, SvwError
from svweave.storage.semantic import loads

DEFAULT_LIST = 50
MAX_DEPTH = 20
DEFINITION_KINDS = frozenset({"module", "interface", "program"})
IMPACT_NOTE = (
    "static impact from one build's elaborated tree and resolved references; counts, not a risk "
    "score; no runtime, UVM factory/config_db or bit-level dataflow"
)
_LISTS = (
    "instances",
    "dependents",
    "dependencies",
    "connections",
    "signal_uses",
    "references",
    "ancestors",
    "descendants",
    "files",
)


def _names(s: _S, ids: set[str]) -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    for did, (qn, kind) in _qualified(s, ids).items():
        out[did] = {"declaration_id": did, "qualified_name": qn, "kind": kind}
    return out


def _definition_graph(
    s: _S,
) -> tuple[dict[str, dict[str, int]], dict[str, dict[str, int]]]:
    """(up, down): definition -> {parent definition: n}, definition -> {child definition: n}."""
    up: dict[str, dict[str, int]] = defaultdict(dict)
    down: dict[str, dict[str, int]] = defaultdict(dict)
    for r in s.rows(
        "SELECT i.definition_declaration_id AS d, p.definition_declaration_id AS pd, COUNT(*) AS n "
        "FROM instances i JOIN instances p ON p.build_id=i.build_id AND p.instance_id=i.parent_id "
        "WHERE i.build_id=? AND i.definition_declaration_id IS NOT NULL "
        "AND p.definition_declaration_id IS NOT NULL GROUP BY d, pd",
        s.bid,
    ):
        up[r["d"]][r["pd"]] = int(r["n"])
        down[r["pd"]][r["d"]] = int(r["n"])
    return up, down


def _closure(
    graph: dict[str, dict[str, int]], start: str, depth: int
) -> list[tuple[int, str, int]]:
    """Breadth-first (level, definition, via_instances) from ``start``; each definition once, at
    its shortest level. ``start`` itself is never listed (recursive instantiation is not looped)."""
    seen = {start}
    frontier = [start]
    out: list[tuple[int, str, int]] = []
    for level in range(1, depth + 1):
        nxt: list[str] = []
        for d in frontier:
            for t, n in sorted(graph.get(d, {}).items()):
                if t in seen:
                    continue
                seen.add(t)
                nxt.append(t)
                out.append((level, t, n))
        frontier = nxt
        if not frontier:
            break
    return out


def _closure_items(
    s: _S, graph: dict[str, dict[str, int]], start: str, depth: int
) -> list[dict[str, Any]]:
    rows = _closure(graph, start, depth)
    names = _names(s, {d for _, d, _ in rows})
    return [
        {**names.get(d, {"declaration_id": d, "qualified_name": None, "kind": None}), "level": lv, "via_instances": n}
        for lv, d, n in rows
    ]


def _instances_of(s: _S, def_id: str, lim: int) -> tuple[int, list[dict[str, Any]]]:
    total = s.count(
        "SELECT COUNT(*) FROM instances WHERE build_id=? AND definition_declaration_id=?",
        s.bid,
        def_id,
    )
    paths = {
        r["instance_id"]: r["path"]
        for r in s.rows(
            "SELECT p.instance_id, p.path FROM instances p WHERE p.build_id=? AND p.instance_id IN "
            "(SELECT parent_id FROM instances WHERE build_id=? AND definition_declaration_id=? "
            "AND parent_id IS NOT NULL LIMIT ?)",
            s.bid,
            s.bid,
            def_id,
            lim,
        )
    }
    items = []
    for r in s.rows(
        "SELECT * FROM instances WHERE build_id=? AND definition_declaration_id=? ORDER BY ord LIMIT ?",
        s.bid,
        def_id,
        lim,
    ):
        d = _inst_dto(r)
        items.append(
            {
                "instance_id": d["instance_id"],
                "path": d["path"],
                "parent_path": paths.get(d["parent_id"]) if d["parent_id"] else None,
                "parameters": d["parameters"],
                "location": d["location"],
            }
        )
    return total, items


def _bindings_referencing(
    s: _S, ids: list[str], lim: int
) -> tuple[int, list[dict[str, Any]]]:
    """Instance port connections whose actual expression references the entity (any twin id)."""
    likes = [f'%"declaration_id":"{i}"%' for i in ids]
    cond = " OR ".join("referenced_symbols LIKE ?" for _ in likes)
    total = s.count(
        f"SELECT COUNT(*) FROM port_bindings WHERE build_id=? AND ({cond})", s.bid, *likes
    )
    rows = s.rows(
        f"SELECT * FROM port_bindings WHERE build_id=? AND ({cond}) ORDER BY ord LIMIT ?",
        s.bid,
        *likes,
        lim,
    )
    paths = _paths_for(s, {r["instance_id"] for r in rows})
    return total, [_conn_item(r, paths) for r in rows]


def _conn_item(r: sqlite3.Row, paths: dict[str, str]) -> dict[str, Any]:
    refs = loads(r["referenced_symbols"]) or []
    return {
        "instance_id": r["instance_id"],
        "instance_path": paths.get(r["instance_id"]),
        "formal_port": r["formal_port"],
        "direction": r["direction"],
        "actual": r["actual_expression"],
        "actual_kind": r["actual_kind"],
        "signals": [x.get("path") for x in refs if isinstance(x, dict)],
        "location": loads(r["actual_loc"]),
        "supported": bool(r["supported"]),
    }


def _paths_for(s: _S, ids: set[str]) -> dict[str, str]:
    out: dict[str, str] = {}
    ids_l = sorted(ids)
    for i in range(0, len(ids_l), 500):
        chunk = ids_l[i : i + 500]
        q = ",".join("?" for _ in chunk)
        for r in s.rows(
            f"SELECT instance_id, path FROM instances WHERE build_id=? AND instance_id IN ({q})",
            s.bid,
            *chunk,
        ):
            out[r["instance_id"]] = r["path"]
    return out


def _references_of(
    s: _S, decl_ids: list[str], lim: int
) -> tuple[dict[str, Any], list[dict[str, Any]], list[dict[str, Any]]]:
    """(summary, sample references, per-file counts) of references to one entity (its declaration
    ids: a port and its implicit internal net are one entity)."""
    q = ",".join("?" for _ in decl_ids)
    total = s.count(
        f'SELECT COUNT(*) FROM "references" WHERE build_id=? AND target_id IN ({q})',
        s.bid,
        *decl_ids,
    )
    by_kind = {
        r["kind"]: int(r["n"])
        for r in s.rows(
            f'SELECT kind, COUNT(*) n FROM "references" WHERE build_id=? AND target_id IN ({q}) '
            "GROUP BY kind",
            s.bid,
            *decl_ids,
        )
    }
    files = [
        {"file": r["file"], "references": int(r["n"])}
        for r in s.rows(
            f'SELECT file, COUNT(*) n FROM "references" WHERE build_id=? AND target_id IN ({q}) '
            "GROUP BY file ORDER BY n DESC, file LIMIT ?",
            s.bid,
            *decl_ids,
            lim,
        )
    ]
    sample = []
    rows = s.rows(
        f'SELECT * FROM "references" WHERE build_id=? AND target_id IN ({q}) '
        "ORDER BY file, span_start LIMIT ?",
        s.bid,
        *decl_ids,
        lim,
    )
    cont = _qualified(s, {r["container_id"] for r in rows if r["container_id"]})
    for r in rows:
        c = cont.get(r["container_id"]) if r["container_id"] else None
        sample.append(
            {
                "kind": r["kind"],
                "status": r["resolution_status"],
                "location": loads(r["loc"]),
                "container": c[0] if c else None,
            }
        )
    distinct_files = s.count(
        f'SELECT COUNT(DISTINCT file) FROM "references" WHERE build_id=? AND target_id IN ({q})',
        s.bid,
        *decl_ids,
    )
    return {"total": total, "by_kind": by_kind, "distinct_files": distinct_files}, sample, files


def _owner_definition(s: _S, decl: sqlite3.Row) -> str | None:
    """Declaration id of the module/interface that owns a port/net/variable (via its scope)."""
    scope = decl["scope"]
    if not scope:
        return None
    r = s.one(
        "SELECT declaration_id FROM declarations WHERE build_id=? AND qualified_name=? "
        "AND kind IN ('module','interface','program') ORDER BY span_start LIMIT 1",
        s.bid,
        scope,
    )
    return str(r["declaration_id"]) if r else None


def _definition_block(
    s: _S, def_id: str, depth: int, lim: int, graphs: tuple[dict[str, dict[str, int]], dict[str, dict[str, int]]]
) -> dict[str, Any]:
    up, down = graphs
    total, items = _instances_of(s, def_id, lim)
    dependents = _closure_items(s, up, def_id, depth)
    dependencies = _closure_items(s, down, def_id, depth)
    return {
        "instances": items,
        "instances_total": total,
        "dependents": dependents,
        "dependencies": dependencies,
    }


def _definition_impact(s: _S, decl: sqlite3.Row, depth: int, lim: int, graphs: Any) -> dict[str, Any]:
    did = str(decl["declaration_id"])
    block = _definition_block(s, did, depth, lim, graphs)
    summary, sample, files = _references_of(s, [did], lim)
    ports = s.count(
        "SELECT COUNT(*) FROM declarations WHERE build_id=? AND kind='port' AND scope=?",
        s.bid,
        decl["qualified_name"],
    )
    return {
        **block,
        "references": sample,
        "references_summary": summary,
        "files": files,
        "port_count": ports,
        "summary": {
            "instances": block["instances_total"],
            "dependent_definitions": len(block["dependents"]),
            "dependency_definitions": len(block["dependencies"]),
            "references": summary["total"],
            "files_with_references": summary["distinct_files"],
        },
    }


def _port_impact(
    s: _S, decl: sqlite3.Row, ids: list[str], depth: int, lim: int, graphs: Any
) -> dict[str, Any]:
    pq = ",".join("?" for _ in ids)
    total = s.count(
        f"SELECT COUNT(*) FROM port_bindings WHERE build_id=? AND port_declaration_id IN ({pq})",
        s.bid,
        *ids,
    )
    rows = s.rows(
        f"SELECT * FROM port_bindings WHERE build_id=? AND port_declaration_id IN ({pq}) "
        "ORDER BY ord LIMIT ?",
        s.bid,
        *ids,
        lim,
    )
    paths = _paths_for(s, {r["instance_id"] for r in rows})
    sig_ids: set[str] = set()
    for r in s.rows(
        f"SELECT referenced_symbols FROM port_bindings WHERE build_id=? AND port_declaration_id IN ({pq})",
        s.bid,
        *ids,
    ):
        for x in loads(r["referenced_symbols"]) or []:
            if isinstance(x, dict) and x.get("declaration_id"):
                sig_ids.add(str(x["declaration_id"]))
    # only signals: a genvar or parameter used as an index is not a connected signal
    signals = {d for d, v in _names(s, sig_ids).items() if v["kind"] in ("net", "variable", "port")}
    owner = _owner_definition(s, decl)
    block: dict[str, Any] = {}
    if owner is not None:
        up, _ = graphs
        block = {
            "owner": _names(s, {owner}).get(owner),
            "dependents": _closure_items(s, up, owner, depth),
        }
    uses_total, uses = _bindings_referencing(s, ids, lim)
    summary, sample, files = _references_of(s, ids, lim)
    return {
        "connections": [_conn_item(r, paths) for r in rows],
        "connections_total": total,
        "distinct_signals": len(signals),
        "signal_uses": uses,
        "signal_uses_total": uses_total,
        **block,
        "references": sample,
        "references_summary": summary,
        "files": files,
        "summary": {
            "connection_sites": total,
            "distinct_signals": len(signals),
            "uses_as_actual": uses_total,
            "dependent_definitions": len(block.get("dependents", [])),
            "references": summary["total"],
        },
    }


def _signal_impact(
    s: _S, decl: sqlite3.Row, ids: list[str], depth: int, lim: int, graphs: Any
) -> dict[str, Any]:
    summary, sample, files = _references_of(s, ids, lim)
    total, conns = _bindings_referencing(s, ids, lim)
    owner = _owner_definition(s, decl)
    up, _ = graphs
    dependents = _closure_items(s, up, owner, depth) if owner else []
    return {
        "connections": conns,
        "connections_total": total,
        "owner": _names(s, {owner}).get(owner) if owner else None,
        "dependents": dependents,
        "references": sample,
        "references_summary": summary,
        "files": files,
        "summary": {
            "references": summary["total"],
            "files_with_references": summary["distinct_files"],
            "port_connection_sites": total,
            "dependent_definitions": len(dependents),
        },
    }


def _instance_impact(s: _S, inst: sqlite3.Row, depth: int, lim: int, graphs: Any) -> dict[str, Any]:
    iid = str(inst["instance_id"])
    anc = s.rows(
        "WITH RECURSIVE a(iid, d) AS (SELECT parent_id, 1 FROM instances WHERE build_id=? AND "
        "instance_id=? AND parent_id IS NOT NULL UNION ALL SELECT i.parent_id, a.d+1 FROM instances i "
        "JOIN a ON i.instance_id=a.iid WHERE i.build_id=? AND i.parent_id IS NOT NULL) "
        "SELECT i.path, i.definition, a.d AS level FROM a JOIN instances i ON i.instance_id=a.iid "
        "AND i.build_id=? ORDER BY a.d",
        s.bid,
        iid,
        s.bid,
        s.bid,
    )
    below = s.rows(
        "WITH RECURSIVE t(iid, d) AS (SELECT instance_id, 0 FROM instances WHERE build_id=? AND "
        "instance_id=? UNION ALL SELECT i.instance_id, t.d+1 FROM instances i JOIN t ON "
        "i.parent_id=t.iid WHERE i.build_id=? AND t.d < ?) "
        "SELECT i.definition AS definition, COUNT(*) AS n FROM t JOIN instances i ON "
        "i.instance_id=t.iid AND i.build_id=? WHERE t.d > 0 GROUP BY i.definition ORDER BY n DESC, "
        "i.definition LIMIT ?",
        s.bid,
        iid,
        s.bid,
        depth,
        s.bid,
        lim,
    )
    total_below = s.count(
        "WITH RECURSIVE t(iid, d) AS (SELECT instance_id, 0 FROM instances WHERE build_id=? AND "
        "instance_id=? UNION ALL SELECT i.instance_id, t.d+1 FROM instances i JOIN t ON "
        "i.parent_id=t.iid WHERE i.build_id=? AND t.d < ?) SELECT COUNT(*) FROM t WHERE d > 0",
        s.bid,
        iid,
        s.bid,
        depth,
    )
    nconn = s.count("SELECT COUNT(*) FROM port_bindings WHERE build_id=? AND instance_id=?", s.bid, iid)
    rows = s.rows(
        "SELECT * FROM port_bindings WHERE build_id=? AND instance_id=? ORDER BY ord LIMIT ?",
        s.bid,
        iid,
        lim,
    )
    did = inst["definition_declaration_id"]
    up, _ = graphs
    dependents = _closure_items(s, up, str(did), depth) if did else []
    return {
        "ancestors": [
            {"path": r["path"], "definition": r["definition"], "level": int(r["level"])} for r in anc
        ],
        "descendants": [{"definition": r["definition"], "instances": int(r["n"])} for r in below],
        "descendants_total": total_below,
        "connections": [_conn_item(r, {iid: str(inst["path"])}) for r in rows],
        "connections_total": nconn,
        "dependents": dependents,
        "summary": {
            "ancestors": len(anc),
            "descendant_instances": total_below,
            "port_connections": nconn,
            "dependent_definitions_of_its_definition": len(dependents),
        },
    }


def _twins(rows: list[sqlite3.Row]) -> list[sqlite3.Row] | None:
    """slang declares an ANSI port as a port AND an implicit internal net at the same span. When
    every candidate has the same name and source span they are ONE entity: return them with the
    port first. Anything else stays ambiguous (None)."""
    if len(rows) < 2:
        return None
    keys = {(r["name"], r["file"], r["span_start"], r["span_end"], r["scope"]) for r in rows}
    if len(keys) != 1:
        return None
    return sorted(rows, key=lambda r: (r["kind"] != "port", r["declaration_id"]))


def _fit(data: dict[str, Any], budget: int) -> list[str]:
    """Shrink the list sections until the JSON fits ``budget``; returns the keys that were cut. A
    cut list keeps its ``*_total``/``summary`` figures, so the omission is visible."""
    cut: list[str] = []
    while len(json.dumps(data, default=str).encode("utf-8")) > budget:
        key = max(
            (k for k in _LISTS if isinstance(data.get(k), list) and len(data[k]) > 1),
            key=lambda k: len(json.dumps(data[k], default=str)),
            default=None,
        )
        if key is None:
            break
        data[key] = data[key][: max(1, len(data[key]) // 2)]
        if key not in cut:
            cut.append(key)
    return cut


def impact(
    opts: WorkspaceOptions,
    build_id: str,
    request_id: str,
    *,
    symbol_id: str | None = None,
    file: str | None = None,
    byte: int | None = None,
    name: str | None = None,
    instance: str | None = None,
    depth: int = 3,
    limit: int | None = None,
) -> Envelope:
    modes = [
        symbol_id is not None,
        file is not None or byte is not None,
        name is not None,
        instance is not None,
    ]
    if sum(modes) != 1:
        raise SvwError(
            ErrorCode.INVALID_CONFIG,
            "give exactly one of: symbol_id, file+byte, name, instance",
            {"fields": ["symbol_id", "file+byte", "name", "instance"]},
        )
    if (file is None) != (byte is None) and not (name or symbol_id or instance):
        raise SvwError(ErrorCode.INVALID_CONFIG, "file and byte must be given together")
    if byte is not None and (not isinstance(byte, int) or byte < 0):
        raise SvwError(ErrorCode.INVALID_CONFIG, "byte must be a non-negative integer")
    if not isinstance(depth, int) or isinstance(depth, bool) or not 1 <= depth <= MAX_DEPTH:
        raise SvwError(ErrorCode.INVALID_CONFIG, f"depth must be in 1..{MAX_DEPTH}")
    lim = DEFAULT_LIST if limit is None else check_limit(limit)
    with _session(opts, build_id) as s:
        target: dict[str, Any]
        data: dict[str, Any]
        graphs = _definition_graph(s)
        if instance is not None:
            inst = _resolve_instance(s, instance)
            target = {"type": "instance", **_inst_dto(inst)}
            data = _instance_impact(s, inst, depth, lim, graphs)
        else:
            if symbol_id is not None:
                rows = s.rows(
                    "SELECT * FROM declarations WHERE build_id=? AND symbol_id=?", s.bid, symbol_id
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
            if not rows:
                raise SvwError(
                    ErrorCode.MISSING_INPUT,
                    "no declaration matches the target",
                    {"reason": "symbol_missing"},
                )
            twins = _twins(rows)
            if twins is not None:
                rows = [twins[0]]
            if len(rows) > 1:
                return build_envelope(
                    s.ctx,
                    s.db,
                    s.build,
                    request_id,
                    {"candidate_count": len(rows)},
                    outcome=Outcome.ERROR,
                    error=ErrorInfo(
                        code=ErrorCode.AMBIGUOUS_SYMBOL.value,
                        message=f"{len(rows)} scoped candidates; choose one by symbol_id "
                        "(no candidate was selected automatically)",
                        details={
                            "candidates": [
                                {
                                    "symbol_id": r["symbol_id"],
                                    "qualified_name": r["qualified_name"],
                                    "kind": r["kind"],
                                    "location": loads(r["loc"]),
                                }
                                for r in rows[:50]
                            ],
                            "candidate_count": len(rows),
                        },
                    ),
                    freshness=s.fresh(),
                )
            decl = rows[0]
            ids = [str(t["declaration_id"]) for t in twins] if twins else [str(decl["declaration_id"])]
            target = {"type": "declaration", **_decl_dto(decl)}
            if twins:
                target["same_entity"] = [
                    {"declaration_id": t["declaration_id"], "kind": t["kind"]} for t in twins
                ]
            if decl["kind"] in DEFINITION_KINDS:
                data = _definition_impact(s, decl, depth, lim, graphs)
            elif decl["kind"] == "port":
                data = _port_impact(s, decl, ids, depth, lim, graphs)
            else:
                data = _signal_impact(s, decl, ids, depth, lim, graphs)
        data = {
            "target": target,
            **data,
            "depth": depth,
            "limit": lim,
            "location_convention": LOCATION_CONVENTION,
            "note": IMPACT_NOTE,
        }
        warns: list[str] = []
        cut = _fit(data, MAX_RESPONSE_BYTES - 32_768)
        data["truncated_lists"] = cut
        if cut:
            warns.append(
                f"truncated: {', '.join(cut)} cut to fit the response cap; the *_total and summary "
                "figures are complete, items not listed must not be assumed absent"
            )
        for k in ("instances", "connections", "signal_uses", "descendants"):
            tot = data.get(f"{k}_total")
            if isinstance(tot, int) and isinstance(data.get(k), list) and tot > len(data[k]):
                warns.append(f"{k}: {len(data[k])} of {tot} listed (limit {lim})")
        return build_envelope(
            s.ctx,
            s.db,
            s.build,
            request_id,
            data,
            warnings=warns,
            extra_limits=[*_gap_limits(s), IMPACT_NOTE],
            freshness=s.fresh(),
        )
