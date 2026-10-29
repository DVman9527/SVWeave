"""Opt-in, content-verified snapshot I/O reuse; never incremental HDL compilation.

Git and stat caches are deliberately absent. Every current source and lookup inventory is
checked, and a new snapshot remains a complete, independent, immutable tree. Reflinks are only
an optional materialization optimization; unsupported filesystems use ordinary byte copies.
"""

from __future__ import annotations

import errno
import fcntl
import hashlib
import os
import stat
import time
import uuid
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from svweave.config.context import WorkspaceContext, snapshot_rel
from svweave.domain.errors import ErrorCode, SvwError
from svweave.domain.ids import is_sha256_hex
from svweave.manifest.models import NormalizedManifest
from svweave.security.safeio import safe_rel_path
from svweave.snapshot import capture as cap
from svweave.snapshot.models import LookupInventory, SnapFile, SnapshotRecord, SnapshotSpec
from svweave.storage.lock import workspace_lock

# Linux FICLONE: a real copy-on-write clone, not a shared writable inode. Failure falls back.
_FICLONE = 0x40049409
_DIR_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
_FILE_FLAGS = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC


@dataclass
class CaptureStats:
    """Attempt-wide actual logical I/O counts; not physical disk-write measurements."""

    mode: str = "full_copy"
    files_hashed: int = 0
    bytes_hashed: int = 0
    snapshot_files_verified: int = 0
    snapshot_bytes_verified: int = 0
    files_copied: int = 0
    bytes_copied: int = 0
    files_cloned: int = 0
    bytes_cloned: int = 0
    reflink_attempts: int = 0
    reflink_skipped: int = 0
    reflink_supported: bool | None = None
    noop_snapshot_reused: bool = False
    attempts: int = 0
    fallback_reasons: list[str] = field(default_factory=list)
    timings_s: dict[str, float] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    def fallback(self, reason: str) -> None:
        if reason not in self.fallback_reasons:
            self.fallback_reasons.append(reason)

    def elapsed(self, stage: str, start: float) -> None:
        self.timings_s[stage] = self.timings_s.get(stage, 0.0) + time.perf_counter() - start


def _open_dir(path: Path) -> int:
    """Open an absolute directory without following ANY path-component symlink."""
    absolute = path.absolute()
    fd = os.open(absolute.anchor, _DIR_FLAGS)
    try:
        for part in absolute.parts[1:]:
            next_fd = os.open(part, _DIR_FLAGS, dir_fd=fd)
            os.close(fd)
            fd = next_fd
        return fd
    except BaseException:
        os.close(fd)
        raise


def _open_regular(path: Path) -> int:
    parent = _open_dir(path.parent)
    try:
        fd = os.open(path.name, _FILE_FLAGS, dir_fd=parent)
    finally:
        os.close(parent)
    if not stat.S_ISREG(os.fstat(fd).st_mode):
        os.close(fd)
        raise OSError(errno.EINVAL, "not a regular file", str(path))
    return fd


def _limit() -> SvwError:
    return SvwError(
        ErrorCode.RESOURCE_LIMIT,
        f"snapshot exceeds {cap.MAX_TOTAL_BYTES} bytes",
        {"limit": cap.MAX_TOTAL_BYTES},
    )


def _hash_fd(
    fd: int, budget: int, stats: CaptureStats, *, source: bool,
) -> tuple[str, int]:
    h = hashlib.sha256()
    size = 0
    if source:
        stats.files_hashed += 1
    else:
        stats.snapshot_files_verified += 1
    while chunk := os.read(fd, min(cap._CHUNK, max(budget - size + 1, 1))):
        size += len(chunk)
        if source:
            stats.bytes_hashed += len(chunk)
        else:
            stats.snapshot_bytes_verified += len(chunk)
        if size > budget:
            raise _limit()
        h.update(chunk)
    return h.hexdigest(), size


def _inventory(
    ctx: WorkspaceContext, spec: SnapshotSpec, stats: CaptureStats,
) -> tuple[list[SnapFile], list[LookupInventory], list[cap._Entry]]:
    start = time.perf_counter()
    entries, lookups, _ = cap._enumerate(ctx, spec, False)
    stats.elapsed("inventory_enumeration", start)
    start = time.perf_counter()
    files: list[SnapFile] = []
    total = 0
    for entry in entries:
        fd = _open_regular(entry.real)
        try:
            digest, size = _hash_fd(fd, cap.MAX_TOTAL_BYTES - total, stats, source=True)
        finally:
            os.close(fd)
        total += size
        files.append(cap._snapfile(entry, digest, size))
    stats.elapsed("source_hash", start)
    return files, cap._lookup_inv(lookups), entries


def _expected_tree(files: list[SnapFile]) -> tuple[dict[str, SnapFile], set[str]]:
    expected: dict[str, SnapFile] = {}
    directories = {"files"}
    for item in files:
        rel = "files/" + snapshot_rel(item.logical_path)
        if safe_rel_path(rel) is None or rel in expected:
            raise ValueError("unsafe or duplicate inventory path")
        expected[rel] = item
        parent = Path(rel).parent
        while parent.as_posix() != ".":
            directories.add(parent.as_posix())
            parent = parent.parent
    return expected, directories


def _read_record(directory: Path) -> SnapshotRecord:
    fd = _open_regular(directory / "snapshot.json")
    try:
        metadata = bytearray()
        while chunk := os.read(fd, min(1 << 20, (32 << 20) - len(metadata) + 1)):
            metadata.extend(chunk)
            if len(metadata) > 32 << 20:
                raise ValueError("snapshot metadata exceeds 32 MiB")
    finally:
        os.close(fd)
    record = SnapshotRecord.model_validate_json(metadata)
    _expected_tree(record.files)  # reject duplicate / unsafe paths even for a donor hint
    if (len(record.files) > cap.MAX_FILES
            or cap.snapshot_id_for(record.spec, record.files, record.lookup) != record.snapshot_id
            or any(f.snapshot_path != "files/" + snapshot_rel(f.logical_path) for f in record.files)):
        raise ValueError("invalid snapshot metadata identity")
    return record


def load_baseline_record(ctx: WorkspaceContext, snapshot_id: str) -> SnapshotRecord | None:
    """Bounded no-follow metadata load. Content is still verified before any reuse."""
    if not is_sha256_hex(snapshot_id):
        return None
    try:
        record = _read_record(cap.snapshots_dir(ctx) / snapshot_id)
        return record if (record.snapshot_id == snapshot_id
                          and record.workspace_id == ctx.workspace_id) else None
    except (OSError, ValueError):
        return None


def _stat_signature(st: os.stat_result) -> tuple[int, ...]:
    return (st.st_dev, st.st_ino, st.st_mode, st.st_nlink, st.st_size,
            st.st_mtime_ns, st.st_ctime_ns)


def _exact_tree(
    fd: int, expected: dict[str, SnapFile], directories: set[str],
    signatures: dict[str, tuple[int, ...]],
) -> bool:
    seen_files: set[str] = set()
    seen_dirs: set[str] = set()
    signatures[""] = _stat_signature(os.fstat(fd))

    def visit(dfd: int, prefix: str, depth: int) -> bool:
        if depth > cap.MAX_WALK_DEPTH:
            return False
        with os.scandir(dfd) as names:
            for entry in names:
                name = entry.name
                rel = f"{prefix}/{name}" if prefix else name
                st = os.stat(name, dir_fd=dfd, follow_symlinks=False)
                signatures[rel] = _stat_signature(st)
                if st.st_mode & 0o222:
                    return False
                if stat.S_ISDIR(st.st_mode):
                    if rel not in directories or len(seen_dirs) >= cap.MAX_DIRS:
                        return False
                    seen_dirs.add(rel)
                    sub = os.open(name, _DIR_FLAGS, dir_fd=dfd)
                    try:
                        if not visit(sub, rel, depth + 1):
                            return False
                    finally:
                        os.close(sub)
                elif stat.S_ISREG(st.st_mode) and st.st_nlink == 1:
                    if rel != "snapshot.json" and rel not in expected:
                        return False
                    seen_files.add(rel)
                else:
                    return False
        return True

    return (visit(fd, "", 0) and seen_files == {*expected, "snapshot.json"}
            and seen_dirs == directories)


def verify_snapshot_tree(
    directory: Path, record: SnapshotRecord, stats: CaptureStats | None = None,
) -> bool:
    """Exact inventory, safe metadata and byte verification; a directory name is not evidence.

    Public for the update planner's read-only cache inspection. Counters are optional. No
    symlinks, hardlinked files, unexpected members or malformed metadata are accepted.
    """
    counts = stats if stats is not None else CaptureStats()
    start = time.perf_counter()
    try:
        expected, directories = _expected_tree(record.files)
        before: dict[str, tuple[int, ...]] = {}
        fd = _open_dir(directory)
        try:
            if os.fstat(fd).st_mode & 0o222 or not _exact_tree(fd, expected, directories, before):
                return False
        finally:
            os.close(fd)
        stored = _read_record(directory)
        if stored.snapshot_id != record.snapshot_id or stored.workspace_id != record.workspace_id:
            return False
        total = 0
        for rel, item in expected.items():
            file_fd = _open_regular(directory / rel)
            try:
                if _stat_signature(os.fstat(file_fd)) != before[rel]:
                    return False
                digest, size = _hash_fd(
                    file_fd, min(item.size, cap.MAX_TOTAL_BYTES - total), counts, source=False,
                )
                if _stat_signature(os.fstat(file_fd)) != before[rel]:
                    return False
            finally:
                os.close(file_fd)
            total += size
            if digest != item.sha256 or size != item.size:
                return False
        # Catch unexpected entries introduced during verification, as well as replacements.
        fd = _open_dir(directory)
        try:
            after: dict[str, tuple[int, ...]] = {}
            return _exact_tree(fd, expected, directories, after) and after == before
        finally:
            os.close(fd)
    except (OSError, ValueError, SvwError):
        return False
    finally:
        counts.elapsed("cached_snapshot_verify", start)


def _copy_source(
    source: Path, destination: Path, budget: int, stats: CaptureStats,
) -> tuple[str, int]:
    start = time.perf_counter()
    src = _open_regular(source)
    try:
        with open(destination, "xb") as out:
            h = hashlib.sha256()
            size = 0
            stats.files_hashed += 1
            stats.files_copied += 1
            while chunk := os.read(src, min(cap._CHUNK, max(budget - size + 1, 1))):
                stats.bytes_hashed += len(chunk)
                if size + len(chunk) > budget:
                    raise _limit()
                h.update(chunk)
                out.write(chunk)
                size += len(chunk)
                stats.bytes_copied += len(chunk)
            return h.hexdigest(), size
    finally:
        os.close(src)
        stats.elapsed("source_copy_and_hash", start)


def _clone_file(source_fd: int, destination_fd: int) -> None:
    fcntl.ioctl(destination_fd, _FICLONE, source_fd)


def _clone_verified(
    donor: Path, destination: Path, item: SnapFile, stats: CaptureStats,
) -> bool:
    # Both donor and destination are under the same snapshots directory. A filesystem-level
    # unsupported result can only make subsequent conservative copies less expensive.
    if stats.reflink_supported is False:
        stats.reflink_skipped += 1
        return False
    attempted = False
    try:
        src = _open_regular(donor)
        try:
            donor_stat = os.fstat(src)
            if donor_stat.st_nlink != 1 or donor_stat.st_size != item.size:
                stats.fallback("donor_unsafe_or_missing")
                return False
            with open(destination, "xb+") as out:
                attempted = True
                stats.reflink_attempts += 1
                _clone_file(src, out.fileno())
                stats.reflink_supported = True
                if os.fstat(src).st_ino == os.fstat(out.fileno()).st_ino:
                    stats.fallback("clone_not_independent")
                else:
                    stats.files_cloned += 1
                    stats.bytes_cloned += os.fstat(out.fileno()).st_size
                    digest, size = _hash_fd(out.fileno(), item.size, stats, source=False)
                    if digest == item.sha256 and size == item.size:
                        return True
                    stats.fallback("donor_content_mismatch")
        finally:
            os.close(src)
    except OSError as exc:
        stats.fallback(f"reflink_unavailable_errno_{exc.errno}")
        if attempted and exc.errno in (errno.EOPNOTSUPP, errno.ENOTTY, errno.ENOSYS,
                                       errno.EXDEV, errno.EINVAL):
            stats.reflink_supported = False
    except SvwError:
        stats.fallback("donor_content_mismatch")
    if destination.exists():
        destination.unlink()
    return False


def _record(
    ctx: WorkspaceContext, nm: NormalizedManifest, spec: SnapshotSpec,
    files: list[SnapFile], lookup: list[LookupInventory], attempt: int,
) -> SnapshotRecord:
    return SnapshotRecord(
        snapshot_id=cap.snapshot_id_for(spec, files, lookup), workspace_id=ctx.workspace_id,
        profile=nm.profile, config_id=nm.config_id,
        created_at=datetime.now(UTC).isoformat(timespec="seconds"), attempts=attempt,
        spec=spec, files=files, lookup=lookup,
    )


def capture_snapshot_incremental(
    ctx: WorkspaceContext, nm: NormalizedManifest, *, baseline: SnapshotRecord | None = None,
    max_retries: int = cap.DEFAULT_RETRIES, _hook: cap.Hook | None = None,
) -> tuple[SnapshotRecord, CaptureStats]:
    """Capture with verified no-op / CoW reuse. Identity/schema and semantic behavior are unchanged.

    A baseline is only a bounded donor hint selected by the build layer, never authority for
    current content. Cold capture retains copy+hash then verify (no added initial hash pass).
    Counts include failed attempts, copy fallbacks and verification work. No native backend runs.
    """
    stats = CaptureStats()
    start = time.perf_counter()
    with workspace_lock(ctx.state_dir):
        root = cap.snapshots_dir(ctx)
        root.mkdir(parents=True, exist_ok=True)
        check = _open_dir(root)
        os.close(check)
        if baseline is not None and (
            baseline.workspace_id != ctx.workspace_id
            or not is_sha256_hex(baseline.snapshot_id)
            or cap.snapshot_id_for(baseline.spec, baseline.files, baseline.lookup) != baseline.snapshot_id
        ):
            stats.fallback("baseline_identity_invalid")
            baseline = None
        if baseline is None:
            stats.fallback("no_baseline")
        for attempt in range(1, max_retries + 1):
            stats.attempts = attempt
            tmp = root / f".tmp-{uuid.uuid4().hex}"
            try:
                spec = cap.build_spec(ctx, nm)
                before: list[SnapFile] | None = None
                lookup: list[LookupInventory]
                if baseline is not None:
                    before, lookup, entries = _inventory(ctx, spec, stats)
                    cap._verify_resolved_inputs(nm, before)
                    rec = _record(ctx, nm, spec, before, lookup, attempt)
                    if _hook is not None:
                        _hook("after_inventory", attempt)
                    final = root / rec.snapshot_id
                    with cap.snapshot_publish_lock(ctx):
                        existing_valid = verify_snapshot_tree(final, rec, stats)
                    if existing_valid:
                        if _hook is not None:
                            _hook("after_copy", attempt)
                        after, lookup_after, _ = _inventory(ctx, spec, stats)
                        if before != after or lookup != lookup_after:
                            continue
                        cap._verify_resolved_inputs(nm, after)
                        stats.mode = "reused_existing"
                        stats.noop_snapshot_reused = True
                        stats.timings_s["total"] = time.perf_counter() - start
                        return rec, stats
                    if final.exists() or final.is_symlink():
                        stats.fallback("snapshot_cache_invalid_rebuilt")

                (tmp / "files").mkdir(parents=True)
                if baseline is None:
                    entries, lookups, _ = cap._enumerate(ctx, spec, False)
                    lookup_copy = cap._lookup_inv(lookups)
                else:
                    # The first inventory already resolved these entries. Reuse the bounded
                    # sequence for materialization, not as freshness authority: the second full
                    # enumeration and content check below remains mandatory.
                    lookup_copy = lookup
                old = {f.logical_path: f for f in baseline.files} if baseline else {}
                current = {f.logical_path: f for f in before} if before is not None else {}
                files: list[SnapFile] = []
                total = 0
                for entry in entries:
                    destination = tmp / "files" / snapshot_rel(entry.logical)
                    destination.parent.mkdir(parents=True, exist_ok=True)
                    expected = current.get(entry.logical)
                    previous = old.get(entry.logical)
                    cloned = False
                    if (expected is not None and previous is not None and baseline is not None
                            and expected.sha256 == previous.sha256 and expected.size == previous.size):
                        cloned = _clone_verified(
                            root / baseline.snapshot_id / "files" / snapshot_rel(entry.logical),
                            destination, expected, stats,
                        )
                    if cloned:
                        assert expected is not None
                        digest, size = expected.sha256, expected.size
                    else:
                        digest, size = _copy_source(
                            entry.real, destination, cap.MAX_TOTAL_BYTES - total, stats,
                        )
                    total += size
                    if total > cap.MAX_TOTAL_BYTES:
                        raise _limit()
                    files.append(cap._snapfile(entry, digest, size))
                if _hook is not None:
                    _hook("after_copy", attempt)
                after, lookup_after, _ = _inventory(ctx, spec, stats)
                if (files != after or lookup_copy != lookup_after
                        or (before is not None and (files != before or lookup_copy != lookup))):
                    cap.remove_snapshot_dir(tmp)
                    continue
                cap._verify_resolved_inputs(nm, files)
                rec = _record(ctx, nm, spec, files, lookup_copy, attempt)
                final = root / rec.snapshot_id
                (tmp / "snapshot.json").write_text(rec.model_dump_json(indent=2), encoding="utf-8")
                cap._freeze(tmp)
                with cap.snapshot_publish_lock(ctx):
                    if final.exists() or final.is_symlink():
                        if verify_snapshot_tree(final, rec, stats):
                            cap.remove_snapshot_dir(tmp)
                            stats.fallback("existing_snapshot_won_publication")
                            stats.timings_s["total"] = time.perf_counter() - start
                            stats.mode = "delta" if baseline else "full_copy"
                            return rec, stats
                        stats.fallback("snapshot_cache_invalid_rebuilt")
                        cap.remove_snapshot_dir(final)
                    os.rename(tmp, final)
                stats.mode = "delta" if baseline else "full_copy"
                stats.timings_s["total"] = time.perf_counter() - start
                return rec, stats
            except BaseException:
                cap.remove_snapshot_dir(tmp)
                raise
    raise SvwError(
        ErrorCode.WORKSPACE_UNSTABLE,
        f"workspace changed during capture in all {max_retries} attempts; no snapshot published",
        {"attempts": max_retries},
    )
