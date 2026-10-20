"""Rename preview: every place a rename of one declaration would touch (``svweave query
rename-preview`` / ``svw_rename_preview``). Read-only: it never writes the source tree.

For one declaration of one build it lists the exact byte edits (the declaration's name and every
statically resolved reference, plus the formal name of named port connections), each VERIFIED
against the snapshot bytes (the text at the span must equal the old name). It adds the unified
diff, and, when nothing blocks it, an edit request for the existing controlled edit loop
(``svweave edit prepare --spec``): candidate copy, slang validation of baseline and candidate,
human approval, compare-and-set commit. The loop, not this preview, is what writes and what can
refuse; applying a rename here is deliberately not offered.

What a rename can break that no semantic table records is surfaced instead of guessed:

* ``blockers`` make the preview not ready: a name that is not a plain identifier, a keyword or the
  old name; a declaration with that name in the same scope; sites that are spelled in a macro body
  or whose bytes do not match; references that slang did not resolve statically; port connections
  made by ``.*``, ``.name`` or ``.name()``; named connections whose formal name could not be
  located; a build that is not COMPLETE and CURRENT; more edits than one request may carry;
* ``warnings`` do not block: a same-named declaration in a nested scope, the profile still naming
  the old top module / top parameter, instances that override a parameter by name;
* ``unconfirmed_occurrences`` is a lexical scan of every file of the snapshot for the old name
  that is NOT part of the edit set (named associations, end labels, comments and strings, other
  uses). It is a safety net, not a verdict: the same name elsewhere is often a different entity.

Counts are counts. Absence from a PARTIAL, STALE or truncated result is not proof of absence.
"""

from __future__ import annotations

import difflib
import hashlib
import re
from typing import Any

from svweave.application.impact import (
    _fit,
    _names,
    _owner_definition,
    _twins,
)
from svweave.application.options import WorkspaceOptions
from svweave.application.queries import (
    LOCATION_CONVENTION,
    _decl_dto,
    _decl_rows_by_location,
    _gap_limits,
    _session,
)
from svweave.application.semantic_common import build_envelope
from svweave.domain.cursor import MAX_RESPONSE_BYTES, check_limit
from svweave.domain.envelope import Envelope, ErrorInfo, Outcome
from svweave.domain.errors import ErrorCode, SvwError
from svweave.domain.states import Completeness, Freshness
from svweave.editing.plan import mask_comments_strings
from svweave.snapshot.capture import load_record, snapshots_dir
from svweave.storage.semantic import loads

DEFAULT_LIST = 50
MAX_EDITS_PER_REQUEST = 50  # schemas/edit.schema.json: maxItems
MAX_DIFF_BYTES = 48 * 1024
MAX_SCAN_MATCHES = 200_000
MAX_SCAN_FILE = 16 * 1024 * 1024
GLOBAL_KINDS = frozenset({"module", "interface", "program", "package", "primitive", "checker"})
END_KEYWORD = {
    "module": rb"endmodule", "interface": rb"endinterface", "program": rb"endprogram",
    "package": rb"endpackage", "primitive": rb"endprimitive", "checker": rb"endchecker",
}
RENAME_NOTE = (
    "every edit site is verified against the snapshot bytes; resolved references and named port "
    "connections only: see blockers, warnings and unconfirmed_occurrences for what a rename can "
    "still break; applying goes through svweave edit (validate, human approval, compare-and-set)"
)
_IDENT = re.compile(r"[A-Za-z_][A-Za-z0-9_$]{0,254}")  # always used with fullmatch: `$` would pass "a\n"
_LISTS = (
    "sites",
    "skipped_sites",
    "files",
    "unconfirmed_occurrences",
    "implicit_connections",
    "diffs",
)
# IEEE 1800-2017 Annex B reserved keywords
KEYWORDS = frozenset(
    ["alias", "always", "always_comb", "always_ff", "always_latch", "and", "assert", "assign", "assume", "automatic", "before", "begin", "bind", "bins", "binsof", "bit", "break", "buf", "bufif0", "bufif1", "byte", "case", "casex", "casez", "cell", "chandle", "checker", "class", "clocking", "cmos", "config", "const", "constraint", "context", "continue", "cover", "covergroup", "coverpoint", "cross", "deassign", "default", "defparam", "design", "disable", "dist", "do", "edge", "else", "end", "endcase", "endchecker", "endclass", "endclocking", "endconfig", "endfunction", "endgenerate", "endgroup", "endinterface", "endmodule", "endpackage", "endprimitive", "endprogram", "endproperty", "endsequence", "endspecify", "endtable", "endtask", "enum", "event", "eventually", "expect", "export", "extends", "extern", "final", "first_match", "for", "force", "foreach", "forever", "fork", "forkjoin", "function", "generate", "genvar", "global", "highz0", "highz1", "if", "iff", "ifnone", "ignore_bins", "illegal_bins", "implements", "implies", "import", "incdir", "include", "initial", "inout", "input", "inside", "instance", "int", "integer", "interconnect", "interface", "intersect", "join", "join_any", "join_none", "large", "let", "liblist", "library", "local", "localparam", "logic", "longint", "macromodule", "matches", "medium", "modport", "module", "nand", "negedge", "nettype", "new", "nexttime", "nmos", "nor", "noshowcancelled", "not", "notif0", "notif1", "null", "or", "output", "package", "packed", "parameter", "pmos", "posedge", "primitive", "priority", "program", "property", "protected", "pull0", "pull1", "pulldown", "pullup", "pulsestyle_ondetect", "pulsestyle_onevent", "pure", "rand", "randc", "randcase", "randsequence", "rcmos", "real", "realtime", "ref", "reg", "reject_on", "release", "repeat", "restrict", "return", "rnmos", "rpmos", "rtran", "rtranif0", "rtranif1", "s_always", "s_eventually", "s_nexttime", "s_until", "s_until_with", "scalared", "sequence", "shortint", "shortreal", "showcancelled", "signed", "small", "soft", "solve", "specify", "specparam", "static", "string", "strong", "strong0", "strong1", "struct", "super", "supply0", "supply1", "sync_accept_on", "sync_reject_on", "table", "tagged", "task", "this", "throughout", "time", "timeprecision", "timeunit", "tran", "tranif0", "tranif1", "tri", "tri0", "tri1", "triand", "trior", "trireg", "type", "typedef", "union", "unique", "unique0", "unsigned", "until", "until_with", "untyped", "use", "uwire", "var", "vectored", "virtual", "void", "wait", "wait_order", "wand", "weak", "weak0", "weak1", "while", "wildcard", "wire", "with", "within", "wor", "xnor", "xor"]
)


def check_identifier(name: str, field: str) -> str:
    if not isinstance(name, str) or not _IDENT.fullmatch(name):
        raise SvwError(
            ErrorCode.INVALID_CONFIG,
            f"{field} must be a plain SystemVerilog identifier ([A-Za-z_][A-Za-z0-9_$]*, max 255); "
            "escaped identifiers are not supported",
            {"field": field},
        )
    return name


def _like_escape(text: str) -> str:
    """``text`` as a literal inside a SQL LIKE pattern (``_`` and ``%`` are wildcards)."""
    return text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _word_re(name: str) -> re.Pattern[bytes]:
    n = re.escape(name.encode())
    return re.compile(rb"(?<![A-Za-z0-9_$])" + n + rb"(?![A-Za-z0-9_$])")


def _line_col(data: bytes, off: int) -> tuple[int, int]:
    line = data.count(b"\n", 0, off) + 1
    col = off - (data.rfind(b"\n", 0, off) + 1) + 1
    return line, col


def _line_text(data: bytes, off: int, width: int = 140) -> str:
    a = data.rfind(b"\n", 0, off) + 1
    b = data.find(b"\n", off)
    b = len(data) if b < 0 else b
    return data[a : min(b, a + width)].decode("utf-8", "replace").strip()


def _macro_body(loc: Any) -> str | None:
    m = (loc or {}).get("macro") if isinstance(loc, dict) else None
    if isinstance(m, dict) and m.get("spelled_in") == "macro_body":
        return str(m.get("name"))
    return None


def _formal_span(masked: bytes, actual_start: int, old: bytes) -> tuple[int, int] | None:
    """Byte span of ``old`` in ``.old(`` that immediately precedes a connection's actual expression
    (comments are masked). None when the bytes before the actual are not exactly that shape."""
    window = masked[max(0, actual_start - 400) : actual_start]
    m = re.search(rb"\.\s*(" + re.escape(old) + rb")\s*\(\s*$", window)
    if m is None:
        return None
    base = max(0, actual_start - 400)
    return base + m.start(1), base + m.end(1)


def rename_preview(
    opts: WorkspaceOptions,
    build_id: str,
    request_id: str,
    *,
    new_name: str,
    symbol_id: str | None = None,
    file: str | None = None,
    byte: int | None = None,
    name: str | None = None,
    limit: int | None = None,
) -> Envelope:
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
    check_identifier(new_name, "new_name")
    lim = DEFAULT_LIST if limit is None else check_limit(limit)
    with _session(opts, build_id) as s:
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
        old = str(decl["name"])
        if not old or not _IDENT.fullmatch(old):
            raise SvwError(
                ErrorCode.INVALID_CONFIG,
                f"the declaration's name {old!r} is not a plain identifier; not supported",
                {"reason": "old_name_unsupported"},
            )
        blockers: list[dict[str, Any]] = []
        warnings: list[dict[str, Any]] = []

        def block(code: str, message: str, **details: Any) -> None:
            blockers.append({"code": code, "message": message, **details})

        def warn(code: str, message: str, **details: Any) -> None:
            warnings.append({"code": code, "message": message, **details})

        # ---- name checks
        if new_name == old:
            block("same_name", "the new name equals the old name")
        if new_name in KEYWORDS:
            block("keyword", f"{new_name!r} is a SystemVerilog reserved keyword")
        scope = decl["scope"] or ""
        id_ph = ",".join("?" for _ in ids)
        clash = s.rows(
            "SELECT qualified_name, kind, file, line FROM declarations WHERE build_id=? AND name=? "
            f"AND scope=? AND declaration_id NOT IN ({id_ph}) LIMIT 20",
            s.bid,
            new_name,
            scope,
            *ids,
        )
        if clash:
            block(
                "name_collision",
                f"{len(clash)} declaration(s) named {new_name!r} already exist in scope {scope!r}",
                declarations=[
                    {"qualified_name": r["qualified_name"], "kind": r["kind"], "file": r["file"],
                     "line": r["line"]}
                    for r in clash
                ],
            )
        if scope:
            nested = s.rows(
                "SELECT qualified_name, kind, file, line FROM declarations WHERE build_id=? "
                "AND name=? AND (scope LIKE ? ESCAPE '\\' OR scope LIKE ? ESCAPE '\\') LIMIT 20",
                s.bid,
                new_name,
                _like_escape(scope) + ".%",
                _like_escape(scope) + "::%",
            )
            if nested:
                warn(
                    "name_in_nested_scope",
                    f"{len(nested)} declaration(s) named {new_name!r} exist in scopes nested under "
                    f"{scope!r}; a renamed reference there could be captured",
                    declarations=[{"qualified_name": r["qualified_name"], "kind": r["kind"]}
                                  for r in nested],
                )

        # ---- snapshot access
        snap_dir = snapshots_dir(s.ctx) / str(s.build["snapshot_id"])
        rec = load_record(snap_dir)
        by_logical = {f.logical_path: f for f in rec.files}
        cache: dict[str, bytes] = {}

        def bytes_of(logical: str) -> bytes | None:
            if logical not in cache:
                sf = by_logical.get(logical)
                if sf is None:
                    return None
                cache[logical] = (snap_dir / sf.snapshot_path).read_bytes()
            return cache[logical]

        masked_cache: dict[str, bytes | None] = {}

        def masked_of(logical: str) -> bytes | None:
            if logical not in masked_cache:
                b = bytes_of(logical)
                masked_cache[logical] = None if b is None else mask_comments_strings(b)
            return masked_cache[logical]

        old_b = old.encode()
        sites: dict[tuple[str, int, int], dict[str, Any]] = {}
        skipped: list[dict[str, Any]] = []

        def add_site(logical: str, start: int, end: int, role: str, container: str | None,
                     loc: Any) -> None:
            if (logical, start, end) in sites:
                return
            mb = _macro_body(loc)
            if mb is not None:
                skipped.append({"file": logical, "start_byte": start, "end_byte": end, "role": role,
                                "reason": "spelled_in_macro_body", "macro": mb})
                return
            data = bytes_of(logical)
            if data is None:
                skipped.append({"file": logical, "start_byte": start, "end_byte": end, "role": role,
                                "reason": "file_not_in_snapshot"})
                return
            found = data[start:end]
            if found != old_b:
                skipped.append({"file": logical, "start_byte": start, "end_byte": end, "role": role,
                                "reason": "text_mismatch",
                                "found": found.decode("utf-8", "replace")[:80]})
                return
            line, col = _line_col(data, start)
            sites[(logical, start, end)] = {
                "file": logical, "start_byte": start, "end_byte": end, "line": line, "col": col,
                "role": role, "container": container,
            }

        # ---- the declaration itself
        add_site(str(decl["file"]), int(decl["span_start"]), int(decl["span_end"]),
                 "declaration", None, loads(decl["loc"]))
        # ---- resolved references
        q = ",".join("?" for _ in ids)
        refs = s.rows(
            f'SELECT * FROM "references" WHERE build_id=? AND target_id IN ({q}) '
            "AND resolution_status='resolved_static' ORDER BY file, span_start",
            s.bid,
            *ids,
        )
        cont_ids = {r["container_id"] for r in refs if r["container_id"]}
        cont = _names(s, cont_ids)
        for r in refs:
            c = cont.get(r["container_id"]) if r["container_id"] else None
            add_site(str(r["file"]), int(r["span_start"]), int(r["span_end"]), "reference",
                     c["qualified_name"] if c else None, loads(r["loc"]))
        unresolved = {
            r["resolution_status"]: int(r["n"])
            for r in s.rows(
                f'SELECT resolution_status, COUNT(*) n FROM "references" WHERE build_id=? '
                f"AND target_id IN ({q}) AND resolution_status!='resolved_static' "
                "GROUP BY resolution_status",
                s.bid,
                *ids,
            )
        }
        if unresolved:
            block("unresolved_references",
                  f"{sum(unresolved.values())} reference(s) to this declaration are not statically "
                  "resolved and cannot be edited safely", by_status=unresolved)

        # ---- port connections by name
        implicit: list[dict[str, Any]] = []
        is_port = any(t["kind"] == "port" for t in (twins or [decl]))
        if is_port:
            pq = ",".join("?" for _ in ids)
            for b in s.rows(
                f"SELECT b.*, i.path AS ipath FROM port_bindings b JOIN instances i ON "
                "i.build_id=b.build_id AND i.instance_id=b.instance_id WHERE b.build_id=? "
                f"AND b.port_declaration_id IN ({pq}) ORDER BY b.ord",
                s.bid,
                *ids,
            ):
                st = b["style"]
                if st == "named":
                    aloc = loads(b["actual_loc"]) or {}
                    lf, a0 = aloc.get("file"), aloc.get("start")
                    m = masked_of(str(lf)) if lf else None
                    span = _formal_span(m, int(a0), old_b) if (m is not None and a0 is not None) else None
                    if span is None:
                        skipped.append({"file": lf, "start_byte": a0, "end_byte": None,
                                        "role": "named_port_connection",
                                        "reason": "named_connection_not_located",
                                        "instance": b["ipath"]})
                    else:
                        add_site(str(lf), span[0], span[1], "named_port_connection", b["ipath"],
                                 aloc)
                elif st in ("wildcard", "named_implicit", "open"):
                    implicit.append({"instance": b["ipath"], "style": st})
            if implicit:
                kinds = sorted({x["style"] for x in implicit})
                block(
                    "implicit_port_connections",
                    f"{len(implicit)} instance(s) connect this port by '.*', '.name' or '.name()': "
                    "renaming the port breaks or changes those connections",
                    styles=kinds,
                )

        # ---- parameter overrides by name, profile references
        owner = _owner_definition(s, decl)
        if decl["kind"] == "parameter" and owner:
            like = '%"name":"' + _like_escape(old) + '","overridden":true%'
            n_over = s.count(
                "SELECT COUNT(*) FROM instances WHERE build_id=? AND definition_declaration_id=? "
                "AND parameters LIKE ? ESCAPE '\\'",
                s.bid,
                owner,
                like,
            )
            if n_over:
                warn("parameter_overrides_by_name",
                     f"{n_over} instance(s) override this parameter by name (#(.{old}(...))); those "
                     "sites are not edited automatically, see unconfirmed_occurrences",
                     instances=n_over)
        try:
            _, prof = s.ctx.profile(str(s.build["profile"]))
            tops = list(getattr(prof, "top_modules", []) or [])
            if decl["kind"] in ("module", "interface", "program") and old in tops:
                warn("profile_top_module",
                     f"profile {s.build['profile']!r} lists {old!r} in top_modules: update "
                     "svweave.yaml after the rename")
            tps = [t for t in (getattr(prof, "top_parameters", []) or []) if t.name == old]
            if tps and decl["kind"] == "parameter":
                warn("profile_top_parameter",
                     f"profile {s.build['profile']!r} sets top parameter {old!r}: update "
                     "svweave.yaml after the rename")
        except SvwError:
            pass

        # ---- lexical safety net over the whole snapshot
        confirmed: dict[str, list[tuple[int, int]]] = {}
        for lf, a0, a1 in sites:
            confirmed.setdefault(lf, []).append((a0, a1))
        word = _word_re(old)
        global_kind = decl["kind"] in GLOBAL_KINDS
        ext = None
        if scope and not global_kind:  # source extent of the enclosing module/package/class
            ext = s.one(
                "SELECT file, COALESCE(full_start, span_start) AS lo, COALESCE(full_end, span_end) "
                "AS hi FROM declarations WHERE build_id=? AND qualified_name=? AND kind IN "
                "('module','interface','program','package','class','checker') "
                "ORDER BY span_start LIMIT 1",
                s.bid,
                scope,
            )
        owner_uses: list[dict[str, Any]] = []
        end_kw = END_KEYWORD.get(str(decl["kind"]))
        unconf: list[dict[str, Any]] = []
        by_cat: dict[str, int] = {}
        scanned = matches = 0
        scan_truncated = False
        for sf in rec.files:
            if sf.role == "filelist" or sf.size > MAX_SCAN_FILE:
                continue
            data = bytes_of(sf.logical_path)
            if data is None or not word.search(data):
                continue
            scanned += 1
            masked = masked_of(sf.logical_path)
            done = confirmed.get(sf.logical_path, [])
            for mt in word.finditer(data):
                if any(x0 <= mt.start() and mt.end() <= x1 for x0, x1 in done):
                    continue
                matches += 1
                if matches > MAX_SCAN_MATCHES:
                    scan_truncated = True
                    break
                off = mt.start()
                if masked is not None and masked[off : mt.end()] != old_b:
                    cat = "comment_or_string"
                else:
                    before = (masked if masked is not None else data)[max(0, off - 64) : off]
                    after = (masked if masked is not None else data)[mt.end() : mt.end() + 16]
                    if re.search(rb"\.\s*$", before) and re.match(rb"\s*\(", after):
                        cat = "named_association"
                    elif re.search(rb"\bend\w*\s*:\s*$", before):
                        cat = "end_label"
                        if end_kw is not None and re.search(
                            rb"\b" + end_kw + rb"\s*:\s*$", before
                        ):
                            add_site(sf.logical_path, off, mt.end(), "end_label", None, None)
                            continue
                    elif re.search(rb"\bbind\s+$", before):
                        cat = "bind_target"
                        if decl["kind"] in ("module", "interface", "program"):
                            add_site(sf.logical_path, off, mt.end(), "bind_target", None, None)
                            continue
                    else:
                        cat = "other_use"
                by_cat[cat] = by_cat.get(cat, 0) + 1
                line, col = _line_col(data, off)
                entry = {"file": sf.logical_path, "line": line, "col": col, "category": cat,
                         "text": _line_text(data, off)}
                if (
                    ext is not None and cat == "other_use" and sf.logical_path == ext["file"]
                    and ext["lo"] <= off < ext["hi"]
                ):
                    owner_uses.append(entry)  # inside the declaring scope, yet no resolved reference
                if len(unconf) < 4 * lim:
                    unconf.append(entry)
            if scan_truncated:
                break
        order = {"bind_target": 0, "named_association": 1, "end_label": 2, "other_use": 3,
                 "comment_or_string": 4}
        unconf.sort(key=lambda x: (order[x["category"]], x["file"], x["line"]))
        leftover = sum(by_cat.get(k, 0) for k in ("other_use", "named_association", "end_label",
                                                  "bind_target"))
        if global_kind and leftover:
            block(
                "unconfirmed_uses_of_global_name",
                f"{leftover} use(s) of the {decl['kind']} name {old!r} are not in the edit set "
                "(not a resolved reference, an end label or a bind target); a module, interface, "
                "program or package name is global, so they are probably the same entity",
                by_category={k: v for k, v in by_cat.items() if k != "comment_or_string"},
                examples=[x for x in unconf if x["category"] != "comment_or_string"][:5],
            )
        if owner_uses:
            block(
                "unresolved_uses_in_owner_scope",
                f"{len(owner_uses)} use(s) of {old!r} inside {scope!r} are not resolved references "
                "(typically an inactive `ifdef` or generate branch, a macro, or unelaborated "
                "code): renaming only the resolved sites would leave them behind",
                examples=owner_uses[:5],
            )
        if by_cat.get("named_association") or by_cat.get("end_label"):
            warn("occurrences_not_in_the_edit_set",
                 f"{by_cat.get('named_association', 0)} named association(s) and "
                 f"{by_cat.get('end_label', 0)} end label(s) mention {old!r} outside the edit set; "
                 "they may belong to this entity (see unconfirmed_occurrences)", by_category=by_cat)

        # ---- build state the edit loop requires
        comp = Completeness(s.build["completeness"])
        fresh = s.fresh()
        if comp != Completeness.COMPLETE:
            block("build_not_complete",
                  f"the build is {comp.value}; the edit loop needs a COMPLETE build")
        if fresh != Freshness.CURRENT:
            block("build_not_current",
                  f"freshness is {fresh.value}; the edit loop needs a CURRENT workspace")
        if skipped:
            by_reason: dict[str, int] = {}
            for x in skipped:
                by_reason[x["reason"]] = by_reason.get(x["reason"], 0) + 1
            block("sites_not_editable",
                  f"{len(skipped)} site(s) cannot be edited safely", by_reason=by_reason)

        # ---- diffs
        edits_by_file: dict[str, list[dict[str, Any]]] = {}
        for st_ in sites.values():
            edits_by_file.setdefault(st_["file"], []).append(st_)
        diffs: list[dict[str, Any]] = []
        diff_bytes = 0
        files_out: list[dict[str, Any]] = []
        changed_lines = 0
        for lf, es in sorted(edits_by_file.items()):
            src = bytes_of(lf) or b""
            es.sort(key=lambda e: e["start_byte"])
            out = bytearray()
            pos = 0
            for e in es:
                out += src[pos : e["start_byte"]] + new_name.encode()
                pos = e["end_byte"]
            out += src[pos:]
            old_lines = src.decode("utf-8", "surrogateescape").splitlines(keepends=True)
            new_lines = bytes(out).decode("utf-8", "surrogateescape").splitlines(keepends=True)
            ud = "".join(difflib.unified_diff(old_lines, new_lines, f"a/{lf}", f"b/{lf}", n=1))
            changed_lines += sum(
                1 for ln in ud.splitlines()
                if ln[:1] in "+-" and not ln.startswith(("+++", "---"))
            ) // 2
            files_out.append({"file": lf, "sites": len(es)})
            if diff_bytes + len(ud) <= MAX_DIFF_BYTES:
                diffs.append({"file": lf, "diff": ud})
                diff_bytes += len(ud)
        # ---- edit request for the controlled edit loop
        n_edits = len(sites)
        limits: dict[str, Any] = {
            "edits": n_edits, "max_edits_per_request": MAX_EDITS_PER_REQUEST,
            "files": len(edits_by_file), "changed_lines": changed_lines,
        }
        try:
            ed = s.ctx.manifest.editing
            limits.update(max_files_per_patch=ed.max_files_per_patch,
                          max_changed_lines=ed.max_changed_lines, editing_enabled=bool(ed.enabled))
            within = (n_edits <= MAX_EDITS_PER_REQUEST and len(edits_by_file) <= ed.max_files_per_patch
                      and changed_lines <= ed.max_changed_lines)
        except Exception:  # noqa: BLE001 - config shape is validated elsewhere; stay conservative
            within = n_edits <= MAX_EDITS_PER_REQUEST
        limits["within_limits"] = within
        if not within:
            block("exceeds_edit_limits",
                  "the rename touches more than one edit request may carry "
                  f"(edits {n_edits}/{MAX_EDITS_PER_REQUEST}, files {len(edits_by_file)}, "
                  f"changed lines {changed_lines}); no edit request is produced",
                  limits={k: v for k, v in limits.items() if k != "within_limits"})
        ready = not blockers
        edit_request: dict[str, Any] | None = None
        if ready:
            key = re.sub(r"[^A-Za-z0-9._:-]", "_", f"rename-{old}-to-{new_name}-{s.bid[:8]}")[:128]
            edit_request = {
                "base_build_id": s.bid,
                "snapshot_id": str(s.build["snapshot_id"]),
                "edits": [
                    {
                        "kind": "exact_range", "logical_path": e["file"],
                        "start_byte": e["start_byte"], "end_byte": e["end_byte"],
                        "expected_sha256": hashlib.sha256(old_b).hexdigest(),
                        "replacement": new_name,
                    }
                    for e in sorted(sites.values(), key=lambda x: (x["file"], x["start_byte"]))
                ],
                "request_key": key.ljust(8, "_"),
                "validate_profiles": [str(s.build["profile"])],
            }
        site_list = sorted(sites.values(), key=lambda x: (x["file"], x["start_byte"]))
        dto = _decl_dto(decl)
        target = {k: dto[k] for k in ("symbol_id", "declaration_id", "kind", "name",
                                      "qualified_name", "scope", "location")}
        if twins:
            target["same_entity"] = [{"declaration_id": t["declaration_id"], "kind": t["kind"]}
                                     for t in twins]
        data_out: dict[str, Any] = {
            "target": target,
            "old_name": old,
            "new_name": new_name,
            "ready_for_edit_request": ready,
            "blockers": blockers,
            "warnings": warnings,
            "sites": site_list,
            "sites_total": len(site_list),
            "skipped_sites": skipped,
            "files": files_out,
            "implicit_connections": implicit,
            "unconfirmed_occurrences": unconf,
            "unconfirmed_summary": {
                "by_category": by_cat, "total": matches, "files_with_matches": scanned,
                "truncated": scan_truncated,
            },
            "diffs": diffs,
            "diffs_truncated": len(diffs) < len(files_out),
            "limits": limits,
            "edit_request": edit_request,
            "summary": {
                "sites": len(site_list), "files": len(files_out), "blockers": len(blockers),
                "warnings": len(warnings), "skipped_sites": len(skipped),
                "unconfirmed_occurrences": matches,
            },
            "limit": lim,
            "location_convention": LOCATION_CONVENTION,
            "note": RENAME_NOTE,
        }
        cut = _fit(data_out, MAX_RESPONSE_BYTES - 32_768, _LISTS)
        for k in _LISTS:
            if isinstance(data_out.get(k), list) and len(data_out[k]) > lim and k != "diffs":
                data_out[k] = data_out[k][:lim]
                cut = [*cut, k] if k not in cut else cut
        if edit_request is not None and cut and "sites" in cut:
            # the inline request is complete by construction; never present a cut list as the edit
            data_out["sites_note"] = "sites is capped; edit_request holds every edit"
        data_out["truncated_lists"] = cut
        wmsgs: list[str] = []
        if cut:
            wmsgs.append(f"truncated: {', '.join(cut)}; the summary figures are complete")
        if blockers:
            wmsgs.append(f"{len(blockers)} blocker(s): no edit request was produced (see blockers)")
        return build_envelope(
            s.ctx,
            s.db,
            s.build,
            request_id,
            data_out,
            warnings=wmsgs,
            extra_limits=[*_gap_limits(s), RENAME_NOTE],
            freshness=fresh,
        )

