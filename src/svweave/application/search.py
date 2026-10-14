"""``svw_search`` use case (plan 14.3, 11.3, 25.1; tests INT-04, API-02).

Graph path: codebase-memory-mcp ``search_graph`` on the index bound to ``snapshot_id``.
Fallback: bounded literal text search over the immutable snapshot files, always reported as
``backend_used="text_fallback"`` with ``degraded=true`` (a successful text search never implies the
graph backend is healthy). No regular expressions are executed by the fallback.
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from svweave.adapters.codebase_memory.adapter import BACKEND as CBM_BACKEND
from svweave.config.paths import glob_match
from svweave.domain import cursor as dcursor
from svweave.domain.envelope import (
    AnalysisCoverage,
    Envelope,
    Pagination,
    Provenance,
    new_request_id,
)
from svweave.domain.envelope import Outcome as EnvOutcome
from svweave.domain.errors import ErrorCode, SvwError
from svweave.domain.states import Completeness, EvidenceKind, Freshness
from svweave.orchestration.registry import CBM, BackendRegistry
from svweave.security import safeio
from svweave.security.sandbox import IsolationMode

TEXT_FALLBACK = "text_fallback"
DEFAULT_LIMIT = dcursor.DEFAULT_LIMIT
MAX_LIMIT = dcursor.MAX_LIMIT
MAX_QUERY_CHARS = 512
MAX_OFFSET = 10_000
MIN_CURSOR_KEY_BYTES = 16
MAX_SCOPE_CHARS = 256
MAX_RESPONSE_BYTES = dcursor.MAX_RESPONSE_BYTES
SNIPPET_CHARS = 200

#: Graph failures that fall back to text search (anything else is a caller/programming error).
FALLBACK_CODES = frozenset(
    {
        ErrorCode.BACKEND_UNAVAILABLE,
        ErrorCode.CAPABILITY_MISMATCH,
        ErrorCode.STALE_SNAPSHOT,
        ErrorCode.BUILD_TIMEOUT,
        ErrorCode.RESOURCE_LIMIT,
        ErrorCode.OUTPUT_LIMIT,
    }
)


@dataclass(frozen=True)
class TextSearchLimits:
    max_files: int = 20_000
    max_file_bytes: int = 2 * 1024 * 1024
    max_total_bytes: int = 256 * 1024 * 1024
    time_budget_s: float = 5.0


DEFAULT_TEXT_LIMITS = TextSearchLimits()


# ---------------------------------------------------------------------- validation / cursor
def validate_request(
    snapshot_id: str, query: str, scope: str | None, limit: int
) -> None:
    if not snapshot_id or len(snapshot_id) > 128 or not all(c.isalnum() or c in "-_." for c in snapshot_id):
        raise SvwError(ErrorCode.INVALID_CONFIG, "invalid snapshot_id", {"field": "snapshot_id"})
    if not query or len(query) > MAX_QUERY_CHARS or "\x00" in query:
        raise SvwError(ErrorCode.INVALID_CONFIG, f"query must be 1..{MAX_QUERY_CHARS} chars without NUL",
                       {"field": "query"})
    if scope is not None:
        parts = scope.split("/")
        if (not scope or len(scope) > MAX_SCOPE_CHARS or scope.startswith("/") or ".." in parts
                or "\x00" in scope):
            raise SvwError(ErrorCode.INVALID_CONFIG, "scope must be a relative glob without '..'",
                           {"field": "scope"})
    dcursor.check_limit(limit)


def query_hash(query: str, scope: str | None) -> str:
    return dcursor.query_hash("svw_search", {"query": query, "scope": scope})


def encode_cursor(snapshot_id: str, qhash: str, mode: str, offset: int, key: bytes) -> str:
    return dcursor.encode_cursor(key, snapshot_id, qhash, mode, offset)


def decode_cursor(
    cursor: str, snapshot_id: str, qhash: str, key: bytes
) -> tuple[str, int]:
    """Validate a cursor against THIS snapshot and query; returns (backend mode, offset).

    The backend mode is the cursor's sort name; a cursor minted by the graph backend and one minted
    by the text fallback are both valid for the same query (the caller decides what to do).
    """
    errors: list[SvwError] = []
    for mode in (CBM_BACKEND, TEXT_FALLBACK):
        try:
            return mode, dcursor.decode_cursor(key, cursor, snapshot_id, qhash, mode)
        except SvwError as e:
            errors.append(e)
    raise next((e for e in errors if "sort order" not in e.message), errors[0])


# ---------------------------------------------------------------------- text fallback
@dataclass
class TextHit:
    file: str
    line: int  # 1-based
    byte_start: int  # canonical span: zero-based half-open over the file bytes
    byte_end: int
    snippet: str


@dataclass
class TextScan:
    hits: list[TextHit]
    has_more: bool
    files_scanned: int
    files_skipped: list[str]
    truncated_by_budget: bool


def _iter_files(root: Path, scope: str | None, max_files: int, deadline: float) -> tuple[list[str], list[str]]:
    """Files in deterministic walk order (sorted per directory). Stops at ``max_files`` / deadline,
    so a gigantic tree cannot make the listing itself unbounded. Returns (files, notes)."""
    rels: list[str] = []
    notes: list[str] = []
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        dirnames[:] = sorted(d for d in dirnames if d != ".git")
        for fn in sorted(filenames):
            if time.monotonic() > deadline:
                notes.append("time budget exhausted while listing files")
                return rels, notes
            p = Path(dirpath) / fn
            if p.is_symlink():
                continue
            rel = p.relative_to(root).as_posix()
            if scope is None or glob_match(scope, rel):
                if len(rels) >= max_files:
                    notes.append(f"file list capped at {max_files} files")
                    return rels, notes
                rels.append(rel)
    return rels, notes


def text_search(
    root: Path,
    query: str,
    scope: str | None,
    offset: int,
    limit: int,
    limits: TextSearchLimits = DEFAULT_TEXT_LIMITS,
) -> TextScan:
    """Bounded literal (case-sensitive, non-regex) search; deterministic order (walk order, offset).

    Every loop is bounded: the deadline is checked per match, line numbers advance incrementally
    (never an O(position) count per match), line bounds are computed once per line, snippets are a
    fixed window around the match and ``offset`` is capped.
    """
    if offset > MAX_OFFSET:
        raise SvwError(ErrorCode.INVALID_CONFIG, f"offset beyond {MAX_OFFSET} is not supported",
                       {"field": "cursor", "reason": "offset_too_large"})
    needle = query.encode("utf-8")
    deadline = time.monotonic() + limits.time_budget_s
    want = offset + limit + 1  # one extra match proves has_more
    page: list[TextHit] = []
    seen = 0
    skipped: list[str] = []
    scanned = 0
    total_bytes = 0
    truncated = False
    files, notes = _iter_files(root, scope, limits.max_files, deadline)
    if notes:
        skipped.extend(notes)
        truncated = True
    for rel in files:
        if seen >= want:
            break
        if time.monotonic() > deadline:
            truncated = True
            skipped.append("time budget exhausted before all files were scanned")
            break
        try:
            size = (root / rel).stat().st_size
            if size > limits.max_file_bytes:
                skipped.append(f"{rel}: larger than {limits.max_file_bytes} bytes, skipped")
                continue
            if total_bytes + size > limits.max_total_bytes:
                truncated = True
                skipped.append("total byte budget exhausted before all files were scanned")
                break
            data = safeio.read_regular(root / rel)
        except (OSError, SvwError) as e:
            skipped.append(f"{rel}: unreadable ({type(e).__name__})")
            continue
        total_bytes += len(data)
        scanned += 1
        if b"\x00" in data[:8192]:
            skipped.append(f"{rel}: binary, skipped")
            continue
        pos = data.find(needle)
        line_no, counted_to = 1, 0  # newlines before `counted_to` are already in line_no - 1
        ls, le = 0, -1  # bounds of the current line (le < 0: not computed yet)
        while pos >= 0:
            if time.monotonic() > deadline:
                truncated = True
                skipped.append(f"time budget exhausted while scanning {rel}")
                break
            if pos > le:  # moved to a later line: advance the counter and recompute the bounds
                line_no += data.count(b"\n", counted_to, pos)
                counted_to = pos
                ls = data.rfind(b"\n", 0, pos) + 1
                le = data.find(b"\n", pos)
                le = len(data) if le < 0 else le
            if seen >= offset:
                lo = max(ls, pos - SNIPPET_CHARS // 2)
                hi = min(le, pos + len(needle) + SNIPPET_CHARS // 2)
                snippet = data[lo:hi].decode("utf-8", errors="replace").rstrip("\r")
                page.append(TextHit(rel, line_no, pos, pos + len(needle), snippet))
            seen += 1
            if seen >= want:
                break
            pos = data.find(needle, pos + max(1, len(needle)))
        if truncated and time.monotonic() > deadline:
            break
    return TextScan(
        hits=page[:limit],
        has_more=seen > offset + limit,
        files_scanned=scanned,
        files_skipped=skipped,
        truncated_by_budget=truncated,
    )


# ---------------------------------------------------------------------- use case
ENVELOPE_RESERVE_BYTES = 8 * 1024  # envelope fields around data.items (limitations, warnings, ...)


def _cap_items(items: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], bool]:
    """Keep the whole serialized response (items + envelope) under MAX_RESPONSE_BYTES."""
    out: list[dict[str, Any]] = []
    size = 0
    for it in items:
        sz = len(json.dumps(it)) + 2
        if size + sz > MAX_RESPONSE_BYTES - ENVELOPE_RESERVE_BYTES:
            return out, True
        out.append(it)
        size += sz
    return out, False


def svw_search(
    *,
    snapshot_id: str,
    snapshot_root: Path,
    query: str,
    scope: str | None = None,
    limit: int = DEFAULT_LIMIT,
    cursor: str | None = None,
    registry: BackendRegistry | None = None,
    request_id: str | None = None,
    workspace_id: str | None = None,
    profile: str | None = None,
    text_limits: TextSearchLimits = DEFAULT_TEXT_LIMITS,
    cursor_key: bytes,
) -> Envelope:
    validate_request(snapshot_id, query, scope, limit)
    if not isinstance(cursor_key, bytes) or len(cursor_key) < MIN_CURSOR_KEY_BYTES:
        raise SvwError(ErrorCode.INVALID_CONFIG,
                       f"cursor_key (workspace secret) of at least {MIN_CURSOR_KEY_BYTES} bytes is required",
                       {"field": "cursor_key"})
    key = cursor_key
    root = Path(snapshot_root)
    if not root.is_dir():
        raise SvwError(ErrorCode.MISSING_INPUT, "snapshot directory not found",
                       {"snapshot_id": snapshot_id})
    qh = query_hash(query, scope)
    rid = request_id or new_request_id()
    cur_mode, offset = ("", 0)
    if cursor:
        cur_mode, offset = decode_cursor(cursor, snapshot_id, qh, key)

    warnings: list[str] = []
    fallback_reason: str | None = None
    if registry is None:
        fallback_reason = "no graph backend configured"
    elif not registry.usable(CBM):
        h = registry.health(CBM)
        fallback_reason = "graph backend " + h.status.value + ": " + "; ".join(h.reasons)[:300]
    else:
        # A cursor minted by the text fallback must keep paging the text results.
        if cur_mode == TEXT_FALLBACK:
            fallback_reason = "continuing a text_fallback cursor"
        else:
            try:
                return _graph_search(registry, snapshot_id, root, query, scope, limit, offset, qh, rid,
                                     workspace_id, profile, key)
            except SvwError as e:
                if e.code not in FALLBACK_CODES:
                    raise
                if e.code in (ErrorCode.BACKEND_UNAVAILABLE, ErrorCode.CAPABILITY_MISMATCH):
                    registry.note_failure(CBM, e)
                fallback_reason = f"graph search failed: {e.code.value} ({e.details.get('reason', e.message)})"
                if cur_mode == CBM_BACKEND:
                    warnings.append("cursor from the graph backend restarted as a text_fallback search")
                    offset = 0
    return _text_response(root, snapshot_id, query, scope, limit, offset, qh, rid, workspace_id, profile,
                          fallback_reason or "", warnings, text_limits, key)


def _envelope(
    *, rid: str, snapshot_id: str, workspace_id: str | None, profile: str | None, outcome: EnvOutcome,
    completeness: Completeness, scope: str | None, basis: str, limitations: list[str], backend: str,
    data: dict[str, Any], pagination: Pagination, warnings: list[str],
) -> Envelope:
    return Envelope(
        request_id=rid,
        outcome=outcome,
        workspace_id=workspace_id,
        snapshot_id=snapshot_id,
        profile=profile,
        freshness=Freshness.UNKNOWN,
        analysis_coverage=AnalysisCoverage(
            completeness=completeness, scope=scope, basis=basis, unresolved_count=0,
            limitations=limitations),
        provenance=Provenance(backend=backend, evidence_kind=EvidenceKind.SOURCE_CANDIDATE),
        data=data,
        pagination=pagination,
        warnings=warnings,
    )


def _graph_search(
    registry: BackendRegistry, snapshot_id: str, root: Path, query: str, scope: str | None,
    limit: int, offset: int, qh: str, rid: str, workspace_id: str | None, profile: str | None,
    key: bytes,
) -> Envelope:
    ad = registry.cbm(root)
    ad.ensure_indexed(snapshot_id, root)
    res = ad.search(snapshot_id, query, file_pattern=scope, limit=limit, offset=offset)
    kept = [
        (c, pos) for c, pos in zip(res.candidates, res.raw_positions, strict=True)
        if scope is None or glob_match(scope, c.file)
    ]
    items, capped = _cap_items([c.to_json() for c, _ in kept])
    warnings = list(res.warnings)
    # Advance by UPSTREAM ROWS CONSUMED (post-filtering must not make the cursor re-read rows).
    consumed = kept[len(items) - 1][1] + 1 if capped and items else res.rows_consumed
    more = res.has_more or capped
    next_off: int | None = offset + consumed if more else None
    if next_off is not None and next_off <= offset:
        next_off = None  # never emit a cursor that makes no progress
        warnings.append("pagination stopped: upstream returned no rows to advance past")
    degraded = ad.isolation is IsolationMode.DEGRADED_NO_ISOLATION
    data: dict[str, Any] = {
        "items": items, "backend_used": CBM_BACKEND, "degraded": degraded, "query": query,
        "total": res.total, "index_fingerprint": res.mapping.content_fingerprint if res.mapping else None,
    }
    if degraded:
        data["degraded_reason"] = "graph backend running WITHOUT process isolation (no bwrap)"
        warnings.insert(0, "DEGRADED ISOLATION: " + data["degraded_reason"])
    return _envelope(
        rid=rid, snapshot_id=snapshot_id, workspace_id=workspace_id, profile=profile,
        outcome=EnvOutcome.PARTIAL, completeness=Completeness.PARTIAL, scope=scope,
        basis="graph_candidates_best_effort", limitations=res.limitations, backend=CBM_BACKEND,
        data=data,
        pagination=Pagination(
            limit=limit, truncated=more,
            next_cursor=encode_cursor(snapshot_id, qh, CBM_BACKEND, next_off, key) if next_off is not None else None),
        warnings=warnings + (["response capped at 128 KiB; use next_cursor"] if capped else []),
    )


def _text_response(
    root: Path, snapshot_id: str, query: str, scope: str | None, limit: int, offset: int, qh: str,
    rid: str, workspace_id: str | None, profile: str | None, reason: str, warnings: list[str],
    limits: TextSearchLimits, key: bytes,
) -> Envelope:
    scan = text_search(root, query, scope, offset, limit, limits)
    items = [
        {
            "snapshot_id": snapshot_id, "backend": TEXT_FALLBACK,
            "evidence_kind": EvidenceKind.SOURCE_CANDIDATE.value, "resolution_status": "candidate_source",
            "file": h.file, "line": h.line, "byte_start": h.byte_start, "byte_end": h.byte_end,
            "position_base": "zero_based_half_open_bytes", "snippet": h.snippet,
        }
        for h in scan.hits
    ]
    items, capped = _cap_items(items)
    truncated = scan.has_more or capped or scan.truncated_by_budget
    more = (scan.has_more or capped) and bool(items)  # a cursor must make progress
    complete = not truncated and not scan.truncated_by_budget and not scan.files_skipped
    limitations = [
        "text_fallback: literal text matches only; no symbol resolution, scoping or build context",
        "a graph backend result set may differ; this is not evidence that the graph backend works",
        *scan.files_skipped[:20],
    ]
    return _envelope(
        rid=rid, snapshot_id=snapshot_id, workspace_id=workspace_id, profile=profile,
        outcome=EnvOutcome.PARTIAL,  # degraded answers are never presented as plain OK
        completeness=Completeness.COMPLETE if complete else Completeness.PARTIAL, scope=scope,
        basis="literal_text_scan", limitations=limitations, backend=TEXT_FALLBACK,
        data={"items": items, "backend_used": TEXT_FALLBACK, "degraded": True, "degraded_reason": reason,
              "query": query, "files_scanned": scan.files_scanned},
        pagination=Pagination(
            limit=limit, truncated=truncated,
            next_cursor=encode_cursor(snapshot_id, qh, TEXT_FALLBACK, offset + len(items), key) if more else None),
        warnings=[f"DEGRADED: {reason}", *warnings]
        + (["response capped at 128 KiB; use next_cursor"] if capped else [])
        + (["text scan hit its file/byte/time budget; results may be incomplete"] if scan.truncated_by_budget else []),
    )
