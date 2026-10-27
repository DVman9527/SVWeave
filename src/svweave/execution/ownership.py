"""Exact-target cancellation decisions. This module never contacts a scheduler."""

from __future__ import annotations

import math
import threading

from svweave.domain.errors import ErrorCode, SvwError
from svweave.execution.models import Ownership, SchedulerObservation


def cancellation_target(
    expected: Ownership, observed: SchedulerObservation, *, now: float, max_age_s: float = 5.0
) -> str:
    """Reject reused IDs, cross-cluster/UID records and stale observations before any scancel."""
    age = now - observed.observed_at_monotonic
    if (not all(math.isfinite(value) for value in (now, max_age_s, observed.observed_at_monotonic))
            or max_age_s <= 0 or age < 0 or age > max_age_s or expected != observed.ownership):
        raise SvwError(
            ErrorCode.POLICY_DENIED,
            "scheduler ownership is not freshly and exactly verified",
            {"reason": "ownership_unverified", "cleanup_confirmed": False},
        )
    return expected.target


class CancellationIntent:
    """Thread-safe, idempotent intent; not a claim that a remote process has stopped."""

    def __init__(self) -> None:
        self._event = threading.Event()

    def cancel(self) -> None:
        self._event.set()

    @property
    def cancelled(self) -> bool:
        return self._event.is_set()
