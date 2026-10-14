"""Structured event log (plan 24.1, SVW-030): one JSON object per line.

Fields: ts, request_id, job_id, build_id, component, event, duration_s, outcome, error_code.
Free text (``detail``) is passed through the redactor; source text is never logged.
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from svweave.security.redaction import Redactor

log = logging.getLogger("svweave.gateway")

EVENT_KEYS = (
    "ts",
    "request_id",
    "job_id",
    "build_id",
    "component",
    "event",
    "duration_s",
    "outcome",
    "error_code",
)


class EventLog:
    def __init__(self, path: Path, redactor: Redactor | None = None) -> None:
        self.path = path
        self.redactor = redactor or Redactor()
        self._lock = threading.Lock()
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.close(os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT | os.O_NOFOLLOW, 0o600))

    @classmethod
    def for_session(cls, state_dir: Path, redactor: Redactor | None = None) -> EventLog:
        sid = f"serve-{datetime.now(UTC):%Y%m%dT%H%M%S}-{os.getpid()}-{uuid.uuid4().hex[:6]}"
        return cls(state_dir / "runs" / sid / "events.jsonl", redactor)

    def emit(
        self,
        component: str,
        event: str,
        *,
        request_id: str | None = None,
        job_id: str | None = None,
        build_id: str | None = None,
        duration_s: float | None = None,
        outcome: str = "ok",
        error_code: str | None = None,
        detail: str | None = None,
    ) -> None:
        def safe(v: str | None) -> str | None:
            # ids come from validated values only, but redact anyway (defense in depth)
            return None if v is None else self.redactor.text(str(v))[:200]

        rec: dict[str, Any] = {
            "ts": datetime.now(UTC).isoformat(timespec="milliseconds"),
            "request_id": safe(request_id),
            "job_id": safe(job_id),
            "build_id": safe(build_id),
            "component": safe(component),
            "event": safe(event),
            "duration_s": None if duration_s is None else round(duration_s, 4),
            "outcome": safe(outcome),
            "error_code": safe(error_code),
        }
        if detail:
            rec["detail"] = self.redactor.text(detail)[:500]
        line = json.dumps(rec, sort_keys=True) + "\n"
        with self._lock:
            fd = os.open(self.path, os.O_WRONLY | os.O_APPEND | os.O_CREAT | os.O_NOFOLLOW, 0o600)
            with os.fdopen(fd, "a", encoding="utf-8") as f:
                f.write(line)
        log.info(
            "%s/%s outcome=%s req=%s code=%s", component, event, outcome, request_id, error_code
        )

    def timer(self) -> float:
        return time.monotonic()


def read_events(path: Path, limit: int = 1000) -> list[dict[str, Any]]:
    if not path.is_file():
        return []
    out: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines()[-limit:]:
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return out
