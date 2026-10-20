"""Symlink-safe file operations for host code that touches backend-writable directories.

A sandboxed backend may plant symlinks anywhere in a directory it can write. Every host-side read or
write below such a directory must therefore never follow a link: each path component is opened with
``O_NOFOLLOW`` relative to its parent (``openat`` style), only regular files are accepted, and
writes go to a fresh ``O_EXCL`` temporary file that is renamed over the target (rename replaces a
planted symlink instead of following it).
"""

from __future__ import annotations

import contextlib
import os
import secrets
import stat
from pathlib import Path

from svweave.domain.errors import ErrorCode, SvwError

_DIR_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC


def unsafe(path: object, why: str) -> SvwError:
    return SvwError(
        ErrorCode.POLICY_DENIED,
        f"refusing unsafe path in a backend-writable directory: {why}",
        {"reason": "unsafe_path", "why": why, "path": str(path)[-200:]},
    )


def safe_rel_path(p: object) -> str | None:
    """Relative POSIX path without empty / '..' components, absolute roots or NULs; else None."""
    if not isinstance(p, str) or not p or p.startswith("/") or "\x00" in p:
        return None
    if any(x in ("..", "", ".") for x in p.split("/")):
        return None
    return p


def _open_dir(name: str | Path, dir_fd: int | None = None) -> int:
    try:
        return os.open(name, _DIR_FLAGS, dir_fd=dir_fd)
    except OSError as e:
        raise unsafe(name, f"cannot open directory without following links ({e.strerror})") from e


def _walk(base: Path, parts: list[str], create: bool) -> int:
    """fd of ``base/parts...`` opened component by component with O_NOFOLLOW (caller closes)."""
    fd = _open_dir(base)
    try:
        for part in parts:
            if create:
                with contextlib.suppress(FileExistsError):
                    os.mkdir(part, 0o700, dir_fd=fd)
            nfd = _open_dir(part, fd)
            os.close(fd)
            fd = nfd
        return fd
    except BaseException:
        os.close(fd)
        raise


def ensure_private_dir(base: Path, rel: str) -> Path:
    """Create ``base/rel`` (each component no-follow) and make every created level 0700."""
    parts = [p for p in rel.split("/") if p]
    fd = _open_dir(base)
    try:
        for part in parts:
            with contextlib.suppress(FileExistsError):
                os.mkdir(part, 0o700, dir_fd=fd)
            nfd = _open_dir(part, fd)
            os.close(fd)
            fd = nfd
            os.fchmod(fd, 0o700)
        return base / rel
    finally:
        os.close(fd)


def write_under(base: Path, rel: str, data: bytes, mode: int = 0o600, *, durable: bool = False) -> None:
    """Atomically write ``data`` to ``base/rel`` without following any link below ``base``.

    ``durable``: fsync the temporary file before the rename and the directory after it (edit commit,
    journal), so a crash leaves either the old or the complete new file."""
    parts = rel.split("/")
    if safe_rel_path(rel) is None:
        raise unsafe(rel, "not a safe relative path")
    dfd = _walk(base, parts[:-1], create=False)
    name = parts[-1]
    tmp = f".{name}.svw-{secrets.token_hex(6)}"
    try:
        try:
            st = os.stat(name, dir_fd=dfd, follow_symlinks=False)
        except FileNotFoundError:
            st = None
        if st is not None and stat.S_ISDIR(st.st_mode):
            raise unsafe(rel, "target is a directory")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC, mode, dir_fd=dfd)
        try:
            with os.fdopen(fd, "wb") as f:
                os.fchmod(f.fileno(), mode)
                f.write(data)
                if durable:
                    f.flush()
                    os.fsync(f.fileno())
            os.rename(tmp, name, src_dir_fd=dfd, dst_dir_fd=dfd)
            if durable:
                os.fsync(dfd)
        except BaseException:
            with contextlib.suppress(OSError):
                os.unlink(tmp, dir_fd=dfd)
            raise
    finally:
        os.close(dfd)


def write_atomic(path: Path, data: bytes, mode: int = 0o600) -> None:
    """Host-private file: same guarantees for the final component and the parent directory."""
    path.parent.mkdir(parents=True, exist_ok=True)
    write_under(path.parent, path.name, data, mode)


def read_regular(path: Path, max_bytes: int | None = None, *, tail: bool = False) -> bytes:
    """Read a regular file without following a link at the final component (raises otherwise)."""
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK)
    except OSError as e:
        if isinstance(e, FileNotFoundError):
            raise
        raise unsafe(path, f"cannot open without following links ({e.strerror})") from e
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode):
            raise unsafe(path, "not a regular file")
        if max_bytes is not None and tail and st.st_size > max_bytes:
            os.lseek(fd, st.st_size - max_bytes, os.SEEK_SET)
        return os.read(fd, max_bytes) if max_bytes is not None else _read_all(fd)
    finally:
        os.close(fd)


def read_regular_under(base: Path, rel: str, max_bytes: int | None = None) -> bytes:
    """Read ``base/rel`` as a regular file, following NO link in any component below ``base``.

    For trees a backend can write (candidate copies): :func:`read_regular` only refuses a link at
    the final component, so a directory symlink inside the tree could redirect the read outside it.
    Raises ``FileNotFoundError`` for a missing final file; any link, non-regular file or missing
    parent directory is the same ``POLICY_DENIED`` refusal :func:`write_under` gives."""
    if safe_rel_path(rel) is None:
        raise unsafe(rel, "not a safe relative path")
    parts = rel.split("/")
    try:
        dfd = _walk(base, parts[:-1], create=False)
    except OSError as e:
        if isinstance(e, FileNotFoundError):
            raise
        raise unsafe(rel, f"cannot traverse without following links ({e.strerror})") from e
    try:
        try:
            fd = os.open(
                parts[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK, dir_fd=dfd
            )
        except OSError as e:
            if isinstance(e, FileNotFoundError):
                raise
            raise unsafe(rel, f"cannot open without following links ({e.strerror})") from e
        try:
            if not stat.S_ISREG(os.fstat(fd).st_mode):
                raise unsafe(rel, "not a regular file")
            return os.read(fd, max_bytes) if max_bytes is not None else _read_all(fd)
        finally:
            os.close(fd)
    finally:
        os.close(dfd)


def _read_all(fd: int) -> bytes:
    chunks = []
    while chunk := os.read(fd, 1 << 20):
        chunks.append(chunk)
    return b"".join(chunks)


def open_append_log(path: Path, max_bytes: int) -> int:
    """fd for appending to a host-private log, rotating it to ``.1`` when over ``max_bytes``.

    Opened by the host BEFORE the backend starts and handed over as stderr: the backend never
    touches the path. Refuses links and non-regular files.
    """
    flags = os.O_WRONLY | os.O_CREAT | os.O_APPEND | os.O_NOFOLLOW | os.O_CLOEXEC
    try:
        st = os.lstat(path)
    except FileNotFoundError:
        st = None
    if st is not None:
        if not stat.S_ISREG(st.st_mode):
            raise unsafe(path, "log path is a link or not a regular file")
        if st.st_size > max_bytes:
            os.replace(path, path.with_name(path.name + ".1"))
    try:
        return os.open(path, flags, 0o600)
    except OSError as e:
        raise unsafe(path, f"cannot open log ({e.strerror})") from e
