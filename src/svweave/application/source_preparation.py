"""Explicit derived-copy preparation; never alters sources or invokes native backends."""

from __future__ import annotations

import hashlib
from pathlib import Path

from svweave.adapters.verilator.protected import SOURCE_LIMIT, prepare_protected_source
from svweave.application.options import WorkspaceOptions
from svweave.application.supplement import _encode, _read
from svweave.domain.envelope import AnalysisCoverage, Envelope, Outcome, Provenance
from svweave.domain.states import Completeness, EvidenceKind
from svweave.security import safeio


def prepare_source(opts: WorkspaceOptions, source: Path, request_id: str) -> Envelope:
    ctx = opts.load()
    original = _read(ctx, source, SOURCE_LIMIT)
    prepared, report = prepare_protected_source(original)
    # Exact input hash + algorithm/report version; original path is not followed downstream.
    report["source_path"] = str(source)
    encoded = _encode(report)
    preparation_id = hashlib.sha256(encoded).hexdigest()
    directory = f".svweave/supplements/prepared/{preparation_id}"
    safeio.ensure_private_dir(ctx.project_root, directory)
    safeio.write_under(ctx.project_root, f"{directory}/source.sv", prepared)
    safeio.write_under(ctx.project_root, f"{directory}/report.json", encoded)
    return Envelope(
        request_id=request_id,
        outcome=Outcome.PARTIAL,
        workspace_id=ctx.workspace_id,
        provenance=Provenance(
            backend="svweave-source-preparation", evidence_kind=EvidenceKind.SOURCE_CANDIDATE
        ),
        analysis_coverage=AnalysisCoverage(
            completeness=Completeness.PARTIAL,
            scope="explicit_source_preparation",
            basis="lexically_detected_protected_envelopes",
            unresolved_count=len(report["gaps"]),
            limitations=report["limitations"],
        ),
        data={
            "preparation_id": preparation_id,
            "prepared_source": f"{directory}/source.sv",
            "report_path": f"{directory}/report.json",
            **report,
        },
    )
