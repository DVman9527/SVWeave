"""Opaque pagination cursors bound to build, query, sort order and offset (plan 14.2, API-02).

A cursor is ``base64url(json) . hmac``. It carries the build id, a hash of the query (tool +
normalised parameters, without limit/cursor), the sort name and the offset. Decoding with a
different build id, query hash or sort, a tampered token or a wrong key is rejected, so a cursor
can never be replayed against another build or another query.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
from typing import Any

from svweave.domain.errors import ErrorCode, SvwError
from svweave.domain.ids import canonical_hash

MAX_LIMIT = 500
DEFAULT_LIMIT = 100
MAX_RESPONSE_BYTES = 128 * 1024
_TAG_LEN = 16


def query_hash(tool: str, params: dict[str, Any]) -> str:
    return canonical_hash({"tool": tool, "params": params})


def check_limit(limit: int | None) -> int:
    if limit is None:
        return DEFAULT_LIMIT
    if not isinstance(limit, int) or isinstance(limit, bool) or limit < 1 or limit > MAX_LIMIT:
        raise SvwError(
            ErrorCode.INVALID_CONFIG,
            f"limit must be an integer in 1..{MAX_LIMIT}",
            {"field": "limit", "value": limit, "max": MAX_LIMIT},
        )
    return limit


def _b64(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).decode("ascii").rstrip("=")


def _unb64(s: str) -> bytes:
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


def encode_cursor(key: bytes, build_id: str, qhash: str, sort: str, offset: int) -> str:
    body = json.dumps(
        {"b": build_id, "q": qhash, "s": sort, "o": int(offset)},
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    tag = hmac.new(key, body, hashlib.sha256).digest()[:_TAG_LEN]
    return f"{_b64(body)}.{_b64(tag)}"


def _bad(reason: str) -> SvwError:
    return SvwError(
        ErrorCode.INVALID_CONFIG, f"invalid cursor: {reason}", {"field": "cursor", "reason": reason}
    )


def decode_cursor(key: bytes, token: str, build_id: str, qhash: str, sort: str) -> int:
    """Return the offset, or raise INVALID_CONFIG for any mismatch (never guesses)."""
    try:
        body_s, tag_s = token.split(".", 1)
        body, tag = _unb64(body_s), _unb64(tag_s)
    except (ValueError, TypeError) as e:
        raise _bad("malformed") from e
    want = hmac.new(key, body, hashlib.sha256).digest()[:_TAG_LEN]
    if not hmac.compare_digest(tag, want):
        raise _bad("signature mismatch")
    try:
        d = json.loads(body)
        offset = int(d["o"])
    except (ValueError, KeyError, TypeError) as e:
        raise _bad("malformed payload") from e
    if d.get("b") != build_id:
        raise _bad("cursor belongs to a different build")
    if d.get("q") != qhash:
        raise _bad("cursor belongs to a different query")
    if d.get("s") != sort:
        raise _bad("cursor was issued for a different sort order")
    if offset < 0:
        raise _bad("negative offset")
    return offset
