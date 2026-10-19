"""State dimensions kept strictly separate (plan section 9.5, 10.4, 12.3, 13.3)."""

from __future__ import annotations

from enum import StrEnum


class JobLifecycle(StrEnum):
    QUEUED = "QUEUED"
    RUNNING = "RUNNING"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"
    TIMED_OUT = "TIMED_OUT"


class Completeness(StrEnum):
    COMPLETE = "COMPLETE"
    PARTIAL = "PARTIAL"
    BLOCKED = "BLOCKED"


class Freshness(StrEnum):
    CURRENT = "CURRENT"
    STALE = "STALE"
    HISTORICAL = "HISTORICAL"
    UNKNOWN = "UNKNOWN"


class DiagnosticOutcome(StrEnum):
    NO_ERRORS = "NO_ERRORS"
    HAS_ERRORS = "HAS_ERRORS"
    NOT_AVAILABLE = "NOT_AVAILABLE"


class BackendAvailability(StrEnum):
    READY = "READY"
    DEGRADED = "DEGRADED"
    UNAVAILABLE = "UNAVAILABLE"


class EvidenceKind(StrEnum):
    SOURCE_CANDIDATE = "SOURCE_CANDIDATE"
    LSP_RESOLVED = "LSP_RESOLVED"
    HDL_STATIC_RESOLVED = "HDL_STATIC_RESOLVED"
    RUNTIME_OBSERVED = "RUNTIME_OBSERVED"
    EXTERNAL_DECLARED = "EXTERNAL_DECLARED"
    UNRESOLVED = "UNRESOLVED"


class ResolutionStatus(StrEnum):
    RESOLVED_STATIC = "resolved_static"
    CANDIDATE_SOURCE = "candidate_source"
    DYNAMIC_UNRESOLVED = "dynamic_unresolved"
    UNSUPPORTED = "unsupported"
    INACTIVE_FOR_PROFILE = "inactive_for_profile"


class TransactionState(StrEnum):
    PREPARED = "PREPARED"
    CANDIDATE_READY = "CANDIDATE_READY"
    VALIDATING = "VALIDATING"
    VALIDATED = "VALIDATED"
    AWAITING_APPROVAL = "AWAITING_APPROVAL"
    APPROVED = "APPROVED"
    COMMITTING = "COMMITTING"
    APPLIED = "APPLIED"
    RECOVERY_REQUIRED = "RECOVERY_REQUIRED"
    POST_APPLY_VERIFIED = "POST_APPLY_VERIFIED"
    ABORTED = "ABORTED"
    EXPIRED = "EXPIRED"
    CONFLICT = "CONFLICT"
    VALIDATION_FAILED = "VALIDATION_FAILED"


#: States that close a transaction (no further writes expected). Everything else
#: is "pending" and protects its snapshots from GC (plan 9.6).
TERMINAL_TRANSACTION_STATES: frozenset[TransactionState] = frozenset(
    {
        TransactionState.POST_APPLY_VERIFIED,
        TransactionState.ABORTED,
        TransactionState.EXPIRED,
        TransactionState.CONFLICT,
        TransactionState.VALIDATION_FAILED,
    }
)

#: Job states that count as "active" for GC protection.
ACTIVE_JOB_STATES: frozenset[JobLifecycle] = frozenset({JobLifecycle.QUEUED, JobLifecycle.RUNNING})
