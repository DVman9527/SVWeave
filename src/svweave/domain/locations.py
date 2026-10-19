"""Source location contract (plan 13.4, LOC-01).

Canonical spans are zero-based, half-open byte ranges ``[start, end)`` into the ORIGINAL file
bytes. Line/column values derived from them are for display and are 1-based; the column counts
UTF-8 *bytes* from the line start (``position_encoding == "utf-8"``). LSP positions (zero-based
line, zero-based character in a negotiated encoding) are converted with :func:`byte_to_lsp` and
:func:`lsp_to_byte`; UTF-16 characters are never confused with UTF-8 bytes.

Line terminators are ``\\n``, ``\\r\\n`` and a lone ``\\r`` (the LSP definition). This module is
pure Python and does not import pyslang.
"""

from __future__ import annotations

import bisect
from dataclasses import dataclass
from typing import Literal

POSITION_ENCODING = "utf-8"
Encoding = Literal["utf-8", "utf-16", "utf-32"]


class LocationError(ValueError):
    """Offset/position is outside the file or splits a multi-byte character."""


@dataclass(frozen=True)
class Span:
    start: int
    end: int

    def __post_init__(self) -> None:
        if self.start < 0 or self.end < self.start:
            raise LocationError(f"invalid span [{self.start},{self.end})")

    def __len__(self) -> int:
        return self.end - self.start

    def contains(self, offset: int) -> bool:
        return self.start <= offset < self.end

    def overlaps(self, other: Span) -> bool:
        return self.start < other.end and other.start < self.end


class LineIndex:
    """Line starts of a byte string, with display and LSP conversions."""

    def __init__(self, data: bytes) -> None:
        self.data = data
        starts = [0]
        i = 0
        n = len(data)
        while i < n:
            b = data[i]
            if b == 0x0A:
                starts.append(i + 1)
            elif b == 0x0D:
                if i + 1 < n and data[i + 1] == 0x0A:
                    i += 1
                starts.append(i + 1)
            i += 1
        self.line_starts = starts

    @property
    def line_count(self) -> int:
        return len(self.line_starts)

    def _line_of(self, offset: int) -> int:
        if offset < 0 or offset > len(self.data):
            raise LocationError(f"offset {offset} outside file of {len(self.data)} bytes")
        return bisect.bisect_right(self.line_starts, offset) - 1

    def line_col(self, offset: int) -> tuple[int, int]:
        """1-based (line, byte column) for display (position_encoding utf-8)."""
        ln = self._line_of(offset)
        return ln + 1, offset - self.line_starts[ln] + 1

    def _check_boundary(self, offset: int) -> None:
        if offset < len(self.data) and (self.data[offset] & 0xC0) == 0x80:
            raise LocationError(f"offset {offset} is inside a multi-byte UTF-8 character")

    def line_text(self, line0: int) -> bytes:
        if not 0 <= line0 < len(self.line_starts):
            raise LocationError(f"line {line0} outside file")
        s = self.line_starts[line0]
        e = self.line_starts[line0 + 1] if line0 + 1 < len(self.line_starts) else len(self.data)
        return self.data[s:e].rstrip(b"\r\n")


def _units(ch: str, encoding: Encoding) -> int:
    if encoding == "utf-16":
        return 2 if ord(ch) > 0xFFFF else 1
    if encoding == "utf-32":
        return 1
    return len(ch.encode("utf-8"))


def byte_to_lsp(idx: LineIndex, offset: int, encoding: Encoding = "utf-16") -> tuple[int, int]:
    """Original-file byte offset -> LSP (line0, character) in ``encoding``."""
    ln = idx._line_of(offset)
    idx._check_boundary(offset)
    start = idx.line_starts[ln]
    prefix = idx.data[start:offset]
    if encoding == "utf-8":
        return ln, len(prefix)
    try:
        text = prefix.decode("utf-8")
    except UnicodeDecodeError as e:
        raise LocationError(f"invalid UTF-8 before offset {offset}") from e
    return ln, sum(_units(c, encoding) for c in text)


def lsp_to_byte(idx: LineIndex, line0: int, character: int, encoding: Encoding = "utf-16") -> int:
    """LSP (line0, character) -> original-file byte offset (round-trips ``byte_to_lsp``).

    A character index past the end of the line clamps to the line end (LSP rule); an index that
    lands inside a surrogate pair or beyond is rejected.
    """
    if not 0 <= line0 < idx.line_count:
        raise LocationError(f"line {line0} outside file")
    if character < 0:
        raise LocationError("negative character")
    start = idx.line_starts[line0]
    text = idx.line_text(line0)
    if encoding == "utf-8":
        if character > len(text):
            return start + len(text)
        off = start + character
        idx._check_boundary(off)
        return off
    try:
        s = text.decode("utf-8")
    except UnicodeDecodeError as e:
        raise LocationError(f"invalid UTF-8 on line {line0}") from e
    pos = 0
    byte = 0
    for ch in s:
        if pos == character:
            return start + byte
        u = _units(ch, encoding)
        if pos + u > character:
            raise LocationError("character index splits a surrogate pair")
        pos += u
        byte += len(ch.encode("utf-8"))
    return start + len(text)  # at or past line end: clamp


def slice_text(data: bytes, span: Span) -> str:
    """Exact source text of a span (decoded; invalid bytes replaced for display only)."""
    return data[span.start : span.end].decode("utf-8", "replace")
