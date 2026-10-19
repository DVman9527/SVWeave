"""Candidate directory (plan 12.4): a real byte copy of the base snapshot.

No hardlink and no symlink is ever created: the candidate never shares a writable inode with the
frozen snapshot or with the original source (ADR-007).
"""

from __future__ import annotations

import os
import shutil
import stat
from pathlib import Path

from svweave.adapters.serena.config import GENERATED_FILES
from svweave.adapters.serena.guard import tree_hashes
from svweave.config.context import WorkspaceContext
from svweave.domain.errors import ErrorCode, SvwError
from svweave.domain.ids import canonical_hash

CANDIDATES_DIR = "candidates"
_CHUNK = 1 << 20


def candidates_root(ctx: WorkspaceContext) -> Path:
    return ctx.state_dir / CANDIDATES_DIR


def candidate_dir(ctx: WorkspaceContext, txn_id: str) -> Path:
    return candidates_root(ctx) / txn_id


def create_candidate(src_files: Path, dest: Path) -> None:
    """Copy ``src_files`` (the snapshot ``files/`` directory) into ``dest`` byte by byte."""
    if dest.exists() or dest.is_symlink():
        raise SvwError(
            ErrorCode.POLICY_DENIED,
            "candidate directory already exists",
            {"reason": "candidate_exists"},
        )
    dest.parent.mkdir(parents=True, exist_ok=True)
    os.chmod(dest.parent, 0o700)
    dest.mkdir(mode=0o700)
    try:
        for dirpath, dirnames, filenames in os.walk(src_files, followlinks=False):
            dirnames.sort()
            rel_dir = Path(dirpath).relative_to(src_files)
            for dn in dirnames:
                if os.path.islink(Path(dirpath) / dn):
                    raise SvwError(ErrorCode.POLICY_DENIED, "symlink inside a snapshot refused")
                (dest / rel_dir / dn).mkdir(mode=0o755, exist_ok=True)
            for fn in sorted(filenames):
                s = Path(dirpath) / fn
                if not stat.S_ISREG(os.lstat(s).st_mode):
                    raise SvwError(
                        ErrorCode.POLICY_DENIED,
                        "non-regular file inside a snapshot refused",
                        {"path": (rel_dir / fn).as_posix()},
                    )
                d = dest / rel_dir / fn
                with open(s, "rb") as fin, open(d, "xb") as fout:
                    while chunk := fin.read(_CHUNK):
                        fout.write(chunk)
                os.chmod(d, 0o644)
    except BaseException:
        remove_candidate(dest)
        raise


def remove_candidate(path: Path) -> None:
    """Delete a candidate tree (never follows links; chmod'ing is not needed, we created it)."""
    if not path.exists() and not path.is_symlink():
        return
    if path.is_symlink():
        path.unlink()
        return
    shutil.rmtree(path, ignore_errors=False)


def tree_state(root: Path, base_names: frozenset[str]) -> dict[str, str]:
    """relative path -> sha256 (or ``symlink:<target>``). Adapter-generated files are excluded,
    but never a file that also exists in the base (that would hide a real source edit)."""
    out = tree_hashes(root)
    for g in GENERATED_FILES:
        if g in out and g not in base_names:
            del out[g]
    return out


def tree_hash(state: dict[str, str]) -> str:
    return canonical_hash({"kind": "candidate-tree", "files": sorted(state.items())})
