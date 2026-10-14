"""Error catalog (plan 14.6) and CLI exit-code mapping (plan 15.4)."""

from __future__ import annotations

from enum import StrEnum
from typing import Any


class ErrorCode(StrEnum):
    INVALID_CONFIG = "INVALID_CONFIG"
    MISSING_INPUT = "MISSING_INPUT"
    UNSUPPORTED_OPTION = "UNSUPPORTED_OPTION"
    PATH_NOT_ALLOWED = "PATH_NOT_ALLOWED"
    WORKSPACE_UNSTABLE = "WORKSPACE_UNSTABLE"
    STALE_SNAPSHOT = "STALE_SNAPSHOT"
    AMBIGUOUS_SYMBOL = "AMBIGUOUS_SYMBOL"
    PARTIAL_ANALYSIS = "PARTIAL_ANALYSIS"
    BACKEND_UNAVAILABLE = "BACKEND_UNAVAILABLE"
    CAPABILITY_MISMATCH = "CAPABILITY_MISMATCH"
    BUILD_TIMEOUT = "BUILD_TIMEOUT"
    RESOURCE_LIMIT = "RESOURCE_LIMIT"
    EDIT_CONFLICT = "EDIT_CONFLICT"
    APPROVAL_REQUIRED = "APPROVAL_REQUIRED"
    APPROVAL_EXPIRED = "APPROVAL_EXPIRED"
    POLICY_DENIED = "POLICY_DENIED"
    RECOVERY_REQUIRED = "RECOVERY_REQUIRED"
    OUTPUT_LIMIT = "OUTPUT_LIMIT"
    # Not in the 14.6 table; required by test EDT-05 (plan 20.4).
    IDEMPOTENCY_CONFLICT = "IDEMPOTENCY_CONFLICT"
    # Unexpected non-domain exception; details carry no traceback (stderr only).
    INTERNAL = "INTERNAL"


EXIT_OK = 0
EXIT_CONFIG = 2
EXIT_BUILD = 3
EXIT_BACKEND = 4
EXIT_STALE = 5
EXIT_POLICY = 6

_EXIT_BY_CODE: dict[ErrorCode, int] = {
    ErrorCode.INVALID_CONFIG: EXIT_CONFIG,
    ErrorCode.MISSING_INPUT: EXIT_BUILD,
    ErrorCode.UNSUPPORTED_OPTION: EXIT_BUILD,
    ErrorCode.PARTIAL_ANALYSIS: EXIT_BUILD,
    ErrorCode.AMBIGUOUS_SYMBOL: EXIT_BUILD,
    ErrorCode.WORKSPACE_UNSTABLE: EXIT_STALE,
    ErrorCode.INTERNAL: EXIT_BACKEND,
    ErrorCode.BACKEND_UNAVAILABLE: EXIT_BACKEND,
    ErrorCode.CAPABILITY_MISMATCH: EXIT_BACKEND,
    ErrorCode.BUILD_TIMEOUT: EXIT_BACKEND,
    ErrorCode.RESOURCE_LIMIT: EXIT_BACKEND,
    ErrorCode.OUTPUT_LIMIT: EXIT_BACKEND,
    ErrorCode.STALE_SNAPSHOT: EXIT_STALE,
    ErrorCode.EDIT_CONFLICT: EXIT_STALE,
    ErrorCode.RECOVERY_REQUIRED: EXIT_STALE,
    ErrorCode.IDEMPOTENCY_CONFLICT: EXIT_STALE,
    ErrorCode.PATH_NOT_ALLOWED: EXIT_POLICY,
    ErrorCode.POLICY_DENIED: EXIT_POLICY,
    ErrorCode.APPROVAL_REQUIRED: EXIT_POLICY,
    ErrorCode.APPROVAL_EXPIRED: EXIT_POLICY,
}

#: Codes that mean "analysis could not proceed" (BLOCKED) rather than a plain error.
BLOCKING_CODES: frozenset[ErrorCode] = frozenset(
    {ErrorCode.MISSING_INPUT, ErrorCode.UNSUPPORTED_OPTION}
)


def exit_code_for(code: ErrorCode) -> int:
    return _EXIT_BY_CODE[code]


class SvwError(Exception):
    """Domain error carrying a catalog code, message and structured details."""

    def __init__(
        self, code: ErrorCode, message: str, details: dict[str, Any] | None = None
    ) -> None:
        super().__init__(f"{code.value}: {message}")
        self.code = code
        self.message = message
        self.details: dict[str, Any] = details or {}

    @property
    def exit_code(self) -> int:
        return exit_code_for(self.code)
