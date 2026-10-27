"""Immutable per-attempt staging publication; no worker or scheduler launch.

Only hash-identified read-only snapshot files are copied. Callers must construct group
files/requests against ``root / attempt_id`` BEFORE publication; local SQLite, journals,
approvals and live original sources are not accepted as staging inputs.
"""
from __future__ import annotations

import contextlib
import ctypes
import errno
import hashlib
import os
import re
import stat
import uuid
from dataclasses import dataclass
from pathlib import Path

from svweave.adapters.slang.protocol import WorkerRequest
from svweave.domain.ids import canonical_json
from svweave.manifest.models import NormalizedManifest
from svweave.snapshot.models import SnapshotRecord

_TOKEN = re.compile(r"[A-Za-z0-9_-]{1,128}\Z")
_DIGEST = re.compile(r"[0-9a-f]{64}\Z")
_MANIFEST = "input-manifest.json"


def _parts(relative: str) -> tuple[str, ...]:
    path = Path(relative)
    if (not path.parts or path.is_absolute() or str(path) != relative or ".." in path.parts
            or any(ord(c) < 32 or ord(c) == 127 for c in relative)):
        raise ValueError("staging entry must be canonical relative path")
    return path.parts


def _root(path: Path, *, private: bool) -> int:
    if not path.is_absolute() or ".." in path.parts:
        raise ValueError("staging root must be absolute and canonical")
    fd = os.open("/", os.O_RDONLY | os.O_DIRECTORY)
    try:
        for part in path.parts[1:]:
            nextfd = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            os.close(fd)
            fd = nextfd
        info = os.fstat(fd)
        if private and (info.st_uid != os.getuid() or info.st_mode & 0o077):
            raise ValueError("publication root must be private and owned by current UID")
        return fd
    except BaseException:
        os.close(fd)
        raise


def _parent(rootfd: int, parts: tuple[str, ...], *, create: bool) -> int:
    fd = os.dup(rootfd)
    try:
        for part in parts[:-1]:
            if create:
                with contextlib.suppress(FileExistsError):
                    os.mkdir(part, 0o700, dir_fd=fd)
            nextfd = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            os.close(fd)
            fd = nextfd
        return fd
    except BaseException:
        os.close(fd)
        raise


def _publish_noreplace(rootfd: int, temporary: str, final: str) -> None:
    # Linux renameat2 is atomic and refuses ANY existing destination, unlike os.rename.
    libc = ctypes.CDLL(None, use_errno=True)
    rename = getattr(libc, "renameat2", None)
    if rename is None:
        raise OSError(errno.ENOSYS, "atomic no-replace staging publication is unavailable")
    rename.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint]
    rename.restype = ctypes.c_int
    if rename(rootfd, temporary.encode(), rootfd, final.encode(), 1) != 0:
        number = ctypes.get_errno()
        raise OSError(number, os.strerror(number), final)


def _seal(fd: int) -> None:
    for name in os.listdir(fd):
        info = os.stat(name, dir_fd=fd, follow_symlinks=False)
        if stat.S_ISDIR(info.st_mode):
            child = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            try:
                _seal(child)
            finally:
                os.close(child)
    os.fchmod(fd, 0o500)
    os.fsync(fd)


def _remove_owned(fd: int) -> None:
    """Remove only our unpublished temp tree by directory fds; never follow symlinks."""
    os.fchmod(fd, 0o700)
    for name in os.listdir(fd):
        info = os.stat(name, dir_fd=fd, follow_symlinks=False)
        if stat.S_ISDIR(info.st_mode):
            child = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            try:
                _remove_owned(child)
            finally:
                os.close(child)
            os.rmdir(name, dir_fd=fd)
        else:
            os.unlink(name, dir_fd=fd)


@dataclass(frozen=True)
class StagedInputs:
    path: Path
    hashes: dict[str, str]
    manifest_sha256: str
    total_bytes: int


def publish_inputs(
    source_root: Path, root: Path, attempt_id: str, expected: dict[str, str], *,
    generated: dict[str, bytes] | None = None,
    max_total_bytes: int = 1024 * 1024 * 1024, max_files: int = 100_000,
) -> StagedInputs:
    """Copy a fixed immutable snapshot closure and atomically publish without replacement.

    ``generated`` is coordinator-produced adapter metadata, not commands. The final
    absolute path is known before calling this function. Publication does not authorize
    launching, claim toolchain qualification, or share the local state root.
    """
    if not _TOKEN.fullmatch(attempt_id):
        raise ValueError("invalid attempt id")
    generated = generated or {}
    if (not expected or type(max_total_bytes) is not int or type(max_files) is not int
            or max_total_bytes <= 0 or max_files <= 0):
        raise ValueError("nonempty snapshot and finite positive quotas required")
    if len(expected) + len(generated) > max_files:
        raise ValueError("staging file count exceeds quota")
    if set(expected) & set(generated) or _MANIFEST in expected or _MANIFEST in generated:
        raise ValueError("duplicate or reserved staging entry")
    for relative in (*expected, *generated):
        _parts(relative)
    if any(not _DIGEST.fullmatch(value) for value in expected.values()):
        raise ValueError("invalid input digest")
    if root in (Path("/"), Path.home(), source_root) or source_root in root.parents:
        raise ValueError("publication root must be separate from original snapshot and HOME")
    srcfd = _root(source_root, private=False)
    rootfd = -1
    tempfd = -1
    temporary = ".tmp-" + uuid.uuid4().hex
    published = False
    total = 0
    hashes: dict[str, str] = {}
    try:
        rootfd = _root(root, private=True)
        os.mkdir(temporary, 0o700, dir_fd=rootfd)
        tempfd = os.open(temporary, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=rootfd)
        for relative in (*expected, *generated):
            parts = _parts(relative)
            destparent = _parent(tempfd, parts, create=True)
            try:
                out = os.open(parts[-1], os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                              0o600, dir_fd=destparent)
                with os.fdopen(out, "wb") as output:
                    digest = hashlib.sha256()
                    if relative in expected:
                        parent = _parent(srcfd, parts, create=False)
                        try:
                            source = os.open(parts[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK,
                                             dir_fd=parent)
                        finally:
                            os.close(parent)
                        with os.fdopen(source, "rb") as stream:
                            before = os.fstat(stream.fileno())
                            if not stat.S_ISREG(before.st_mode) or before.st_mode & 0o222:
                                raise ValueError("staging accepts only immutable regular snapshot files")
                            while chunk := stream.read(1024 * 1024):
                                total += len(chunk)
                                if total > max_total_bytes:
                                    raise ValueError("staging byte quota exceeded")
                                digest.update(chunk)
                                output.write(chunk)
                            after = os.fstat(stream.fileno())
                            if (before.st_size, before.st_mtime_ns, before.st_ctime_ns) != (
                                    after.st_size, after.st_mtime_ns, after.st_ctime_ns):
                                raise ValueError("snapshot changed while staging")
                        if digest.hexdigest() != expected[relative]:
                            raise ValueError("snapshot hash mismatch")
                    else:
                        data = generated[relative]
                        total += len(data)
                        if total > max_total_bytes:
                            raise ValueError("staging byte quota exceeded")
                        digest.update(data)
                        output.write(data)
                    output.flush()
                    os.fchmod(output.fileno(), 0o400)
                    os.fsync(output.fileno())
                    hashes[relative] = digest.hexdigest()
            finally:
                os.close(destparent)
        manifest = canonical_json({"schema_version": "1", "attempt_id": attempt_id,
                                   "files": hashes, "total_bytes": total}).encode()
        if total + len(manifest) > max_total_bytes:
            raise ValueError("staging byte quota exceeded by manifest")
        if len(manifest) > 16 * 1024 * 1024:
            raise ValueError("staging manifest exceeds limit")
        fd = os.open(_MANIFEST, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o400, dir_fd=tempfd)
        with os.fdopen(fd, "wb") as stream:
            stream.write(manifest)
            stream.flush()
            os.fsync(stream.fileno())
        _seal(tempfd)
        _publish_noreplace(rootfd, temporary, attempt_id)
        published = True
        os.fsync(rootfd)
        return StagedInputs(root / attempt_id, hashes, hashlib.sha256(manifest).hexdigest(), total + len(manifest))
    finally:
        if tempfd >= 0:
            if not published:
                _remove_owned(tempfd)
            os.close(tempfd)
        if rootfd >= 0:
            if not published:
                with contextlib.suppress(FileNotFoundError):
                    os.rmdir(temporary, dir_fd=rootfd)
            os.close(rootfd)
        os.close(srcfd)


@dataclass(frozen=True)
class StagedWorker:
    inputs: StagedInputs
    request: WorkerRequest


def prepare_slang_staging(
    manifest: NormalizedManifest, snapshot: SnapshotRecord, source_snapshot_dir: Path,
    root: Path, attempt_id: str, *, max_total_bytes: int = 1024 * 1024 * 1024,
) -> StagedWorker:
    """Render final group/include paths, then atomically publish fixed snapshot inputs.

    The live DB is never copied. Temporary adapter files stay coordinator-local and
    are discarded after their bytes join the immutable publication. This is preparation
    only, not provider submission, qualification or authorization.
    """
    import tempfile

    from svweave.adapters.slang.args import build_invocation
    from svweave.config.context import snapshot_rel

    if (manifest.config_id != snapshot.config_id
            or source_snapshot_dir.name != snapshot.snapshot_id):
        raise ValueError("snapshot/config identity mismatch")
    if not _TOKEN.fullmatch(attempt_id):
        raise ValueError("invalid attempt id")
    final = root / attempt_id
    with tempfile.TemporaryDirectory(prefix="svweave-stage-groups-") as temporary:
        group_dir = Path(temporary)
        invocation = build_invocation(manifest, final / "files", group_dir,
                                      published_group_dir=final / "groups")
        request = WorkerRequest(snapshot_root=str(final / "files"), args=invocation.args,
                                group_files=invocation.group_files, tops=list(manifest.top_modules))
        generated = {"groups/" + p.name: p.read_bytes() for p in group_dir.iterdir()}
        generated["request.json"] = request.model_dump_json().encode("utf-8")
        expected = {"files/" + snapshot_rel(f.logical_path): f.sha256 for f in snapshot.files}
        inputs = publish_inputs(source_snapshot_dir, root, attempt_id, expected,
                                generated=generated, max_total_bytes=max_total_bytes)
    return StagedWorker(inputs, request)
