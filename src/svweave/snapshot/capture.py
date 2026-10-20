"""Conservative immutable snapshot capture (plan 9.2) and freshness (plan 9.3, 9.6).

Snapshots copy bytes (no hardlinks) into `.svweave/snapshots/<snapshot_id>/files/` and then make
files read-only. Git state is never consulted: untracked and uncommitted files are captured
exactly as they are on disk.
"""

from __future__ import annotations

import hashlib
import os
import posixpath
import re
import shutil
import stat
import uuid
from collections.abc import Callable, Iterator
from datetime import UTC, datetime
from pathlib import Path

from svweave.config.context import WorkspaceContext, snapshot_rel
from svweave.config.paths import is_hardlinked
from svweave.domain.errors import ErrorCode, SvwError
from svweave.domain.ids import compute_snapshot_id
from svweave.manifest.models import NormalizedManifest
from svweave.snapshot.models import (
    Change,
    ExplicitEntry,
    FreshnessResult,
    LookupInventory,
    LookupSpec,
    SnapFile,
    SnapshotRecord,
    SnapshotSpec,
)
from svweave.storage.lock import workspace_lock

MAX_FILES = 20_000
MAX_DIRS = 20_000
MAX_WALK_DEPTH = 200
MAX_INCLUDE_SCAN_FILES = 5_000
MAX_INCLUDE_SCAN_BYTES = 4 << 20
MAX_TOTAL_BYTES = 1 << 30
DEFAULT_RETRIES = 3
_CHUNK = 1 << 20

Hook = Callable[[str, int], None]


_INCLUDE_RE = re.compile(r'`include\s+"([^"\n]+)"')
_HDL_SUFFIXES = frozenset({".sv", ".svh", ".v", ".vh", ".h", ".vp", ".svp"})


def _nested_include_dirs(ctx: WorkspaceContext, seeds: list[str]) -> list[str]:
    """Logical directories reachable through relative `include "dir/file"` directives, found by
    scanning the HDL files of the seed files' directories and, transitively, of the directories
    found. Only existing directories that pass the root/trust/exclusion rules are returned; an
    include slang cannot resolve is left to the build's dependency check (BLOCKED / diagnostics).
    The scan is bounded (files, bytes) and deterministic (sorted)."""
    start = {posixpath.dirname(x) for x in seeds}
    seen: set[str] = set(start)
    found: list[str] = []
    work = sorted(start)
    scanned = 0
    while work:
        ldir = work.pop(0)
        base = ctx.to_abs(ldir)
        try:
            names = sorted(os.listdir(base))
        except OSError:
            continue
        for name in names:
            if Path(name).suffix not in _HDL_SUFFIXES:
                continue
            fp = base / name
            if scanned >= MAX_INCLUDE_SCAN_FILES or not fp.is_file():
                continue
            try:
                if fp.stat().st_size > MAX_INCLUDE_SCAN_BYTES:
                    continue
                text = fp.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            scanned += 1
            for inc in _INCLUDE_RE.findall(text):
                if inc.startswith("/") or "/" not in inc:
                    continue
                tgt = posixpath.normpath(posixpath.join(ldir, posixpath.dirname(inc)))
                if tgt == ".":
                    tgt = ""
                if tgt.startswith("..") or tgt in seen or (ldir.startswith("@") and not tgt.startswith("@")):
                    continue
                seen.add(tgt)
                d = ctx.to_abs(tgt)
                try:
                    if not d.is_dir():
                        continue
                    ctx.resolve(str(d), ctx.project_root, is_dir=True)
                except SvwError:
                    continue
                found.append(tgt)
                work.append(tgt)
    return sorted(found)


def walk_dirs(ctx: WorkspaceContext, top: Path) -> Iterator[Path]:
    """Bounded directory traversal shared by capture and the freshness fingerprint: yields ``top``
    and every sub-directory that is not excluded by the root rules, visits each REAL directory at
    most once (symlink cycles / aliases), and refuses symlinks that escape the trusted roots."""
    visited: set[Path] = set()
    stack = [top]
    while stack:
        d = stack.pop()
        real = Path(os.path.realpath(d))
        if real in visited:
            continue
        visited.add(real)
        if len(visited) > MAX_DIRS:
            raise SvwError(
                ErrorCode.RESOURCE_LIMIT,
                f"directory traversal exceeds {MAX_DIRS} directories",
                {"limit": MAX_DIRS},
            )
        yield d
        for name in reversed(_listdir(d)):
            child = d / name
            if child.is_symlink():
                ctx.check_trusted(str(child), d)  # PATH_NOT_ALLOWED on escape
            if child.is_dir() and not ctx.dir_excluded(child):
                stack.append(child)


def build_spec(ctx: WorkspaceContext, nm: NormalizedManifest) -> SnapshotSpec:
    explicit: list[ExplicitEntry] = []
    seen: set[str] = set()

    def add(logical: str, role: str) -> None:
        if logical not in seen:
            seen.add(logical)
            explicit.append(ExplicitEntry(logical=logical, role=role))
        elif role == "generated":  # a generated input that is also a source stays "generated"
            for n, e in enumerate(explicit):
                if e.logical == logical and e.role != "generated":
                    explicit[n] = ExplicitEntry(logical=logical, role="generated")

    for f in nm.filelists:
        add(f, "filelist")
    for u in nm.units:
        for s in u.sources:
            add(s.path, "source")
    for u in nm.units:
        for lf in u.library_files:
            add(lf.path, "library_file")
    for g in nm.generated_inputs:
        add(g.path, "generated")
    lookups: list[LookupSpec] = []
    lseen: set[tuple[str, str, bool, tuple[str, ...]]] = set()
    # slang resolves `include "x"` relative to the including file's directory first, then the
    # -I dirs (provisional: M2 compares slang's real dependency set with the captured set and
    # BLOCKs any leak). So every explicit input's directory is part of the search space.
    for e in explicit:
        if e.role == "filelist":
            continue
        sdir = posixpath.dirname(e.logical)
        if ("include", sdir, False, ()) not in lseen:
            lseen.add(("include", sdir, False, ()))
            lookups.append(LookupSpec(kind="include", logical_dir=sdir, recursive=False))
    # nested relative includes (`include "headers/defs.svh"` from rtl/top.sv resolves to
    # rtl/headers/defs.svh): follow the directives transitively and add every directory they name
    # as an inventoried (negative-lookup aware) search dir, within the root rules (B6)
    for nested in _nested_include_dirs(ctx, [e.logical for e in explicit if e.role != "filelist"]):
        if ("include", nested, False, ()) not in lseen:
            lseen.add(("include", nested, False, ()))
            lookups.append(LookupSpec(kind="include", logical_dir=nested, recursive=False))
    for u in nm.units:
        for d in u.include_dirs:
            if ("include", d.path, True, ()) not in lseen:
                lseen.add(("include", d.path, True, ()))
                lookups.append(LookupSpec(kind="include", logical_dir=d.path))
        for ld in u.library_dirs:
            lkey = ("library", ld.path, False, tuple(u.library_extensions))
            if lkey not in lseen:  # same dir with different +libext is a different search
                lseen.add(lkey)
                lookups.append(
                    LookupSpec(
                        kind="library",
                        logical_dir=ld.path,
                        exts=list(u.library_extensions),
                        recursive=False,
                    )
                )
    return SnapshotSpec(explicit=explicit, lookups=lookups, root_ids=[r.id for r in ctx.roots])


# --------------------------------------------------------------------------- inventory
class _Entry:
    __slots__ = ("logical", "original", "real", "role", "symlink_target", "hardlinked")

    def __init__(self, logical: str, original: Path, real: Path, role: str, target: str | None):
        self.logical, self.original, self.real, self.role, self.symlink_target = (
            logical,
            original,
            real,
            role,
            target,
        )
        self.hardlinked = is_hardlinked(real)


def _listdir(directory: Path) -> list[str]:
    """Sorted directory listing; an unreadable directory is an error, never a silent gap."""
    try:
        return sorted(os.listdir(directory))
    except OSError as e:
        raise SvwError(
            ErrorCode.MISSING_INPUT,
            f"cannot list lookup directory {directory}: {e.strerror or e}",
            {"reason": "dir_unreadable", "path": str(directory)},
        ) from e


def _walk(
    ctx: WorkspaceContext,
    directory: Path,
    rel: str,
    visited: set[Path],
    out: list[tuple[str, Path]],
    depth: int = 0,
) -> None:
    """Recursive listing of allowed files; symlinks must stay inside trusted roots. Bounded in
    directories (``MAX_DIRS``, like :func:`walk_dirs`) and in depth, so a hostile or huge tree is a
    clean ``RESOURCE_LIMIT`` and never a ``RecursionError``."""
    if depth > MAX_WALK_DEPTH:
        raise SvwError(
            ErrorCode.RESOURCE_LIMIT,
            f"directory nesting exceeds {MAX_WALK_DEPTH} levels",
            {"limit": MAX_WALK_DEPTH},
        )
    real_dir = Path(os.path.realpath(directory))
    if real_dir in visited:
        return
    visited.add(real_dir)
    if len(visited) > MAX_DIRS:
        raise SvwError(
            ErrorCode.RESOURCE_LIMIT,
            f"directory traversal exceeds {MAX_DIRS} directories",
            {"limit": MAX_DIRS},
        )
    names = _listdir(directory)
    for name in names:
        child = directory / name
        crel = f"{rel}{name}"
        if child.is_symlink():
            ctx.check_trusted(str(child), directory)  # PATH_NOT_ALLOWED on escape
        if child.is_dir():
            if not ctx.dir_excluded(child):
                _walk(ctx, child, crel + "/", visited, out, depth + 1)
        elif child.is_file() and ctx.allows_file(child):
            out.append((crel, child))


def _enumerate(
    ctx: WorkspaceContext, spec: SnapshotSpec, tolerate_missing: bool
) -> tuple[list[_Entry], list[tuple[LookupSpec, Path, list[str]]], list[str]]:
    entries: list[_Entry] = []
    missing: list[str] = []
    seen: set[str] = set()
    for ex in spec.explicit:
        original = ctx.to_abs(ex.logical)
        if tolerate_missing and not original.is_file():
            missing.append(ex.logical)
            continue
        rp = ctx.resolve(str(original), ctx.project_root)
        seen.add(rp.logical)
        entries.append(_Entry(rp.logical, rp.abs, rp.real, ex.role, rp.symlink_target))
    lookups: list[tuple[LookupSpec, Path, list[str]]] = []
    for lk in spec.lookups:
        d = ctx.to_abs(lk.logical_dir)
        if tolerate_missing and not d.is_dir():
            lookups.append((lk, d, []))
            continue
        ctx.resolve(str(d), ctx.project_root, is_dir=True)
        found: list[tuple[str, Path]] = []
        if lk.kind == "include" and lk.recursive:
            _walk(ctx, d, "", set(), found)
        elif lk.kind == "include":
            for name in _listdir(d):
                p = d / name
                if p.is_symlink():
                    ctx.check_trusted(str(p), d)
                if p.is_file() and ctx.allows_file(p):
                    found.append((name, p))
        else:
            for name in _listdir(d):
                p = d / name
                if p.is_file() and Path(name).suffix in lk.exts and ctx.allows_file(p):
                    found.append((name, p))
        lookups.append((lk, d, [n for n, _ in found]))
        for _rel, p in found:
            rp = ctx.resolve(str(p), ctx.project_root)
            if rp.logical not in seen:
                seen.add(rp.logical)
                entries.append(
                    _Entry(rp.logical, rp.abs, rp.real, f"{lk.kind}_lookup", rp.symlink_target)
                )
    if len(entries) > MAX_FILES:
        raise SvwError(
            ErrorCode.RESOURCE_LIMIT,
            f"snapshot would contain {len(entries)} files (limit {MAX_FILES})",
            {"files": len(entries), "limit": MAX_FILES},
        )
    return entries, lookups, missing


def _hash_file(path: Path) -> tuple[str, int]:
    h = hashlib.sha256()
    n = 0
    with open(path, "rb") as f:
        while chunk := f.read(_CHUNK):
            h.update(chunk)
            n += len(chunk)
    return h.hexdigest(), n


def _inventory(
    ctx: WorkspaceContext, spec: SnapshotSpec, tolerate_missing: bool = False
) -> tuple[list[SnapFile], list[LookupInventory], list[str]]:
    entries, lookups, missing = _enumerate(ctx, spec, tolerate_missing)
    files: list[SnapFile] = []
    for e in entries:
        try:
            sha, size = _hash_file(e.real)
        except FileNotFoundError:
            if not tolerate_missing:
                raise
            missing.append(e.logical)
            continue
        files.append(_snapfile(e, sha, size))
    return files, _lookup_inv(lookups), missing


def _snapfile(e: _Entry, sha: str, size: int) -> SnapFile:
    return SnapFile(
        logical_path=e.logical,
        original_path=str(e.original),
        snapshot_path="files/" + snapshot_rel(e.logical),
        sha256=sha,
        size=size,
        role=e.role,
        symlink_target=e.symlink_target,
        hardlinked=e.hardlinked,
    )


def _lookup_inv(lookups: list[tuple[LookupSpec, Path, list[str]]]) -> list[LookupInventory]:
    return [
        LookupInventory(
            kind=lk.kind,
            logical_dir=lk.logical_dir,
            recursive=lk.recursive,
            exts=list(lk.exts),
            original_dir=str(d),
            entries=sorted(names),
        )
        for lk, d, names in lookups
    ]


def snapshot_id_for(
    spec: SnapshotSpec, files: list[SnapFile], lookup: list[LookupInventory]
) -> str:
    """snapshot_id = H(ordered inventory + path mapping (root ids) + lookup search space).

    Host absolute paths are deliberately NOT part of the id so a snapshot is relocatable.
    Array order (files, lookup dirs = include/library search order) is significant.
    """
    return compute_snapshot_id(
        {
            "schema": "snapshot-v1",
            "roots": list(spec.root_ids),
            "files": [[f.logical_path, f.sha256, f.size] for f in files],
            "lookup": [
                {
                    "kind": lk.kind,
                    "dir": lk.logical_dir,
                    "recursive": lk.recursive,
                    "exts": list(lk.exts),
                    "entries": list(lk.entries),
                }
                for lk in lookup
            ],
        }
    )


# --------------------------------------------------------------------------- capture
def _force_rmtree(path: Path) -> None:
    def onerr(func: Callable[..., object], p: str, _exc: object) -> None:
        os.chmod(os.path.dirname(p), 0o755)
        os.chmod(p, 0o755)
        func(p)

    for root, dirs, _files in os.walk(path):
        os.chmod(root, 0o755)
        for d in dirs:
            os.chmod(os.path.join(root, d), 0o755)
    shutil.rmtree(path, onerror=onerr)


def remove_snapshot_dir(path: Path) -> None:
    if path.exists():
        _force_rmtree(path)


def _freeze(root: Path) -> None:
    for dirpath, dirnames, filenames in os.walk(root, topdown=False):
        for fn in filenames:
            os.chmod(os.path.join(dirpath, fn), stat.S_IRUSR | stat.S_IRGRP | stat.S_IROTH)
        for dn in dirnames:
            os.chmod(os.path.join(dirpath, dn), 0o555)
    os.chmod(root, 0o555)


def snapshots_dir(ctx: WorkspaceContext) -> Path:
    return ctx.state_dir / "snapshots"


def capture_snapshot(
    ctx: WorkspaceContext,
    nm: NormalizedManifest,
    *,
    max_retries: int = DEFAULT_RETRIES,
    _hook: Hook | None = None,
) -> SnapshotRecord:
    """Capture an immutable snapshot, or raise WORKSPACE_UNSTABLE after bounded retries.

    Holds the workspace lock (shared) so GC cannot remove this capture's temp dir.
    """
    with workspace_lock(ctx.state_dir):
        return _capture(ctx, nm, max_retries=max_retries, _hook=_hook)


def _capture(
    ctx: WorkspaceContext,
    nm: NormalizedManifest,
    *,
    max_retries: int = DEFAULT_RETRIES,
    _hook: Hook | None = None,
) -> SnapshotRecord:
    """Capture an immutable snapshot, or raise WORKSPACE_UNSTABLE after bounded retries."""
    spec = build_spec(ctx, nm)
    root = snapshots_dir(ctx)
    root.mkdir(parents=True, exist_ok=True)
    for attempt in range(1, max_retries + 1):
        tmp = root / f".tmp-{uuid.uuid4().hex}"
        try:
            (tmp / "files").mkdir(parents=True)
            entries, lookups, _ = _enumerate(ctx, spec, False)
            files: list[SnapFile] = []
            total = 0
            for e in entries:
                dest = tmp / "files" / snapshot_rel(e.logical)
                dest.parent.mkdir(parents=True, exist_ok=True)
                h = hashlib.sha256()
                size = 0
                with open(e.real, "rb") as src, open(dest, "xb") as dst:
                    while chunk := src.read(_CHUNK):
                        # enforced WHILE copying: one huge (or growing) file must not fill the disk
                        if total + size + len(chunk) > MAX_TOTAL_BYTES:
                            raise SvwError(
                                ErrorCode.RESOURCE_LIMIT,
                                f"snapshot exceeds {MAX_TOTAL_BYTES} bytes",
                                {"limit": MAX_TOTAL_BYTES},
                            )
                        h.update(chunk)
                        dst.write(chunk)
                        size += len(chunk)
                total += size
                files.append(_snapfile(e, h.hexdigest(), size))
            lookup1 = _lookup_inv(lookups)
            if _hook is not None:
                _hook("after_copy", attempt)
            files2, lookup2, _ = _inventory(ctx, spec)
            if files != files2 or lookup1 != lookup2:
                remove_snapshot_dir(tmp)
                continue
            _verify_resolved_inputs(nm, files)
            sid = snapshot_id_for(spec, files, lookup1)
            record = SnapshotRecord(
                snapshot_id=sid,
                workspace_id=ctx.workspace_id,
                profile=nm.profile,
                config_id=nm.config_id,
                created_at=datetime.now(UTC).isoformat(timespec="seconds"),
                attempts=attempt,
                spec=spec,
                files=files,
                lookup=lookup1,
            )
            final = root / sid
            if final.exists():
                remove_snapshot_dir(tmp)
                return load_record(final)
            (tmp / "snapshot.json").write_text(record.model_dump_json(indent=2), encoding="utf-8")
            _freeze(tmp)
            os.rename(tmp, final)
            return record
        except BaseException:
            remove_snapshot_dir(tmp)
            raise
    raise SvwError(
        ErrorCode.WORKSPACE_UNSTABLE,
        f"workspace changed during capture in all {max_retries} attempts; "
        "no snapshot published, create a new one once sources are stable",
        {"attempts": max_retries},
    )


def _verify_resolved_inputs(nm: NormalizedManifest, files: list[SnapFile]) -> None:
    """The bytes captured must be the bytes the manifest was resolved from.

    A filelist edited between resolution and capture would otherwise yield a snapshot whose
    files do not correspond to its config_id.
    """
    got = {f.logical_path: f.sha256 for f in files}
    expected = dict(nm.filelist_hashes)
    expected.update({g.path: g.sha256 for g in nm.generated_inputs})
    changed = sorted(p for p, h in expected.items() if got.get(p) != h)
    if changed:
        raise SvwError(
            ErrorCode.WORKSPACE_UNSTABLE,
            "inputs changed between manifest resolution and capture: "
            + ", ".join(changed)
            + "; re-resolve the manifest and capture again",
            {"changed": changed},
        )


def load_record(snapshot_dir: Path) -> SnapshotRecord:
    return SnapshotRecord.model_validate_json((snapshot_dir / "snapshot.json").read_text("utf-8"))


# --------------------------------------------------------------------------- freshness
def check_freshness(ctx: WorkspaceContext, record: SnapshotRecord) -> FreshnessResult:
    """Recompute the current workspace inventory for the snapshot's spec and diff it.

    Detects modified/removed/added files and lookup search-space changes (new or removed
    candidate headers/library members), including untracked and uncommitted files.
    """
    files, lookup, missing = _inventory(ctx, record.spec, tolerate_missing=True)
    cur_id = snapshot_id_for(record.spec, files, lookup)
    changes: list[Change] = []
    old = {f.logical_path: f for f in record.files}
    new = {f.logical_path: f for f in files}
    for lp, f in old.items():
        if lp in new:
            if new[lp].sha256 != f.sha256 or new[lp].size != f.size:
                changes.append(Change(path=lp, change="modified"))
        else:
            changes.append(Change(path=lp, change="removed"))
    for lp in new:
        if lp not in old:
            changes.append(Change(path=lp, change="added"))
    old_lk = {
        (lk.kind, lk.logical_dir, lk.recursive, tuple(lk.exts)): set(lk.entries)
        for lk in record.lookup
    }
    for lk in lookup:
        prev = old_lk.get((lk.kind, lk.logical_dir, lk.recursive, tuple(lk.exts)), set())
        base = lk.logical_dir.rstrip("/")
        sep = "" if base == "" or base.endswith("/") else "/"
        for n in sorted(set(lk.entries) - prev):
            changes.append(Change(path=f"{base}{sep}{n}", change="lookup_added"))
        for n in sorted(prev - set(lk.entries)):
            changes.append(Change(path=f"{base}{sep}{n}", change="lookup_removed"))
    current = cur_id == record.snapshot_id and not changes and not missing
    return FreshnessResult(
        freshness="CURRENT" if current else "STALE",
        snapshot_id=record.snapshot_id,
        current_snapshot_id=cur_id,
        changed=changes,
    )
