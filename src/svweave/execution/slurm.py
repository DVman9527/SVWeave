"""Fail-closed Slurm provider entry point and offline typed command preview.

No scheduler transport is enabled until a real site's pending/step ownership query
contract is qualified. A digest, installed srun, or local sandbox probe cannot unlock it.
"""

from __future__ import annotations

import math
from pathlib import Path
from typing import Any

from svweave.adapters.slang.protocol import WorkerRequest
from svweave.adapters.slang.runner import WorkerHandle, WorkerOutcome
from svweave.config.execution import ResolvedExecution
from svweave.domain.errors import ErrorCode, SvwError


def ensure_available(execution: ResolvedExecution) -> None:
    if execution.provider == "srun":
        execution.require_srun_authorized()


def preview_argv(execution: ResolvedExecution, *, request_path: Path) -> list[str]:
    """Offline preview only. It is NOT authority to execute the returned argv."""
    site = execution.site_profile
    if execution.provider != "srun" or site is None:
        raise SvwError(ErrorCode.INVALID_CONFIG, "srun preview needs a trusted site profile")
    root = Path(site.shared_root)
    if (not request_path.is_absolute() or ".." in request_path.parts
            or root not in request_path.parents
            or any(ord(c) < 32 or ord(c) == 127 for c in str(request_path))):
        raise SvwError(ErrorCode.PATH_NOT_ALLOWED, "request must be below the shared staging root")
    resources = site.resources
    argv = [
        site.srun_path, "--nodes=1", "--ntasks=1", "--mpi=none",
        f"--cpus-per-task={resources.cpus_per_task}", f"--mem={resources.memory_mib}M",
        f"--time={math.ceil(resources.walltime_seconds / 60)}", "--export=NONE",
        f"--chdir={site.shared_root}",
    ]
    if execution.selection.allocation_mode == "existing_allocation":
        argv.append(f"--jobid={execution.selection.allocation_id}")
    else:
        argv.append(f"--clusters={site.cluster}")
    for name in ("partition", "account", "qos", "constraint", "reservation"):
        value = getattr(site, name)
        if value is not None:
            argv.append(f"--{name}={value}")
    argv += ["--", site.python_path, "-I", site.bootstrap_path, str(request_path)]
    return argv


def run_worker(
    req: WorkerRequest,
    *,
    execution: ResolvedExecution,
    timeout_s: float,
    memory_mib: int,
    policy: Any = None,
    handle: WorkerHandle | None = None,
    run_dir: Path | None = None,
    job_id: str | None = None,
    snapshot_id: str | None = None,
    toolchain_id: str | None = None,
    test_fault: str | None = None,
) -> WorkerOutcome:
    """Preserve worker outcome shape, without submitting an unqualified remote job."""
    try:
        if execution.provider != "srun":
            raise SvwError(ErrorCode.INVALID_CONFIG, "Slurm provider requires explicit srun selection")
        execution.require_srun_authorized()
    except SvwError as exc:
        return WorkerOutcome(
            kind="exited", error_code=exc.code, message=exc.message,
            isolation="UNAVAILABLE", resource_usage={
                "provider": "srun", "submitted": False, "qualification": "BLOCKED",
                "failure_phase": "VALIDATING", "reason": exc.details.get("reason"),
                "execution_fingerprint": execution.fingerprint,
                "cleanup_confirmed": True, "scheduler_accounting": None,
            },
        )
    # Defense in depth: adding a policy contract does not accidentally enable incomplete transport.
    raise SvwError(ErrorCode.BACKEND_UNAVAILABLE, "qualified Slurm launch adapter is not installed")
