"""JobManager: lifecycle persistence, single active build, cancel, orphans (no real worker)."""

from __future__ import annotations

import threading
import time
from pathlib import Path
from typing import Any

import pytest

from svweave.adapters.slang.runner import WorkerHandle
from svweave.application.build import DB_FILENAME
from svweave.application.events import EventLog, read_events
from svweave.application.jobs import JobManager
from svweave.application.options import WorkspaceOptions
from svweave.domain.envelope import Envelope, Outcome
from svweave.domain.errors import ErrorCode, SvwError
from svweave.domain.states import JobLifecycle
from svweave.storage.db import Database
from tests.conftest import Ws, profile


class FakeBuild:
    """Stands in for run_build: registers the job like the real one and waits to be released."""

    def __init__(self, result: str = "ok") -> None:
        self.release = threading.Event()
        self.started = threading.Event()
        self.result = result
        self.calls: list[dict[str, Any]] = []

    def __call__(self, opts: WorkspaceOptions, profile_name: str, rid: str, **kw: Any) -> Envelope:
        self.calls.append(kw)
        ctx = opts.load()
        handle: WorkerHandle = kw["handle"]
        with Database(ctx.state_dir / DB_FILENAME) as db:
            db.start_job(kw["job_id"], "b" * 64, "s" * 64)
        self.started.set()
        while not self.release.wait(0.02):
            if handle.cancelled:
                with Database(ctx.state_dir / DB_FILENAME) as db:
                    db.finish_job(kw["job_id"], JobLifecycle.CANCELLED, error="cancelled")
                return Envelope(request_id=rid, outcome=Outcome.ERROR, build_id="b" * 64)
        if self.result == "boom":
            raise RuntimeError("worker exploded")
        with Database(ctx.state_dir / DB_FILENAME) as db:
            db.finish_job(kw["job_id"], JobLifecycle.SUCCEEDED, exit_code=0)
        return Envelope(request_id=rid, outcome=Outcome.OK, build_id="b" * 64, snapshot_id="s" * 64)


def wait_done(jm: JobManager, jid: str, timeout: float = 10) -> dict[str, Any]:
    t0 = time.time()
    while time.time() - t0 < timeout:
        st = jm.status(jid)
        if st["lifecycle"] not in ("QUEUED", "RUNNING"):
            return st
        time.sleep(0.02)
    raise AssertionError("job did not finish")


@pytest.fixture
def two_profiles(ws: Ws) -> Ws:
    ws.config({"p": profile(), "q": profile()})
    return ws


def test_returns_immediately_then_succeeds_and_is_persisted(two_profiles: Ws) -> None:
    fb = FakeBuild()
    jm = JobManager(WorkspaceOptions(root=two_profiles.root), build_fn=fb)
    t0 = time.time()
    sub = jm.submit_build("p", "req-1", reuse=False)
    assert time.time() - t0 < 2 and sub["lifecycle"] in ("QUEUED", "RUNNING")
    assert sub["already_running"] is False and sub["profile"] == "p"
    assert fb.started.wait(5)
    assert jm.status(sub["job_id"])["lifecycle"] == "RUNNING"
    fb.release.set()
    st = wait_done(jm, sub["job_id"])
    assert st["lifecycle"] == "SUCCEEDED" and st["build_id"] == "b" * 64
    assert "result" in st, st
    assert st["result"]["outcome"] == "OK"
    assert fb.calls[0]["reuse"] is False and fb.calls[0]["test_fault"] is None
    # a fresh manager (new process) reads the persisted terminal state
    jm2 = JobManager(WorkspaceOptions(root=two_profiles.root))
    assert jm2.status(sub["job_id"])["lifecycle"] == "SUCCEEDED"


def test_one_active_build_same_profile_joins_other_profile_refused(two_profiles: Ws) -> None:
    fb = FakeBuild()
    jm = JobManager(WorkspaceOptions(root=two_profiles.root), build_fn=fb)
    a = jm.submit_build("p", "r1")
    assert fb.started.wait(5)
    again = jm.submit_build("p", "r2")
    assert again["job_id"] == a["job_id"] and again["already_running"] is True
    with pytest.raises(SvwError) as ei:
        jm.submit_build("q", "r3")
    assert ei.value.code is ErrorCode.RESOURCE_LIMIT
    assert ei.value.details["active_job_id"] == a["job_id"]
    assert len(fb.calls) == 1  # no second concurrent/queued run, no retry
    fb.release.set()
    wait_done(jm, a["job_id"])
    deadline = time.time() + 5
    while jm._thread is not None and jm._thread.is_alive() and time.time() < deadline:  # noqa: SLF001
        time.sleep(0.02)
    fb2 = FakeBuild()
    fb2.release.set()
    jm._build_fn = fb2  # noqa: SLF001
    b = jm.submit_build("q", "r4")  # free again
    wait_done(jm, b["job_id"])


def test_unknown_profile_fails_synchronously_without_a_job(two_profiles: Ws) -> None:
    jm = JobManager(WorkspaceOptions(root=two_profiles.root), build_fn=FakeBuild())
    with pytest.raises(SvwError) as ei:
        jm.submit_build("nope", "r")
    assert ei.value.code is ErrorCode.INVALID_CONFIG
    with Database(two_profiles.ctx().state_dir / DB_FILENAME) as db:
        assert db.conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0] == 0


def test_cancel_running_job(two_profiles: Ws) -> None:
    fb = FakeBuild()
    jm = JobManager(WorkspaceOptions(root=two_profiles.root), build_fn=fb)
    a = jm.submit_build("p", "r1")
    assert fb.started.wait(5)
    out = jm.cancel(a["job_id"])
    assert out["cancel_accepted"] is True and out["lifecycle"] == "CANCELLED"
    assert out["cancel_requested"] is True
    again = jm.cancel(a["job_id"])  # finished job: reported, not an error
    assert again["cancel_accepted"] is False


def test_cancel_unknown_and_foreign_jobs(two_profiles: Ws) -> None:
    jm = JobManager(WorkspaceOptions(root=two_profiles.root), build_fn=FakeBuild())
    with pytest.raises(SvwError) as ei:
        jm.cancel("00000000-0000-0000-0000-000000000000")
    assert ei.value.code is ErrorCode.MISSING_INPUT
    ctx = two_profiles.ctx()
    import os

    with Database(ctx.state_dir / DB_FILENAME) as db:
        db.create_job(None, None, JobLifecycle.RUNNING, "11111111-1111-1111-1111-111111111111")
        db.conn.execute(
            "UPDATE jobs SET kind='build', owner_pid=? WHERE job_id=?",
            (os.getppid(), "11111111-1111-1111-1111-111111111111"),
        )
    with pytest.raises(SvwError) as ei:
        jm.cancel("11111111-1111-1111-1111-111111111111")
    assert ei.value.code is ErrorCode.POLICY_DENIED
    # and a live foreign build blocks a new one (single write job per workspace)
    with pytest.raises(SvwError) as ei2:
        jm.submit_build("p", "r")
    assert ei2.value.code is ErrorCode.RESOURCE_LIMIT


def test_worker_exception_becomes_failed_job_and_error_result(two_profiles: Ws) -> None:
    fb = FakeBuild("boom")
    fb.release.set()
    jm = JobManager(WorkspaceOptions(root=two_profiles.root), build_fn=fb)
    a = jm.submit_build("p", "r")
    st = wait_done(jm, a["job_id"])
    assert st["lifecycle"] == "FAILED" and "RuntimeError" in (st["error"] or "")
    assert st["result"]["error"]["code"] == "INTERNAL"
    assert "worker exploded" not in str(st)  # exception text is not leaked to the client


def test_orphaned_jobs_of_dead_processes_are_failed(two_profiles: Ws) -> None:
    ctx = two_profiles.ctx()
    with Database(ctx.state_dir / DB_FILENAME) as db:
        db.create_job(None, None, JobLifecycle.RUNNING, "22222222-2222-2222-2222-222222222222")
        db.conn.execute(
            "UPDATE jobs SET kind='build', owner_pid=? WHERE job_id=?",
            (2**22 + 12345, "22222222-2222-2222-2222-222222222222"),
        )
    jm = JobManager(WorkspaceOptions(root=two_profiles.root), build_fn=FakeBuild())
    st = jm.status("22222222-2222-2222-2222-222222222222")
    assert st["lifecycle"] == "FAILED" and "orphaned" in st["error"]


def test_events_are_logged_with_ids(two_profiles: Ws, tmp_path: Path) -> None:
    ev = EventLog(tmp_path / "e" / "events.jsonl")
    fb = FakeBuild()
    fb.release.set()
    jm = JobManager(WorkspaceOptions(root=two_profiles.root), events=ev, build_fn=fb)
    a = jm.submit_build("p", "req-xyz")
    wait_done(jm, a["job_id"])
    time.sleep(0.2)
    recs = read_events(ev.path)
    assert {"queued", "finished"} <= {r["event"] for r in recs}
    q = next(r for r in recs if r["event"] == "queued")
    assert q["request_id"] == "req-xyz" and q["job_id"] == a["job_id"] and q["component"] == "jobs"
    for r in recs:
        assert set(r) >= {
            "ts",
            "request_id",
            "job_id",
            "build_id",
            "component",
            "event",
            "duration_s",
            "outcome",
            "error_code",
        }


def test_event_log_redacts_paths(tmp_path: Path) -> None:
    ev = EventLog(tmp_path / "events.jsonl")
    ev.emit("x", "y", detail="failed at /home/secret/project/file.sv with API_TOKEN=abcd1234xyz")
    text = ev.path.read_text()
    assert "/home/secret" not in text and "abcd1234xyz" not in text
