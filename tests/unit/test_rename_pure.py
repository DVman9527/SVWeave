"""RNM-U: the pure parts of rename-preview."""

from __future__ import annotations

import pytest

from svweave.application.rename import (
    KEYWORDS,
    _formal_span,
    _like_escape,
    _word_re,
    check_identifier,
)
from svweave.domain.errors import SvwError


@pytest.mark.parametrize("ok", ["a", "_a", "data_o", "A1_b$c", "x" * 255])
def test_RNM_U01_plain_identifiers_pass(ok: str) -> None:
    assert check_identifier(ok, "new_name") == ok


@pytest.mark.parametrize(
    "bad", ["", "1a", "a b", "a-b", "a;b", "$sys", "\\esc ", "a\n", "é", "x" * 256, "a.b", "a[0]"]
)
def test_RNM_U02_everything_else_is_refused(bad: str) -> None:
    with pytest.raises(SvwError) as e:
        check_identifier(bad, "new_name")
    assert e.value.code.value == "INVALID_CONFIG"


def test_RNM_U03_keyword_list_has_the_reserved_words_and_not_ordinary_names() -> None:
    for kw in ("wire", "module", "endmodule", "logic", "input", "output", "class", "bind", "new",
               "static", "string", "unique0", "s_until_with", "implements", "interconnect"):
        assert kw in KEYWORDS, kw
    for ok in ("data_o", "clk", "valid_i", "rst_n", "foo", "Module", "WIRE"):
        assert ok not in KEYWORDS  # keywords are case sensitive
    assert len(KEYWORDS) >= 240 and all(k.isidentifier() for k in KEYWORDS)


def test_RNM_U04_word_match_is_a_whole_identifier_not_a_substring() -> None:
    w = _word_re("clk")
    text = b"clk clk_i my_clk clk$ clk2 .clk(clk) xclk clk;"
    assert [m.start() for m in w.finditer(text)] == [0, 28, 32, 42]  # whole identifiers only: not clk_i, my_clk, clk$, clk2, xclk


def test_RNM_U05_formal_name_is_located_only_in_the_exact_dot_name_paren_shape() -> None:
    old = b"valid_i"
    data = b"leaf u (\n    .valid_i (valid_i[lane]),\n    .x(1));"
    start = data.index(b"valid_i[lane]")
    span = _formal_span(data, start, old)
    assert span is not None and data[span[0]:span[1]] == old and span[0] == data.index(b".valid_i") + 1
    assert _formal_span(b"foo (valid_i)", 5, old) is None  # no dot: an ordered connection
    assert _formal_span(b".other (valid_i)", 9, old) is None  # a different formal
    masked = b".valid_i (        valid_i)"  # a comment between ( and the actual, already masked
    assert _formal_span(masked, masked.rindex(b"valid_i"), old) == (1, 8)
    assert _formal_span(b".valid_i x (valid_i)", 12, old) is None  # junk between name and (


def test_RNM_U06_like_escape_makes_wildcards_literal() -> None:
    import sqlite3

    assert _like_escape("a_b") == "a\\_b" and _like_escape("50%") == "50\\%"
    assert _like_escape("a\\b") == "a\\\\b"
    db = sqlite3.connect(":memory:")
    db.execute("CREATE TABLE t (v TEXT)")
    db.executemany("INSERT INTO t VALUES (?)", [("a_b",), ("axb",), ("50%",), ("500",), ("a\\b",)])
    for lit, want in (("a_b", ["a_b"]), ("50%", ["50%"]), ("a\\b", ["a\\b"])):
        got = [r[0] for r in db.execute("SELECT v FROM t WHERE v LIKE ? ESCAPE '\\'", (_like_escape(lit),))]
        assert got == want, lit
    assert len(db.execute("SELECT v FROM t WHERE v LIKE 'a_b'").fetchall()) == 3  # unescaped: a_b, axb, a\b
