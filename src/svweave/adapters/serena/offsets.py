"""Line / column to byte-offset conversion on the candidate file bytes (plan 13.4).

Canonical SVWeave spans are zero-based half-open byte ranges over the ORIGINAL file bytes. What
upstream gives us (M0 facts):

* ``find_symbol`` / ``find_referencing_symbols``: ZERO-BASED LINE numbers only
  (``body_location.start_line`` / ``end_line``, both inclusive); no columns. The symbol ``body`` text
  is returned when requested, so the exact byte span is recovered by locating that text inside the
  line window (:func:`locate_body`).
* ``get_diagnostics_for_file`` ranges: LSP ``line`` / ``character`` (zero-based). Verible does not
  advertise ``positionEncoding``, so the LSP default applies: ``character`` counts UTF-16 code units
  (:func:`lsp_position_to_byte`), never UTF-8 bytes.

Line terminators follow LSP: ``\\n``, ``\\r\\n`` and a lone ``\\r``.
"""

from __future__ import annotations

from typing import Literal

Encoding = Literal["utf-16", "utf-8", "utf-32"]


def line_starts(data: bytes) -> list[int]:
    """Byte offset of the start of every line (LSP line terminators)."""
    starts = [0]
    i, n = 0, len(data)
    while i < n:
        c = data[i]
        if c == 0x0A:
            starts.append(i + 1)
        elif c == 0x0D:
            if i + 1 < n and data[i + 1] == 0x0A:
                i += 1
            starts.append(i + 1)
        i += 1
    return starts


def line_end(data: bytes, starts: list[int], line: int) -> int:
    """Byte offset just before the terminator of ``line`` (end of content)."""
    end = starts[line + 1] if line + 1 < len(starts) else len(data)
    while end > starts[line] and data[end - 1] in (0x0A, 0x0D):
        end -= 1
    return end


def lsp_position_to_byte(
    data: bytes, line: int, character: int, encoding: Encoding = "utf-16"
) -> int:
    """Byte offset of an LSP position. ``character`` past the end of the line clamps to its end."""
    starts = line_starts(data)
    if line < 0 or line >= len(starts):
        raise ValueError(f"line {line} out of range (file has {len(starts)} lines)")
    start, end = starts[line], line_end(data, starts, line)
    if encoding == "utf-8":
        return min(start + character, end)
    text = data[start:end].decode("utf-8", errors="surrogateescape")
    units = 0
    offset = start
    for ch in text:
        if units >= character:
            break
        cp = ord(ch)
        if encoding == "utf-16":
            units += 2 if cp > 0xFFFF else 1
        else:
            units += 1
        offset += len(ch.encode("utf-8", errors="surrogateescape"))
    return offset


def line_window(data: bytes, start_line: int, end_line: int) -> tuple[int, int]:
    """Byte range covering whole lines ``start_line..end_line`` (inclusive, zero-based)."""
    starts = line_starts(data)
    if not (0 <= start_line <= end_line < len(starts)):
        raise ValueError(f"line window {start_line}-{end_line} out of range ({len(starts)} lines)")
    return starts[start_line], line_end(data, starts, end_line)


def locate_body(data: bytes, start_line: int, end_line: int, body: str) -> tuple[int, int] | None:
    """Exact byte span of ``body`` inside the line window, or None if absent / not unique.

    The body Serena returns starts at the symbol keyword (indentation stripped) and ends at the
    closing keyword; searching the window makes the span independent of column conventions.
    """
    lo, hi = line_window(data, start_line, end_line)
    needle = body.encode("utf-8", errors="surrogateescape")
    if not needle:
        return None
    window = data[lo:hi]
    first = window.find(needle)
    if first < 0 or window.find(needle, first + 1) >= 0:
        return None
    return lo + first, lo + first + len(needle)
