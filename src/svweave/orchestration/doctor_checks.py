"""``svweave doctor`` backend section (plan 7.2 / 7.4). Wire with ``register_check(backend_checks)``.

Static checks are cheap and always run. Real fixture probes (~1 min, launches cbm and Serena) run
only with ``probe=True``; without a probe backend health is reported as not proven, never READY.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from svweave.application.doctor import CheckResult
from svweave.domain.errors import ErrorCode, SvwError
from svweave.domain.states import BackendAvailability
from svweave.orchestration.registry import BACKEND_NAMES, BackendRegistry
from svweave.orchestration.toolchain import Toolchain, sha256_file
from svweave.security.sandbox import probe_host

_STATUS = {
    BackendAvailability.READY: "PASS",
    BackendAvailability.DEGRADED: "WARN",
    BackendAvailability.UNAVAILABLE: "FAIL",
}


def _fail(name: str, err: SvwError) -> CheckResult:
    return CheckResult(name=name, status="FAIL", code=err.code.value, message=err.message, details=err.details)


def static_checks(tc: Toolchain) -> list[CheckResult]:
    out: list[CheckResult] = []
    out.append(CheckResult(name="toolchain_lock", status="PASS",
                           message=f"{tc.lock.get('qualification_state')} lock at {tc.root.name}/"))
    pins: list[tuple[str, Path, str]] = [
        ("backend:cbm_binary", tc.cbm_binary, tc.cbm_binary_sha256),
        ("backend:verible_ls_binary", tc.verible_ls, tc.verible_ls_sha256),
    ]
    for name, path, want in pins:
        if not path.is_file():
            out.append(CheckResult(name=name, status="FAIL", code=ErrorCode.BACKEND_UNAVAILABLE.value,
                                   message="binary missing", details={"path": path.name}))
            continue
        got = sha256_file(path)
        ok = got == want
        out.append(CheckResult(
            name=name, status="PASS" if ok else "FAIL",
            code=None if ok else ErrorCode.CAPABILITY_MISMATCH.value,
            message="sha256 matches lock" if ok else "sha256 differs from toolchain.lock.json",
            details={} if ok else {"expected": want, "actual": got}))
    serena = tc.serena_exe
    out.append(CheckResult(
        name="backend:serena_exe", status="PASS" if serena.is_file() else "FAIL",
        code=None if serena.is_file() else ErrorCode.BACKEND_UNAVAILABLE.value,
        message=f"serena {tc.serena_version} venv present" if serena.is_file() else "serena venv missing"))
    rep = tc.root / "capability_report.json"
    want_rep = tc.lock.get("capability_report_sha256")
    if rep.is_file() and want_rep:
        same = sha256_file(rep) == want_rep
        out.append(CheckResult(
            name="capability_report", status="PASS" if same else "WARN",
            code=None if same else ErrorCode.CAPABILITY_MISMATCH.value,
            message="capability_report.json matches the lock hash" if same
            else "capability_report.json hash differs from toolchain.lock.json"))
    cap = probe_host()
    out.append(CheckResult(
        name="isolation:bwrap", status="PASS" if cap.available else "FAIL",
        code=None if cap.available else ErrorCode.BACKEND_UNAVAILABLE.value,
        message=f"{cap.version} works unprivileged" if cap.available
        else "bwrap unusable: backends may only run in explicit DEGRADED isolation; editing disabled",
        details={} if cap.available else {"reason": cap.reason}))
    return out


def health_checks(registry: BackendRegistry, *, probe: bool) -> list[CheckResult]:
    out: list[CheckResult] = []
    if probe:
        registry.probe_all()
    for name in BACKEND_NAMES:
        h = registry.health(name)
        if not probe and not h.probed:
            out.append(CheckResult(
                name=f"backend:{name}", status="SKIP", code=None,
                message="not probed (run doctor with --probe: READY needs a real fixture query)"))
            continue
        msg = h.status.value + ("; " + "; ".join(h.reasons) if h.reasons else "")
        out.append(CheckResult(
            name=f"backend:{name}", status=_STATUS[h.status],
            code=h.error_code, message=msg,
            details={"limitations": h.limitations, "features": h.features, "isolation": h.isolation,
                     "version": h.version, "protocol_version": h.protocol_version}))
    return out


def backend_checks(
    dc: Any = None,
    *,
    probe: bool = False,
    toolchain_root: Path | None = None,
    registry: BackendRegistry | None = None,
) -> list[CheckResult]:
    """Doctor ``Check`` plugin. ``dc`` is the doctor context (uses ``dc.options.root`` when given)."""
    try:
        tc = registry.toolchain if registry is not None else Toolchain.discover(toolchain_root)
    except SvwError as e:
        return [_fail("toolchain_lock", e)]
    out = static_checks(tc)
    reg = registry
    own = False
    if reg is None:
        ws = getattr(dc, "workspace", None)
        if ws is not None:
            reg = BackendRegistry.for_workspace(ws, tc)  # original source roots are protected
        else:
            ws_root = Path(dc.options.root) if dc is not None and hasattr(dc, "options") else Path.cwd()
            reg = BackendRegistry(
                tc, ws_root / ".svweave" / "backends", ws_root / ".svweave" / "snapshots",
                protected_paths=(ws_root,),  # no manifest loaded: protect the whole project root
            )
        own = True
    try:
        out.extend(health_checks(reg, probe=probe))
    finally:
        if own:
            reg.shutdown()
    return out
