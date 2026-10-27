"""Independent-review regressions; memory streams/fakes only, no Slurm or sockets."""
from __future__ import annotations

import io
import time

import pytest

from svweave.domain.errors import SvwError
from svweave.execution.deadlines import Deadlines
from svweave.execution.models import Ownership, SchedulerObservation
from svweave.execution.ownership import CancellationIntent, cancellation_target
from svweave.execution.supervisor import supervise


@pytest.mark.parametrize("field", ["now", "max_age_s", "observed_at_monotonic"])
@pytest.mark.parametrize("value", [float("nan"), float("inf"), float("-inf")])
def test_SRUN_11_freshness_requires_finite_clock(field: str, value: float) -> None:
    owner = Ownership(attempt_id="a", nonce="n", cluster="c", uid=1,
                      job_id="123", step_id="4", submitted_at="time")
    observation = SchedulerObservation(ownership=owner, state="RUNNING", terminal=False,
                                       observed_at_monotonic=1.0)
    now, age = 2.0, 5.0
    if field == "now":
        now = value
    elif field == "max_age_s":
        age = value
    else:
        observation = observation.model_copy(update={field: value})
    with pytest.raises(SvwError):
        cancellation_target(owner, observation, now=now, max_age_s=age)


class FakeLauncher:
    def __init__(self, returncode: int | None) -> None:
        self.stdin = io.BytesIO()
        self.stdout = io.BytesIO(b"{}")
        self.stderr = io.BytesIO()
        self.returncode = returncode

    def poll(self) -> int | None:
        return self.returncode


def test_SRUN_07_overall_reserves_cleanup_budget_and_retains_reason() -> None:
    start = time.monotonic()
    callbacks: list[tuple[bool, float]] = []
    def reconcile(cancel: bool, deadline: float) -> bool:
        callbacks.append((cancel, deadline - time.monotonic()))
        return False
    outcome = supervise(
        FakeLauncher(None), b"request", deadlines=Deadlines(start, 5, 5, 5, 0.3),
        handle=CancellationIntent(), stdout_limit=1024, stderr_limit=1024,
        cancel_grace_seconds=0.2, reconcile=reconcile, record_cleanup=lambda _: None,
        terminate_launcher=lambda: None,
    )
    assert callbacks[0][0] is True
    assert 0 < callbacks[0][1] <= 0.2
    assert time.monotonic() - start < 0.5
    assert outcome.reason == "overall_timeout" and not outcome.cleanup_confirmed
    assert outcome.stdout == b""


@pytest.mark.parametrize("rc", [0, 2, -9])
def test_SRUN_12_failed_launcher_requests_remote_cancel_before_cleanup(rc: int) -> None:
    calls: list[bool] = []
    def reconcile(cancel: bool, deadline: float) -> bool:
        calls.append(cancel)
        return False
    outcome = supervise(
        FakeLauncher(rc), b"request", deadlines=Deadlines(time.monotonic(), 5, 5, 5, 10),
        handle=CancellationIntent(), stdout_limit=1024, stderr_limit=1024,
        cancel_grace_seconds=1, reconcile=reconcile, record_cleanup=lambda _: None,
        terminate_launcher=lambda: None,
    )
    assert calls == [rc != 0]
    assert not outcome.cleanup_confirmed and not outcome.stdout
    assert outcome.reason == ("launcher_failure" if rc else "cleanup_unconfirmed")


def test_SRUN_07_already_expired_total_never_starts_late_reconciliation() -> None:
    outcome = supervise(
        FakeLauncher(None), b"request", deadlines=Deadlines(time.monotonic() - 5, 1, 1, 1, 2),
        handle=CancellationIntent(), stdout_limit=1024, stderr_limit=1024,
        cancel_grace_seconds=1, reconcile=lambda *_: pytest.fail("must not start late cancellation"),
        record_cleanup=lambda _: None, terminate_launcher=lambda: None,
    )
    assert outcome.reason == "overall_timeout" and not outcome.cleanup_confirmed


def test_SRUN_13_internal_stdio_error_is_not_operator_cancel() -> None:
    class Broken(io.BytesIO):
        def read(self, size: int = -1) -> bytes:
            raise OSError("read failed")
    launcher = FakeLauncher(None)
    launcher.stdout = Broken()
    calls: list[bool] = []
    outcome = supervise(
        launcher, b"request", deadlines=Deadlines(time.monotonic(), 5, 5, 5, 10),
        handle=CancellationIntent(), stdout_limit=1024, stderr_limit=1024,
        cancel_grace_seconds=1,
        reconcile=lambda cancel, deadline: (calls.append(cancel) or False),
        record_cleanup=lambda _: None, terminate_launcher=lambda: None,
    )
    assert calls == [True]
    assert outcome.reason == "stdio_failure" and not outcome.cleanup_confirmed


def test_SRUN_16_async_execution_change_cannot_start_different_provider(ws, monkeypatch) -> None:
    import json
    import threading

    from svweave.application.jobs import JobManager
    from svweave.application.options import WorkspaceOptions

    started, release = threading.Event(), threading.Event()
    manager = JobManager(WorkspaceOptions(root=ws.root),
                         build_fn=lambda *_a, **_kw: pytest.fail("changed request must not launch"))
    real_run = manager._run
    def paused(*args) -> None:
        started.set()
        assert release.wait(3)
        real_run(*args)
    monkeypatch.setattr(manager, "_run", paused)
    job = manager.submit_build("p", "request")
    assert started.wait(3)
    ws.write("svweave.local.yaml", json.dumps({"schema_version": "1.0", "execution": {
        "provider": "srun", "site_profile": "site", "allocation_id": "123"}}))
    release.set()
    manager.wait(job["job_id"], timeout=3)
    status = manager.status(job["job_id"])
    assert status["lifecycle"] == "FAILED"
    assert status["result"]["error"]["details"]["reason"] == "execution_context_changed"


def test_SRUN_16_direct_build_reload_race_is_rejected(ws, monkeypatch) -> None:
    from svweave.application import build
    from svweave.application.options import WorkspaceOptions

    original = WorkspaceOptions.load
    calls = 0
    def changed(options):
        nonlocal calls
        calls += 1
        if calls == 2:
            ws.policy(execution={"scheduler_network_approved": True})
        return original(options)
    monkeypatch.setattr(WorkspaceOptions, "load", changed)
    monkeypatch.setattr(build, "capture_snapshot", lambda *_: pytest.fail("must reject before snapshot"))
    with pytest.raises(SvwError) as error:
        build.run_build(WorkspaceOptions(root=ws.root), "p", "request")
    assert error.value.details["reason"] == "execution_context_changed"


@pytest.mark.parametrize("owned", [False, True])
@pytest.mark.parametrize("evidence", ["missing", "corrupt", "unknown"])
def test_SRUN_10_failed_remote_attempt_blocks_direct_and_async_new_work(ws, monkeypatch, owned, evidence) -> None:
    from svweave.application import build
    from svweave.application.jobs import JobManager
    from svweave.application.options import WorkspaceOptions
    from svweave.domain.states import JobLifecycle
    from svweave.execution.ledger import ExecutionLedger
    from svweave.execution.models import AttemptRecord
    from svweave.storage.db import Database

    ctx = ws.ctx()
    record = AttemptRecord(attempt_id="attempt", job_id="remote-job", snapshot_id="snapshot",
                           execution_fingerprint="0" * 64, owner_host="host", owner_boot_id="boot",
                           owner_pid=123, owner_start_identity="start", submission_started=True)
    with Database(ctx.state_dir / "svweave.db") as db:
        db.start_job("remote-job", "b" * 64, "snapshot")
        db.finish_job("remote-job", JobLifecycle.FAILED, error="lost remote state")
        db.add_execution_attempt_ref(record)
    if evidence != "missing":
        directory = ctx.state_dir / "execution"
        directory.mkdir(mode=0o700)
        ExecutionLedger(directory).save(record)
        if evidence == "corrupt":
            (directory / "attempt.json").write_text("broken")
    options = WorkspaceOptions(root=ws.root)
    manager = JobManager(options, build_fn=lambda *_a, **_kw: pytest.fail("must not launch"))
    if owned:
        manager._owned.add("remote-job")
    monkeypatch.setattr(build, "capture_snapshot", lambda *_: pytest.fail("must not capture"))
    for action in (lambda: manager.submit_build("p", "new"), lambda: build.run_build(options, "p", "new")):
        with pytest.raises(SvwError) as error:
            action()
        assert error.value.details["reason"] == "execution_reconciliation_required"
