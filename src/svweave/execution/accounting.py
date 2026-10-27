"""Slurm 22.05.9 sacct display-field decoder: diagnostic evidence, never authority.

The JSON input is SVWeave's own single-row field envelope, NOT native sacct JSON.
No scheduler command is run and no Ownership/SchedulerObservation is constructed.
See docs/srun_accounting.md for source semantics, provenance and excluded formats.
"""
from __future__ import annotations

import hashlib
import json
import re
from typing import Annotated, Any, Final, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator

MAX_PAYLOAD_BYTES = 8192
MAX_STATE_CHARS = 128
SLURM_VERSION: Final = "22.05.9"
CONTRACT: Final = "svweave-sacct-fields-v1"
_TOKEN = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_.-]{0,63}\Z")
_ID = re.compile(r"([1-9][0-9]{0,7})(?:\.(0|[1-9][0-9]{0,9}))?\Z")
_PAIR = re.compile(r"(0|[1-9][0-9]{0,2}):(0|[1-9][0-9]{0,2})\Z")
_BY = re.compile(r"([A-Z_]+) by (0|[1-9][0-9]{0,9})\Z")
_FLAGS = frozenset({
    "COMPLETING", "STAGE_OUT", "CONFIGURING", "RESIZING", "REQUEUED", "REQUEUE_FED",
    "REQUEUE_HOLD", "SPECIAL_EXIT", "STOPPED", "REVOKED", "RESV_DEL_HOLD", "SIGNALING",
})
_BASE = frozenset({
    "PENDING", "RUNNING", "SUSPENDED", "COMPLETED", "CANCELLED", "FAILED", "TIMEOUT",
    "NODE_FAIL", "PREEMPTED", "BOOT_FAIL", "DEADLINE", "OUT_OF_MEMORY",
})
_FAILURES = _BASE - {"PENDING", "RUNNING", "SUSPENDED", "COMPLETED"}


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)


def _regular_id(value: str) -> Literal["job", "step"]:
    match = _ID.fullmatch(value)
    if match is None or int(match[1]) > 0x03FFFFFF:
        raise ValueError("only canonical single-cluster, non-array, non-heterogeneous numeric job IDs are supported")
    if match[2] is not None:
        if int(match[2]) >= 0xFFFFFFF0:
            raise ValueError("reserved or unsupported step ID")
        return "step"
    return "job"


class AccountingScope(_Strict):
    """Expected lexical row correlation, NOT established scheduler ownership."""
    cluster: str = Field(min_length=1, max_length=64)
    job_id: str = Field(min_length=1, max_length=32)

    @model_validator(mode="after")
    def _validate(self) -> AccountingScope:
        if not _TOKEN.fullmatch(self.cluster) or self.cluster.lower() == "all":
            raise ValueError("exact single cluster required")
        _regular_id(self.job_id)
        return self


class SacctFields(_Strict):
    contract: Literal["svweave-sacct-fields-v1"]
    slurm_version: Literal["22.05.9"]
    source: Literal["sacct_job_fields", "sacct_step_fields"]
    scope: AccountingScope
    # Require both fields: numeric JobIDRaw alone hides array/heterogeneous identity.
    job_id_raw: str = Field(min_length=1, max_length=32)
    state: str | None = Field(default=None, max_length=4096)
    exit_code: str | None = Field(default=None, max_length=1024)
    derived_exit_code: str | None = Field(default=None, max_length=1024)

    @model_validator(mode="after")
    def _validate(self) -> SacctFields:
        kind = _regular_id(self.job_id_raw)
        if self.job_id_raw != self.scope.job_id or self.source != f"sacct_{kind}_fields":
            raise ValueError("full JobID, JobIDRaw and declared record scope must match")
        if kind == "step" and self.derived_exit_code not in (None, ""):
            raise ValueError("DerivedExitCode is only supported on job rows")
        return self


class StateEvidence(_Strict):
    status: Literal["PARSED", "MISSING", "UNKNOWN", "TRUNCATED"]
    display: str | None
    normalized: str | None = None
    category: Literal["pending", "active", "transition", "completion_reported", "failure_reported", "unknown"] = "unknown"
    reason: str
    cancellation_uid_reported: int | None = None


class ExitEvidence(_Strict):
    status: Literal["PARSED", "MISSING", "INVALID"]
    display: str | None
    printed_exit_status: int | None = None
    printed_signal: int | None = None
    meaning: Literal["printed_components_only"] = "printed_components_only"


class AccountingEvidence(_Strict):
    contract: Literal["svweave-sacct-fields-v1"] = CONTRACT
    slurm_version: Literal["22.05.9"] = SLURM_VERSION
    interpretation: Literal["diagnostic_only"] = "diagnostic_only"
    evidence_kind: Literal["EXTERNAL_DECLARED"] = "EXTERNAL_DECLARED"
    requested_scope: AccountingScope
    recorded_scope: AccountingScope | None
    input_sha256: Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{64}$")] | None
    accounting_available: bool
    parse_status: Literal["PARSED", "INCOMPLETE"]
    state: StateEvidence
    exit_code: ExitEvidence
    derived_exit_code: ExitEvidence
    remote_cleanup: Literal["UNKNOWN"] = "UNKNOWN"
    worker_success: Literal["NOT_ESTABLISHED"] = "NOT_ESTABLISHED"
    limitations: tuple[str, ...]


def _display(value: str | None) -> str | None:
    if value is None:
        return None
    # Escape control characters in diagnostic rendering and cap output independently.
    return "".join(f"\\u{ord(c):04x}" if ord(c) < 32 or ord(c) == 127 else c for c in value[:128])[:256]


def _state(value: str | None) -> StateEvidence:
    shown = _display(value)
    if value is None or not value.strip(" "):
        return StateEvidence(status="MISSING", display=shown, reason="state_unavailable")
    if len(value) > MAX_STATE_CHARS or any(ord(c) < 32 or ord(c) == 127 for c in value):
        return StateEvidence(status="UNKNOWN", display=shown, reason="invalid_or_overlong_state")
    text = value.strip(" ")
    if text.endswith("+"):
        return StateEvidence(status="TRUNCATED", display=shown, reason="truncated_state")
    uid = None
    decoration = _BY.fullmatch(text)
    if decoration is not None:
        text, uid = decoration[1], int(decoration[2])
        if (text != "CANCELLED" and text not in _FLAGS) or uid >= 0xFFFFFFFF:
            return StateEvidence(status="UNKNOWN", display=shown, reason="unsupported_state_decoration")
    if text not in _BASE | _FLAGS:
        return StateEvidence(status="UNKNOWN", display=shown, reason="unknown_state")
    category: Literal["pending", "active", "transition", "completion_reported", "failure_reported", "unknown"]
    if text in _FLAGS:
        category = "transition"
    elif text == "PENDING":
        category = "pending"
    elif text == "COMPLETED":
        category = "completion_reported"
    elif text in _FAILURES:
        category = "failure_reported"
    else:
        category = "active"
    return StateEvidence(status="PARSED", display=shown, normalized=text, category=category,
                         reason="scheduler_reported_" + ("oom" if text == "OUT_OF_MEMORY" else text.lower()),
                         cancellation_uid_reported=uid)


def _exit(value: str | None, *, derived: bool = False) -> ExitEvidence:
    shown = _display(value)
    if value is None or not value.strip(" "):
        return ExitEvidence(status="MISSING", display=shown)
    match = _PAIR.fullmatch(value.strip(" ")) if len(value) <= 32 else None
    if match is None:
        return ExitEvidence(status="INVALID", display=shown)
    status, signal = int(match[1]), int(match[2])
    if status > (255 if derived else 127) or signal >= 127 or (status and signal):
        return ExitEvidence(status="INVALID", display=shown)
    return ExitEvidence(status="PARSED", display=shown, printed_exit_status=status, printed_signal=signal)


def _unique(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate accounting field")
        result[key] = value
    return result


def _constant(value: str) -> None:
    raise ValueError(f"invalid JSON constant {value}")


def decode_accounting(payload: bytes | None, *, expected_scope: AccountingScope) -> AccountingEvidence:
    """Decode one bounded SVWeave field envelope; None/empty bytes mean missing accounting.

    Scope equality is correlation only. No source authenticity, owner identity, temporal
    freshness, remote termination, result validity or successful analysis is established.
    Invalid serialization/version/identity fails closed with ValueError; malformed field
    values remain independently UNKNOWN/INVALID rather than defaulting to zero/success.
    """
    limitations = [
        "External declared display fields only; no scheduler query or source authentication was performed.",
        "Row correlation, state and cancellation attribution do not establish ownership, cleanup or worker success.",
        "Slurm 22.05.9 sacct ExitCode folds exited statuses >=128; original wait status cannot be reconstructed.",
        "DerivedExitCode 0:0 can represent unavailable internal data; missing accounting is never zero usage.",
    ]
    if payload is not None and (not isinstance(payload, bytes) or len(payload) > MAX_PAYLOAD_BYTES):
        raise ValueError("accounting payload must be bytes within the 8192-byte limit")
    if payload is None or payload == b"":
        return AccountingEvidence(requested_scope=expected_scope, recorded_scope=None,
                                  input_sha256=None, accounting_available=False, parse_status="INCOMPLETE",
                                  state=_state(None), exit_code=_exit(None), derived_exit_code=_exit(None),
                                  limitations=tuple(limitations))
    try:
        raw = json.loads(payload.decode("utf-8"), object_pairs_hook=_unique, parse_constant=_constant)
        fields = SacctFields.model_validate(raw)
    except (ValueError, RecursionError) as error:
        raise ValueError("invalid or unsupported accounting field envelope") from error
    if fields.scope != expected_scope:
        raise ValueError("accounting scope does not match requested lexical scope")
    state, exit_code = _state(fields.state), _exit(fields.exit_code)
    derived = _exit(fields.derived_exit_code, derived=True)
    if exit_code.printed_signal or derived.printed_signal:
        limitations.append("A reported signal, including SIGKILL (9), does not establish OOM or any particular termination cause.")
    if state.normalized == "PREEMPTED" or state.normalized in _FLAGS:
        limitations.append("Flag/historical state does not establish final termination; requeue or later execution remains possible.")
    parsed = state.status == "PARSED" and exit_code.status == "PARSED" and derived.status != "INVALID"
    return AccountingEvidence(requested_scope=expected_scope, recorded_scope=fields.scope,
                              input_sha256=hashlib.sha256(payload).hexdigest(), accounting_available=True,
                              parse_status="PARSED" if parsed else "INCOMPLETE", state=state,
                              exit_code=exit_code, derived_exit_code=derived, limitations=tuple(limitations))
