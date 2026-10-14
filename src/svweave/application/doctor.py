"""`svweave doctor`: pluggable checks (config/policy here; backends and client boundary in
`doctor_wiring`).

Another component can add a backend section with `register_check(fn)`; each check receives a
DoctorContext and returns CheckResult items. A failing check never aborts the others.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Literal

from pydantic import BaseModel

from svweave.application.options import WorkspaceOptions
from svweave.config.context import (
    LOCAL_FILENAME,
    MANIFEST_FILENAME,
    POLICY_FILENAME,
    WorkspaceContext,
)
from svweave.config.loader import load_local_config, load_manifest, load_policy
from svweave.config.models import LocalConfig, Manifest, Policy
from svweave.config.trusted import load_trusted_policy, policy_path_for
from svweave.domain.envelope import AnalysisCoverage, Envelope, ErrorInfo, Outcome
from svweave.domain.errors import ErrorCode, SvwError
from svweave.domain.states import Completeness, Freshness

Status = Literal["PASS", "FAIL", "WARN", "SKIP"]


class CheckResult(BaseModel):
    name: str
    status: Status
    code: str | None = None
    message: str = ""
    details: dict[str, object] = {}


@dataclass
class DoctorContext:
    options: WorkspaceOptions
    manifest: Manifest | None = None
    local: LocalConfig | None = None
    policy: Policy | None = None
    workspace: WorkspaceContext | None = None
    errors: dict[str, SvwError] = field(default_factory=dict)
    probe: bool = False


Check = Callable[[DoctorContext], list[CheckResult]]
CHECKS: list[Check] = []


def register_check(fn: Check) -> Check:
    CHECKS.append(fn)
    return fn


def _fail(name: str, err: SvwError) -> CheckResult:
    return CheckResult(
        name=name, status="FAIL", code=err.code.value, message=err.message, details=err.details
    )


@register_check
def check_manifest(dc: DoctorContext) -> list[CheckResult]:
    err = dc.errors.get("manifest")
    if err is not None:
        return [_fail("manifest", err)]
    if dc.manifest is None:
        return [
            CheckResult(
                name="manifest",
                status="FAIL",
                code=ErrorCode.INVALID_CONFIG.value,
                message="manifest not loaded",
            )
        ]
    return [
        CheckResult(
            name="manifest", status="PASS", message=f"{len(dc.manifest.profiles)} profile(s) valid"
        )
    ]


@register_check
def check_local(dc: DoctorContext) -> list[CheckResult]:
    err = dc.errors.get("local")
    if err is not None:
        return [_fail("local_config", err)]
    if dc.local is None:
        return [
            CheckResult(name="local_config", status="SKIP", message=f"{LOCAL_FILENAME} not present")
        ]
    return [
        CheckResult(
            name="local_config",
            status="PASS",
            message=f"{len(dc.local.root_mappings)} root mapping(s)",
        )
    ]


@register_check
def check_policy(dc: DoctorContext) -> list[CheckResult]:
    err = dc.errors.get("policy")
    if err is not None:
        return [_fail("policy", err)]
    return [CheckResult(name="policy", status="PASS", message="trusted policy valid")]


@register_check
def check_repo_local_policy(dc: DoctorContext) -> list[CheckResult]:
    local = dc.options.root / ".svweave" / POLICY_FILENAME
    if local.exists():
        return [
            CheckResult(
                name="repo_local_policy",
                status="WARN",
                message=f"{local} exists inside the repo tree and is IGNORED; the trusted "
                "policy lives outside the repository",
            )
        ]
    return []


@register_check
def check_tightening(dc: DoctorContext) -> list[CheckResult]:
    err = dc.errors.get("workspace")
    if err is not None:
        return [_fail("policy_tightening", err)]
    if dc.workspace is None:
        return [
            CheckResult(
                name="policy_tightening", status="SKIP", message="needs valid manifest and policy"
            )
        ]
    return [
        CheckResult(
            name="policy_tightening",
            status="PASS",
            message="repo config stays within trusted policy",
        )
    ]


@register_check
def check_root_mappings(dc: DoctorContext) -> list[CheckResult]:
    if dc.workspace is None:
        return []
    out = []
    for r in dc.workspace.roots:
        if r.abs is None:
            out.append(
                CheckResult(
                    name=f"root_mapping:{r.id}",
                    status="WARN",
                    code=ErrorCode.MISSING_INPUT.value,
                    message=f"external root {r.id!r} has no mapping in {LOCAL_FILENAME}; "
                    "profiles using it will be BLOCKED",
                )
            )
        elif not r.abs.is_dir():
            out.append(
                CheckResult(
                    name=f"root_mapping:{r.id}",
                    status="WARN",
                    code=ErrorCode.MISSING_INPUT.value,
                    message=f"root {r.id!r} directory does not exist",
                )
            )
        else:
            out.append(CheckResult(name=f"root_mapping:{r.id}", status="PASS", message="mapped"))
    return out


def build_context(options: WorkspaceOptions, probe: bool = False) -> DoctorContext:
    dc = DoctorContext(options=options, probe=probe)
    mpath = options.manifest_path or (options.root / MANIFEST_FILENAME)
    try:
        dc.manifest = load_manifest(mpath)
    except SvwError as e:
        dc.errors["manifest"] = e
    lpath = options.local_path or (mpath.parent / LOCAL_FILENAME)
    if lpath.exists():
        try:
            dc.local = load_local_config(lpath)
        except SvwError as e:
            dc.errors["local"] = e
    try:
        if dc.manifest is not None:
            _, dc.policy = load_trusted_policy(mpath, dc.manifest, options.policy_path)
        else:  # still report policy validity when the manifest is broken
            dc.policy = load_policy(policy_path_for(mpath, options.policy_path))
    except SvwError as e:
        dc.errors["policy"] = e
    if not dc.errors:
        try:
            dc.workspace = options.load()
        except SvwError as e:
            dc.errors["workspace"] = e
    return dc


def run_doctor(options: WorkspaceOptions, request_id: str, *, probe: bool = False) -> Envelope:
    from svweave.application import doctor_wiring  # noqa: F401 - registers backend/boundary checks

    dc = build_context(options, probe)
    results: list[CheckResult] = []
    for chk in CHECKS:
        results.extend(chk(dc))
    failures = [r for r in results if r.status == "FAIL"]
    warnings = [f"{r.name}: {r.message}" for r in results if r.status == "WARN"]
    error = None
    if failures:
        f = failures[0]
        error = ErrorInfo(
            code=f.code or ErrorCode.INVALID_CONFIG.value,
            message=f.message,
            details={"check": f.name},
        )
    return Envelope(
        request_id=request_id,
        outcome=Outcome.ERROR if failures else Outcome.OK,
        workspace_id=dc.manifest.project.id if dc.manifest else None,
        freshness=Freshness.UNKNOWN,
        analysis_coverage=AnalysisCoverage(
            completeness=Completeness.BLOCKED if failures else Completeness.COMPLETE,
            scope="config_and_policy",
            basis="static_validation",
            limitations=[
                "Backends are only proven by a real fixture probe (--probe); without it they are "
                "reported as SKIP, never READY.",
                "Editing stays disabled while the client boundary is not qualified.",
            ],
        ),
        data={"checks": [r.model_dump() for r in results]},
        warnings=warnings,
        error=error,
    )
