"""Registers the backend section and the client-boundary item of ``svweave doctor``.

Imported by ``run_doctor``. Backends are only started with ``--probe`` (a real fixture query is
the only proof of READY); the probe result is stored so later ``svw_status`` calls can show the
last-known state honestly. Every backend launched here is shut down before returning.
"""

from __future__ import annotations

from svweave.application.doctor import CheckResult, DoctorContext, register_check
from svweave.application.session import save_probe
from svweave.application.status import CLIENT_BOUNDARY_MESSAGE
from svweave.domain.errors import ErrorCode, SvwError
from svweave.orchestration.doctor_checks import backend_checks
from svweave.orchestration.registry import BackendRegistry


@register_check
def backend_section(dc: DoctorContext) -> list[CheckResult]:
    ws = dc.workspace
    reg: BackendRegistry | None = None
    if ws is not None:
        try:
            reg = BackendRegistry.for_workspace(ws)
        except SvwError:
            reg = None  # backend_checks reports the missing toolchain itself
    try:
        results = backend_checks(dc, probe=dc.probe, registry=reg)
        if dc.probe and reg is not None and ws is not None:
            save_probe(ws.trusted_dir, {n: h.to_dict() for n, h in reg.all_health().items()})
        return results
    finally:
        if reg is not None:
            reg.shutdown()


@register_check
def client_boundary(dc: DoctorContext) -> list[CheckResult]:
    out = [
        CheckResult(
            name="client_boundary",
            status="WARN",
            code=ErrorCode.POLICY_DENIED.value,
            message=CLIENT_BOUNDARY_MESSAGE,
            details={"qualified": False, "editing_enabled": False},
        )
    ]
    if dc.policy is not None and getattr(dc.policy, "editing_enabled", False):
        out.append(
            CheckResult(
                name="editing",
                status="WARN",
                code=ErrorCode.POLICY_DENIED.value,
                message="policy.editing_enabled is set but editing stays disabled: it is not "
                "implemented before M4 and the client boundary is not qualified",
            )
        )
    return out
