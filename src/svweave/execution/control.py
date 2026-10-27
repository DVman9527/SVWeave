"""Private bounded control sidecars outside every backend sandbox mount."""
from __future__ import annotations

import contextlib
import os
import stat
import uuid
from pathlib import Path

from svweave.execution.ledger import MAX_RECORD_BYTES, ExecutionLedger
from svweave.execution.remote_bootstrap import ControlRecord


class ControlStore:
    """A private operator-created shared directory; immutable one-record-per-attempt.

    The same-UID host trust boundary is unchanged. A control record is not scheduler
    ownership evidence; consumers must call validate_control against a trusted query.
    """
    def __init__(self, directory: Path) -> None:
        self._directory = ExecutionLedger(directory)

    def publish(self, control: ControlRecord) -> None:
        data = control.model_dump_json().encode()
        if len(data) > MAX_RECORD_BYTES:
            raise ValueError("control record exceeds limit")
        directory = self._directory._open()
        temporary = ".control-" + uuid.uuid4().hex
        try:
            fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=directory)
            with os.fdopen(fd, "wb") as stream:
                stream.write(data)
                stream.flush()
                os.fsync(stream.fileno())
            # link is atomic and refuses an existing target: no ready-record replacement.
            os.link(temporary, self._directory._name(control.attempt_id), src_dir_fd=directory, dst_dir_fd=directory, follow_symlinks=False)
            os.fsync(directory)
        finally:
            with contextlib.suppress(FileNotFoundError):
                os.unlink(temporary, dir_fd=directory)
            os.close(directory)

    def read(self, attempt_id: str) -> ControlRecord:
        directory = self._directory._open()
        try:
            fd = os.open(self._directory._name(attempt_id), os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
            with os.fdopen(fd, "rb") as stream:
                info = os.fstat(stream.fileno())
                if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
                    raise ValueError("unsafe control record")
                data = stream.read(MAX_RECORD_BYTES + 1)
            if len(data) > MAX_RECORD_BYTES:
                raise ValueError("control record exceeds limit")
            control = ControlRecord.model_validate_json(data)
            if control.attempt_id != attempt_id:
                raise ValueError("control attempt mismatch")
            return control
        finally:
            os.close(directory)
