"""Lexer for dialect `svweave-filelist-v1` (rules documented in docs/manifest.md).

Pure text processing: no eval, no shell, no subprocess.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from svweave.domain.errors import ErrorCode, SvwError

_ENV_RE = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}|\$([A-Za-z_][A-Za-z0-9_]*)")
_BAD_BRACE_RE = re.compile(r"\$\{(?![A-Za-z_][A-Za-z0-9_]*\})")
_SHELL_BARE = set(";|<>&")


@dataclass(frozen=True)
class Token:
    raw: str  # verbatim text including quotes and backslashes
    line: int
    col: int


def tokenize(text: str, file: str) -> list[Token]:
    """Split a filelist into tokens.

    - whitespace separates tokens, except inside double quotes
    - `//` or `#` at the START of a token begins a comment to end of line
    - a token starting with `/*` is rejected (block comments are not supported)
    - inside double quotes `\\"` and `\\\\` are escapes; any other backslash is literal
    - quotes may not span lines; there is no line continuation
    """
    if text.startswith("﻿"):
        text = text[1:]
    tokens: list[Token] = []
    for lineno, line in enumerate(text.split("\n"), 1):
        line = line.rstrip("\r")
        i, n = 0, len(line)
        while i < n:
            c = line[i]
            if c.isspace():
                i += 1
                continue
            if line.startswith("//", i) or c == "#":
                break
            start = i
            in_q = False
            while i < n:
                c = line[i]
                if in_q:
                    if c == "\\" and i + 1 < n and line[i + 1] in '"\\':
                        i += 2
                        continue
                    if c == '"':
                        in_q = False
                elif c == '"':
                    in_q = True
                elif c.isspace():
                    break
                i += 1
            if in_q:
                raise SvwError(
                    ErrorCode.INVALID_CONFIG,
                    f"{file}:{lineno}:{start + 1}: unterminated double quote",
                    {"file": file, "line": lineno, "column": start + 1},
                )
            raw = line[start:i]
            if raw.startswith("/*"):
                raise SvwError(
                    ErrorCode.UNSUPPORTED_OPTION,
                    f"{file}:{lineno}:{start + 1}: block comments are not supported",
                    {"file": file, "line": lineno, "column": start + 1, "option": raw},
                )
            tokens.append(Token(raw, lineno, start + 1))
    return tokens


def unquote(raw: str) -> str:
    """Remove quote characters (and honour `\\"`/`\\\\` inside quotes)."""
    out: list[str] = []
    in_q = False
    i, n = 0, len(raw)
    while i < n:
        c = raw[i]
        if in_q:
            if c == "\\" and i + 1 < n and raw[i + 1] in '"\\':
                out.append(raw[i + 1])
                i += 2
                continue
            if c == '"':
                in_q = False
            else:
                out.append(c)
        elif c == '"':
            in_q = True
        else:
            out.append(c)
        i += 1
    return "".join(out)


def split_unquoted(raw: str, sep: str) -> list[str]:
    """Split on `sep` characters that are outside double quotes (raw pieces keep quotes)."""
    parts: list[str] = []
    cur: list[str] = []
    in_q = False
    i, n = 0, len(raw)
    while i < n:
        c = raw[i]
        if in_q:
            if c == "\\" and i + 1 < n and raw[i + 1] in '"\\':
                cur.append(raw[i : i + 2])
                i += 2
                continue
            if c == '"':
                in_q = False
        elif c == '"':
            in_q = True
        elif c == sep:
            parts.append("".join(cur))
            cur = []
            i += 1
            continue
        cur.append(c)
        i += 1
    parts.append("".join(cur))
    return parts


def _bare_text(raw: str) -> str:
    out: list[str] = []
    in_q = False
    i, n = 0, len(raw)
    while i < n:
        c = raw[i]
        if in_q:
            if c == "\\" and i + 1 < n:
                i += 2
                continue
            if c == '"':
                in_q = False
        elif c == '"':
            in_q = True
        else:
            out.append(c)
        i += 1
    return "".join(out)


def shell_syntax_reason(raw: str) -> str | None:
    """Return a reason if the token carries shell/Make/Tcl substitution syntax.

    Backticks, `$(`, and malformed `${` are rejected anywhere (a shell would expand them even
    inside double quotes). `; | < > &` are rejected only when unquoted. Nothing is executed.
    """
    if "`" in raw:
        return "backtick command substitution"
    if "$(" in raw:
        return "$(...) command substitution"
    if _BAD_BRACE_RE.search(raw):
        return "malformed ${...} expansion"
    if any(ch in _SHELL_BARE for ch in _bare_text(raw)):
        return "unquoted shell metacharacter"
    return None


def env_names(text: str) -> list[str]:
    return [m.group(1) or m.group(2) for m in _ENV_RE.finditer(text)]


def expand_env(text: str, values: dict[str, str]) -> str:
    """Substitute `$VAR`/`${VAR}` from an already-validated mapping (caller checks presence)."""
    return _ENV_RE.sub(lambda m: values[m.group(1) or m.group(2)], text)
