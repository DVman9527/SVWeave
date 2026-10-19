"""Backend health model (plan 7.4: READY only after a real fixture query, never process liveness)."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from svweave.domain.states import BackendAvailability


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


@dataclass
class BackendHealth:
    name: str
    status: BackendAvailability
    reasons: list[str] = field(default_factory=list)
    #: Known, accepted gaps (by design / ADR) that do not lower the status but must be shown.
    limitations: list[str] = field(default_factory=list)
    version: str | None = None
    protocol_version: str | None = None
    isolation: str | None = None
    features: dict[str, bool] = field(default_factory=dict)
    #: True only when a real fixture query succeeded in THIS health record's probe.
    probed: bool = False
    error_code: str | None = None
    checked_at: str = field(default_factory=_now)
    details: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "status": self.status.value,
            "reasons": list(self.reasons),
            "limitations": list(self.limitations),
            "version": self.version,
            "protocol_version": self.protocol_version,
            "isolation": self.isolation,
            "features": dict(self.features),
            "probed": self.probed,
            "error_code": self.error_code,
            "checked_at": self.checked_at,
            "details": self.details,
        }


def unknown(name: str) -> BackendHealth:
    return BackendHealth(
        name=name,
        status=BackendAvailability.UNAVAILABLE,
        reasons=["not probed: READY requires a successful real fixture query"],
    )
