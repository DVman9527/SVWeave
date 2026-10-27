"""Bounded stream primitives shared by future qualified scheduler supervisors.

No process creation or scheduler contact occurs here. Overflow must trigger the caller's
exact-target cancellation path, not a local process-group kill masquerading as remote cleanup.
"""

from __future__ import annotations

import threading
from collections.abc import Callable
from typing import BinaryIO


class BoundedStreams:
    def __init__(self, stdout_limit: int, stderr_limit: int, cancel: Callable[[], None]) -> None:
        if stdout_limit <= 0 or stderr_limit <= 0:
            raise ValueError("stream limits must be positive")
        self.stdout_limit = stdout_limit
        self.stderr_limit = stderr_limit
        self._cancel = cancel
        self._stdout = bytearray()
        self._stderr = bytearray()
        self._overflow = threading.Event()
        self._lock = threading.Lock()

    @property
    def overflow(self) -> bool:
        return self._overflow.is_set()

    @property
    def stdout(self) -> bytes:
        with self._lock:
            return b"" if self.overflow else bytes(self._stdout)

    @property
    def stderr_tail(self) -> bytes:
        with self._lock:
            return bytes(self._stderr)

    def drain_stdout(self, stream: BinaryIO) -> None:
        while chunk := stream.read(64 * 1024):
            cancel = False
            with self._lock:
                if self.overflow:
                    continue
                if len(self._stdout) + len(chunk) > self.stdout_limit:
                    self._stdout.clear()
                    self._overflow.set()
                    cancel = True
                else:
                    self._stdout.extend(chunk)
            if cancel:
                self._cancel()

    def drain_stderr(self, stream: BinaryIO) -> None:
        while chunk := stream.read(64 * 1024):
            with self._lock:
                self._stderr.extend(chunk)
                if len(self._stderr) > self.stderr_limit:
                    del self._stderr[:-self.stderr_limit]


def feed_request(stream: BinaryIO, payload: bytes) -> None:
    """Send exactly one request then EOF, including broken-pipe cleanup."""
    try:
        sent = 0
        while sent < len(payload):
            count = stream.write(payload[sent:])
            if count is None or count <= 0:
                raise OSError("request stream made no write progress")
            sent += count
        stream.flush()
    finally:
        stream.close()
