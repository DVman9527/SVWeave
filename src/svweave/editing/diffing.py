"""Candidate-vs-base comparison, bounded unified diff and the canonical patch record (plan 12.4).

``patch_hash`` = sha256(canonical patch record). The record contains the base snapshot id and, per
file, the logical path, before/after hashes and the unified diff text: any change to the bytes
changes the hash, which invalidates an approval (EDT-04).
"""

from __future__ import annotations

import difflib
import os
import re
import stat
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from svweave.adapters.serena.guard import ChangeSet, diff_hashes
from svweave.domain.ids import canonical_hash, sha256_hex

DIFF_DISPLAY_LIMIT = 64 * 1024


@dataclass
class FileChange:
    logical_path: str
    rel_path: str  # path inside snapshot files/ and the candidate
    before_sha256: str
    after_sha256: str
    added_lines: int
    removed_lines: int
    diff: str
    after_size: int
    after_bytes: bytes = field(repr=False, default=b"")


@dataclass
class Comparison:
    changes: ChangeSet
    #: Candidate entries that are not plain regular files (symlink, fifo, ...).
    irregular: list[str]
    hardlinked: list[str]
    binary: list[str]


_DIAG_CONTROL = re.compile(
    r"`\s*pragma\s+(diagnostic|protect)\b|\blint_?(off|on|save|restore)\b|\bpragma\s+protect\b",
    re.IGNORECASE,
)


def diagnostic_control_lines(diff: str) -> list[str]:
    """Added or removed diff lines that touch slang / lint diagnostic-control directives: a
    candidate must not hide its own diagnostics (F11)."""
    return [
        ln[:120].rstrip()
        for ln in diff.splitlines()
        if ln[:1] in "+-" and not ln.startswith(("+++", "---")) and _DIAG_CONTROL.search(ln)
    ]


_BIDI = frozenset(chr(c) for c in (*range(0x202A, 0x202F), *range(0x2066, 0x206A)))


def _unsafe_char(c: str) -> bool:
    o = ord(c)
    return (o < 0x20 and c not in "\t\n\r") or o == 0x7F or 0x80 <= o <= 0x9F or c in _BIDI


def has_unsafe_control(text: str) -> bool:
    """True if ``text`` holds a terminal-driving or display-reordering character: any C0 control
    except TAB / LF (CR only directly before LF, i.e. CRLF), DEL, C1 controls and the Unicode
    bidi embedding / override / isolate characters (A1: approval spoofing)."""
    for i, c in enumerate(text):
        if c == "\r":
            if text[i + 1 : i + 2] != "\n":
                return True
        elif _unsafe_char(c):
            return True
    return False


def visible(text: str) -> str:
    """``text`` with every non-printable character shown as a visible escape (\\x1b, \\r,
    \\u202e); only LF and TAB pass through, so content can never drive the terminal."""
    out: list[str] = []
    for c in text:
        if c in "\n\t" or (c.isprintable()):
            out.append(c)
        elif c == "\r":
            out.append("\\r")
        else:
            o = ord(c)
            out.append(f"\\x{o:02x}" if o < 0x100 else f"\\u{o:04x}" if o < 0x10000 else f"\\U{o:08x}")
    return "".join(out)


def is_text(data: bytes) -> bool:
    if b"\x00" in data:
        return False
    try:
        data.decode("utf-8")
    except UnicodeDecodeError:
        return False
    return True


def compare_trees(base: dict[str, str], cand: dict[str, str], cand_root: Path) -> Comparison:
    cs = diff_hashes(base, cand)
    irregular: list[str] = []
    hardlinked: list[str] = []
    binary: list[str] = []
    for rel, h in cand.items():
        p = cand_root / rel
        try:
            st = os.lstat(p)
        except OSError:
            irregular.append(rel)
            continue
        if h.startswith("symlink:") or not stat.S_ISREG(st.st_mode):
            irregular.append(rel)
            continue
        if rel in cs.modified or rel in cs.added:
            if st.st_nlink > 1:
                hardlinked.append(rel)
            if not is_text(p.read_bytes()):
                binary.append(rel)
    return Comparison(cs, irregular, hardlinked, binary)


def unified(logical: str, before: bytes, after: bytes) -> tuple[str, int, int]:
    a = before.decode("utf-8").splitlines(keepends=True)
    b = after.decode("utf-8").splitlines(keepends=True)
    lines = list(difflib.unified_diff(a, b, f"a/{logical}", f"b/{logical}", n=3))
    out: list[str] = []
    for ln in lines:
        out.append(ln if ln.endswith("\n") else ln + "\n\\ No newline at end of file\n")
    added = sum(1 for ln in lines if ln.startswith("+") and not ln.startswith("+++"))
    removed = sum(1 for ln in lines if ln.startswith("-") and not ln.startswith("---"))
    return "".join(out), added, removed


def build_file_change(
    logical: str, rel: str, base_file: Path, cand_file: Path
) -> FileChange:
    before = base_file.read_bytes()
    after = cand_file.read_bytes()
    diff, added, removed = unified(logical, before, after)
    return FileChange(
        logical_path=logical,
        rel_path=rel,
        before_sha256=sha256_hex(before),
        after_sha256=sha256_hex(after),
        added_lines=added,
        removed_lines=removed,
        diff=diff,
        after_size=len(after),
        after_bytes=after,
    )


def patch_record(base_snapshot_id: str, changes: list[FileChange]) -> dict[str, Any]:
    return {
        "kind": "svweave-patch-v1",
        "base_snapshot_id": base_snapshot_id,
        "files": [
            {
                "logical_path": c.logical_path,
                "before_sha256": c.before_sha256,
                "after_sha256": c.after_sha256,
                "diff": c.diff,
            }
            for c in sorted(changes, key=lambda c: c.logical_path)
        ],
    }


def patch_hash(record: dict[str, Any]) -> str:
    return canonical_hash(record)


def combined_diff(changes: list[FileChange]) -> str:
    return "".join(c.diff for c in sorted(changes, key=lambda c: c.logical_path))
