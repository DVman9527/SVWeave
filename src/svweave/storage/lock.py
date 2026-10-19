"""Workspace lock (flock on `.svweave/lock`) shared by capture, GC and future edit commit.

Capture holds it SHARED (many captures may run); GC and edit commit take it EXCLUSIVE and, when
non-blocking, must back off if another process holds it. flock locks belong to the open file
description, so two acquisitions conflict even inside one process (used by tests).
"""

from __future__ import annotations

import fcntl
import os
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

LOCK_NAME = "lock"


class WorkspaceBusy(Exception):
    """Raised by a non-blocking acquisition when another holder conflicts."""


@contextmanager
def workspace_lock(
    state_dir: Path, *, exclusive: bool = False, blocking: bool = True
) -> Iterator[None]:
    state_dir.mkdir(parents=True, exist_ok=True)
    fd = os.open(state_dir / LOCK_NAME, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        flags = fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH
        if not blocking:
            flags |= fcntl.LOCK_NB
        try:
            fcntl.flock(fd, flags)
        except BlockingIOError as e:
            raise WorkspaceBusy(str(state_dir)) from e
        try:
            yield
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        os.close(fd)


BUILD_LOCK_NAME = "build.lock"


@contextmanager
def build_lock(state_dir: Path) -> Iterator[None]:
    """Exclusive, NON-blocking lock held for the whole of a build (CLI and MCP alike): at most one
    build runs per workspace. Raises :class:`WorkspaceBusy` when another build holds it."""
    state_dir.mkdir(parents=True, exist_ok=True)
    fd = os.open(state_dir / BUILD_LOCK_NAME, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as e:
            raise WorkspaceBusy(str(state_dir)) from e
        try:
            yield
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        os.close(fd)


@contextmanager
def init_lock(db_path: Path) -> Iterator[None]:
    """Blocking exclusive lock serialising database setup (journal mode + migrations)."""
    db_path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(db_path.with_name(db_path.name + ".init.lock"), os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        os.close(fd)
