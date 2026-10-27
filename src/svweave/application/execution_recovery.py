"""Offline recovery inspection shared by CLI and potential future read-only clients."""
from __future__ import annotations

from svweave.application.options import WorkspaceOptions
from svweave.domain.envelope import AnalysisCoverage, Envelope, ErrorInfo, Outcome, Provenance
from svweave.domain.errors import ErrorCode
from svweave.domain.states import Completeness
from svweave.execution.recovery import inspect_recovery


def inspect_execution(opts: WorkspaceOptions, request_id: str, *, limit: int = 200) -> Envelope:
    ctx = opts.load()
    report = inspect_recovery(ctx.state_dir, limit=limit)
    blocked = report["workspace_blocked"]
    return Envelope(
        request_id=request_id, workspace_id=ctx.workspace_id,
        outcome=Outcome.BLOCKED if blocked else Outcome.OK,
        analysis_coverage=AnalysisCoverage(
            completeness=Completeness.PARTIAL, scope="local_execution_recovery_records",
            limitations=report["limitations"],
        ),
        provenance=Provenance(backend="svweave-execution-ledger"), data=report,
        error=ErrorInfo(code=ErrorCode.RECOVERY_REQUIRED.value,
                        message="recorded remote execution requires reconciliation; inspection made no changes")
        if blocked else None,
    )
