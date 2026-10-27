"""Opt-in execution configuration. Repository data never grants scheduler authority.

Profiles are operator-maintained in the existing out-of-repository trusted policy.
Loading/previewing configuration is offline; authorization is checked before launch.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import Annotated, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    field_validator,
    model_validator,
)

from svweave.domain.errors import ErrorCode, SvwError
from svweave.domain.ids import canonical_hash

Token = Annotated[str, StringConstraints(pattern=r"^[A-Za-z0-9_][A-Za-z0-9_.-]*$")]
Digest = Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{64}$")]
JobId = Annotated[str, StringConstraints(pattern=r"^[1-9][0-9]*$")]


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)


class ExecutionResources(_Strict):
    nodes: Literal[1] = 1
    tasks: Literal[1] = 1
    cpus_per_task: int = Field(default=1, ge=1, le=256)
    memory_mib: int = Field(default=8192, ge=64, le=1048576)
    walltime_seconds: int = Field(default=120, ge=1, le=604800)

    @field_validator("nodes", "tasks", mode="before")
    @classmethod
    def _exact_integer(cls, value: object) -> object:
        # Literal equality otherwise accepts True and 1.0 even under strict=True.
        if type(value) is not int:
            raise ValueError("nodes and tasks must be exact integers")
        return value


class ExecutionDeadlines(_Strict):
    queue_seconds: int = Field(default=120, ge=1, le=604800)
    startup_seconds: int = Field(default=30, ge=1, le=3600)
    cancel_grace_seconds: int = Field(default=10, ge=1, le=600)
    overall_seconds: int = Field(default=300, ge=1, le=1209600)

    @model_validator(mode="after")
    def _valid(self) -> ExecutionDeadlines:
        if self.overall_seconds <= self.cancel_grace_seconds:
            raise ValueError("overall deadline must reserve time before cleanup")
        return self


class ExecutionSelection(_Strict):
    provider: Literal["local", "srun"] = "local"
    site_profile: Token | None = None
    allocation_mode: Literal["existing_allocation", "new_allocation"] = "existing_allocation"
    allocation_id: JobId | None = None

    @model_validator(mode="after")
    def _valid(self) -> ExecutionSelection:
        if self.provider == "local":
            if (
                self.site_profile
                or self.allocation_id
                or self.allocation_mode != "existing_allocation"
            ):
                raise ValueError("local execution does not accept Slurm selection fields")
        elif not self.site_profile:
            raise ValueError("srun requires an explicit site_profile")
        elif self.allocation_mode == "existing_allocation" and not self.allocation_id:
            raise ValueError("existing_allocation requires an explicit allocation_id")
        elif self.allocation_mode == "new_allocation" and self.allocation_id:
            raise ValueError("new_allocation must not specify allocation_id")
        return self


class SlurmSiteProfile(_Strict):
    slurm_version: Literal["22.05.9"] = "22.05.9"
    cluster: Token
    srun_path: str
    scancel_path: str
    scontrol_path: str
    python_path: str
    bootstrap_path: str
    shared_root: str
    executable_hashes: dict[str, Digest] = Field(default_factory=dict)
    resources: ExecutionResources = Field(default_factory=ExecutionResources)
    deadlines: ExecutionDeadlines = Field(default_factory=ExecutionDeadlines)
    partition: Token | None = None
    account: Token | None = None
    qos: Token | None = None
    constraint: Token | None = None
    reservation: Token | None = None
    qualification_digest: Digest | None = None
    scheduler_query_contract: Literal["unqualified"] = "unqualified"

    @model_validator(mode="after")
    def _valid(self) -> SlurmSiteProfile:
        if self.cluster.casefold() == "all":
            raise ValueError("cluster must name exactly one site, not the Slurm all selector")
        for field in (
            "srun_path",
            "scancel_path",
            "scontrol_path",
            "python_path",
            "bootstrap_path",
            "shared_root",
        ):
            value = getattr(self, field)
            p = PurePosixPath(value)
            if (
                not p.is_absolute()
                or any(ord(c) < 32 or ord(c) == 127 for c in value)
                or ".." in p.parts
                or str(p) != value
            ):
                raise ValueError(
                    f"{field} must be a canonical absolute path without control characters"
                )
        if self.shared_root in ("/", "/home", "/root", "/tmp"):
            raise ValueError("shared_root must be a dedicated directory, not a broad system root")
        return self


class ExecutionPolicy(_Strict):
    scheduler_network_approved: bool = False
    site_profiles: dict[Token, SlurmSiteProfile] = Field(default_factory=dict)


@dataclass(frozen=True)
class ResolvedExecution:
    selection: ExecutionSelection
    site_profile: SlurmSiteProfile | None
    scheduler_network_approved: bool
    fingerprint: str

    @property
    def provider(self) -> Literal["local", "srun"]:
        return self.selection.provider

    def require_srun_authorized(self) -> SlurmSiteProfile:
        if self.provider != "srun" or not self.scheduler_network_approved:
            raise SvwError(
                ErrorCode.POLICY_DENIED,
                "scheduler control-plane network exception is not approved",
                {"reason": "scheduler_network_not_approved", "submitted": False},
            )
        if self.site_profile is None:
            raise SvwError(
                ErrorCode.POLICY_DENIED,
                "execution profile is not in trusted policy",
                {"reason": "execution_profile_not_approved", "submitted": False},
            )
        if self.selection.allocation_mode == "new_allocation" and os.environ.get("SLURM_JOB_ID"):
            raise SvwError(
                ErrorCode.POLICY_DENIED,
                "new allocation inside an ambient allocation is ambiguous",
                {"reason": "ambient_allocation", "submitted": False},
            )
        # A digest alone is not qualification: a supported, reviewed query contract is required.
        raise SvwError(
            ErrorCode.BACKEND_UNAVAILABLE,
            "Slurm site ownership/query contract is not qualified",
            {"reason": "slurm_site_unqualified", "submitted": False, "qualification": "BLOCKED"},
        )


def resolve_execution(selection: ExecutionSelection, policy: ExecutionPolicy) -> ResolvedExecution:
    site = policy.site_profiles.get(selection.site_profile) if selection.site_profile else None
    identity = {
        "selection": selection.model_dump(mode="json"),
        "site_profile": site.model_dump(mode="json") if site else None,
        "scheduler_network_approved": policy.scheduler_network_approved,
    }
    return ResolvedExecution(
        selection, site, policy.scheduler_network_approved, canonical_hash(identity)
    )
