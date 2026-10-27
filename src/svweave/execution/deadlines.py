"""Monotonic transport deadlines; unknown scheduler state is never called queue time."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass
class Deadlines:
    submitted: float
    queue_seconds: float
    startup_seconds: float
    execution_seconds: float
    overall_seconds: float
    scheduler_started: float | None = None
    worker_started: float | None = None
    scheduler_pending: bool = False

    def __post_init__(self) -> None:
        if min(self.queue_seconds, self.startup_seconds, self.execution_seconds, self.overall_seconds) <= 0:
            raise ValueError("all deadlines must be finite positive durations")
        import math

        if not all(math.isfinite(x) for x in (
            self.submitted, self.queue_seconds, self.startup_seconds,
            self.execution_seconds, self.overall_seconds,
        )):
            raise ValueError("non-finite deadline")

    def expired_reason(self, now: float) -> str | None:
        if now >= self.submitted + self.overall_seconds:
            return "overall_timeout"
        if self.worker_started is not None:
            return "execution_timeout" if now >= self.worker_started + self.execution_seconds else None
        if self.scheduler_started is not None:
            return "startup_timeout" if now >= self.scheduler_started + self.startup_seconds else None
        if self.scheduler_pending and now >= self.submitted + self.queue_seconds:
            return "queue_timeout"
        return None
