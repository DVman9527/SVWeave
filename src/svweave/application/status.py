"""``svw_status`` service (plan 14.3, 15.2, 23.2): honest state, no heavy probes by default.

Backend health is reported as ``NOT_PROBED`` unless this session probed (``probe=True``), a runtime
failure was observed, or ``svweave doctor --probe`` stored a record earlier. READY is never inferred
from "the binary exists".
"""

from __future__ import annotations

from typing import Any

from svweave.application.build import DB_FILENAME
from svweave.application.semantic_common import freshness_of
from svweave.application.session import Session, load_probe
from svweave.domain.envelope import AnalysisCoverage, Envelope, Outcome
from svweave.domain.states import TERMINAL_TRANSACTION_STATES, Completeness, Freshness
from svweave.orchestration.registry import BACKEND_NAMES, CBM
from svweave.security.sandbox import probe_host
from svweave.storage.db import Database

CLIENT_BOUNDARY_MESSAGE = (
    "client boundary not qualified: SVWeave only controls operations that pass through its "
    "gateway; an MCP client with its own shell or file tools can still write the source tree. "
    "Controlled editing exists but is disabled by default; it runs only when both svweave.yaml "
    "and the trusted policy enable it, and the client boundary stays not qualified either way "
    "until a filesystem/process-isolated client is qualified (plan 23.2)."
)


def editing_capability(editing_enabled: bool) -> str:
    """Human-readable editing state for ``svw_status`` capabilities."""
    if editing_enabled:
        return "enabled (svweave.yaml and trusted policy); client boundary not qualified"
    return "disabled by default (needs svweave.yaml and trusted policy); client boundary not qualified"


def client_boundary(editing_enabled: bool = False) -> dict[str, Any]:
    """No client boundary is qualified in this release (``qualified`` is always False).
    ``editing_enabled`` is the EFFECTIVE setting, ``ctx.effective.editing_enabled``: svweave.yaml
    ``editing.enabled`` AND the trusted policy (config/policy.py), the value that
    ``editing.plan.require_editing_enabled`` enforces; never the raw ``ctx.policy`` field. It is
    a separate fact from ``qualified``: editing may be enabled while the boundary is still not
    qualified."""
    return {
        "qualified": False,
        "editing_enabled": bool(editing_enabled),
        "message": CLIENT_BOUNDARY_MESSAGE,
        "tool_annotations_are_not_isolation": True,
    }


def isolation_info() -> dict[str, Any]:
    cap = probe_host()
    return {
        "mode": "bwrap" if cap.available else "degraded_no_isolation",
        "bwrap_available": cap.available,
        "bwrap_version": cap.version,
        "reason": None if cap.available else (cap.reason or "")[:200],
    }


def _backend_view(session: Session, probe: bool) -> dict[str, Any]:
    ctx = session.opts.load()
    out: dict[str, Any] = {}
    source = "none"
    health: dict[str, Any] = {}
    if probe:
        reg = session.registry
        if reg is None:
            err = session.registry_error
            for n in BACKEND_NAMES:
                health[n] = {
                    "status": "UNAVAILABLE",
                    "probed": False,
                    "checked_at": None,
                    "reasons": [f"toolchain unavailable: {err.message if err else 'unknown'}"],
                    "limitations": [],
                    "version": None,
                    "features": {},
                }
            source = "probe_now"
        else:
            health = {n: h.to_dict() for n, h in reg.probe_all().items()}
            session.session_probe = health
            source = "probe_now"
    elif session.session_probe is not None:
        health, source = session.session_probe, "session_probe"
    else:
        health = {}
    persisted = load_probe(ctx.trusted_dir) or {}
    live = session.registry.all_health() if session.registry_created and session.registry else {}
    for n in BACKEND_NAMES:
        h = health.get(n)
        src = source
        if h is None and n in live and (live[n].probed or live[n].error_code):
            h, src = live[n].to_dict(), "session_runtime"
        if h is None and n in persisted:
            h, src = persisted[n], "last_doctor_probe"
        if h is None:
            out[n] = {
                "status": "NOT_PROBED",
                "probed": False,
                "source": "none",
                "note": "no probe has run; READY requires a real fixture query "
                "(use probe=true or `svweave doctor --probe`)",
            }
            continue
        out[n] = {
            "status": h.get("status"),
            "probed": bool(h.get("probed")),
            "source": src,
            "checked_at": h.get("checked_at"),
            "reasons": h.get("reasons", []),
            "limitations": h.get("limitations", []),
            "version": h.get("version"),
            "features": h.get("features", {}),
        }
    return out


def _builds_view(ctx: Any, db: Database, profiles: list[str]) -> list[dict[str, Any]]:
    """Per-profile current/latest build view from ONE read snapshot. A build can publish while
    this runs; a deferred read transaction (WAL) makes the published pointer, the build rows and
    the pointer read inside ``freshness_of`` all see the same instant, so ``current_build`` and
    ``latest_build`` can never describe different moments. Nothing in here writes."""
    if db.conn.in_transaction:  # already inside a caller's transaction: it is the snapshot
        return _builds_view_unlocked(ctx, db, profiles)
    db.conn.execute("BEGIN")  # deferred: the snapshot starts at the first read
    try:
        return _builds_view_unlocked(ctx, db, profiles)
    finally:
        if db.conn.in_transaction:  # SQLite may already have rolled back (e.g. I/O error)
            db.conn.execute("ROLLBACK")  # read-only: nothing to commit, never leaves a txn open


def _builds_view_unlocked(ctx: Any, db: Database, profiles: list[str]) -> list[dict[str, Any]]:
    out = []
    for p in profiles:
        pub = db.published(ctx.workspace_id, p)
        cur = None
        if pub is not None:
            row = db.conn.execute(
                "SELECT * FROM builds WHERE build_id=?", (pub["build_id"],)
            ).fetchone()
            if row is not None:
                cur = {
                    "build_id": row["build_id"],
                    "snapshot_id": row["snapshot_id"],
                    "completeness": row["completeness"],
                    "diagnostic_outcome": row["diagnostic_outcome"],
                    "freshness": freshness_of(ctx, db, row).value,
                    "published_at": pub["published_at"],
                }
        latest = db.conn.execute(
            "SELECT * FROM builds WHERE workspace_id=? AND profile=? "
            "ORDER BY created_at DESC, rowid DESC LIMIT 1",
            (ctx.workspace_id, p),
        ).fetchone()
        entry: dict[str, Any] = {"profile": p, "current_build": cur, "latest_build": None}
        if latest is not None and (cur is None or latest["build_id"] != cur["build_id"]):
            entry["latest_build"] = {
                "build_id": latest["build_id"],
                "completeness": latest["completeness"],
                "diagnostic_outcome": latest["diagnostic_outcome"],
                "freshness": freshness_of(ctx, db, latest).value,
                "note": "not published: only COMPLETE builds become the current build",
            }
        if cur is None:
            entry["note"] = "no published (COMPLETE) build; run svw_build"
        out.append(entry)
    return out


def status(
    session: Session, request_id: str, *, profile: str | None = None, probe: bool = False
) -> Envelope:
    ctx = session.opts.load()
    if profile is not None:
        ctx.profile(profile)
    profiles = [profile] if profile else sorted(ctx.manifest.profiles)
    term = ",".join(f"'{s.value}'" for s in TERMINAL_TRANSACTION_STATES)
    with Database(ctx.state_dir / DB_FILENAME) as db:
        builds = _builds_view(ctx, db, profiles)
        pending = int(
            db.conn.execute(
                f"SELECT COUNT(*) FROM edit_transactions WHERE state NOT IN ({term})"
            ).fetchone()[0]
        )
        active = [
            {"job_id": r["job_id"], "profile": r["profile"], "lifecycle": r["lifecycle"]}
            for r in db.conn.execute(
                "SELECT job_id, profile, lifecycle FROM jobs WHERE lifecycle IN ('QUEUED','RUNNING')"
            )
        ]
        unlinked = int(
            db.conn.execute("SELECT COUNT(*) FROM evidence WHERE link_state='unlinked'").fetchone()[
                0
            ]
        )
    backends = _backend_view(session, probe)
    graph = backends.get(CBM)
    caps = {
        "semantic_queries": "slang static analysis (qualified subset, see docs/supported_features.md)",
        "search": "graph candidates when the graph backend is READY, else literal text fallback",
        "text_fallback": True,
        "graph_backend_state": (graph or {}).get("status", "NOT_PROBED"),
        "editing": editing_capability(bool(ctx.effective.editing_enabled)),
        "eda_compile_simulation_formal": "NOT_RUN unless external evidence was imported",
    }
    freshness = Freshness.UNKNOWN
    if profile and builds and builds[0]["current_build"]:
        freshness = Freshness(builds[0]["current_build"]["freshness"])
    boundary = client_boundary(bool(ctx.effective.editing_enabled))
    warnings = [boundary["message"]]
    if any(b["current_build"] is None for b in builds):
        warnings.append("some profiles have no published COMPLETE build")
    return Envelope(
        request_id=request_id,
        outcome=Outcome.OK,
        workspace_id=ctx.workspace_id,
        profile=profile,
        freshness=freshness,
        analysis_coverage=AnalysisCoverage(
            completeness=Completeness.COMPLETE,
            scope="workspace_status",
            basis="metadata_read",
            limitations=[
                "Backend health is NOT_PROBED unless a real probe ran; process presence is not health.",
                "Build freshness compares snapshot inventory with the current workspace.",
            ],
        ),
        data={
            "workspace_id": ctx.workspace_id,
            "isolation": isolation_info(),
            "client_boundary": boundary,
            "backends": backends,
            "capabilities": caps,
            "profiles": builds,
            "pending_edits": pending,
            "active_jobs": active,
            "unlinked_evidence": unlinked,
            "probe_requested": probe,
        },
        warnings=warnings,
    )
