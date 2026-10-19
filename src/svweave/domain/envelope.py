"""Common result envelope (plan 14.5)."""

from __future__ import annotations

import uuid
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from svweave.domain.errors import BLOCKING_CODES, SvwError
from svweave.domain.states import Completeness, EvidenceKind, Freshness

SCHEMA_VERSION = "1.0"


class Outcome(StrEnum):
    OK = "OK"
    PARTIAL = "PARTIAL"
    BLOCKED = "BLOCKED"
    ERROR = "ERROR"


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class AnalysisCoverage(_Strict):
    completeness: Completeness
    scope: str | None = None
    basis: str | None = None
    unresolved_count: int = 0
    limitations: list[str] = Field(default_factory=list)


class Provenance(_Strict):
    backend: str | None = None
    toolchain_id: str | None = None
    evidence_kind: EvidenceKind | None = None


class Pagination(_Strict):
    limit: int = 100
    next_cursor: str | None = None
    truncated: bool = False


class ErrorInfo(_Strict):
    code: str
    message: str
    details: dict[str, Any] = Field(default_factory=dict)


class Envelope(_Strict):
    schema_version: str = SCHEMA_VERSION
    request_id: str
    outcome: Outcome
    workspace_id: str | None = None
    snapshot_id: str | None = None
    build_id: str | None = None
    profile: str | None = None
    freshness: Freshness = Freshness.UNKNOWN
    analysis_coverage: AnalysisCoverage | None = None
    provenance: Provenance = Field(default_factory=Provenance)
    data: dict[str, Any] = Field(default_factory=dict)
    pagination: Pagination = Field(default_factory=Pagination)
    warnings: list[str] = Field(default_factory=list)
    error: ErrorInfo | None = None

    def to_json(self, indent: int | None = 2) -> str:
        return self.model_dump_json(indent=indent)


def new_request_id() -> str:
    return f"req-{uuid.uuid4().hex[:12]}"


def error_envelope(
    err: SvwError,
    *,
    request_id: str,
    workspace_id: str | None = None,
    profile: str | None = None,
) -> Envelope:
    blocked = err.code in BLOCKING_CODES
    return Envelope(
        request_id=request_id,
        outcome=Outcome.BLOCKED if blocked else Outcome.ERROR,
        workspace_id=workspace_id,
        profile=profile,
        analysis_coverage=AnalysisCoverage(completeness=Completeness.BLOCKED) if blocked else None,
        error=ErrorInfo(code=err.code.value, message=err.message, details=err.details),
    )
