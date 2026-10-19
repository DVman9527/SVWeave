"""Transaction state machine (plan 12.3). Every state change goes through :func:`check_transition`;
an illegal transition is a programming error (INTERNAL), never silently accepted."""

from __future__ import annotations

from svweave.domain.errors import ErrorCode, SvwError
from svweave.domain.states import TransactionState as S

#: States from which "any not-yet-applied state" may abort / expire / conflict / fail validation.
_ABANDON = frozenset({S.ABORTED, S.EXPIRED, S.CONFLICT, S.VALIDATION_FAILED})

LEGAL: dict[S, frozenset[S]] = {
    S.PREPARED: frozenset({S.CANDIDATE_READY}) | _ABANDON,
    S.CANDIDATE_READY: frozenset({S.VALIDATING}) | _ABANDON,
    S.VALIDATING: frozenset({S.VALIDATED}) | _ABANDON,
    S.VALIDATED: frozenset({S.AWAITING_APPROVAL}) | _ABANDON,
    S.AWAITING_APPROVAL: frozenset({S.APPROVED}) | _ABANDON,
    # APPROVED -> RECOVERY_REQUIRED: a crash after the journal was written but before COMMITTING
    # was recorded; the journal makes it COMMITTING-equivalent.
    S.APPROVED: frozenset({S.COMMITTING, S.RECOVERY_REQUIRED}) | _ABANDON,
    # COMMITTING may already have written files: it can only finish or require recovery.
    S.COMMITTING: frozenset({S.APPLIED, S.RECOVERY_REQUIRED}),
    S.APPLIED: frozenset({S.POST_APPLY_VERIFIED}),
    # Recovery: roll forward (APPLIED), nothing applied / rolled back (ABORTED) or the human's
    # version was kept (CONFLICT).
    S.RECOVERY_REQUIRED: frozenset({S.APPLIED, S.ABORTED, S.CONFLICT}),
    S.POST_APPLY_VERIFIED: frozenset(),
    S.ABORTED: frozenset(),
    S.EXPIRED: frozenset(),
    S.CONFLICT: frozenset(),
    S.VALIDATION_FAILED: frozenset(),
}

#: States in which the workspace may differ from the base snapshot because of THIS transaction.
APPLY_STATES = frozenset(
    {S.COMMITTING, S.APPLIED, S.POST_APPLY_VERIFIED, S.RECOVERY_REQUIRED}
)
#: States that block every new prepare/commit (an unfinished write exists).
BLOCKING_STATES = frozenset({S.COMMITTING, S.RECOVERY_REQUIRED})


def check_transition(frm: S, to: S) -> None:
    if to not in LEGAL[frm]:
        raise SvwError(
            ErrorCode.INTERNAL,
            f"illegal transaction transition {frm.value} -> {to.value}",
            {"from": frm.value, "to": to.value},
        )
