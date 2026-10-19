"""Parse cbm tool output and normalize it into SVWeave candidates (pure functions)."""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any

from svweave.adapters.codebase_memory.models import CbmCandidate, IndexMapping

STATIC_LIMITATIONS: tuple[str, ...] = (
    "codebase-memory-mcp extracts SystemVerilog only partially: ports, generate blocks, "
    "instances, bind directives and assertions are not in the graph; absence of a hit is not "
    "proof of absence",
    "cbm labels SystemVerilog modules/packages as 'Class' and uses 'Module' for a source file; "
    "the upstream label is reported unchanged",
    "graph hits are source candidates, not evidence that a given build uses them",
)

_LINES = re.compile(r"^(\d+)(?:-(\d+))?$")


def parse_lines(s: object) -> tuple[int | None, int | None]:
    m = _LINES.match(str(s).strip()) if s is not None else None
    if not m:
        return None, None
    a = int(m.group(1))
    return a, int(m.group(2)) if m.group(2) else a


def safe_rel_path(p: object) -> str | None:
    """Snapshot-relative POSIX path, or None when the upstream path is absolute / escaping."""
    if not isinstance(p, str) or not p or p.startswith("/") or "\x00" in p:
        return None
    parts = p.split("/")
    if any(x in ("..", "") for x in parts):
        return None
    return p


@dataclass
class IndexReport:
    project: str
    status: str
    nodes: int
    edges: int
    parse_unusable: tuple[str, ...]
    parse_partial: tuple[tuple[str, str], ...]
    root_path: str | None
    indexed_at: str | None


def _files(block: Any) -> list[dict[str, Any]]:
    if isinstance(block, dict):
        files = block.get("files", [])
        return [f for f in files if isinstance(f, dict)]
    return []


def parse_index_report(payload: dict[str, Any]) -> IndexReport:
    unusable = tuple(str(f["path"]) for f in _files(payload.get("parse_unusable")) if "path" in f)
    partial = tuple(
        (str(f["path"]), str(f.get("error_ranges", ""))) for f in _files(payload.get("parse_partial")) if "path" in f
    )
    return IndexReport(
        project=str(payload.get("project", "")),
        status=str(payload.get("status", "")),
        nodes=int(payload.get("nodes", 0) or 0),
        edges=int(payload.get("edges", 0) or 0),
        parse_unusable=unusable,
        parse_partial=partial,
        root_path=payload.get("root_path") if isinstance(payload.get("root_path"), str) else None,
        indexed_at=payload.get("indexed_at") if isinstance(payload.get("indexed_at"), str) else None,
    )


def _overlaps(ranges: str, start: int | None, end: int | None) -> bool:
    if start is None or end is None:
        return True  # unknown span: assume affected
    for part in ranges.split(","):
        a, b = parse_lines(part)
        if a is not None and b is not None and not (b < start or a > end):
            return True
    return False


def coverage_for(m: IndexMapping, file: str, start: int | None, end: int | None) -> dict[str, Any]:
    if file in m.parse_unusable:
        return {"status": "parse_unusable", "note": "whole file failed to parse; graph content unreliable"}
    for path, ranges in m.parse_partial:
        if path == file:
            return {
                "status": "parse_partial",
                "error_ranges": ranges,
                "overlaps_hit": _overlaps(ranges, start, end),
            }
    return {"status": "no_recorded_issue"}


def index_limitations(m: IndexMapping, cap: int = 20) -> list[str]:
    out: list[str] = []
    for p in m.parse_unusable[:cap]:
        out.append(f"{p}: parse_unusable (cbm could not parse this file; read the source directly)")
    for p, r in m.parse_partial[:cap]:
        out.append(f"{p}: parse_partial at lines {r} (constructs in these lines may be missing)")
    extra = len(m.parse_unusable) + len(m.parse_partial) - len(out)
    if extra > 0:
        out.append(f"... and {extra} more files with recorded parse gaps")
    return out


def parse_search_rows(payload: dict[str, Any]) -> list[dict[str, Any]]:
    """Rows of a ``search_graph`` JSON response (``query`` mode: cols qn,label,file,lines,rank)."""
    cols = payload.get("cols")
    rows = payload.get("rows")
    if not isinstance(cols, list) or not isinstance(rows, list):
        return []
    out: list[dict[str, Any]] = []
    for r in rows:
        if isinstance(r, list) and len(r) == len(cols):
            out.append(dict(zip(cols, r, strict=True)))
    return out


def normalize_hits(
    rows: Iterable[dict[str, Any]], m: IndexMapping
) -> tuple[list[CbmCandidate], list[str], list[int]]:
    """Rows -> candidates. Hits whose qualified name is not under this snapshot's project, or whose
    file path escapes the snapshot, are dropped and reported (never silently)."""
    prefix = m.project + "."
    cands: list[CbmCandidate] = []
    dropped: list[str] = []
    positions: list[int] = []  # upstream row index of each kept candidate (pagination progress)
    for idx, row in enumerate(rows):
        qn = str(row.get("qn", ""))
        file = safe_rel_path(row.get("file"))
        if not qn.startswith(prefix) or file is None:
            dropped.append(f"dropped hit with foreign project or unsafe path: {qn[:80]!r}")
            continue
        positions.append(idx)
        local = qn[len(prefix) :]
        start, end = parse_lines(row.get("lines"))
        rank = row.get("rank")
        cands.append(
            CbmCandidate(
                snapshot_id=m.snapshot_id,
                name=local.rsplit(".", 1)[-1],
                upstream_label=str(row.get("label", "")),
                qualified_name=local,
                upstream_qualified_name=qn,
                file=file,
                start_line=start,
                end_line=end,
                rank=float(rank) if isinstance(rank, int | float) else None,
                coverage=coverage_for(m, file, start, end),
            )
        )
    return cands, dropped, positions


def parse_snippet(text: str) -> dict[str, Any]:
    """``get_code_snippet`` returns a YAML-like block; extract scalar fields and the source body."""
    meta: dict[str, Any] = {}
    body: list[str] = []
    in_src = False
    for line in text.splitlines():
        if in_src:
            if line.startswith("  ") or line == "":
                body.append(line[2:] if line.startswith("  ") else "")
                continue
            in_src = False
        if line.startswith("source: |"):
            in_src = True
            continue
        if ": " in line and not line.startswith(" "):
            k, v = line.split(": ", 1)
            meta[k.strip()] = v.strip().strip('"')
    meta["source"] = "\n".join(body).rstrip("\n")
    return meta
