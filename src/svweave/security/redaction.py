"""Redaction of host paths and secrets in log lines and evidence (plan 23.1, test SEC-04).

Raw backend stderr stays in the backend's private state directory (mode 0600); anything that is
surfaced (errors, health details, evidence, doctor output) goes through :class:`Redactor`.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Any

from svweave.domain.errors import SvwError
from svweave.security import safeio

REDACTED = "<redacted>"
REDACTED_PATH = "<host-path-redacted>"

# KEY=value / KEY: value where KEY looks like a credential name.
_SECRET_NAME = r"[A-Za-z0-9_.-]*(?:KEY|TOKEN|SECRET|PASSWORD|PASSWD|PASSPHRASE|CREDENTIAL|AUTH|COOKIE)[A-Za-z0-9_.-]*"
_ENV_SECRET = re.compile(rf"(?i)\b({_SECRET_NAME})(\s*[=:]\s*)(\"[^\"]*\"|'[^']*'|\S+)")
_BEARER = re.compile(r"(?i)\b(bearer|basic)\s+[A-Za-z0-9._~+/=-]{8,}")
_KNOWN_TOKENS = re.compile(
    r"\b(?:gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,}|sk-[A-Za-z0-9_-]{20,}"
    r"|AKIA[0-9A-Z]{16}|xox[baprs]-[A-Za-z0-9-]{10,}|AIza[0-9A-Za-z_-]{30,})\b"
)
_PEM = re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?(?:-----END [A-Z ]*PRIVATE KEY-----|\Z)", re.S)
_URL_CRED = re.compile(r"(?i)\b([a-z][a-z0-9+.-]*://)[^/\s:@]+:[^/\s@]+@")
# Absolute POSIX path with at least two components, not glued to a preceding word/path char.
_ABS_PATH = re.compile(r"(?<![\w.~:/@+-])/(?:[\w.@+%=,-]+/)+[\w.@+%=,-]+/?")

#: System locations that carry no private information.
SYSTEM_PREFIXES: tuple[str, ...] = (
    "/usr/",
    "/bin/",
    "/sbin/",
    "/lib/",
    "/lib64/",
    "/proc/",
    "/sys/",
    "/dev/",
)

_SECRET_KEY_NAME = re.compile(r"(?i)(key|token|secret|password|passwd|passphrase|credential|cookie|auth)")


class Redactor:
    """Redacts secrets everywhere and absolute host paths that are not under an allowed root."""

    def __init__(self, allowed_roots: Iterable[Path | str] = ()) -> None:
        roots: list[str] = []
        for r in allowed_roots:
            s = str(r).rstrip("/")
            if s:
                roots.append(s)
        self._roots = tuple(sorted(set(roots), key=len, reverse=True))

    @property
    def allowed_roots(self) -> tuple[str, ...]:
        return self._roots

    def _path_allowed(self, p: str) -> bool:
        if p.startswith(SYSTEM_PREFIXES):
            return True
        return any(p == r or p.startswith(r + "/") for r in self._roots)

    def _sub_path(self, m: re.Match[str]) -> str:
        p = m.group(0)
        return p if self._path_allowed(p.rstrip("/")) else REDACTED_PATH

    def text(self, s: str) -> str:
        s = _PEM.sub(REDACTED, s)
        s = _URL_CRED.sub(lambda m: m.group(1) + REDACTED + "@", s)
        s = _BEARER.sub(lambda m: f"{m.group(1)} {REDACTED}", s)
        s = _ENV_SECRET.sub(lambda m: f"{m.group(1)}{m.group(2)}{REDACTED}", s)
        s = _KNOWN_TOKENS.sub(REDACTED, s)
        return _ABS_PATH.sub(self._sub_path, s)

    def value(self, obj: Any) -> Any:
        """Recursively redact strings in JSON-like data; secret-named keys lose their value."""
        if isinstance(obj, str):
            return self.text(obj)
        if isinstance(obj, Mapping):
            out: dict[Any, Any] = {}
            for k, v in obj.items():
                if isinstance(k, str) and _SECRET_KEY_NAME.search(k) and isinstance(v, str | bytes):
                    out[k] = REDACTED
                else:
                    out[k] = self.value(v)
            return out
        if isinstance(obj, list | tuple):
            return [self.value(v) for v in obj]
        return obj

    def tail_file(self, path: Path, max_bytes: int = 4096) -> str:
        """Redacted last ``max_bytes`` of a (raw) log; empty if missing. Never follows a link."""
        try:
            raw = safeio.read_regular(path, max_bytes, tail=True)
        except (OSError, SvwError):
            return ""
        return self.text(raw.decode("utf-8", errors="replace"))

    def redact_file(self, src: Path, dst: Path) -> None:
        """Write a redacted copy of ``src`` to ``dst`` (mode 0600), refusing links at both ends."""
        raw = safeio.read_regular(src)
        out = self.text(raw.decode("utf-8", errors="replace"))
        safeio.write_atomic(dst, out.encode("utf-8"), 0o600)
