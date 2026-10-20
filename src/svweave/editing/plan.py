"""Edit planning (plan 12.2, 12.4 prepare; tests EDT-03, EDT-10, SEC-01).

Turns the request's edits into concrete, hash-guarded byte replacements against the BASE snapshot
and refuses everything outside the v1 envelope: ambiguous or bare-name symbols, macro-expansion
locations, paths outside the policy, symlinks, hardlinks, binary files, new / deleted / renamed
files. Nothing here writes anything.
"""

from __future__ import annotations

import os
import re
import stat
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from svweave.config.context import WorkspaceContext, snapshot_rel
from svweave.config.paths import glob_match, is_hardlinked
from svweave.domain.errors import ErrorCode, SvwError
from svweave.domain.ids import sha256_hex
from svweave.editing.diffing import has_unsafe_control, is_text
from svweave.editing.models import EditSpec, ExactRange, SlangSymbolEdit, SymbolReplacement
from svweave.security import safeio
from svweave.snapshot.capture import snapshots_dir
from svweave.snapshot.models import SnapFile, SnapshotRecord
from svweave.storage.db import Database
from svweave.storage.semantic import loads

#: roles whose bytes feed the config identity: editing them would change config_id, so v1 refuses.
FORBIDDEN_ROLES = frozenset({"filelist", "generated"})


@dataclass
class ResolvedEdit:
    index: int
    kind: str  # symbol_replacement | exact_range | slang_symbol
    logical_path: str
    rel: str  # path inside snapshot files/ and the candidate
    start: int
    end: int
    expected_sha256: str
    replacement: bytes
    edit_path: str = "exact_range"  # serena_symbol | exact_range | slang_symbol (ADR-013)
    reason: str = ""
    declaration_id: str | None = None
    qualified_name: str | None = None
    decl_kind: str | None = None
    notes: list[str] = field(default_factory=list)
    serena: dict[str, Any] | None = None  # outcome summary after the edit
    serena_pre: bytes = b""  # qualifiers Serena's symbol span has before the declaration span
    serena_post: bytes = b""
    resolved_symbol: dict[str, Any] | None = None  # slang_symbol only: what the name resolved to

    def to_record(self) -> dict[str, Any]:
        return {
            "index": self.index,
            "kind": self.kind,
            "logical_path": self.logical_path,
            "start_byte": self.start,
            "end_byte": self.end,
            "expected_sha256": self.expected_sha256,
            "replacement_sha256": sha256_hex(self.replacement),
            "replacement_bytes": len(self.replacement),
            "edit_path": self.edit_path,
            "counted_as_serena_semantic_edit": self.edit_path == "serena_symbol",
            "reason": self.reason,
            "declaration_id": self.declaration_id,
            "qualified_name": self.qualified_name,
            "decl_kind": self.decl_kind,
            "notes": self.notes,
            "serena": self.serena,
            "resolved_symbol": self.resolved_symbol,
        }


def denied(reason: str, message: str, **details: Any) -> SvwError:
    return SvwError(ErrorCode.POLICY_DENIED, message, {"reason": reason, **details})


def require_editing_enabled(ctx: WorkspaceContext) -> None:
    if not ctx.effective.editing_enabled:
        raise denied(
            "editing_disabled",
            "editing is disabled (trusted policy.editing_enabled and svweave.yaml "
            "editing.enabled must both be true); it is off by default",
        )


def path_allowed(ctx: WorkspaceContext, logical: str) -> bool:
    ed = ctx.manifest.editing
    return (
        any(glob_match(p, logical) for p in ed.allowed_paths)
        and any(glob_match(p, logical) for p in ctx.policy.writable_paths)
    )


def edit_policy_state(ctx: WorkspaceContext, files: list[str]) -> dict[str, Any]:
    """The editing-policy inputs that decide whether ``files`` may be written NOW: the mandatory
    validate-profile floor, ``allowed_paths`` and the governing source root (id + writable flag) of
    each file. Hashed into the approval binding (A4)."""
    roots: dict[str, list[Any]] = {}
    for lp in sorted(files):
        root = ctx._governing_root(ctx.to_abs(lp))
        roots[lp] = [None, False] if root is None else [root.id, bool(root.writable)]
    ed = ctx.manifest.editing
    return {
        "validate_profiles": sorted(ed.validate_profiles),
        "allowed_paths": sorted(ed.allowed_paths),
        "roots": roots,
    }


def recheck_edit_policy(ctx: WorkspaceContext, files: list[str], profiles: list[str]) -> None:
    """Commit-time re-check of the CURRENT policy (A4): a validate profile that became mandatory
    after validation, a path outside allowed_paths / writable_paths, or a source root that is no
    longer writable all refuse the commit; nothing is written and a new validation is needed."""
    missing = sorted(set(ctx.manifest.editing.validate_profiles) - set(profiles))
    if missing:
        raise SvwError(
            ErrorCode.EDIT_CONFLICT,
            "editing.validate_profiles gained mandatory profile(s) since validation; prepare and "
            "validate a new transaction",
            {"reason": "validate_profiles_changed", "missing": missing},
        )
    for lp in files:
        if not path_allowed(ctx, lp):
            raise denied(
                "path_not_allowed",
                "the path is no longer allowed by editing.allowed_paths / policy writable_paths",
                path=lp,
            )
        root = ctx._governing_root(ctx.to_abs(lp))
        if root is None or not root.writable:
            raise denied("root_not_writable",
                         "the source root of this file is no longer writable", path=lp)


def generated_logicals(ctx: WorkspaceContext) -> frozenset[str]:
    """Logical paths of every ``generated_inputs`` entry of EVERY profile, read from the manifest
    itself: a generated file that is also listed as a source keeps its protection (A6)."""
    out: set[str] = set()
    for profile in ctx.manifest.profiles.values():
        gens = getattr(profile, "generated_inputs", None) or []
        if not gens:
            continue
        try:
            cwd = ctx.resolve(profile.cwd, ctx.project_root, is_dir=True, rules=False)
        except SvwError:
            continue
        for g in gens:
            try:
                out.add(ctx.resolve(g.path, cwd.abs).logical)
            except SvwError:
                out.add(g.path)  # unresolvable: still protect the spelled path
    return frozenset(out)


def check_target_file(ctx: WorkspaceContext, logical: str, snap: SnapFile | None) -> bytes:
    """All per-file refusals; returns the CURRENT bytes of the workspace file."""
    if snap is None:
        # not part of the base inventory: a create / unknown path / escape (EDT-10, SEC-01)
        if safeio.safe_rel_path(logical.removeprefix("@")) is None or logical.startswith("/"):
            raise SvwError(
                ErrorCode.PATH_NOT_ALLOWED,
                "path is not a safe relative logical path",
                {"reason": "unsafe_path", "path": logical[:200]},
            )
        raise denied(
            "not_in_base_snapshot",
            "only existing files of the base snapshot can be edited; creating files is not "
            "supported in v1",
            path=logical[:200],
        )
    if logical in generated_logicals(ctx):
        raise denied("role_not_editable", "generated inputs cannot be edited", path=logical)
    if snap.role in FORBIDDEN_ROLES:
        raise denied("role_not_editable", f"{snap.role} inputs cannot be edited", path=logical)
    if snap.symlink_target is not None:
        raise denied("symlink_target", "symlink targets cannot be edited", path=logical)
    if snap.hardlinked:
        raise denied("hardlinked", "hardlinked files cannot be edited", path=logical)
    if not path_allowed(ctx, logical):
        raise denied(
            "path_not_allowed",
            "path is outside editing.allowed_paths or the trusted policy writable_paths",
            path=logical,
        )
    abs_path = ctx.to_abs(logical)
    rp = ctx.resolve(str(abs_path), ctx.project_root)  # PATH_NOT_ALLOWED on escapes
    if rp.symlink_target is not None or rp.real != rp.abs:
        raise denied("symlink", "symlinks cannot be edited", path=logical)
    root = ctx._governing_root(abs_path)
    if root is None or not root.writable:
        raise denied("root_not_writable", "the source root of this file is not writable", path=logical)
    try:
        st = os.lstat(abs_path)
    except FileNotFoundError as e:
        raise SvwError(
            ErrorCode.EDIT_CONFLICT, "file no longer exists", {"reason": "target_missing", "path": logical}
        ) from e
    if stat.S_ISLNK(st.st_mode) or not stat.S_ISREG(st.st_mode):
        raise denied("not_regular", "not a regular file", path=logical)
    if st.st_nlink > 1 or is_hardlinked(abs_path):
        raise denied("hardlinked", "hardlinked files cannot be edited", path=logical)
    data = safeio.read_regular(abs_path)
    if not is_text(data):
        raise denied("binary", "binary files cannot be edited", path=logical)
    if sha256_hex(data) != snap.sha256:
        raise SvwError(
            ErrorCode.EDIT_CONFLICT,
            "file changed since the base snapshot",
            {"reason": "file_changed", "path": logical},
        )
    return data


def _candidates_for(db: Database, build_id: str, decl_id: str) -> list[dict[str, Any]]:
    prefix = decl_id if len(decl_id) >= 8 and all(c in "0123456789abcdef" for c in decl_id) else None
    rows = db.conn.execute(
        "SELECT declaration_id, kind, qualified_name, scope, file, span_start, span_end, "
        "full_start, full_end FROM declarations WHERE build_id=? AND "
        "(name=? OR qualified_name=? OR (? IS NOT NULL AND declaration_id LIKE ? || '%')) "
        "ORDER BY file, span_start LIMIT 50",
        (build_id, decl_id, decl_id, prefix, prefix),
    ).fetchall()
    return [
        {
            "declaration_id": r["declaration_id"],
            "qualified_name": r["qualified_name"],
            "kind": r["kind"],
            "scope": r["scope"],
            "file": r["file"],
            "span": [r["span_start"], r["span_end"]],
        }
        for r in rows
    ]


def macro_refusal(db: Database, build_id: str, file: str, start: int, end: int) -> str | None:
    """Reason when [start,end) overlaps a location that only exists through a macro BODY (shared
    by every expansion). Macro ARGUMENTS are real bytes at the invocation and stay editable."""
    for r in db.conn.execute(
        "SELECT macro FROM declarations WHERE build_id=? AND file=? "
        "AND COALESCE(full_start, span_start)<? AND COALESCE(full_end, span_end)>? "
        "AND macro IS NOT NULL AND macro!='null'",
        (build_id, file, end, start),
    ):
        m = loads(r["macro"]) or {}
        if m.get("spelled_in") == "macro_body":
            return f"declaration spelled in macro body {m.get('name')!r}"
    for r in db.conn.execute(
        'SELECT loc FROM "references" WHERE build_id=? AND file=? AND span_start<? AND span_end>?',
        (build_id, file, end, start),
    ):
        m = (loads(r["loc"]) or {}).get("macro") or {}
        if m.get("spelled_in") == "macro_body":
            return f"reference spelled in macro body {m.get('name')!r}"
    return None


_DEFINE = re.compile(rb"`[ \t]*define\b")


def define_spans(data: bytes) -> list[tuple[int, int]]:
    """Byte spans of every ```define`` directive (with ``\\``-continued lines), wherever it starts
    on a line (``module m; endmodule `define W 8`` is legal); comments and strings are masked so a
    ```define`` inside them does not count."""
    out: list[tuple[int, int]] = []
    masked = mask_comments_strings(data)
    for m in _DEFINE.finditer(masked if masked is not None else data):
        start = m.start()
        lead = start
        while lead > 0 and data[lead - 1] in b" \t":
            lead -= 1
        if lead == 0 or data[lead - 1] == 0x0A:  # only indentation before it: the span is the line
            start = lead
        pos = m.end()
        while True:
            nl = data.find(b"\n", pos)
            if nl < 0:
                end = len(data)
                break
            if data[pos:nl].rstrip(b"\r").endswith(b"\\"):
                pos = nl + 1
                continue
            end = nl
            break
        out.append((start, end))
    return out


def resolve_edits(
    ctx: WorkspaceContext,
    db: Database,
    build_id: str,
    rec: SnapshotRecord,
    edits: list[EditSpec],
) -> tuple[list[ResolvedEdit], dict[str, bytes]]:
    """Resolve every edit against the base build/snapshot. Returns edits + base bytes per file."""
    inv = {f.logical_path: f for f in rec.files}
    base_bytes: dict[str, bytes] = {}
    out: list[ResolvedEdit] = []
    for i, e in enumerate(edits):
        if isinstance(e, SymbolReplacement):
            out.append(_resolve_symbol(ctx, db, build_id, inv, base_bytes, i, e))
        elif isinstance(e, SlangSymbolEdit):
            out.append(_resolve_slang_symbol(ctx, db, build_id, rec, inv, base_bytes, i, e))
        else:
            out.append(_resolve_range(ctx, db, build_id, inv, base_bytes, i, e))
    _check_overlaps(out)
    return out, base_bytes


def _file_bytes(
    ctx: WorkspaceContext, inv: dict[str, SnapFile], cache: dict[str, bytes], logical: str
) -> bytes:
    if logical not in cache:
        cache[logical] = check_target_file(ctx, logical, inv.get(logical))
    return cache[logical]


def _resolve_symbol(
    ctx: WorkspaceContext,
    db: Database,
    build_id: str,
    inv: dict[str, SnapFile],
    cache: dict[str, bytes],
    i: int,
    e: SymbolReplacement,
) -> ResolvedEdit:
    row = db.conn.execute(
        "SELECT * FROM declarations WHERE build_id=? AND declaration_id=?",
        (build_id, e.declaration_id),
    ).fetchone()
    if row is None:
        cands = _candidates_for(db, build_id, e.declaration_id)
        if not cands:
            raise SvwError(
                ErrorCode.MISSING_INPUT,
                "no declaration with this id in the base build",
                {"reason": "declaration_missing", "index": i},
            )
        raise SvwError(
            ErrorCode.AMBIGUOUS_SYMBOL,
            "a bare name or partial id is never accepted for an edit; choose one scoped "
            "declaration_id (nothing was selected automatically)",
            {"reason": "bare_name_or_unknown_id", "index": i, "candidates": cands},
        )
    file, fs, fe, data = _checked_declaration(ctx, db, build_id, inv, cache, i, row)
    return ResolvedEdit(
        index=i,
        kind="symbol_replacement",
        logical_path=file,
        rel=snapshot_rel(file),
        start=fs,
        end=fe,
        expected_sha256=sha256_hex(data[fs:fe]),
        replacement=_checked_text(e.new_body, i),
        declaration_id=e.declaration_id,
        qualified_name=row["qualified_name"],
        decl_kind=row["kind"],
    )


_SUBR_HEAD_STRICT = re.compile(rb"\A(task|function)\b")
_SUBR_HEAD_LENIENT = re.compile(
    rb"\A(?:(?:virtual|static|automatic|protected|local|pure|context|forkjoin)\s+)*"
    rb"(task|function)\b"
)
_SUBR_TAIL = re.compile(rb"\b(endtask|endfunction)\b(?:[ \t]*:[ \t]*[A-Za-z_][\w$]*)?\s*\Z")
_SUBR_END_TOKEN = re.compile(rb"\bend(?:task|function)\b")
#: closers of enclosing / sibling constructs: a subroutine text can never contain one
_FOREIGN_END = re.compile(
    rb"\bend(?:class|module|package|interface|program|checker|generate|clocking|group|property|"
    rb"sequence|config|primitive|specify|table)\b"
)
_EXTERN_BEFORE = re.compile(rb"\b(?:extern|import|export)\b[^;]*\Z")


#: compiler directives a replacement may never contain: they change what the REST of the
#: compilation (later declarations, later files) sees, or hide text from the shape check
_FORBIDDEN_DIRECTIVES = frozenset({
    b"define", b"undef", b"undefineall", b"include", b"ifdef", b"ifndef", b"elsif", b"else",
    b"endif", b"resetall", b"default_nettype", b"timescale", b"line", b"pragma",
    b"begin_keywords", b"end_keywords", b"celldefine", b"endcelldefine", b"unconnected_drive",
    b"nounconnected_drive",
})
#: directives that change compiler state: refused in the replacement AND in every macro body it
#: reaches (conditionals stay allowed in bodies: the UVM field macros use them)
_CONDITIONALS = frozenset({b"ifdef", b"ifndef", b"elsif", b"else", b"endif"})
_STATE_DIRECTIVES = _FORBIDDEN_DIRECTIVES - _CONDITIONALS
_PREDEFINED_OK = frozenset({b"__FILE__", b"__LINE__"})
#: closing keywords a macro expansion may not contain (it could end the target early)
_ANY_CLOSER = re.compile(
    rb"\bend(?:task|function|class|module|package|interface|program|checker|generate|clocking|"
    rb"group|property|sequence|config|primitive|specify|table)\b"
)
_MACRO_DEF = re.compile(rb"[ \t]*`[ \t]*define[ \t]+([A-Za-z_][\w$]*)(.*)", re.DOTALL)
_MACRO_DEPTH = 16


def backtick_tokens(text: bytes) -> list[bytes] | None:
    """Backtick tokens of ``text`` outside comments and string literals, in order: the macro or
    directive NAME after a backtick, and the literal tokens for pasting (two backticks) and
    stringification (backtick-quote and backtick-backslash-backtick-quote). None when a comment
    or string is unterminated."""
    out: list[bytes] = []
    i, n = 0, len(text)
    while i < n:
        c2 = text[i : i + 2]
        if c2 == b"//":
            j = text.find(b"\n", i)
            i = n if j < 0 else j
        elif c2 == b"/*":
            j = text.find(b"*/", i + 2)
            if j < 0:
                return None
            i = j + 2
        elif text[i] == 0x22:
            j = i + 1
            while j < n and text[j] != 0x22:
                if text[j] == 0x5C:
                    j += 1
                elif text[j] == 0x0A:
                    return None
                j += 1
            if j >= n:
                return None
            i = j + 1
        elif text[i] == 0x60:  # `
            if text[i : i + 3] == b'`\\`"':
                out.append(b'`\\`"')
                i += 3
            elif c2 in (b"``", b'`"'):
                out.append(c2)
                i += 2
            else:
                m = re.compile(rb"[A-Za-z_][\w$]*").match(text, i + 1)
                if m:
                    out.append(m.group(0))
                    i = m.end()
                else:
                    out.append(b"`")
                    i += 1
        else:
            i += 1
    return out


def macro_definitions(ctx: WorkspaceContext, rec: SnapshotRecord) -> dict[bytes, list[bytes]]:
    """Every ```define`` of the BASE snapshot's files (sources and headers): name -> bodies."""
    root = snapshots_dir(ctx) / rec.snapshot_id
    defs: dict[bytes, list[bytes]] = {}
    for f in rec.files:
        try:
            data = (root / f.snapshot_path).read_bytes()
        except OSError:
            continue
        if not is_text(data):
            continue
        for d0, d1 in define_spans(data):
            m = _MACRO_DEF.match(data[d0:d1])
            if m:
                defs.setdefault(m.group(1), []).append(m.group(2))
    return defs


def replacement_directive_problem(
    repl: bytes, defs: Callable[[], dict[bytes, list[bytes]]]
) -> str | None:
    """Why ``repl`` may not be spliced into a declaration, or None. Forbidden: every compiler
    directive in :data:`_FORBIDDEN_DIRECTIVES` and token pasting / stringification (they change
    what later code sees or let text end the declaration unseen). A macro INVOCATION is allowed
    only when the macro is defined in the base snapshot and neither its body nor the bodies of the
    macros it invokes (transitively, all definitions) contain a closing keyword."""
    toks = backtick_tokens(repl)
    if toks is None:
        return None  # unterminated comment / string: the shape check refuses it
    table: dict[bytes, list[bytes]] | None = None
    pending: list[tuple[bytes, int]] = []
    for t in toks:
        if t in (b"``", b'`"', b'`\\`"', b"`"):
            return f"token {t.decode()} (pasting / stringification) is not allowed"
        if t in _FORBIDDEN_DIRECTIVES:
            return f"compiler directive `{t.decode()} is not allowed"
        if t not in _PREDEFINED_OK:
            pending.append((t, 0))
    seen: set[bytes] = set()
    while pending:
        name, depth = pending.pop()
        if name in seen:
            continue
        seen.add(name)
        if table is None:
            table = defs()
        if name not in table:
            return f"macro `{name.decode()} is not defined in the base snapshot"
        if depth >= _MACRO_DEPTH:
            return f"macro `{name.decode()} nests too deeply"
        for body in table[name]:
            masked = mask_comments_strings(body)
            if _ANY_CLOSER.search(masked if masked is not None else body):
                return f"the expansion of macro `{name.decode()} contains a closing keyword"
            for inner in backtick_tokens(body) or []:
                if inner in _STATE_DIRECTIVES:
                    return (f"the expansion of macro `{name.decode()} contains the "
                            f"state-changing directive `{inner.decode()}")
                if inner in table or inner not in _FORBIDDEN_DIRECTIVES | _PREDEFINED_OK | {
                    b"``", b'`"', b'`\\`"', b"`"
                }:
                    pending.append((inner, depth + 1))
    return None


def mask_comments_strings(text: bytes) -> bytes | None:
    """``text`` with every comment and string literal replaced by spaces (newlines and length
    kept), or None when a block comment or string literal is unterminated. A small SystemVerilog
    aware scanner: ``//`` to end of line, ``/* */``, ``"..."`` with backslash escapes."""
    out = bytearray(text)
    i, n = 0, len(text)
    while i < n:
        c = text[i : i + 2]
        if c == b"//":
            j = text.find(b"\n", i)
            j = n if j < 0 else j
            for k in range(i, j):
                out[k] = 0x20
            i = j
        elif c == b"/*":
            j = text.find(b"*/", i + 2)
            if j < 0:
                return None
            for k in range(i, j + 2):
                if text[k] != 0x0A:
                    out[k] = 0x20
            i = j + 2
        elif text[i] == 0x22:  # "
            j = i + 1
            while j < n and text[j] != 0x22:
                if text[j] == 0x5C:  # backslash escapes the next byte
                    j += 1
                elif text[j] == 0x0A:
                    return None  # a string literal may not span lines without a backslash
                j += 1
            if j >= n:
                return None
            for k in range(i, j + 1):
                if text[k] != 0x0A:
                    out[k] = 0x20
            i = j + 1
        else:
            i += 1
    return bytes(out)


def name_segments(qn: str) -> list[str]:
    """Name segments of a slang qualified name: ``::`` (package / class scopes) and ``.``
    (module / interface / generate scopes) both separate."""
    return re.split(r"::|\.", qn)


def _subroutine_shape(text: bytes, *, strict: bool = True) -> tuple[bytes, bytes] | None:
    """(opening keyword, closing keyword) when ``text`` is exactly ONE whole task / function
    declaration (``task|function`` ... ``endtask|endfunction`` with an optional ``: label``), else
    None. Comments and strings are masked first, so a tail hidden in a comment or string does not
    count; ``strict`` (replacement text) refuses leading qualifiers, because the stored declaration
    span excludes them and they are preserved outside it. Exactly one ``endtask|endfunction`` token
    and no closer of another construct may appear."""
    m = mask_comments_strings(text)
    if m is None:
        return None
    h = (_SUBR_HEAD_STRICT if strict else _SUBR_HEAD_LENIENT).match(m)
    t = _SUBR_TAIL.search(m)
    if h is None or t is None:
        return None
    if (h.group(1), t.group(1)) not in ((b"task", b"endtask"), (b"function", b"endfunction")):
        return None
    if len(_SUBR_END_TOKEN.findall(m)) != 1 or _FOREIGN_END.search(m):
        return None
    return h.group(1), t.group(1)


def _slang_candidates(rows: list[Any]) -> list[dict[str, Any]]:
    return [
        {
            "symbol_id": r["symbol_id"],
            "declaration_id": r["declaration_id"],
            "qualified_name": r["qualified_name"],
            "kind": r["kind"],
            "scope": r["scope"],
            "file": r["file"],
            "span": [r["span_start"], r["span_end"]],
        }
        for r in rows[:50]
    ]


def _resolve_slang_symbol(
    ctx: WorkspaceContext,
    db: Database,
    build_id: str,
    rec: SnapshotRecord,
    inv: dict[str, SnapFile],
    cache: dict[str, bytes],
    i: int,
    e: SlangSymbolEdit,
) -> ResolvedEdit:
    """ADR-013: locate ONE task / function / constructor by (qualified) name or symbol_id in the
    COMPLETE semantic database of the build that belongs to the base snapshot, then plan it as the
    exact-range edit of its stored full span. Fails closed at every step; never picks one of
    several candidates."""
    # the build must be the base snapshot's COMPLETE build: absence / ambiguity in anything else
    # is no proof (AGENTS rule 5)
    b = db.conn.execute(
        "SELECT snapshot_id, completeness FROM builds WHERE build_id=?", (build_id,)
    ).fetchone()
    if b is None:
        raise SvwError(ErrorCode.MISSING_INPUT, "unknown base build",
                       {"reason": "build_missing", "index": i})
    if b["snapshot_id"] != rec.snapshot_id:
        raise SvwError(
            ErrorCode.STALE_SNAPSHOT,
            "the build does not belong to the base snapshot of the transaction",
            {"reason": "build_snapshot_mismatch", "index": i},
        )
    if b["completeness"] != "COMPLETE":
        raise SvwError(
            ErrorCode.PARTIAL_ANALYSIS,
            "the build is not COMPLETE: a name that is absent or ambiguous in a PARTIAL result "
            "proves nothing; no name-based edit",
            {"reason": "base_not_complete", "completeness": b["completeness"], "index": i},
        )
    subr = ("task", "function")
    if e.symbol_id is not None:
        row = db.conn.execute(
            "SELECT * FROM declarations WHERE build_id=? AND symbol_id=?", (build_id, e.symbol_id)
        ).fetchone()
        if row is None:
            raise SvwError(ErrorCode.MISSING_INPUT, "no declaration with this symbol_id in the base build",
                           {"reason": "symbol_missing", "index": i})
        if row["kind"] not in subr:
            raise denied("not_a_subroutine",
                         "slang_symbol edits only tasks and functions (including constructors)",
                         index=i, kind=row["kind"])
    else:
        assert e.qualified_name is not None
        want = e.qualified_name
        want_segs = name_segments(want)
        rooted = bool(want_segs) and want_segs[0] == "$unit"
        if rooted:
            want_segs = want_segs[1:]
        if not want_segs or any(not x for x in want_segs):
            raise SvwError(ErrorCode.INVALID_CONFIG, "qualified_name is empty or malformed",
                           {"reason": "bad_qualified_name", "index": i})
        # every task / function whose LAST name segment matches is a candidate: ambiguity is
        # decided over all of them (class / package members are spelled ``a::b``, module /
        # interface members ``a.b`` by slang), never over a pre-filtered subset
        by_last = [
            (r, name_segments(str(r["qualified_name"])))
            for r in db.conn.execute(
                "SELECT * FROM declarations WHERE build_id=? AND name=? AND kind IN ('task','function') "
                "ORDER BY file, span_start",
                (build_id, want_segs[-1]),
            )
        ]
        n = len(want_segs)
        rows = [
            r for r, segs in by_last
            if segs[-n:] == want_segs and (not rooted or len(segs) == n)
        ]
        if not rows:
            raise SvwError(
                ErrorCode.MISSING_INPUT,
                "no task or function with this name in the COMPLETE base build "
                "(spell it as slang does: pkg::cls::name, module.name, ifc.name, $unit::name; "
                "'::' and '.' are interchangeable)",
                {"reason": "symbol_missing", "index": i, "name": want[:200]},
            )
        if len(rows) > 1:
            raise SvwError(
                ErrorCode.AMBIGUOUS_SYMBOL,
                f"{len(rows)} tasks / functions match this name; choose one scoped qualified_name "
                "or symbol_id (nothing was selected automatically)",
                {"reason": "ambiguous_name", "index": i, "candidates": _slang_candidates(rows)},
            )
        row = rows[0]
    file, fs, fe, data = _checked_declaration(ctx, db, build_id, inv, cache, i, row)
    cur = data[fs:fe]
    shape = _subroutine_shape(cur, strict=False)
    if shape is None:
        if cur.rstrip().endswith(b";") or _EXTERN_BEFORE.search(data[max(0, fs - 80) : fs]):
            raise denied(
                "extern_prototype",
                "the stored declaration is an extern / import / export prototype, not a body; "
                "edit the out-of-block definition instead (it is not stored as its own "
                "declaration when this symbol resolved to the prototype)",
                index=i,
            )
        raise denied("unexpected_declaration_shape",
                     "the stored declaration span is not a whole task / function", index=i)
    want_kind = "task" if row["kind"] == "task" else "function"
    if shape[0].decode() != want_kind:
        raise denied("unexpected_declaration_shape",
                     "the stored span's keyword disagrees with the declaration kind", index=i)
    cur_sha = sha256_hex(cur)
    if e.expected_sha256 is not None and e.expected_sha256 != cur_sha:
        raise SvwError(
            ErrorCode.EDIT_CONFLICT,
            "the declaration bytes do not hash to expected_sha256",
            {"reason": "expected_bytes_mismatch", "index": i},
        )
    repl = _text_bytes(e.replacement, i)
    why = replacement_directive_problem(repl, lambda: macro_definitions(ctx, rec))
    if why is not None:
        raise denied(
            "replacement_directive",
            f"the replacement may not contain preprocessor effects: {why}",
            index=i,
        )
    rshape = _subroutine_shape(repl)
    if rshape is None or rshape[0] != shape[0]:
        raise denied(
            "replacement_shape",
            "the replacement must be a whole declaration of the same kind: from the "
            "task / function keyword to endtask / endfunction",
            index=i,
        )
    attrs = loads(row["attrs"]) or {}
    constructor = bool(attrs.get("constructor"))
    return ResolvedEdit(
        index=i,
        kind="slang_symbol",
        logical_path=file,
        rel=snapshot_rel(file),
        start=fs,
        end=fe,
        expected_sha256=cur_sha,
        replacement=repl,
        edit_path="slang_symbol",
        reason="declaration located by name in the slang semantic database (ADR-013); "
        "exact-range patch on its stored full span, not a Serena semantic edit",
        declaration_id=row["declaration_id"],
        qualified_name=row["qualified_name"],
        decl_kind=row["kind"],
        resolved_symbol={
            "symbol_id": row["symbol_id"],
            "qualified_name": row["qualified_name"],
            "kind": row["kind"],
            "constructor": constructor,
            "file": file,
            "start_byte": fs,
            "end_byte": fe,
            "build_id": build_id,
        },
    )


def _checked_declaration(
    ctx: WorkspaceContext,
    db: Database,
    build_id: str,
    inv: dict[str, SnapFile],
    cache: dict[str, bytes],
    i: int,
    row: Any,
) -> tuple[str, int, int, bytes]:
    """Every refusal shared by the declaration-based edits (``symbol_replacement`` and
    ``slang_symbol``): no span, macro expansion, shared span, file policy, macro definition.
    Returns (logical file, full start, full end, CURRENT file bytes)."""
    if row["full_start"] is None or row["file"] is None:
        raise denied("no_full_span", "declaration has no full source span", index=i)
    macro = loads(row["macro"])
    if macro:
        raise denied(
            "macro_expansion",
            "the declaration comes from a macro expansion; edit refused",
            index=i,
            macro=macro.get("name"),
        )
    file, fs, fe = row["file"], int(row["full_start"]), int(row["full_end"])
    twins = db.conn.execute(
        "SELECT declaration_id, kind, qualified_name, file, span_start, span_end FROM declarations "
        "WHERE build_id=? AND file=? AND full_start=? AND full_end=? AND declaration_id!=?",
        (build_id, file, fs, fe, row["declaration_id"]),
    ).fetchall()
    if twins:
        raise SvwError(
            ErrorCode.AMBIGUOUS_SYMBOL,
            "several declarations share this source span (for example one per compilation "
            "unit); refusing to pick one",
            {
                "reason": "shared_span",
                "index": i,
                "candidates": [
                    {"declaration_id": t["declaration_id"], "qualified_name": t["qualified_name"],
                     "kind": t["kind"], "file": t["file"]}
                    for t in twins
                ],
            },
        )
    data = _file_bytes(ctx, inv, cache, file)
    if not (0 <= fs < fe <= len(data)):
        raise denied("span_out_of_bounds", "declaration span is outside the file", index=i)
    for d0, d1 in define_spans(data):
        if fs < d1 and d0 < fe:
            raise denied(
                "macro_definition_edit_unsupported",
                "the declaration lies inside a macro definition; v1 does not edit those",
                index=i,
            )
    reason = macro_refusal(db, build_id, file, fs, fe)
    if reason is not None:
        raise denied("macro_expansion", f"edit refused: {reason}", index=i)
    return file, fs, fe, data


_MACRO_STATE = frozenset({b"define", b"undef", b"undefineall"})


def _refuse_macro_state_change(repl: bytes, i: int) -> None:
    """exact_range / symbol_replacement text may not (re)define or undefine macros: that changes
    every later expansion, which is the macro-definition edit v1 refuses (comments and strings are
    masked; a directive spelled there is text)."""
    toks = backtick_tokens(repl)
    for t in toks or ():
        if t in _MACRO_STATE:
            raise denied(
                "macro_definition_edit_unsupported",
                f"replacement text contains `{t.decode()}: (re)defining or undefining a macro is "
                "outside the qualified v1 subset",
                index=i,
            )


def _checked_text(text: str, i: int) -> bytes:
    b = _text_bytes(text, i)
    _refuse_macro_state_change(b, i)
    return b


def _text_bytes(text: str, i: int) -> bytes:
    b = text.encode("utf-8")
    if b"\x00" in b:
        raise denied("binary_replacement", "replacement text contains NUL", index=i)
    if has_unsafe_control(text):
        raise denied(
            "control_character_in_edit",
            "replacement text contains a terminal control character, DEL or a bidi override "
            "(only TAB, LF and CRLF are allowed)",
            index=i,
        )
    return b


def _resolve_range(
    ctx: WorkspaceContext,
    db: Database,
    build_id: str,
    inv: dict[str, SnapFile],
    cache: dict[str, bytes],
    i: int,
    e: ExactRange,
) -> ResolvedEdit:
    data = _file_bytes(ctx, inv, cache, e.logical_path)
    if not (0 <= e.start_byte <= e.end_byte <= len(data)):
        raise SvwError(
            ErrorCode.EDIT_CONFLICT,
            "byte range is outside the file",
            {"reason": "range_out_of_bounds", "index": i},
        )
    if sha256_hex(data[e.start_byte : e.end_byte]) != e.expected_sha256:
        raise SvwError(
            ErrorCode.EDIT_CONFLICT,
            "the bytes at the range do not hash to expected_sha256",
            {"reason": "expected_bytes_mismatch", "index": i},
        )
    for d0, d1 in define_spans(data):
        # inclusive bounds: the newline that ENDS the directive (at d1) and the byte just before it
        # starts (d0) belong to it; replacing or removing either changes the macro's text
        if e.start_byte <= d1 and d0 <= e.end_byte:
            raise denied(
                "macro_definition_edit_unsupported",
                "editing a macro definition (`define directive) is outside the qualified v1 "
                "subset: its text is shared by every expansion",
                index=i,
            )
    reason = macro_refusal(db, build_id, e.logical_path, e.start_byte, e.end_byte)
    if reason is not None:
        raise denied("macro_expansion", f"edit refused: {reason}", index=i)
    return ResolvedEdit(
        index=i,
        kind="exact_range",
        logical_path=e.logical_path,
        rel=snapshot_rel(e.logical_path),
        start=e.start_byte,
        end=e.end_byte,
        expected_sha256=e.expected_sha256,
        replacement=_checked_text(e.replacement, i),
        reason="explicit exact-range patch requested",
    )


def _check_overlaps(edits: list[ResolvedEdit]) -> None:
    by_file: dict[str, list[ResolvedEdit]] = {}
    for e in edits:
        by_file.setdefault(e.logical_path, []).append(e)
    for logical, es in by_file.items():
        es.sort(key=lambda x: (x.start, x.end))
        for a, b in zip(es, es[1:], strict=False):
            if b.start < a.end or (a.start == a.end == b.start):
                raise denied(
                    "overlapping_edits",
                    "two edits overlap in the same file",
                    path=logical,
                    indexes=[a.index, b.index],
                )


def slang_scope_escape(
    db: Database, base_build_id: str, cand_build_id: str, targets: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """Decisive containment check of ``slang_symbol`` edits (ADR-013), run on the slang builds of
    one validate profile (baseline vs candidate). Returns the problems (empty = contained):

    * every target must exist exactly once in both builds under the same qualified name, kind and
      constructor flag;
    * in the candidate the target's full declaration span must be EXACTLY the replacement text
      (``new_start`` / ``new_end`` in candidate coordinates), so nothing is declared through a
      macro tail that ends the declaration elsewhere;
    * outside the targets' own declaration spans the multiset of (qualified name, kind, file) of
      ALL declarations must be identical. A replacement that closes its class and declares more
      (or renames the subroutine) changes it.

    Declarations whose name lies INSIDE a target span (arguments, locals) are not compared: that
    is the part the edit is allowed to change. Everything else the edit could do (statements, text
    the declaration list does not show) is covered only by the slang diagnostics and the diff."""

    def load(build_id: str) -> list[Any]:
        return db.conn.execute(
            "SELECT qualified_name, kind, file, span_start, full_start, full_end, attrs "
            "FROM declarations WHERE build_id=?", (build_id,)).fetchall()

    problems: list[dict[str, Any]] = []
    keys = {(t["qualified_name"], t["kind"], t["file"]) for t in targets}
    sides: dict[str, tuple[Counter[tuple[str, str, str]], list[Any]]] = {}
    for side, bid in (("baseline", base_build_id), ("candidate", cand_build_id)):
        rows = load(bid)
        found = {k: [r for r in rows if (r["qualified_name"], r["kind"], r["file"]) == k]
                 for k in keys}
        spans: dict[str, list[tuple[int, int]]] = {}
        for k, hits in found.items():
            if len(hits) != 1 or hits[0]["full_start"] is None:
                problems.append({"side": side, "target": k[0], "problem": "target_not_unique",
                                 "count": len(hits)})
                continue
            spans.setdefault(k[2], []).append((hits[0]["full_start"], hits[0]["full_end"]))
        for t in targets:
            hits = found[(t["qualified_name"], t["kind"], t["file"])]
            if len(hits) == 1:
                if side == "candidate" and t.get("new_start") is not None and (
                    (hits[0]["full_start"], hits[0]["full_end"]) != (t["new_start"], t["new_end"])
                ):
                    problems.append({
                        "side": side, "target": t["qualified_name"], "problem": "span_mismatch",
                        "expected": [t["new_start"], t["new_end"]],
                        "got": [hits[0]["full_start"], hits[0]["full_end"]],
                    })
                flag = bool((loads(hits[0]["attrs"]) or {}).get("constructor"))
                if flag != bool(t["constructor"]):
                    problems.append({"side": side, "target": t["qualified_name"],
                                     "problem": "constructor_flag_changed"})
        outside: Counter[tuple[str, str, str]] = Counter()
        for r in rows:
            if (r["qualified_name"], r["kind"], r["file"]) in keys:
                continue
            ps = r["span_start"]
            if ps is not None and any(a <= ps < b for a, b in spans.get(r["file"], [])):
                continue
            outside[(str(r["qualified_name"]), str(r["kind"]), str(r["file"]))] += 1
        sides[side] = (outside, rows)
    added = sides["candidate"][0] - sides["baseline"][0]
    removed = sides["baseline"][0] - sides["candidate"][0]
    if added or removed:
        problems.append({"problem": "declarations_outside_target_changed",
                         "added": [list(k) for k in sorted(added)][:10],
                         "removed": [list(k) for k in sorted(removed)][:10]})
    return problems


def declared_files(edits: list[ResolvedEdit]) -> list[str]:
    return sorted({e.logical_path for e in edits})


def expected_after(base: bytes, edits: list[ResolvedEdit]) -> bytes:
    """Base bytes with every edit of one file applied (descending offsets: base coordinates)."""
    out = base
    for e in sorted(edits, key=lambda x: x.start, reverse=True):
        out = out[: e.start] + e.replacement + out[e.end :]
    return out


def base_file_path(rec_dir: Path, rel: str) -> Path:
    return rec_dir / "files" / rel
