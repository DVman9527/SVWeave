"""Static graph export of one build (ADR-014).

``svweave graph export --build B`` writes ONE self-contained HTML file under
``.svweave/exports/``: the elaborated instance hierarchy plus the connections derived from the
build's direct port associations, drawn by an inline viewer. Nothing is served: no socket, no
network, no external script. The page is data derived from one immutable build; it is not a netlist.

Connection edges mean "these two instances reference the same signal declaration in a direct port
association", oriented by the DECLARED port direction. There is no driver/load analysis, no bit
level and no cross-hierarchy dataflow (docs/limitations.md). Text coming from the sources is data:
it is embedded as JSON with ``<``, ``>``, ``&`` and the line separators escaped, and the viewer only
ever assigns it through ``textContent``.
"""

from __future__ import annotations

import hashlib
import re
from collections import defaultdict
from typing import Any

from svweave.application.graph_template import render_html
from svweave.application.options import WorkspaceOptions
from svweave.application.queries import _gap_limits, _session
from svweave.application.semantic_common import build_envelope
from svweave.domain.envelope import Envelope
from svweave.domain.errors import ErrorCode, SvwError
from svweave.security.safeio import ensure_private_dir, write_under
from svweave.storage.semantic import loads

EXPORT_DIR = "exports"
DEFAULT_MAX_INSTANCES = 5000
DEFAULT_MAX_FANOUT = 16
MAX_PORTS_PER_INSTANCE = 200
# declaration kinds that name a signal; parameters, genvars and the like are never connectivity
_SIGNAL_KINDS = frozenset({"net", "variable", "port"})

_SAFE_NAME = re.compile(r"[A-Za-z0-9._-]{1,120}")

GRAPH_NOTE = (
    "connection edges are derived from direct port associations: two instances share a signal "
    "declaration; direction is the declared port direction, not a driver/load analysis; not a netlist"
)


def _local_name(components: list[Any] | None, path: str) -> str:
    if components:
        last = components[-1]
        if isinstance(last, str):
            return last
        if isinstance(last, dict):
            for k in ("name", "text", "label"):
                v = last.get(k)
                if isinstance(v, str):
                    return v
    return path.rsplit(".", 1)[-1]


def _owner_instance(sig_path: str, by_path: dict[str, str]) -> tuple[str, str] | None:
    """(path, id) of the instance whose scope declares the signal: the longest instance path that
    prefixes the signal path. Walks the dotted path from the right, so cost is the path depth, not
    the number of instances (115k on a real SoC)."""
    end = len(sig_path)
    while True:
        end = sig_path.rfind(".", 0, end)
        if end <= 0:
            return None
        iid = by_path.get(sig_path[:end])
        if iid is not None:
            return sig_path[:end], iid


def build_model(s: Any, *, max_instances: int, max_fanout: int) -> dict[str, Any]:
    """Graph model of the build behind session ``s``. Pure function of the stored rows."""
    rows = s.rows("SELECT * FROM instances WHERE build_id=? ORDER BY ord", s.bid)
    total = len(rows)
    depth: dict[str, int] = {}
    for r in rows:  # ord is pre-order, so a parent is always seen before its children
        pid = r["parent_id"]
        depth[r["instance_id"]] = 0 if pid is None or pid not in depth else depth[pid] + 1
    kept_rows = sorted(rows, key=lambda r: (depth[r["instance_id"]], r["ord"]))[:max_instances]
    kept = {r["instance_id"] for r in kept_rows}
    kept_rows = sorted(kept_rows, key=lambda r: r["ord"])
    by_path = {r["path"]: r["instance_id"] for r in rows}
    path_by_id = {r["instance_id"]: r["path"] for r in rows}

    decl_kind: dict[str, str] = {}
    for d in s.rows("SELECT declaration_id, kind FROM declarations WHERE build_id=?", s.bid):
        decl_kind[d["declaration_id"]] = d["kind"]

    ports: dict[str, list[dict[str, Any]]] = defaultdict(list)
    members: dict[str, dict[str, Any]] = {}
    for b in s.rows("SELECT * FROM port_bindings WHERE build_id=? ORDER BY ord", s.bid):
        iid = b["instance_id"]
        refs = loads(b["referenced_symbols"]) or []
        sigs = [
            x
            for x in refs
            if isinstance(x, dict) and decl_kind.get(str(x.get("declaration_id"))) in _SIGNAL_KINDS
        ]
        if iid in kept and len(ports[iid]) < MAX_PORTS_PER_INSTANCE:
            ports[iid].append(
                {
                    "port": b["formal_port"],
                    "dir": b["direction"],
                    "actual": b["actual_expression"],
                    "kind": b["actual_kind"],
                    "signals": [str(x.get("path")) for x in sigs],
                    "supported": bool(b["supported"]),
                }
            )
        for x in sigs:
            m = members.setdefault(
                str(x["declaration_id"]), {"path": str(x.get("path")), "uses": []}
            )
            m["uses"].append((iid, b["formal_port"], b["direction"]))

    edges: list[dict[str, Any]] = []
    hubs: list[dict[str, Any]] = []
    for m in members.values():
        users = {iid for iid, _, _ in m["uses"]}
        if len(users) > max_fanout:
            hubs.append({"signal": m["path"], "instances": len(users)})
            continue
        found = _owner_instance(m["path"], by_path)
        owner_path, owner = found if found is not None else ("", None)
        outs = [u for u in m["uses"] if u[2] == "out"]
        ins = [u for u in m["uses"] if u[2] == "in"]
        for iid, port, dirn in m["uses"]:  # signal declared in a parent scope -> parent edge
            if owner is None or owner == iid or iid not in kept or owner not in kept:
                continue
            if not path_by_id.get(iid, "").startswith(owner_path + "."):
                continue
            if dirn == "out":
                src, dst = iid, owner
            else:
                src, dst = owner, iid
            edges.append(
                {
                    "from": src,
                    "to": dst,
                    "signal": m["path"],
                    "port": port,
                    "directed": dirn in ("in", "out"),
                }
            )
        for o_iid, o_port, _ in outs:
            for i_iid, i_port, _ in ins:
                if o_iid == i_iid or o_iid not in kept or i_iid not in kept:
                    continue
                edges.append(
                    {
                        "from": o_iid,
                        "to": i_iid,
                        "signal": m["path"],
                        "port": f"{o_port} -> {i_port}",
                        "directed": True,
                    }
                )
    seen: set[tuple[Any, ...]] = set()
    uniq = []
    for e in edges:
        k = (e["from"], e["to"], e["signal"], e["port"])
        if k not in seen:
            seen.add(k)
            uniq.append(e)

    nodes = []
    for r in kept_rows:
        iid = r["instance_id"]
        loc = loads(r["loc"]) or {}
        nodes.append(
            {
                "id": iid,
                "parent": r["parent_id"] if r["parent_id"] in kept else None,
                "path": r["path"],
                "name": _local_name(loads(r["components"]), r["path"]),
                "definition": r["definition"],
                "depth": depth[iid],
                "top": bool(r["is_top"]),
                "interface": bool(r["is_interface"]),
                "stub": bool(r["is_stub"]),
                "blackbox": bool(r["blackbox_candidate"]),
                "generate": loads(r["generate_context"]) or [],
                "params": [
                    {
                        "name": p.get("name"),
                        "value": p.get("value"),
                        "overridden": bool(p.get("overridden")),
                    }
                    for p in (loads(r["parameters"]) or [])
                    if isinstance(p, dict)
                ],
                "file": loc.get("file") if isinstance(loc, dict) else None,
                "line": loc.get("line") if isinstance(loc, dict) else None,
                "ports": ports.get(iid, []),
            }
        )
    return {
        "nodes": nodes,
        "edges": uniq,
        "hubs": sorted(hubs, key=lambda h: (-h["instances"], h["signal"])),
        "stats": {
            "instances_total": total,
            "instances_shown": len(nodes),
            "edges": len(uniq),
            "hub_signals": len(hubs),
            "max_instances": max_instances,
            "max_fanout": max_fanout,
        },
        "truncated": total > len(nodes),
        "note": GRAPH_NOTE,
    }


def export_graph(
    opts: WorkspaceOptions,
    build_id: str,
    request_id: str,
    *,
    name: str | None = None,
    max_instances: int = DEFAULT_MAX_INSTANCES,
    max_fanout: int = DEFAULT_MAX_FANOUT,
) -> Envelope:
    if not isinstance(max_instances, int) or not 1 <= max_instances <= 100_000:
        raise SvwError(ErrorCode.INVALID_CONFIG, "max_instances must be in 1..100000")
    if not isinstance(max_fanout, int) or not 2 <= max_fanout <= 1000:
        raise SvwError(ErrorCode.INVALID_CONFIG, "max_fanout must be in 2..1000")
    with _session(opts, build_id) as s:
        model = build_model(s, max_instances=max_instances, max_fanout=max_fanout)
        fname = f"{name or 'graph-' + s.bid[:12]}.html"
        if not _SAFE_NAME.fullmatch(fname) or fname.startswith("."):
            raise SvwError(ErrorCode.INVALID_CONFIG, "name must match [A-Za-z0-9._-]+ (no path)")
        meta = {
            "build_id": s.bid,
            "snapshot_id": str(s.build["snapshot_id"]),
            "profile": str(s.build["profile"]),
            "completeness": str(s.build["completeness"]),
            "freshness": s.fresh().value,
            "evidence_kind": "HDL_STATIC_RESOLVED",
            "workspace_id": str(s.build["workspace_id"]),
        }
        page = render_html({"meta": meta, **model})
        data = page.encode("utf-8")
        exports = ensure_private_dir(s.ctx.state_dir, EXPORT_DIR)
        write_under(exports, fname, data)
        warns = []
        limits = _gap_limits(s) + [GRAPH_NOTE]
        if model["truncated"]:
            warns.append(
                f"truncated: {model['stats']['instances_shown']} of "
                f"{model['stats']['instances_total']} instances shown (shallowest first); "
                "an instance not drawn must not be assumed absent"
            )
        if model["hubs"]:
            warns.append(
                f"{len(model['hubs'])} signal(s) referenced by more than {max_fanout} instances "
                "are listed as hubs, not drawn as edges"
            )
        return build_envelope(
            s.ctx,
            s.db,
            s.build,
            request_id,
            {
                "path": f".svweave/{EXPORT_DIR}/{fname}",
                "bytes": len(data),
                "sha256": hashlib.sha256(data).hexdigest(),
                "stats": model["stats"],
                "truncated": model["truncated"],
                "note": GRAPH_NOTE,
                "served": False,
            },
            warnings=warns,
            extra_limits=limits,
            freshness=s.fresh(),
        )
