"""Job manager for ``svw_build`` / ``svw_job`` (plan 14.2, 14.3, 9.5; test API-03).

* ``submit_build`` validates the profile synchronously, registers a QUEUED row in the ``jobs``
  table and returns the job id immediately; the build itself runs in ONE background thread using
  the existing :func:`run_build` use case (it stays the only writer of build results);
* at most one build runs per manager (low-RAM host); a second request for the SAME profile joins
  the running job, a different profile is refused with ``RESOURCE_LIMIT`` (no hidden queue, no
  retries, no infinite background work);
* ``cancel`` kills the whole worker process tree through the existing ``WorkerHandle``;
* lifecycle, timings and the final result envelope are persisted, so ``svw_job`` also answers
  for jobs finished earlier; jobs whose owner process died are marked FAILED ("orphaned").
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

from svweave.adapters.slang.runner import WorkerHandle
from svweave.application.build import DB_FILENAME, run_build
from svweave.application.events import EventLog, read_events
from svweave.application.evidence import record_static_for
from svweave.application.options import WorkspaceOptions
from svweave.config.context import WorkspaceContext
from svweave.domain.envelope import Envelope, ErrorInfo, Outcome, error_envelope
from svweave.domain.errors import ErrorCode, SvwError
from svweave.domain.ids import new_job_id
from svweave.domain.states import ACTIVE_JOB_STATES, JobLifecycle
from svweave.storage.db import Database
from svweave.storage.lock import WorkspaceBusy, build_lock

log = logging.getLogger("svweave.gateway")
BuildFn = Callable[..., Envelope]
CANCEL_WAIT_S = 5.0
SETTLE_WAIT_S = 3.0
ACTIVE = tuple(s.value for s in ACTIVE_JOB_STATES)


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def _pid_alive(pid: int | None) -> bool:
    if not pid:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


class JobManager:
    def __init__(
        self,
        opts: WorkspaceOptions,
        *,
        events: EventLog | None = None,
        build_fn: BuildFn = run_build,
        test_fault: str | None = None,
    ) -> None:
        """``build_fn`` / ``test_fault`` are injection points for tests, never request data."""
        self.opts = opts
        self.events = events
        self._build_fn = build_fn
        self._test_fault = test_fault
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._active_id: str | None = None
        self._active_profile: str | None = None
        self._handle: WorkerHandle | None = None
        self._owned: set[str] = set()

    # ------------------------------------------------------------------ helpers
    def _db(self, ctx: WorkspaceContext) -> Database:
        return Database(ctx.state_dir / DB_FILENAME)

    def _emit(self, event: str, **kw: Any) -> None:
        if self.events is not None:
            self.events.emit("jobs", event, **kw)

    def sweep_orphans(self, ctx: WorkspaceContext, db: Database) -> int:
        """Mark active build jobs of dead gateway processes FAILED (no zombie 'RUNNING')."""
        n = 0
        rows = db.conn.execute(
            f"SELECT job_id, owner_pid FROM jobs WHERE kind='build' AND lifecycle IN "
            f"({','.join('?' * len(ACTIVE))})",
            ACTIVE,
        ).fetchall()
        for r in rows:
            if r["job_id"] in self._owned or r["owner_pid"] == os.getpid():
                continue
            if not _pid_alive(r["owner_pid"]):
                db.finish_job(
                    r["job_id"],
                    JobLifecycle.FAILED,
                    error="orphaned: the process that owned this job exited",
                )
                n += 1
        return n

    # ------------------------------------------------------------------ submit
    def submit_build(
        self,
        profile: str,
        request_id: str,
        *,
        reuse: bool = True,
        expected_snapshot_id: str | None = None,
    ) -> dict[str, Any]:
        ctx = self.opts.load()
        name, _ = ctx.profile(profile)  # INVALID_CONFIG now, not inside the thread
        with self._lock, self._db(ctx) as db:
            self.sweep_orphans(ctx, db)
            if self._thread is not None and self._thread.is_alive() and self._active_id:
                if self._active_profile == name:
                    return {**self._row(db, self._active_id), "already_running": True}
                raise SvwError(
                    ErrorCode.RESOURCE_LIMIT,
                    "another build job is active; only one build runs at a time",
                    {"active_job_id": self._active_id, "active_profile": self._active_profile},
                )
            try:  # a CLI build (or another process) may hold the workspace build lock
                with build_lock(ctx.state_dir):
                    pass
            except WorkspaceBusy as e:
                raise SvwError(
                    ErrorCode.RESOURCE_LIMIT,
                    "another build is running in this workspace",
                    {"reason": "build_in_progress"},
                ) from e
            job_id = new_job_id()
            with db.transaction() as c:  # BEGIN IMMEDIATE: check and insert are one atomic step
                for r in c.execute(
                    f"SELECT job_id, owner_pid FROM jobs WHERE kind='build' AND lifecycle IN "
                    f"({','.join('?' * len(ACTIVE))})",
                    ACTIVE,
                ).fetchall():
                    if r["job_id"] not in self._owned and _pid_alive(r["owner_pid"]):
                        raise SvwError(
                            ErrorCode.RESOURCE_LIMIT,
                            "a build job of another gateway process is active",
                            {"active_job_id": r["job_id"]},
                        )
                c.execute(
                    "INSERT INTO jobs(job_id, build_id, snapshot_id, lifecycle, kind, profile, "
                    "request_id, owner_pid, created_at) VALUES (?,?,?,?,?,?,?,?,?)",
                    (
                        job_id,
                        None,
                        None,
                        JobLifecycle.QUEUED.value,
                        "build",
                        name,
                        request_id,
                        os.getpid(),
                        _now(),
                    ),
                )
            handle = WorkerHandle()
            self._handle, self._active_id, self._active_profile = handle, job_id, name
            self._owned.add(job_id)
            t = threading.Thread(
                target=self._run,
                args=(job_id, name, request_id, reuse, expected_snapshot_id, handle),
                name=f"svw-job-{job_id[:8]}",
                daemon=True,
            )
            self._thread = t
            self._emit("queued", request_id=request_id, job_id=job_id)
            t.start()
            return {**self._row(db, job_id), "already_running": False}

    # ------------------------------------------------------------------ worker thread
    def _run(
        self,
        job_id: str,
        profile: str,
        request_id: str,
        reuse: bool,
        expected: str | None,
        handle: WorkerHandle,
    ) -> None:
        t0 = time.monotonic()
        env: Envelope | None = None
        build_id: str | None = None
        try:
            ctx = self.opts.load()
            with self._db(ctx) as db:
                if handle.cancelled:
                    db.finish_job(job_id, JobLifecycle.CANCELLED, error="cancelled before start")
                    self._emit(
                        "cancelled", request_id=request_id, job_id=job_id, outcome="cancelled"
                    )
                    return
            env = self._build_fn(
                self.opts,
                profile,
                request_id,
                reuse=reuse,
                handle=handle,
                test_fault=self._test_fault,
                job_id=job_id,
                expected_snapshot_id=expected,
            )
            build_id = env.build_id
            if expected is not None and env.snapshot_id != expected:  # belt and braces
                raise SvwError(
                    ErrorCode.STALE_SNAPSHOT,
                    "the built snapshot differs from expected_snapshot_id",
                    {"expected_snapshot_id": expected, "built_snapshot_id": env.snapshot_id},
                )
            with self._db(ctx) as db:
                row = db.job(job_id)
                if row is not None and row["lifecycle"] in ACTIVE:  # reuse path: no worker job
                    db.finish_job(job_id, JobLifecycle.SUCCEEDED, exit_code=0)
                    db.conn.execute(
                        "UPDATE jobs SET build_id=?, snapshot_id=? WHERE job_id=?",
                        (env.build_id, env.snapshot_id, job_id),
                    )
            record_static_for(self.opts, build_id)
        except SvwError as e:
            env = error_envelope(
                e,
                request_id=request_id,
                profile=profile,
                workspace_id=self.opts.workspace_id(),
            )
            # a build for the wrong snapshot must not stay SUCCEEDED even if the worker finished
            self._fail(
                job_id, f"{e.code.value}: {e.message}", force=e.code is ErrorCode.STALE_SNAPSHOT
            )
        except BaseException as e:  # noqa: BLE001 - a job thread must never die silently
            env = Envelope(
                request_id=request_id,
                outcome=Outcome.ERROR,
                workspace_id=self.opts.workspace_id(),
                profile=profile,
                error=ErrorInfo(
                    code=ErrorCode.INTERNAL.value, message=f"internal error ({type(e).__name__})"
                ),
            )
            self._fail(job_id, f"INTERNAL: {type(e).__name__}")
        finally:
            life = self._store_result(job_id, env)
            self._emit(
                "finished",
                request_id=request_id,
                job_id=job_id,
                build_id=build_id,
                duration_s=time.monotonic() - t0,
                outcome=str(life),
                error_code=env.error.code if env is not None and env.error else None,
            )
            with self._lock:
                self._owned.discard(job_id)
                if self._active_id == job_id:
                    self._active_id = self._active_profile = self._handle = None

    def _store_result(self, job_id: str, env: Envelope | None) -> str:
        """Persist the result envelope; sqlite contention is retried, never silently dropped."""
        last: Exception | None = None
        for attempt in range(8):
            try:
                with self._db(self.opts.load()) as db:
                    if env is not None:
                        db.conn.execute(
                            "UPDATE jobs SET result=? WHERE job_id=?",
                            (env.model_dump_json(), job_id),
                        )
                    row = db.job(job_id)
                    return str(row["lifecycle"]) if row else "UNKNOWN"
            except Exception as e:  # noqa: BLE001 - retried, then logged
                last = e
                time.sleep(0.1 * (attempt + 1))
        log.error("job %s: could not store its result (%s)", job_id, type(last).__name__)
        return "UNKNOWN"

    def _fail(self, job_id: str, msg: str, force: bool = False) -> None:
        for attempt in range(8):
            try:
                with self._db(self.opts.load()) as db:
                    row = db.job(job_id)
                    if row is not None and (force or row["lifecycle"] in ACTIVE):
                        db.finish_job(job_id, JobLifecycle.FAILED, error=msg[:500])
                return
            except Exception:  # noqa: BLE001
                time.sleep(0.1 * (attempt + 1))
        log.error("job %s: could not record failure", job_id)

    # ------------------------------------------------------------------ read / cancel
    def _row(self, db: Database, job_id: str) -> dict[str, Any]:
        r = db.job(job_id)
        if r is None:
            raise SvwError(
                ErrorCode.MISSING_INPUT, f"unknown job {job_id}", {"reason": "job_missing"}
            )
        data: dict[str, Any] = {
            "job_id": r["job_id"],
            "kind": r["kind"] or "build",
            "profile": r["profile"],
            "lifecycle": r["lifecycle"],
            "build_id": r["build_id"],
            "snapshot_id": r["snapshot_id"],
            "created_at": r["created_at"],
            "started_at": r["started_at"],
            "finished_at": r["finished_at"],
            "exit_code": r["exit_code"],
            "error": r["error"],
            "cancel_requested": bool(r["cancel_requested"]),
            "owned_by_this_session": r["job_id"] in self._owned,
        }
        return data

    def status(self, job_id: str) -> dict[str, Any]:
        """Job state. The build use case marks a job terminal slightly before this manager
        stores the result summary, so a terminal job of this session without a summary is
        re-read (bounded) instead of being reported half-finished."""
        deadline = time.monotonic() + SETTLE_WAIT_S
        while True:
            data = self._status_once(job_id)
            half = (
                data["lifecycle"] not in ACTIVE
                and "result" not in data
                and data["owned_by_this_session"]
            )
            if not half or time.monotonic() > deadline:
                return data
            time.sleep(0.02)

    def _status_once(self, job_id: str) -> dict[str, Any]:
        ctx = self.opts.load()
        with self._db(ctx) as db:
            self.sweep_orphans(ctx, db)
            data = self._row(db, job_id)
            r = db.job(job_id)
            assert r is not None
            run_events = read_events(ctx.state_dir / "runs" / job_id / "events.jsonl", 200)
            data["progress"] = {
                "events": len(run_events),
                "last_event": (
                    {k: run_events[-1].get(k) for k in ("component", "event", "outcome", "ts")}
                    if run_events
                    else None
                ),
            }
            if r["result"]:
                res = json.loads(r["result"])
                d = res.get("data") or {}
                data["result"] = {
                    "outcome": res.get("outcome"),
                    "build_id": res.get("build_id"),
                    "completeness": (res.get("analysis_coverage") or {}).get("completeness"),
                    "diagnostic_outcome": d.get("diagnostic_outcome"),
                    "freshness": res.get("freshness"),
                    "published": d.get("published"),
                    "reused": d.get("reused"),
                    "counts": d.get("counts"),
                    "error": res.get("error"),
                    "warnings": res.get("warnings", []),
                }
            return data

    def cancel(self, job_id: str) -> dict[str, Any]:
        ctx = self.opts.load()
        with self._lock:
            owned = job_id in self._owned
            handle = self._handle if self._active_id == job_id else None
            thread = self._thread
        with self._db(ctx) as db:
            r = db.job(job_id)
            if r is None:
                raise SvwError(
                    ErrorCode.MISSING_INPUT, f"unknown job {job_id}", {"reason": "job_missing"}
                )
            if r["lifecycle"] not in ACTIVE:
                return {
                    **self.status(job_id),
                    "cancel_accepted": False,
                    "cancel_note": "job already finished",
                }
            if not owned:
                raise SvwError(
                    ErrorCode.POLICY_DENIED,
                    "job is owned by another process and cannot be cancelled here",
                    {"reason": "job_not_owned", "job_id": job_id},
                )
            if self._worker_finished(ctx, job_id):
                return {
                    **self.status(job_id),
                    "cancel_accepted": False,
                    "cancel_note": "the worker already finished; the job is publishing its result "
                    "and can no longer be cancelled",
                }
            db.conn.execute("UPDATE jobs SET cancel_requested=1 WHERE job_id=?", (job_id,))
        self._emit("cancel_requested", job_id=job_id)
        if handle is not None:
            handle.cancel()  # kills the worker process group
        if thread is not None:
            thread.join(CANCEL_WAIT_S)
        data = self.status(job_id)
        if data["lifecycle"] in ACTIVE and thread is not None and not thread.is_alive():
            with self._db(ctx) as db:
                db.finish_job(job_id, JobLifecycle.CANCELLED, error="cancelled")
            data = self.status(job_id)
        if data["lifecycle"] in ACTIVE:
            return {**data, "cancel_accepted": True, "cancel_note": "cancel requested; still stopping"}
        if data["lifecycle"] != JobLifecycle.CANCELLED.value:
            with self._db(ctx) as db:
                db.conn.execute("UPDATE jobs SET cancel_requested=0 WHERE job_id=?", (job_id,))
            return {
                **self.status(job_id),
                "cancel_accepted": False,
                "cancel_note": "the job finished before the cancel took effect",
            }
        return {**data, "cancel_accepted": True}

    @staticmethod
    def _worker_finished(ctx: WorkspaceContext, job_id: str) -> bool:
        evs = read_events(ctx.state_dir / "runs" / job_id / "events.jsonl", 500)
        return any(e.get("component") == "worker" and e.get("event") == "finish" for e in evs)

    def wait(self, job_id: str, timeout: float = 120.0) -> None:
        """Test/CLI helper: block until the job thread ended."""
        t = self._thread
        if t is not None and t.is_alive() and self._active_id == job_id:
            t.join(timeout)

    def shutdown(self) -> None:
        """Kill any running worker tree and wait briefly (server exit)."""
        with self._lock:
            h, t = self._handle, self._thread
        if h is not None:
            h.cancel()
        if t is not None:
            t.join(CANCEL_WAIT_S)
