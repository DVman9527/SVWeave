"""Path policy primitives: glob matching and normalize + realpath + root allowlist (plan 23.1)."""

from __future__ import annotations

import os
import re
import stat
from functools import lru_cache
from pathlib import Path

from svweave.domain.errors import ErrorCode, SvwError


@lru_cache(maxsize=256)
def _glob_regex(pattern: str) -> re.Pattern[str]:
    out = []
    i = 0
    while i < len(pattern):
        if pattern.startswith("**/", i):
            out.append("(?:.*/)?")
            i += 3
        elif pattern.startswith("**", i):
            out.append(".*")
            i += 2
        elif pattern[i] == "*":
            out.append("[^/]*")
            i += 1
        elif pattern[i] == "?":
            out.append("[^/]")
            i += 1
        else:
            out.append(re.escape(pattern[i]))
            i += 1
    return re.compile("".join(out) + r"\Z", re.DOTALL)


def glob_match(pattern: str, rel_posix: str) -> bool:
    """Gitignore-flavoured glob: `*` stays inside a path segment, `**` crosses segments."""
    return bool(_glob_regex(pattern).match(rel_posix))


def is_within(child: Path, parent: Path) -> bool:
    return child == parent or parent in child.parents


def lexical_join(base: Path, raw: str) -> Path:
    """Absolute, lexically normalised (collapses `..`) path; does not touch the filesystem."""
    p = Path(raw)
    if not p.is_absolute():
        p = base / p
    return Path(os.path.normpath(p))


def is_hardlinked(path: Path) -> bool:
    """True for a regular file with more than one hard link (symlinks are followed).

    A hardlinked file can be modified through its other name outside the approved roots, so
    editing code (M4) must refuse it and snapshots flag it (`hardlinked`).
    """
    try:
        st = os.stat(path)
    except OSError:
        return False
    return stat.S_ISREG(st.st_mode) and st.st_nlink > 1


class PathPolicy:
    """Allowlist of realpath'd roots. A path passes only if BOTH its lexical normal form and
    its fully symlink-resolved form (including the un-normalised spelling) stay inside."""

    def __init__(self, roots: list[Path]) -> None:
        self.roots = [Path(os.path.realpath(r)) for r in roots]

    def contains_real(self, real: Path) -> bool:
        return any(is_within(real, r) for r in self.roots)

    def check(self, raw: str, base: Path) -> tuple[Path, Path]:
        """Return (lexical_abs, realpath) or raise PATH_NOT_ALLOWED. Never reads file content."""
        lexical = lexical_join(base, raw)
        unnormalised = Path(raw) if Path(raw).is_absolute() else base / raw
        real_lex = Path(os.path.realpath(lexical))
        real_raw = Path(os.path.realpath(unnormalised))
        if not self.contains_real(lexical):
            raise self.deny(raw, "outside allowed roots")
        if not self.contains_real(real_lex) or not self.contains_real(real_raw):
            raise self.deny(raw, "resolves (via symlink or '..') outside allowed roots")
        return lexical, real_lex

    @staticmethod
    def deny(raw: str, reason: str, hint: str | None = None) -> SvwError:
        details = {"path": raw, "reason": reason}
        if hint is not None:
            details["hint"] = hint
        return SvwError(
            ErrorCode.PATH_NOT_ALLOWED,
            f"path not allowed: {raw!r} ({reason})",
            details,
        )
