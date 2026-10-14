"""pyslang SourceLocation -> JSON location DTO (plan 13.4, LOC-01/02).

Canonical span = zero-based half-open byte range into the ORIGINAL file bytes (the physical
file, never the `line-adjusted name). Macro-expanded tokens keep both the spelling location (where
the token text physically lives: a macro body or a macro argument) and the expansion location
(where the macro was invoked). `line directives only change the `display` entry.
"""

from __future__ import annotations

import os
import posixpath
from pathlib import Path
from typing import Any

from svweave.domain.locations import LineIndex

Loc = dict[str, Any]

_PLACEHOLDER = 268435455


class Locator:
    def __init__(self, sm: Any, root: Path, max_text: int = 4096) -> None:
        self.sm = sm
        self.root = root
        self.max_text = max_text
        self._data: dict[str, bytes] = {}
        self._index: dict[str, LineIndex] = {}
        self._buf: dict[int, tuple[str, str, bool] | None] = {}
        self._abs_by_rel: dict[str, str] = {}
        self._root_s = str(root)

    # ------------------------------------------------------------------ files
    def data(self, abs_path: str) -> bytes:
        d = self._data.get(abs_path)
        if d is None:
            try:
                d = Path(abs_path).read_bytes()
            except OSError:
                d = b""
            self._data[abs_path] = d
        return d

    def index(self, abs_path: str) -> LineIndex:
        i = self._index.get(abs_path)
        if i is None:
            i = LineIndex(self.data(abs_path))
            self._index[abs_path] = i
        return i

    def rel(self, abs_path: str) -> tuple[str, bool]:
        """(root-relative POSIX path, outside_root)."""
        try:
            r = os.path.relpath(abs_path, self.root)
        except ValueError:
            return abs_path, True
        if r == ".." or r.startswith(".." + os.sep):
            return abs_path, True
        return (r if os.sep == "/" else r.replace(os.sep, "/")), False

    # ------------------------------------------------------------------ locations
    @staticmethod
    def valid(loc: Any) -> bool:
        return loc is not None and loc.buffer.id != _PLACEHOLDER

    def _file_loc(self, loc: Any) -> tuple[Any, bool, Any]:
        """(original file location, is_macro, expansion location)."""
        sm = self.sm
        if sm.isMacroLoc(loc):
            return sm.getFullyOriginalLoc(loc), True, sm.getFullyExpandedLoc(loc)
        return loc, False, loc

    def buffer_info(self, buf: Any) -> tuple[str, str, bool] | None:
        """(absolute path, root-relative POSIX path, outside_root) for a file buffer, cached."""
        bid = buf.id
        if bid in self._buf:
            return self._buf[bid]
        info: tuple[str, str, bool] | None = None
        name = self.sm.getRawFileName(buf)
        if name and not name.startswith("<"):
            ap = str(self.sm.getFullPath(buf))
            rel, outside = self.rel(ap)
            self._abs_by_rel[rel] = ap
            info = (ap, rel, outside)
        self._buf[bid] = info
        return info

    def abs_path(self, loc: Any) -> str | None:
        if not self.valid(loc):
            return None
        info = self.buffer_info(loc.buffer)
        return info[0] if info else None

    def span(self, loc: Any, end: Any = None, min_len: int = 0) -> Loc | None:
        """Location DTO for ``loc`` (and optional exclusive ``end`` location)."""
        if not self.valid(loc):
            return None
        orig, is_macro, exp = self._file_loc(loc)
        info = self.buffer_info(orig.buffer)
        if info is None:
            return None
        path, rel, outside = info
        start = orig.offset
        stop = start + min_len
        if end is not None and self.valid(end):
            oend = self.sm.getFullyOriginalLoc(end) if self.sm.isMacroLoc(end) else end
            if oend.buffer == orig.buffer and oend.offset >= start:
                stop = max(oend.offset, start + min_len)
        idx = self.index(path)
        size = len(idx.data)
        start = min(start, size)
        stop = min(max(stop, start), size)
        line, col = idx.line_col(start)
        eline, ecol = idx.line_col(stop)
        out: Loc = {
            "file": rel,
            "start": start,
            "end": stop,
            "line": line,
            "col": col,
            "end_line": eline,
            "end_col": ecol,
        }
        if outside:
            out["outside_root"] = True
        presumed = self.sm.getFileName(orig)
        pline = self.sm.getLineNumber(orig)
        if posixpath.normpath(str(presumed)) != posixpath.normpath(rel) or pline != line:
            out["display"] = {"file": str(presumed), "line": pline}
        if is_macro:
            out["macro"] = self._macro(loc, orig, exp)
        return out

    def _macro(self, loc: Any, orig: Any, exp: Any) -> dict[str, Any]:
        sm = self.sm
        m: dict[str, Any] = {
            "name": sm.getMacroName(loc),
            "spelled_in": "macro_argument" if sm.isMacroArgLoc(loc) else "macro_body",
        }
        einfo = self.buffer_info(exp.buffer) if self.valid(exp) else None
        if einfo is not None:
            epath, rel, outside = einfo
            line, col = self.index(epath).line_col(min(exp.offset, len(self.data(epath))))
            m["expansion"] = {"file": rel, "start": exp.offset, "line": line, "col": col}
            if outside:
                m["expansion"]["outside_root"] = True
        return m

    def name_span(self, sym: Any) -> Loc | None:
        """Span of a symbol's name token (handles escaped identifiers)."""
        loc = sym.location
        if not self.valid(loc):
            return None
        name = sym.name or ""
        base = self.span(loc, None, 0)
        if base is None:
            return None
        path = self._abs_of(loc)
        n = len(name.encode("utf-8"))
        if path is not None:
            data = self.data(path)
            s = base["start"]
            if s < len(data) and data[s : s + 1] == b"\\":
                j = s + 1
                while j < len(data) and data[j] not in b" \t\r\n":
                    j += 1
                if data[s + 1 : j].decode("utf-8", "replace") == name:
                    n = j - s
        out = self.span(loc, None, n)
        if out is not None:
            self.verify(out, name)
        return out

    def verify(self, out: Loc, name: str) -> Loc:
        """The bytes at an identifier span must BE the name (or its escaped spelling). Token
        pasting (``r_``n``) and similar make slang's location point at the spelling, not at a
        token with that text: such a span is marked inexact so editing refuses it (B3)."""
        if out.get("outside_root"):
            return out
        got = self.text(out) or ""
        if got != name and got != "\\" + name:
            out["exact"] = False
            out["inexact_reason"] = (
                "bytes at the span differ from the symbol name (token pasting or synthesized "
                "name); the span is the spelling location, not an editable occurrence"
            )
        return out

    def use_site_range(self, rng: Any) -> Loc | None:
        """Range of an expression that starts and/or ends inside a macro expansion, mapped to the
        USE SITE (the macro invocation) at both ends (B2). Returns None if that cannot be formed
        consistently (different files/buffers or reversed): callers then mark the item
        unsupported instead of reporting a bogus span. A non-macro range is a plain span."""
        sm = self.sm
        s, e = rng.start, rng.end
        if not (sm.isMacroLoc(s) or sm.isMacroLoc(e)):
            return self.span(s, e)
        s2 = self._expansion_edge(s, start=True)
        e2 = self._expansion_edge(e, start=False)
        if s2 is None or e2 is None or s2.buffer != e2.buffer or e2.offset < s2.offset:
            return None
        out = self.span(s2, e2)
        if out is None:
            return None
        ref = s if sm.isMacroLoc(s) else e
        mac: dict[str, Any] = {
            "name": sm.getMacroName(ref),
            "spelled_in": "use_site_range",
            "use_site": True,
        }
        try:
            so = sm.getFullyOriginalLoc(s) if sm.isMacroLoc(s) else s
            eo = sm.getFullyOriginalLoc(e) if sm.isMacroLoc(e) else e
            sp = self.span(so, eo)
            if sp is not None and so.buffer == eo.buffer:
                mac["spelling"] = {k: sp[k] for k in ("file", "start", "end")}
        except Exception:  # noqa: BLE001
            pass
        out["macro"] = mac
        return out

    def _expansion_edge(self, loc: Any, *, start: bool) -> Any:
        sm = self.sm
        for _ in range(64):
            if not sm.isMacroLoc(loc):
                return loc
            r = sm.getExpansionRange(loc)
            loc = r.start if start else r.end
        return None

    def _abs_of(self, loc: Any) -> str | None:
        orig, _, _ = self._file_loc(loc)
        return self.abs_path(orig)

    def range(self, rng: Any) -> Loc | None:
        return self.span(rng.start, rng.end)

    def text(self, loc: Loc | None) -> str | None:
        if loc is None or loc.get("outside_root"):
            return None
        # `loc["file"]` is root-relative; rebuild absolute path.
        data = self.data(self._abs_by_rel.get(loc["file"]) or str(self.root / loc["file"]))
        raw = data[loc["start"] : loc["end"]]
        if len(raw) > self.max_text:
            return raw[: self.max_text].decode("utf-8", "replace") + "…"
        return raw.decode("utf-8", "replace")
