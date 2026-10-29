"""Opt-in snapshot-I/O update; never partial semantic elaboration (plan 9.6).

The last published profile snapshot is only a materialization hint. Current content and the
existing config/toolchain/execution identity remain the sole semantic-cache authority. Reports
are request-local: neither SnapshotRecord nor historical build/run metadata is changed.
"""

from __future__ import annotations

from typing import Any

from svweave.application.build import DB_FILENAME, _run_build_with_capture
from svweave.application.options import WorkspaceOptions
from svweave.config.context import WorkspaceContext
from svweave.domain.cursor import MAX_RESPONSE_BYTES
from svweave.domain.envelope import Envelope, error_envelope
from svweave.domain.errors import SvwError
from svweave.domain.ids import is_sha256_hex
from svweave.manifest.models import NormalizedManifest
from svweave.snapshot.incremental import capture_snapshot_incremental, load_baseline_record
from svweave.snapshot.models import SnapshotRecord
from svweave.storage.db import Database

MAX_CHANGED_FILES = 200
LIMITATIONS = [
    "Phase 1 optimizes immutable snapshot materialization, not semantic elaboration: an exact "
    "COMPLETE build may be reused; otherwise whole-profile analysis is required.",
    "Git is optional advisory information only. The complete relevant input inventory, including "
    "dirty, untracked, ignored, external-root and lookup candidates, is checked independently.",
    "No speedup is guaranteed: every relevant input is still hashed and verified; clone support "
    "depends on the filesystem.",
]


def _published_baseline(
    ctx: WorkspaceContext, profile: str
) -> tuple[SnapshotRecord | None, dict[str, Any]]:
    """Select a donor by a published pointer, never a caller-supplied filesystem path.

    GC cannot remove it: the build pipeline holds the workspace lock throughout this function,
    capture and semantic build. Corrupt/missing metadata is an unavailable optimization hint, not
    permission to skip current-input capture. The incremental engine separately verifies bytes.
    """
    with Database(ctx.state_dir / DB_FILENAME) as db:
        row = db.conn.execute(
            "SELECT b.build_id, b.snapshot_id, b.config_id, b.completeness, s.gc_state "
            "FROM published_pointer p JOIN builds b ON p.build_id=b.build_id "
            "JOIN snapshots s ON s.snapshot_id=b.snapshot_id "
            "WHERE p.workspace_id=? AND p.profile=? AND b.workspace_id=?",
            (ctx.workspace_id, profile, ctx.workspace_id),
        ).fetchone()
    if row is None:
        return None, {"status": "unavailable", "reason": "no_published_profile_build"}
    sid = str(row["snapshot_id"])
    info: dict[str, Any] = {
        "status": "unavailable", "build_id": str(row["build_id"]),
        "snapshot_id": sid, "config_id": str(row["config_id"]),
    }
    if not is_sha256_hex(sid) or row["completeness"] != "COMPLETE" or row["gc_state"] != "PRESENT":
        return None, {**info, "reason": "invalid_published_baseline"}
    rec = load_baseline_record(ctx, sid)
    if rec is None:
        return None, {**info, "reason": "baseline_metadata_unavailable_or_unsafe"}
    return rec, {**info, "status": "available"}


def _inventory_changes(
    baseline: SnapshotRecord | None, current: SnapshotRecord, baseline_config: str | None,
) -> dict[str, Any]:
    old = {f.logical_path: f for f in baseline.files} if baseline is not None else {}
    new = {f.logical_path: f for f in current.files}
    counts = {"added": 0, "modified": 0, "removed": 0, "metadata_changed": 0}
    items: list[dict[str, str]] = []
    for path in sorted(old.keys() | new.keys()):
        before, after = old.get(path), new.get(path)
        if before is None:
            change = "added"
        elif after is None:
            change = "removed"
        elif (before.sha256, before.size) != (after.sha256, after.size):
            change = "modified"
        elif (before.role, before.symlink_target, before.hardlinked) != (
            after.role, after.symlink_target, after.hardlinked,
        ):
            change = "metadata_changed"
        else:
            continue
        counts[change] += 1
        if len(items) < MAX_CHANGED_FILES:
            items.append({"path": path, "change": change})
    total = sum(counts.values())

    def lookup(rec: SnapshotRecord) -> list[dict[str, Any]]:
        return [item.model_dump(exclude={"original_dir"}) for item in rec.lookup]

    return {
        "baseline_available": baseline is not None,
        "comparison": "published_snapshot" if baseline is not None else "initial_capture",
        "total": total, "counts": counts, "files": items, "truncated": total > len(items),
        "config_changed": baseline_config != current.config_id if baseline_config else None,
        "lookup_changed": lookup(baseline) != lookup(current) if baseline is not None else None,
        "file_order_changed": [f.logical_path for f in baseline.files] != [
            f.logical_path for f in current.files
        ] if baseline is not None else None,
    }


def _bound_report(env: Envelope) -> None:
    """Respect the existing response budget; truncation never implies unchanged omitted files."""
    report = env.data["update"]
    inventories = [report["changes"]["files"], report["git"].get("paths", [])]
    while len(env.to_json(indent=None).encode("utf-8")) > MAX_RESPONSE_BYTES:
        largest = max(inventories, key=len)
        if not largest:
            break  # a pre-existing build envelope, not update inventory, is oversized
        largest.pop()
        if largest is inventories[1]:
            report["git"]["truncated"] = True
        report["truncated"] = True
    if len(report["changes"]["files"]) < report["changes"]["total"]:
        report["changes"]["truncated"] = True
    if report["truncated"]:
        env.pagination.truncated = True


def run_update(
    opts: WorkspaceOptions,
    profile: str | None,
    request_id: str,
    *,
    base: str | None = None,
    git: bool = True,
) -> Envelope:
    """Capture conservatively with optional immutable reuse, then run the ordinary build path.

    ``base`` affects only the advisory Git report, never the snapshot baseline or invalidation.
    There is deliberately no partial-row merge, arbitrary donor path, or new MCP tool/schema.
    """
    report: dict[str, Any] = {
        "limitations": list(LIMITATIONS), "truncated": False, "analysis_scope": "whole_profile",
    }
    captured: SnapshotRecord | None = None

    def capture(ctx: WorkspaceContext, nm: NormalizedManifest) -> SnapshotRecord:
        nonlocal captured
        baseline, baseline_info = _published_baseline(ctx, nm.profile)
        report["baseline"] = baseline_info
        report["baseline_snapshot_id"] = baseline.snapshot_id if baseline is not None else None
        if git:
            from svweave.application.update_git import git_advisory

            report["git"] = git_advisory(ctx, base=base)
        else:
            report["git"] = {
                "status": "DISABLED", "advisory_only": True, "paths": [],
                "limitations": ["Git reporting disabled; any requested Git base is not evaluated. "
                                "Snapshot verification is unchanged."],
            }
        rec, stats = capture_snapshot_incremental(ctx, nm, baseline=baseline)
        report["materialization"] = stats.to_dict()
        report["changes"] = _inventory_changes(baseline, rec, baseline_info.get("config_id"))
        report["truncated"] = report["changes"]["truncated"] or report["git"].get("truncated", False)
        captured = rec
        return rec

    try:
        env = _run_build_with_capture(opts, profile, request_id, capture=capture)
    except SvwError as err:
        if captured is None:
            raise  # preserve pre-capture guard and failure behavior
        env = error_envelope(
            err, request_id=request_id, workspace_id=captured.workspace_id, profile=captured.profile,
        )
        env.snapshot_id = captured.snapshot_id  # UNKNOWN freshness; never a claim of publication
    if env.data.get("reused"):
        report["semantic_action"] = "exact_build_reuse"
        report["semantic_status"] = "reused"
    elif env.data.get("job_lifecycle", env.data.get("lifecycle")) == "SUCCEEDED":
        report["semantic_action"] = "full_profile_analysis"
        report["semantic_status"] = "completed"
    else:
        # A requested job can fail before a sandboxed compiler is even launched. Neither an
        # allocated build/job id nor a request artifact is evidence that analysis actually ran.
        report["semantic_action"] = "full_profile_analysis_requested"
        report["semantic_status"] = "no_validated_result"
    env.data["update"] = report
    _bound_report(env)
    return env
