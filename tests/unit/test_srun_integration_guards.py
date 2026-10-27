"""SRUN application boundaries; pure unit tests, NOT Slurm qualification."""
from __future__ import annotations

import json
from typing import Any

import pytest

from svweave.application import build, edit
from svweave.application.jobs import JobManager
from svweave.application.options import WorkspaceOptions
from svweave.domain.errors import ErrorCode, SvwError
from svweave.domain.ids import compute_build_id, sha256_hex
from svweave.domain.states import JobLifecycle
from svweave.editing.plan import require_editing_enabled
from svweave.execution.ledger import ExecutionLedger
from svweave.execution.models import AttemptRecord, Phase
from svweave.manifest.resolver import resolve_profile
from svweave.snapshot.capture import capture_snapshot, snapshots_dir
from svweave.storage.db import Database, plan_gc
from tests.conftest import Ws, profile


def remote(ws: Ws) -> WorkspaceOptions:
    ws.write("svweave.local.yaml", json.dumps({"schema_version": "1.0", "execution": {
        "provider": "srun", "site_profile": "reviewed", "allocation_id": "123"}}))
    return WorkspaceOptions(root=ws.root)


def test_SRUN_15_namespace_preserves_local_and_separates_remote() -> None:
    parts = ("a" * 64, "b" * 64, "c" * 64)
    assert compute_build_id(*parts) == sha256_hex(":".join((*parts, "1")))
    remote_id = compute_build_id(*parts, execution_fingerprint="d" * 64)
    assert remote_id != compute_build_id(*parts)
    assert remote_id != compute_build_id(*parts, execution_fingerprint="e" * 64)
    with pytest.raises(SvwError):
        compute_build_id(*parts, execution_fingerprint="bad")


def test_SRUN_21_build_denied_before_snapshot_or_cache(ws: Ws, monkeypatch: pytest.MonkeyPatch) -> None:
    opts = remote(ws)
    def forbidden(*args: Any, **kw: Any) -> None:
        pytest.fail("remote denial must precede snapshot, cache, and launch")
    monkeypatch.setattr(build, "capture_snapshot", forbidden)
    monkeypatch.setattr(build, "compute_toolchain", forbidden)
    monkeypatch.setattr(build, "run_worker", forbidden)
    with pytest.raises(SvwError) as err:
        build.run_build(opts, "p", "request")
    assert err.value.code == ErrorCode.POLICY_DENIED
    assert err.value.details["submitted"] is False
    with pytest.raises(SvwError):
        JobManager(opts).submit_build("p", "request")


def test_SRUN_P0_edit_guard_precedes_baseline_and_candidate(ws: Ws, monkeypatch: pytest.MonkeyPatch) -> None:
    ws.policy(editing_enabled=True)
    ws.config({"p": profile()}, editing={"enabled": True})
    opts = remote(ws)
    ctx = opts.load()
    assert ctx.effective.editing_enabled
    def forbidden(*args: Any, **kw: Any) -> None:
        pytest.fail("srun edit must not start baseline or candidate")
    monkeypatch.setattr(edit, "run_build", forbidden)
    monkeypatch.setattr(edit, "run_build_on_record", forbidden)
    with pytest.raises(SvwError, match="not qualified"):
        require_editing_enabled(ctx)
    with pytest.raises(SvwError, match="not qualified"):
        edit._run_profile_validation(opts, ctx, None, None, "p", {}, "req", [])  # type: ignore[arg-type]
    with pytest.raises(SvwError, match="not qualified"):
        build.run_build_on_record(opts, "p", None, "req", publish=False)  # type: ignore[arg-type]


def record(snapshot: str, job: str = "remote-job") -> AttemptRecord:
    return AttemptRecord(attempt_id="attempt", job_id=job, snapshot_id=snapshot,
                         execution_fingerprint="a" * 64, owner_host="host", owner_boot_id="boot",
                         owner_pid=123, owner_start_identity="start", phase=Phase.UNKNOWN,
                         submission_started=True)


def test_SRUN_17_unknown_cleanup_pins_snapshots_and_orphans(ws: Ws) -> None:
    ctx = ws.ctx()
    rec = capture_snapshot(ctx, resolve_profile(ctx, "p"))
    root = ctx.state_dir / "execution"
    root.mkdir(mode=0o700)
    ExecutionLedger(root).save(record(rec.snapshot_id))
    with Database(ctx.state_dir / "svweave.db") as db:
        db.upsert_workspace(ctx.workspace_id, {}, "a" * 64)
        db.record_snapshot(rec)
        db.start_job("remote-job", "b" * 64, rec.snapshot_id)
        db.conn.execute("UPDATE jobs SET owner_pid=2147483647 WHERE job_id='remote-job'")
        plan = plan_gc(db, snapshots_dir(ctx), keep_last=0)
        assert not plan.delete
        assert JobManager(WorkspaceOptions(root=ws.root)).sweep_orphans(ctx, db) == 0
        assert db.job("remote-job")["lifecycle"] == "RUNNING"
        # Even a FAILED local DB row cannot release remote inputs with UNKNOWN cleanup.
        db.finish_job("remote-job", JobLifecycle.FAILED, error="gateway lost remote state")
        plan = plan_gc(db, snapshots_dir(ctx), keep_last=0)
        assert not plan.delete
        assert plan.keep[rec.snapshot_id] == ["execution_cleanup_unconfirmed"]
        (root / "attempt.json").write_text("broken")
        assert not plan_gc(db, snapshots_dir(ctx), keep_last=0).delete


def test_SRUN_20_remote_registry_never_falls_back_local(ws: Ws) -> None:
    from svweave.orchestration.registry import BackendRegistry
    with pytest.raises(SvwError) as err:
        BackendRegistry.for_workspace(remote(ws).load())
    assert err.value.details["reason"] == "srun_mcp_not_supported"


@pytest.mark.parametrize("operation", ["validate", "approve", "commit"])
def test_SRUN_P0_no_approval_or_apply_under_remote_context(ws: Ws, operation: str) -> None:
    opts = remote(ws)
    kw: dict[str, Any] = {"transaction_id": "txn-" + "a" * 20}
    if operation == "approve":
        kw.update(patch_hash="b" * 64, confirm=lambda _: pytest.fail("must not ask approval"))
    elif operation == "commit":
        kw.update(expected_patch_hash="b" * 64, request_key="request")
    with pytest.raises(SvwError) as err:
        getattr(edit, operation)(opts, "request", **kw)
    assert err.value.details["reason"] == "srun_editing_not_supported"
