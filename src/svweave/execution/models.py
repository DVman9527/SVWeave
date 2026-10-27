"""Strict transport evidence, separate from public build lifecycle and semantic identity."""

from __future__ import annotations

from enum import StrEnum
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator

Token = Annotated[str, StringConstraints(pattern=r"^[A-Za-z0-9_-]{1,128}$")]
Digest = Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{64}$")]
JobID = Annotated[str, StringConstraints(pattern=r"^[1-9][0-9]{0,17}$")]
StepID = Annotated[str, StringConstraints(pattern=r"^[0-9]{1,18}$")]


class Phase(StrEnum):
    VALIDATING = "VALIDATING"
    SUBMITTING = "SUBMITTING"
    WAITING_RESOURCES = "WAITING_RESOURCES"
    STARTING = "STARTING"
    SANDBOX_VERIFIED = "SANDBOX_VERIFIED"
    WORKER_RUNNING = "WORKER_RUNNING"
    COLLECTING = "COLLECTING"
    VERIFIED = "VERIFIED"
    CANCELLING = "CANCELLING"
    CLEANUP_UNCONFIRMED = "CLEANUP_UNCONFIRMED"
    UNKNOWN = "UNKNOWN"


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)


class Ownership(StrictModel):
    """Identity established by trusted scheduler observation, never stdout or a name."""

    attempt_id: Token
    nonce: Token
    cluster: Token
    uid: int = Field(ge=0)
    job_id: JobID
    step_id: StepID | None = None
    allocation_owned: bool = False
    submitted_at: str = Field(min_length=1, max_length=64)

    @model_validator(mode="after")
    def borrowed_step(self) -> Ownership:
        if not self.allocation_owned and self.step_id is None:
            raise ValueError("borrowed allocation ownership requires an exact numeric step")
        return self

    @property
    def target(self) -> str:
        return self.job_id if self.allocation_owned else f"{self.job_id}.{self.step_id}"


class SchedulerObservation(StrictModel):
    ownership: Ownership
    state: str = Field(min_length=1, max_length=64)
    terminal: bool
    observed_at_monotonic: float = Field(ge=0)


class AttemptRecord(StrictModel):
    schema_version: Literal["1"] = "1"
    attempt_id: Token
    job_id: str = Field(min_length=1, max_length=256)
    snapshot_id: str = Field(min_length=1, max_length=256)
    execution_fingerprint: Digest
    owner_host: Token
    owner_boot_id: Token
    owner_pid: int = Field(gt=0)
    owner_start_identity: Token
    phase: Phase = Phase.VALIDATING
    ownership: Ownership | None = None
    submission_started: bool = False
    cleanup_confirmed: bool = False
    cancel_requested: bool = False
    result_sha256: Digest | None = None

    @model_validator(mode="after")
    def consistent(self) -> AttemptRecord:
        if self.ownership is not None and self.ownership.attempt_id != self.attempt_id:
            raise ValueError("ownership belongs to a different attempt")
        if self.phase == Phase.VERIFIED and (not self.cleanup_confirmed or self.result_sha256 is None):
            raise ValueError("VERIFIED requires result hash and confirmed cleanup")
        return self

    @property
    def pins_inputs(self) -> bool:
        return self.submission_started and not self.cleanup_confirmed
