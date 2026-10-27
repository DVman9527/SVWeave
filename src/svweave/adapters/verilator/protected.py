"""Bounded lexical preparation, NOT an HDL parser or compiler.

Plan 2.1/A01, 4.3, 9.5: opaque envelopes become whitespace, never guessed stubs.
Only explicit standalone IEEE begin_protected/end_protected and legacy
`protected/`endprotected delimiters are supported. No macro/include evaluation.
"""

from __future__ import annotations

import hashlib
import io
import re
from dataclasses import dataclass
from typing import Any

from svweave.domain.errors import ErrorCode as EC
from svweave.domain.errors import SvwError

SOURCE_LIMIT = 16 << 20
GAP_LIMIT = 100
_TOKEN = re.compile(
    rb"`(?:pragma[ \t]+protect\b|protected\b|endprotected\b|protect\b|endprotect\b)"
)
_BOUNDARY = re.compile(
    rb"[ \t]*`(?:(?:pragma[ \t]+protect[ \t]+(begin_protected|end_protected))"
    rb"|(protected|endprotected))[ \t]*"
)


@dataclass
class _Lex:
    state: str = "code"

    def visible(self, line: bytes) -> bytes:
        """Hide comments, strings and escaped identifiers without moving byte offsets."""
        out = bytearray(line)
        i = 0
        while i < len(line):
            if self.state == "comment":
                if line[i : i + 2] == b"*/":
                    out[i : i + 2] = b"  "
                    i += 2
                    self.state = "code"
                    continue
                out[i] = 32
            elif self.state in {"string", "triple"}:
                close = b'"""' if self.state == "triple" else b'"'
                if line[i : i + len(close)] == close:
                    out[i : i + len(close)] = b"x" * len(close)
                    i += len(close)
                    self.state = "code"
                    continue
                if line[i : i + 1] == b"\\" and i + 1 < len(line):
                    out[i : i + 2] = b"xx"
                    i += 2
                    continue
                out[i] = 120
            elif self.state == "escaped":
                if line[i : i + 1].isspace():
                    self.state = "code"
                    continue
                out[i] = 120
            elif line[i : i + 2] == b"//":
                out[i:] = b" " * (len(line) - i)
                break
            elif line[i : i + 2] == b"/*":
                self.state = "comment"
                out[i : i + 2] = b"  "
                i += 2
                continue
            elif line[i : i + 1] == b'"':
                self.state = "triple" if line[i : i + 3] == b'"""' else "string"
                size = 3 if self.state == "triple" else 1
                out[i : i + size] = b"x" * size
                i += size
                continue
            elif line[i : i + 1] == b"\\":
                self.state = "escaped"
                out[i] = 120
            i += 1
        return bytes(out)


def prepare_protected_source(source: bytes) -> tuple[bytes, dict[str, Any]]:
    """Return a derived source and explicit gaps; raise before producing any malformed result."""
    if len(source) > SOURCE_LIMIT:
        raise SvwError(EC.RESOURCE_LIMIT, "source preparation exceeds 16 MiB limit")
    if b"\x00" in source:
        raise SvwError(EC.INVALID_CONFIG, "NUL/binary source is unsupported")
    lexer = _Lex()
    result = bytearray()
    gaps: list[dict[str, Any]] = []
    active: tuple[str, int, int] | None = None
    offset = 0
    continued = False
    # Split only on LF; preserve CRLF and final unterminated lines byte-for-byte.
    if b"\r" in source.replace(b"\r\n", b""):
        raise SvwError(EC.INVALID_CONFIG, "bare CR line endings are unsupported")
    for number, line in enumerate(io.BytesIO(source), 1):
        body = line.rstrip(b"\r\n")
        # Protected payload is opaque, not SV: quotes or slash characters in ciphertext
        # cannot change the lexical state of subsequent plaintext. Boundary lines alone
        # are lexed independently. No payload is returned in report/error messages.
        start_state = lexer.state
        boundary_lexer = _Lex() if active else lexer
        visible = boundary_lexer.visible(body + b"\n")[:-1]
        match = _BOUNDARY.fullmatch(visible)
        token = _TOKEN.search(visible)
        marker = (match.group(1) or match.group(2)).decode() if match else None
        if (
            token
            and not match
            and (
                not active
                or not re.match(rb"[ \t]*`pragma[ \t]+protect[ \t]+", visible)
                or re.search(rb"\b(?:begin_protected|end_protected|begin|end)\b", visible)
                or len(_TOKEN.findall(visible)) != 1
            )
        ):
            raise SvwError(EC.INVALID_CONFIG, f"unsupported protection directive at line {number}")
        if marker:
            if (
                continued
                or boundary_lexer.state != "code"
                or (not active and start_state != "code")
            ):
                raise SvwError(EC.INVALID_CONFIG, f"ambiguous protection boundary at line {number}")
            if marker in {"begin_protected", "protected"}:
                if active:
                    raise SvwError(
                        EC.INVALID_CONFIG, f"nested protection boundary at line {number}"
                    )
                if len(gaps) >= GAP_LIMIT:
                    raise SvwError(EC.RESOURCE_LIMIT, "too many protected regions")
                active = (marker, number, offset)
            else:
                expected = (
                    "end_protected" if active and active[0] == "begin_protected" else "endprotected"
                )
                if not active or marker != expected:
                    raise SvwError(
                        EC.INVALID_CONFIG, f"unmatched protection boundary at line {number}"
                    )
                gaps.append(
                    {
                        "kind": "protected_source_skipped",
                        "start_line": active[1],
                        "end_line": number,
                        "start_byte": active[2],
                        "end_byte": offset + len(line),
                        "definitions": "UNKNOWN",
                        "interfaces": "UNKNOWN",
                        "ports": "UNKNOWN",
                    }
                )
        skipping = active is not None
        result.extend(bytes(c if c in (10, 13) else 32 for c in line) if skipping else line)
        if marker in {"end_protected", "endprotected"}:
            active = None
        continued = body.endswith(b"\\") if not skipping else False
        offset += len(line)
    if active:
        raise SvwError(EC.INVALID_CONFIG, f"unterminated protection boundary at line {active[1]}")
    prepared = bytes(result)
    return prepared, {
        "schema_version": "svw-protected-source-1",
        "completeness": "PARTIAL",
        "source_sha256": hashlib.sha256(source).hexdigest(),
        "prepared_sha256": hashlib.sha256(prepared).hexdigest(),
        "byte_count": len(source),
        "line_mapping": "identity_byte_offsets_and_line_endings",
        "gaps": gaps,
        "skipped_region_count": len(gaps),
        "missing_definitions": "UNKNOWN" if gaps else "NOT_CHECKED",
        "encrypted_interfaces": "UNKNOWN" if gaps else "NOT_CHECKED",
        "static_compile": "NOT_RUN",
        "simulation": "NOT_RUN",
        "formal": "NOT_RUN",
        "coverage": "NOT_RUN",
        "source_binding": "UNVERIFIED",
        "limitations": [
            "Lexical preparation only, not successful HDL compilation or elaboration.",
            "Protected definitions, interfaces and ports are unknown; no blackbox stubs invented.",
            "Missing definitions and unresolved references must remain compiler errors downstream.",
            "Includes, macros and conditionals are not evaluated; prepare each input explicitly.",
            "Derived path changes relative include lookup; caller must preserve original include context.",
            "No decryption, backend execution, simulation, formal or coverage validation.",
            "PARTIAL: absence is not proof of absence; no authoritative snapshot/build association.",
        ],
    }
