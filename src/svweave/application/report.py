"""``svweave report --build <id> --format json``: the plan 28.2 status template from real data.

Nothing is invented: gate fields that are not computed here say ``NOT_EVALUATED``, EDA compile,
simulation and formal stay ``NOT_RUN`` unless evidence exists (shown as externally declared, never
merged into one PASS), backend integration comes from the last real probe, and the release
decision is ``BLOCKED`` while any gate is unevaluated.
"""

from __future__ import annotations

import hashlib
import platform
import subprocess
from pathlib import Path
from typing import Any

from svweave import __version__
from svweave.application.build import DB_FILENAME
from svweave.application.evidence import states_for_build
from svweave.application.semantic_common import build_envelope, report_of, require_build
from svweave.application.session import Session, load_probe
from svweave.application.status import client_boundary, isolation_info
from svweave.application.toolchain import find_lock
from svweave.domain.envelope import Envelope
from svweave.domain.errors import SvwError
from svweave.orchestration.registry import BACKEND_NAMES
from svweave.storage.db import Database

NOT_EVALUATED = "NOT_EVALUATED"
GATES = ("G0", "G1", "G2", "G3", "G4", "G5")


def _git_commit(root: Path) -> str | None:
    try:
        r = subprocess.run(  # noqa: S603 - fixed argv, no shell
            ["git", "-C", str(root), "rev-parse", "--verify", "-q", "HEAD"],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    out = r.stdout.strip()
    return out if r.returncode == 0 and out else None


def _lock_hash(root: Path) -> str | None:
    try:
        return hashlib.sha256(find_lock(root).read_bytes()).hexdigest()
    except (SvwError, OSError):
        return None


def _ext(state: dict[str, Any]) -> str:
    if state["state"] == "NOT_RUN":
        return "NOT_RUN"
    by = ",".join(f"{k}={v}" for k, v in sorted(state["by_result"].items()))
    return f"EXTERNAL_DECLARED ({by}; not verified by SVWeave)"


def _integration(session: Session, state_dir: Path) -> dict[str, Any]:
    probe = session.session_probe or load_probe(state_dir)
    out: dict[str, Any] = {}
    for n in BACKEND_NAMES:
        h = (probe or {}).get(n)
        out[n] = (
            {"status": "NOT_PROBED"}
            if h is None
            else {
                "status": h.get("status"),
                "probed": bool(h.get("probed")),
                "checked_at": h.get("checked_at"),
                "reasons": h.get("reasons", []),
            }
        )
    return out


def build_report(session: Session, request_id: str, build_id: str) -> Envelope:
    ctx = session.opts.load()
    with Database(ctx.state_dir / DB_FILENAME) as db:
        b = require_build(db, build_id)
        rep = report_of(db, build_id)
        states = states_for_build(db, b)
        stats = rep.get("stats") or {}
        job_id = rep.get("job_id")
        report: dict[str, Any] = {
            "svweave_version": __version__,
            "source_commit": _git_commit(ctx.project_root) or "NO_COMMIT",
            "toolchain_lock_hash": _lock_hash(ctx.project_root),
            "qualified_platform": f"{platform.system()} {platform.machine()} "
            f"python {platform.python_version()} (not a qualification claim)",
            "qualified_feature_subset": "static preview subset in docs/supported_features.md "
            "(qualification itself: " + NOT_EVALUATED + ")",
            "gates": {g: NOT_EVALUATED for g in GATES},
            "p0": {
                "passed": NOT_EVALUATED,
                "failed": NOT_EVALUATED,
                "blocked": NOT_EVALUATED,
                "skipped": NOT_EVALUATED,
            },
            "real_integration": _integration(session, ctx.trusted_dir),
            "editing": {
                "isolation": isolation_info(),
                "enabled": bool(ctx.effective.editing_enabled),
                "enabled_note": "editing exists but is disabled unless svweave.yaml editing.enabled "
                "AND the trusted policy editing_enabled are both true",
                "approval": "IMPLEMENTED (human approval bound to the patch hash; "
                "qualification " + NOT_EVALUATED + ")",
                "recovery": "IMPLEMENTED (write journal + recovery state machine; "
                "qualification " + NOT_EVALUATED + ")",
                "client_boundary": client_boundary(bool(ctx.effective.editing_enabled)),
            },
            "statement_branch_coverage": NOT_EVALUATED,
            "critical_mutation_results": NOT_EVALUATED,
            "benchmark_report": NOT_EVALUATED,
            "eda_compile": _ext(states["eda_compile"]),
            "simulation": _ext(states["simulation"]),
            "formal": _ext(states["formal"]),
            "coverage": "NOT_RUN"
            if states["coverage"]["state"] == "NOT_RUN"
            else "EXTERNAL_DECLARED (not measured by SVWeave)",
            "evidence_states": states,
            "build": {
                "build_id": b["build_id"],
                "snapshot_id": b["snapshot_id"],
                "profile": b["profile"],
                "toolchain_id": b["toolchain_id"],
                "completeness": b["completeness"],
                "diagnostic_outcome": b["diagnostic_outcome"],
                "scope": rep.get("scope"),
                "unresolved_count": rep.get("unresolved_count"),
                "counts": {k: v for k, v in stats.items() if isinstance(v, int)},
                "completeness_reasons": rep.get("reasons", []),
            },
            "known_limitations": [
                *rep.get("limitations", []),
                "Static slang analysis only: no EDA compile, simulation, formal or functional "
                "coverage is represented.",
            ],
            # where the analysis knowingly differs from the original tree / a vendor flow (the
            # manifest's semantic_deviations), NOT the completeness reasons (listed separately)
            "semantic_deviations": stats.get("semantic_deviations") or [],
            "completeness_reasons": rep.get("reasons", []),
            "security_license_review": NOT_EVALUATED,
            "evidence_paths": [
                *([f".svweave/runs/{job_id}/"] if job_id else []),
                "toolchain.lock.json",
                "capability_report.json",
                "docs/supported_features.md",
            ],
            "release_decision": "BLOCKED",
            "release_decision_reason": "gates G0-G5 and the security/license review are "
            + NOT_EVALUATED
            + " by this command",
        }
        return build_envelope(ctx, db, b, request_id, {"report": report})
