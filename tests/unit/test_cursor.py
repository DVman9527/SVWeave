from __future__ import annotations

import pytest

from svweave.domain.cursor import (
    DEFAULT_LIMIT,
    MAX_LIMIT,
    check_limit,
    decode_cursor,
    encode_cursor,
    query_hash,
)
from svweave.domain.errors import ErrorCode, SvwError
from svweave.domain.ids import sha256_hex

KEY = b"k" * 32
B1, B2 = sha256_hex("b1"), sha256_hex("b2")
Q1 = query_hash("references", {"symbol_id": "x"})
Q2 = query_hash("references", {"symbol_id": "y"})


def test_roundtrip_and_determinism() -> None:
    c = encode_cursor(KEY, B1, Q1, "file,span", 300)
    assert decode_cursor(KEY, c, B1, Q1, "file,span") == 300
    assert c == encode_cursor(KEY, B1, Q1, "file,span", 300)
    assert "=" not in c and " " not in c


@pytest.mark.parametrize(
    ("build", "q", "sort", "msg"),
    [(B2, Q1, "s", "different build"), (B1, Q2, "s", "different query"), (B1, Q1, "t", "sort")],
)
def test_cursor_cannot_be_replayed_elsewhere(build: str, q: str, sort: str, msg: str) -> None:
    c = encode_cursor(KEY, B1, Q1, "s", 5)
    with pytest.raises(SvwError) as ei:
        decode_cursor(KEY, c, build, q, sort)
    assert ei.value.code == ErrorCode.INVALID_CONFIG and msg in ei.value.message


def test_tamper_wrong_key_and_malformed() -> None:
    c = encode_cursor(KEY, B1, Q1, "s", 5)
    body, tag = c.split(".")
    for bad in (
        body + "." + tag[:-1] + ("A" if tag[-1] != "A" else "B"),
        "x",
        "a.b",
        body,
        ".",
        "é.é",
    ):
        with pytest.raises(SvwError):
            decode_cursor(KEY, bad, B1, Q1, "s")
    with pytest.raises(SvwError):
        decode_cursor(b"o" * 32, c, B1, Q1, "s")
    forged_offset = encode_cursor(KEY, B1, Q1, "s", 0).split(".")[0] + "." + tag
    with pytest.raises(SvwError):
        decode_cursor(KEY, forged_offset, B1, Q1, "s")


def test_query_hash_depends_on_tool_and_params_not_on_key_order() -> None:
    assert query_hash("a", {"x": 1, "y": 2}) == query_hash("a", {"y": 2, "x": 1})
    assert query_hash("a", {"x": 1}) != query_hash("b", {"x": 1})
    assert query_hash("a", {"x": None}) != query_hash("a", {})


def test_limit_rules() -> None:
    assert check_limit(None) == DEFAULT_LIMIT == 100
    assert check_limit(1) == 1 and check_limit(MAX_LIMIT) == 500
    for bad in (0, -3, 501, True, "5", 2.5):
        with pytest.raises(SvwError) as ei:
            check_limit(bad)  # type: ignore[arg-type]
        assert ei.value.code == ErrorCode.INVALID_CONFIG
