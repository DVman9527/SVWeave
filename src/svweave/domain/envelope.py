"""Common result envelope (plan 14.5)."""

from __future__ import annotations

import uuid
from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from svweave.domain.errors import BLOCKING_CODES, SvwError
from svweave.domain.states import Completeness, EvidenceKind, Freshness

# Output envelope only. Manifest/config/semantic schema versions are independent.
# 1.1 adds reference_coverage; strict 1.0 output-schema consumers must upgrade.
SCHEMA_VERSION = "1.1"


class Outcome(StrEnum):
    OK = "OK"
    PARTIAL = "PARTIAL"
    BLOCKED = "BLOCKED"
    ERROR = "ERROR"


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ReferenceObservations(_Strict):
    """Recorded occurrences, not an exhaustive census of omitted source references."""

    unsupported_nodes_project: int = Field(ge=0)
    unsupported_nodes_external: int = Field(ge=0)
    dropped_references_project: int = Field(ge=0)
    dropped_references_external: int = Field(ge=0)
    unresolved_references: int = Field(ge=0)
    dynamic_references: int = Field(ge=0)
    scope_gaps: dict[str, int]
    inactive_regions: dict[str, int]


class ReferenceCoverage(_Strict):
    """HDL-R01 v1: coverage of the reference index, independent of build success.

    A COMPLETE build can have a PARTIAL reference index. Zero observed gaps is not
    evidence of exhaustive traversal. This contract never authorizes negative reference,
    impact, unused-symbol or safe-deletion conclusions, nor complete-rename claims.
    """

    contract_version: Literal["1"] = "1"
    status: Literal["PARTIAL", "UNKNOWN"]
    features: dict[str, Literal["PARTIAL", "UNSUPPORTED", "UNKNOWN"]]
    scopes: dict[str, Literal["PARTIAL", "NOT_SEARCHED", "UNKNOWN"]]
    observations: ReferenceObservations | None = None
    query: Literal["references", "impact", "rename_preview"] | None = None
    searched_scope: str | None = None
    depth_limit: int | None = Field(default=None, ge=1)
    result_truncated: bool = False
    absence_proof_supported: Literal[False] = False
    blockers: list[str] = Field(default_factory=list)


class AnalysisCoverage(_Strict):
    completeness: Completeness
    scope: str | None = None
    basis: str | None = None
    unresolved_count: int = 0
    limitations: list[str] = Field(default_factory=list)
    # Additive/defaulted: historical envelopes without this field still validate.
    reference_coverage: ReferenceCoverage | None = None


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
