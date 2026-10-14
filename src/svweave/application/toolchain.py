"""toolchain_id from toolchain.lock.json component hashes + extractor identity (plan 9.1).

The lock is located by ONE discovery path (``orchestration.toolchain.locate_root``): an explicit
root, ``SVWEAVE_TOOLCHAIN_ROOT``, or the installed tool; never the cwd or the analysed repository.
The identity also covers the extractor's own source (``adapters/slang/*.py``) and the verified
content of the installed pyslang (dist-info RECORD hashes, RECORD pinned in the lock).
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
from importlib import metadata
from pathlib import Path
from typing import Any

import svweave.adapters.slang as _slang_pkg
from svweave.adapters.slang.protocol import DTO_SCHEMA, EXTRACTOR_VERSION
from svweave.domain.errors import ErrorCode, SvwError
from svweave.domain.ids import SEMANTIC_SCHEMA_VERSION, compute_toolchain_id
from svweave.orchestration.toolchain import ENV_ROOT, LOCK_NAME, installed_root, locate_root

__all__ = [
    "EXTRACTOR_VERSION",
    "LOCK_NAME",
    "compute_toolchain",
    "extractor_source_hash",
    "find_lock",
    "toolchain_parts",
    "verify_pyslang_install",
]


def find_lock(project_root: Path | None = None) -> Path:
    """The lock of the installed tool or of the explicitly configured toolchain root. The
    ``project_root`` argument is accepted for call-site compatibility and IGNORED: a repository
    under analysis can never supply or override the qualified toolchain."""
    root = locate_root()
    if root is None:
        raise SvwError(
            ErrorCode.CAPABILITY_MISMATCH,
            f"{LOCK_NAME} not found; the toolchain identity cannot be computed "
            f"(set {ENV_ROOT} or use an installed tool)",
            {"searched": os.environ.get(ENV_ROOT) or str(installed_root())},
        )
    return root / LOCK_NAME


# ------------------------------------------------------------------ extractor source identity
_SRC_CACHE: dict[tuple[tuple[str, int, int], ...], str] = {}


def extractor_source_hash() -> str:
    """sha256 over the extractor package sources (name + bytes), cached by stat fingerprint, so an
    extractor change without an ``EXTRACTOR_VERSION`` bump cannot reuse an old COMPLETE build."""
    base = Path(_slang_pkg.__file__).resolve().parent
    files = sorted(base.glob("*.py"))
    fp = tuple((p.name, p.stat().st_size, p.stat().st_mtime_ns) for p in files)
    hit = _SRC_CACHE.get(fp)
    if hit is not None:
        return hit
    h = hashlib.sha256()
    for p in files:
        h.update(p.name.encode() + b"\0" + p.read_bytes() + b"\0")
    _SRC_CACHE.clear()
    _SRC_CACHE[fp] = h.hexdigest()
    return _SRC_CACHE[fp]


# ------------------------------------------------------------------ installed pyslang content
_PYSLANG_CACHE: dict[tuple[Any, ...], str] = {}


def _mismatch(msg: str, **details: Any) -> SvwError:
    return SvwError(ErrorCode.CAPABILITY_MISMATCH, msg, details)


def verify_pyslang_install(locked_record_sha256: str | None) -> str:
    """Check the installed pyslang files against its dist-info RECORD hashes (and for unlisted
    files in its package directories) and that the RECORD equals the lock's pin. Returns the RECORD
    sha256. The result is cached by a stat fingerprint of every file. Raises CAPABILITY_MISMATCH."""
    try:
        dist = metadata.distribution("pyslang")
    except metadata.PackageNotFoundError as e:
        raise _mismatch("pyslang is not installed") from e
    files = list(dist.files or [])
    rec = next((f for f in files if f.name == "RECORD" and f.parent.name.endswith(".dist-info")), None)
    if rec is None:
        raise _mismatch("installed pyslang has no dist-info RECORD; its content cannot be verified")
    rec_path = Path(str(dist.locate_file(rec)))
    entries = [(f, Path(str(dist.locate_file(f)))) for f in files if f.hash is not None]
    try:
        fp: tuple[Any, ...] = (
            locked_record_sha256,
            *((str(p), p.stat().st_size, p.stat().st_mtime_ns) for _, p in entries),
            (str(rec_path), rec_path.stat().st_size, rec_path.stat().st_mtime_ns),
        )
    except OSError as e:
        raise _mismatch(f"installed pyslang file missing: {e}") from e
    hit = _PYSLANG_CACHE.get(fp)
    if hit is not None:
        return hit
    record_sha = hashlib.sha256(rec_path.read_bytes()).hexdigest()
    if not locked_record_sha256:
        raise _mismatch("toolchain.lock.json pins no pyslang record_sha256", record_sha256=record_sha)
    if record_sha != locked_record_sha256:
        raise _mismatch(
            "installed pyslang RECORD differs from the lock",
            installed=record_sha,
            locked=locked_record_sha256,
        )
    listed: set[Path] = set()
    for f, p in entries:
        listed.add(p)
        assert f.hash is not None
        if f.hash.mode != "sha256":
            raise _mismatch(f"RECORD hash mode {f.hash.mode!r} is not sha256", file=str(f))
        got = base64.urlsafe_b64encode(hashlib.sha256(p.read_bytes()).digest()).rstrip(b"=").decode()
        if got != f.hash.value:
            raise _mismatch("installed pyslang file differs from its RECORD hash", file=str(f))
    pkg_dirs = {Path(str(dist.locate_file(f.parts[0]))) for f in files if len(f.parts) > 1 and not f.parts[0].endswith(".dist-info")}
    for d in pkg_dirs:
        for q in d.rglob("*"):
            if q.is_file() and "__pycache__" not in q.parts and q.suffix != ".pyc" and q not in listed:
                raise _mismatch("unlisted file inside the installed pyslang package", file=str(q))
    _PYSLANG_CACHE.clear()
    _PYSLANG_CACHE[fp] = record_sha
    return record_sha


def toolchain_parts(lock_path: Path) -> dict[str, Any]:
    lock = json.loads(lock_path.read_text(encoding="utf-8"))
    comps = lock["components"]
    py = comps["pyslang"]
    sl = comps["slang"]
    installed = _installed_pyslang()
    if installed != py["version"]:
        raise SvwError(
            ErrorCode.CAPABILITY_MISMATCH,
            f"installed pyslang {installed!r} differs from the lock ({py['version']!r}); "
            "refusing to build with an unqualified backend",
            {"installed": installed, "locked": py["version"]},
        )
    record_sha = verify_pyslang_install(py.get("record_sha256"))
    return {
        "pyslang": {
            "version": py["version"],
            "wheel_sha256": py["wheel_sha256"],
            "record_sha256": record_sha,
        },
        "slang": {"commit": sl["commit"], "binary_sha256": sl["binary_sha256"]},
        "extractor": EXTRACTOR_VERSION,
        "extractor_source_sha256": extractor_source_hash(),
        "dto_schema": DTO_SCHEMA,
        "semantic_schema_version": SEMANTIC_SCHEMA_VERSION,
    }


def _installed_pyslang() -> str | None:
    try:
        return metadata.version("pyslang")
    except metadata.PackageNotFoundError:
        return None


def compute_toolchain(project_root: Path | None = None) -> tuple[str, dict[str, Any], Path]:
    lock = find_lock(project_root)
    parts = toolchain_parts(lock)
    return compute_toolchain_id(parts), parts, lock
