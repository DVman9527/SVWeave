"""Location contract (plan 13.4): zero-based half-open byte spans; LSP conversions (LOC-01)."""

from __future__ import annotations

import pytest
from hypothesis import given
from hypothesis import strategies as st

from svweave.domain.locations import (
    LineIndex,
    LocationError,
    Span,
    byte_to_lsp,
    lsp_to_byte,
    slice_text,
)

TEXT = "// 中文\r\nmodule\tm;\r\n\tint a; // 😀 x\r\nend\n\rlast\r"
DATA = TEXT.encode("utf-8")


def test_line_terminators_crlf_lf_cr() -> None:
    idx = LineIndex(DATA)
    # lines: "// 中文", "module\tm;", "\tint a; // 😀 x", "end", "" (\n\r = two breaks), "last", ""
    assert idx.line_count == 7
    assert idx.line_text(0) == "// 中文".encode()
    assert idx.line_text(2) == "\tint a; // 😀 x".encode()
    assert idx.line_col(0) == (1, 1)
    first_m = DATA.index(b"module")
    assert idx.line_col(first_m) == (2, 1)
    assert idx.line_col(DATA.index(b"m;")) == (2, 8)  # tab counts as one byte
    assert idx.line_col(len(DATA)) == (7, 1)


def test_span_is_half_open() -> None:
    s = Span(3, 6)
    assert len(s) == 3 and s.contains(3) and s.contains(5) and not s.contains(6)
    assert s.overlaps(Span(5, 9)) and not s.overlaps(Span(6, 9))
    with pytest.raises(LocationError):
        Span(4, 3)
    assert slice_text(DATA, Span(3, 9)) == "中文"


@pytest.mark.parametrize("enc", ["utf-8", "utf-16", "utf-32"])
def test_roundtrip_every_character_boundary(enc: str) -> None:
    idx = LineIndex(DATA)
    off = 0
    for ch in TEXT:
        if ch not in "\r\n":  # offsets inside a line terminator clamp to the line end (LSP)
            ln, c = byte_to_lsp(idx, off, enc)  # type: ignore[arg-type]
            assert lsp_to_byte(idx, ln, c, enc) == off  # type: ignore[arg-type]
        off += len(ch.encode())


def test_utf16_is_not_utf8_bytes() -> None:
    idx = LineIndex(DATA)
    emoji = DATA.index("😀".encode())
    after = emoji + 4
    assert byte_to_lsp(idx, after, "utf-8")[1] == byte_to_lsp(idx, emoji, "utf-8")[1] + 4
    assert (
        byte_to_lsp(idx, after, "utf-16")[1] == byte_to_lsp(idx, emoji, "utf-16")[1] + 2
    )  # surrogate pair
    assert byte_to_lsp(idx, after, "utf-32")[1] == byte_to_lsp(idx, emoji, "utf-32")[1] + 1
    cn = DATA.index("文".encode())
    assert byte_to_lsp(idx, cn, "utf-16")[1] == 4 and byte_to_lsp(idx, cn, "utf-8")[1] == 6


def test_invalid_positions_are_errors_not_roundings() -> None:
    idx = LineIndex(DATA)
    with pytest.raises(LocationError):
        byte_to_lsp(idx, DATA.index("文".encode()) + 1)  # inside a 3-byte character
    with pytest.raises(LocationError):
        byte_to_lsp(idx, len(DATA) + 1)
    with pytest.raises(LocationError):
        byte_to_lsp(idx, -1)
    with pytest.raises(LocationError):
        lsp_to_byte(idx, 99, 0)
    emoji_line = 2
    emoji_char = byte_to_lsp(idx, DATA.index("😀".encode()), "utf-16")[1]
    with pytest.raises(LocationError):
        lsp_to_byte(idx, emoji_line, emoji_char + 1, "utf-16")  # middle of a surrogate pair
    with pytest.raises(LocationError):
        lsp_to_byte(idx, 0, -1)
    # past the end of the line clamps to the line end (not into the line terminator)
    assert lsp_to_byte(idx, 0, 999) == len("// 中文".encode())


@given(
    st.text(
        alphabet=st.characters(blacklist_categories=("Cs",), blacklist_characters="\r\n"),
        max_size=40,
    )
)
def test_hypothesis_single_line_roundtrip(s: str) -> None:
    data = s.encode("utf-8")
    idx = LineIndex(data)
    off = 0
    for ch in s + "\0":
        for enc in ("utf-8", "utf-16"):
            ln, c = byte_to_lsp(idx, off, enc)  # type: ignore[arg-type]
            assert lsp_to_byte(idx, ln, c, enc) == min(off, len(data))  # type: ignore[arg-type]
        off += len(ch.encode())
        if off > len(data):
            break
