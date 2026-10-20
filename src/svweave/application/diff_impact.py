"""What a git diff touches, mapped onto one build (``svweave query diff-impact`` / ``svw_diff_impact``).

The diff is read with ``git diff -U0`` (fixed argv, no shell, no external diff driver, no textconv,
no fsmonitor; a read of the object database and the working tree, never a write). Its OLD-side line
ranges are mapped onto the build's SNAPSHOT bytes, because the build describes the code as it was
when it was captured: the intended use is "I built (or can build) the baseline; what does my change
touch in it?". The mapping is only exact for a file whose snapshot bytes equal the base version, and
the answer says per file whether they do (``aligned``). A file that does not align is mapped on a
best-effort basis and flagged; it is never presented as exact.

Each changed range is attributed to the innermost declaration(s) of the build that cover it, then
to the module/interface that owns them: the instances of that definition, the definitions that
instantiate it, and, for a changed port, its connection sites. Declarations that are not instance
based (package, class, free subroutine) are listed with their reference counts only.

What it does not do: a change inside an ``include``d header or a macro definition is attributed to
declarations in that file only, the files that include it are not re-expanded; a changed file that
is not part of the build (testbench, docs, an inactive library member) is listed, not analysed; no
runtime, UVM factory/config_db or bit-level reasoning. Counts are counts, not a risk score.
"""

from __future__ import annotations

import hashlib
import os
import re
import sqlite3
import subprocess
import threading
import time
from pathlib import Path
from typing import Any

from svweave.application.impact import (
    DEFAULT_LIST,
    DEFINITION_KINDS,
    MAX_DEPTH,
    _closure_items,
    _definition_graph,
    _fit,
    _instances_of,
    _names,
    _owner_definition,
    _references_of,
    _twins,
)
from svweave.application.options import WorkspaceOptions
from svweave.application.queries import _decl_dto, _gap_limits, _session
from svweave.application.semantic_common import build_envelope
from svweave.domain.cursor import MAX_RESPONSE_BYTES, check_limit
from svweave.domain.envelope import Envelope
from svweave.domain.errors import ErrorCode, SvwError
from svweave.snapshot.capture import load_record, snapshots_dir

DIFF_NOTE = (
    "old-side line ranges of the git diff mapped onto the build's snapshot; exact only for files "
    "marked aligned; header/macro changes are not re-expanded into including files; counts, not a "
    "risk score"
)
MAX_DIFF_BYTES = 64 * 1024 * 1024
GIT_TIMEOUT_S = 60
_REF = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9._/@^~+:-]{0,199}$")
_HUNK = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")
_LISTS = (
    "affected_definitions",
    "affected_declarations",
    "non_instance_declarations",
    "files",
    "unmapped_files",
    "unattributed",
)


def check_ref(ref: str, field: str) -> str:
    """A git revision as a plain token: never an option, never whitespace or control characters."""
    if not isinstance(ref, str) or not _REF.match(ref) or ".." in ref:
        raise SvwError(
            ErrorCode.INVALID_CONFIG,
            f"{field} must be a plain git revision (letters, digits, . _ / @ ^ ~ + : -; no '..', "
            "no leading '-')",
            {"field": field},
        )
    return ref


def _git(repo: Path, args: list[str]) -> bytes:
    argv = [
        "git",
        "-C",
        str(repo),
        "-c",
        "core.fsmonitor=false",
        "-c",
        "core.quotePath=false",
        "--no-pager",
        *args,
    ]
    env = {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "HOME": os.environ.get("HOME", "/nonexistent"),
        "LC_ALL": "C",
        "GIT_OPTIONAL_LOCKS": "0",
        "GIT_TERMINAL_PROMPT": "0",
    }
    try:
        proc = subprocess.Popen(  # noqa: S603 - fixed argv, no shell, refs validated by check_ref
            argv,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            stdin=subprocess.DEVNULL,
            env=env,
        )
    except FileNotFoundError as e:
        raise SvwError(
            ErrorCode.BACKEND_UNAVAILABLE, "git is not installed", {"reason": "git_missing"}
        ) from e
    # stdout is read in bounded chunks and git is killed the moment the cap is exceeded: the diff
    # (or a blob) is never buffered whole just to be refused afterwards. stderr is drained on a
    # thread so a chatty git cannot block on a full pipe.
    err: list[bytes] = []

    def drain_err() -> None:
        assert proc.stderr is not None
        err.append(proc.stderr.read(1 << 16))
        while proc.stderr.read(1 << 16):
            pass

    t = threading.Thread(target=drain_err, daemon=True)
    t.start()
    chunks: list[bytes] = []
    size = 0
    over = timed_out = False
    deadline = time.monotonic() + GIT_TIMEOUT_S
    assert proc.stdout is not None
    while True:
        if time.monotonic() > deadline:
            timed_out = True
            proc.kill()
            break
        chunk = proc.stdout.read(1 << 20)
        if not chunk:
            break
        size += len(chunk)
        if size > MAX_DIFF_BYTES:
            over = True
            proc.kill()
            break
        chunks.append(chunk)
    rc = proc.wait()
    t.join(timeout=5)
    for f in (proc.stdout, proc.stderr):
        if f is not None:
            f.close()
    if timed_out:
        raise SvwError(
            ErrorCode.BUILD_TIMEOUT, f"git did not finish in {GIT_TIMEOUT_S}s", {"reason": "git_timeout"}
        )
    if over:
        raise SvwError(
            ErrorCode.OUTPUT_LIMIT, "the diff is larger than 64 MiB", {"reason": "diff_too_large"}
        )
    if rc != 0:
        first = (err[0] if err else b"").decode("utf-8", "replace").strip().splitlines()[:1]
        raise SvwError(
            ErrorCode.MISSING_INPUT,
            "git could not answer: " + (first[0][:200] if first else f"exit {rc}"),
            {"reason": "git_failed", "args": args[:3]},
        )
    return b"".join(chunks)


class FileDiff:
    def __init__(self, old: str | None, new: str | None) -> None:
        self.old, self.new = old, new
        self.added = 0
        self.removed = 0
        self.binary = False
        self.hunks: list[tuple[int, int]] = []  # (old_start, old_len)

    @property
    def path(self) -> str:
        return self.old or self.new or ""


def _path(s: str) -> str | None:
    s = s.split("\t", 1)[0]
    if s == "/dev/null":
        return None
    return s[2:] if s[:2] in ("a/", "b/") else s


def parse_diff(text: str) -> list[FileDiff]:
    out: list[FileDiff] = []
    cur: FileDiff | None = None
    old: str | None = None
    for line in text.splitlines():
        if line.startswith("--- "):
            old = _path(line[4:])
        elif line.startswith("+++ "):
            cur = FileDiff(old, _path(line[4:]))
            out.append(cur)
        elif line.startswith("Binary files ") or line.startswith("GIT binary patch"):
            m = re.match(r"Binary files (?:a/)?(.*?) and (?:b/)?(.*?) differ$", line)
            b = FileDiff(m.group(1) if m else None, m.group(2) if m else None)
            b.binary = True
            out.append(b)
            cur = None
        elif cur is not None and line.startswith("@@ "):
            m2 = _HUNK.match(line)
            if m2:
                a = int(m2.group(1))
                n = 1 if m2.group(2) is None else int(m2.group(2))
                d = 1 if m2.group(4) is None else int(m2.group(4))
                cur.hunks.append((a, n))
                cur.removed += n
                cur.added += d
    return out


def _line_starts(data: bytes) -> list[int]:
    starts = [0]
    i = data.find(b"\n")
    while i != -1:
        starts.append(i + 1)
        i = data.find(b"\n", i + 1)
    return starts


def _ranges(hunks: list[tuple[int, int]], data: bytes) -> list[tuple[int, int, int, int]]:
    """(byte_start, byte_end, first_line, last_line) per old-side hunk. A pure insertion (length 0)
    anchors at the start of the line that follows it, as a one-byte range."""
    starts = _line_starts(data)
    nlines = len(starts) - (1 if starts[-1] == len(data) else 0)
    out = []
    for a, n in hunks:
        if n == 0:
            line = min(a + 1, max(nlines, 1))
            off = starts[line - 1] if line - 1 < len(starts) else max(len(data) - 1, 0)
            out.append((off, off + 1, line, line))
            continue
        first, last = max(a, 1), max(min(a + n - 1, nlines), 1)
        end = starts[last] if last < len(starts) else len(data)
        out.append((starts[first - 1] if first - 1 < len(starts) else 0, max(end, 1), first, last))
    return out


def _innermost(rows: list[sqlite3.Row]) -> list[sqlite3.Row]:
    """Declarations of one range that do not strictly contain another overlapping declaration."""

    def span(r: sqlite3.Row) -> tuple[int, int]:
        lo = r["full_start"] if r["full_start"] is not None else r["span_start"]
        hi = r["full_end"] if r["full_end"] is not None else r["span_end"]
        return int(lo), int(hi)

    spans = {r["declaration_id"]: span(r) for r in rows}
    keep = []
    for r in rows:
        lo, hi = spans[r["declaration_id"]]
        contains_other = any(
            o != r["declaration_id"] and lo <= olo and ohi <= hi and (lo, hi) != (olo, ohi)
            for o, (olo, ohi) in spans.items()
        )
        if not contains_other:
            keep.append(r)
    return keep


def diff_impact(
    opts: WorkspaceOptions,
    build_id: str,
    request_id: str,
    *,
    base: str = "HEAD",
    head: str | None = None,
    repo: str | None = None,
    depth: int = 3,
    limit: int | None = None,
) -> Envelope:
    check_ref(base, "base")
    if head is not None:
        check_ref(head, "head")
    if not isinstance(depth, int) or isinstance(depth, bool) or not 1 <= depth <= MAX_DEPTH:
        raise SvwError(ErrorCode.INVALID_CONFIG, f"depth must be in 1..{MAX_DEPTH}")
    lim = DEFAULT_LIST if limit is None else check_limit(limit)
    with _session(opts, build_id) as s:
        repo_dir = Path(repo) if repo else s.ctx.project_root
        _, real = s.ctx.path_policy.check(str(repo_dir), s.ctx.project_root)
        top = Path(_git(real, ["rev-parse", "--show-toplevel"]).decode().strip())
        _git(top, ["rev-parse", "--verify", "-q", base + "^{commit}"])
        if head is not None:
            _git(top, ["rev-parse", "--verify", "-q", head + "^{commit}"])
        diff_args = ["diff", "-U0", "--no-color", "--no-ext-diff", "--no-textconv", "--no-renames"]
        diff_args += [base] + ([head] if head else []) + ["--"]
        diffs = parse_diff(_git(top, diff_args).decode("utf-8", "replace"))

        snap_dir = snapshots_dir(s.ctx) / str(s.build["snapshot_id"])
        rec = load_record(snap_dir)
        by_origin = {os.path.realpath(f.original_path): f for f in rec.files}
        graphs = _definition_graph(s)
        files_out: list[dict[str, Any]] = []
        unmapped: list[dict[str, Any]] = []
        unattributed: list[dict[str, Any]] = []
        decl_hits: dict[str, dict[str, Any]] = {}
        decl_rows: dict[str, sqlite3.Row] = {}
        for fd in diffs:
            host = os.path.realpath(top / fd.path)
            sf = by_origin.get(host)
            counts = {"added": fd.added, "removed": fd.removed}
            if sf is None:
                unmapped.append({"path": fd.path, **counts, "reason": "not_in_build_snapshot"})
                continue
            entry: dict[str, Any] = {"path": fd.path, "logical_path": sf.logical_path, **counts,
                                     "role": sf.role, "hunks": len(fd.hunks)}
            if fd.binary:
                entry.update(aligned=None, note="binary file")
                files_out.append(entry)
                continue
            src_bytes = (snap_dir / sf.snapshot_path).read_bytes()
            aligned: bool | None = None
            if fd.old is not None:
                try:
                    old = _git(top, ["cat-file", "blob", f"{base}:{fd.old}"])
                    aligned = hashlib.sha256(old).hexdigest() == sf.sha256
                except SvwError:
                    aligned = False
            entry["aligned"] = aligned
            if aligned is not True:
                entry["note"] = (
                    "the build's snapshot of this file differs from the base version (or the file "
                    "is new): line mapping is approximate"
                )
            if sf.role != "source":
                entry["note"] = (entry.get("note", "") + " role is " + sf.role + ": files that "
                                 "include it are not re-expanded").strip()
            files_out.append(entry)
            for b0, b1, l0, l1 in _ranges(fd.hunks, src_bytes):
                rows = s.rows(
                    "SELECT * FROM declarations WHERE build_id=? AND file=? "
                    "AND COALESCE(full_start, span_start) < ? AND COALESCE(full_end, span_end) > ?",
                    s.bid,
                    sf.logical_path,
                    b1,
                    b0,
                )
                inner = _innermost(rows)
                if not inner:
                    unattributed.append({"file": sf.logical_path, "lines": [l0, l1]})
                    continue
                for r in inner:
                    did = str(r["declaration_id"])
                    decl_rows[did] = r
                    h = decl_hits.setdefault(did, {"ranges": 0, "lines": []})
                    h["ranges"] += 1
                    h["lines"].append([l0, l1])

        # --- group twins, attribute to owning definitions
        groups: dict[tuple[Any, ...], list[sqlite3.Row]] = {}
        for r in decl_rows.values():
            groups.setdefault(
                (r["name"], r["file"], r["span_start"], r["span_end"], r["scope"]), []
            ).append(r)
        affected: list[dict[str, Any]] = []
        non_instance: list[dict[str, Any]] = []
        by_def: dict[str, dict[str, Any]] = {}
        for rows in groups.values():
            twins = _twins(rows) or sorted(rows, key=lambda x: x["declaration_id"])[:1]
            rep = twins[0]
            ids = [str(t["declaration_id"]) for t in twins]
            hit = decl_hits[ids[0]]
            dto = _decl_dto(rep)
            item = {
                "declaration_id": dto["declaration_id"],
                "symbol_id": dto["symbol_id"],
                "kind": dto["kind"],
                "qualified_name": dto["qualified_name"],
                "location": dto["location"],
                "changed_ranges": hit["ranges"],
                "changed_lines": hit["lines"][:20],
            }
            owner = (
                str(rep["declaration_id"])
                if rep["kind"] in DEFINITION_KINDS
                else _owner_definition(s, rep)
            )
            if owner is None:
                summ, _, _ = _references_of(s, ids, 1)
                item["references"] = summ["total"]
                non_instance.append(item)
                continue
            item["owner_definition_id"] = owner
            affected.append(item)
            d = by_def.setdefault(owner, {"declarations": 0, "port_changes": []})
            d["declarations"] += 1
            if rep["kind"] == "port":
                pq = ",".join("?" for _ in ids)
                sites = s.count(
                    f"SELECT COUNT(*) FROM port_bindings WHERE build_id=? AND port_declaration_id IN ({pq})",
                    s.bid,
                    *ids,
                )
                d["port_changes"].append({"port": rep["qualified_name"], "connection_sites": sites})
        up, down = graphs
        names = _names(s, set(by_def))
        defs_out: list[dict[str, Any]] = []
        total_instances = 0
        dependent_union: set[str] = set()
        for did, d in by_def.items():
            total, _items = _instances_of(s, did, 0)
            dependents = _closure_items(s, up, did, depth)
            dependent_union.update(x["declaration_id"] for x in dependents)
            total_instances += total
            defs_out.append(
                {
                    **names.get(did, {"declaration_id": did, "qualified_name": None, "kind": None}),
                    "changed_declarations": d["declarations"],
                    "port_changes": d["port_changes"],
                    "instances": total,
                    "dependents": dependents,
                    "dependency_definitions": len(_closure_items(s, down, did, depth)),
                }
            )
        defs_out.sort(key=lambda x: (-x["instances"], str(x["qualified_name"])))
        affected.sort(key=lambda x: (str(x["location"] and x["location"].get("file")), x["qualified_name"]))
        data: dict[str, Any] = {
            "base": base,
            "head": head or "working tree",
            "repo": str(top),
            "affected_definitions": defs_out,
            "affected_declarations": affected,
            "non_instance_declarations": non_instance,
            "files": files_out,
            "unmapped_files": unmapped,
            "unattributed": unattributed,
            "summary": {
                "files_changed": len(diffs),
                "files_in_build": len(files_out),
                "files_not_in_build": len(unmapped),
                "files_aligned": sum(1 for f in files_out if f.get("aligned") is True),
                "files_unaligned": sum(1 for f in files_out if f.get("aligned") is not True),
                "declarations_affected": len(affected) + len(non_instance),
                "definitions_affected": len(defs_out),
                "instances_of_affected_definitions": total_instances,
                "dependent_definitions": len(dependent_union - set(by_def)),
                "unattributed_ranges": len(unattributed),
            },
            "depth": depth,
            "limit": lim,
            "note": DIFF_NOTE,
        }
        cut = _fit(data, MAX_RESPONSE_BYTES - 32_768, _LISTS)
        for k in _LISTS:  # the lists are capped by `limit` after the size fit
            if isinstance(data.get(k), list) and len(data[k]) > lim:
                data[k] = data[k][:lim]
                cut = [*cut, k] if k not in cut else cut
        data["truncated_lists"] = cut
        warns: list[str] = []
        unaligned = [f["path"] for f in files_out if f.get("aligned") is not True]
        if unaligned:
            warns.append(
                f"{len(unaligned)} changed file(s) do not match the build's snapshot of the base "
                "version: their line mapping is approximate (see files[].aligned)"
            )
        if unmapped:
            warns.append(f"{len(unmapped)} changed file(s) are not part of this build and were not analysed")
        if cut:
            warns.append(
                f"truncated: {', '.join(cut)}; the summary figures are complete, items not listed "
                "must not be assumed unaffected"
            )
        return build_envelope(
            s.ctx,
            s.db,
            s.build,
            request_id,
            data,
            warnings=warns,
            extra_limits=[*_gap_limits(s), DIFF_NOTE],
            freshness=s.fresh(),
        )

