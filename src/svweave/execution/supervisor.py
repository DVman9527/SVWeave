"""Bounded transport supervisor, independent of site-specific scheduler submission.

The site adapter must create a dedicated launcher and durably record submission BEFORE
calling this function. Callbacks must use exact freshly verified scheduler ownership.
No scheduler adapter is implemented or enabled by this generic transport module.
"""
from __future__ import annotations

import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import BinaryIO, Protocol

from svweave.execution.deadlines import Deadlines
from svweave.execution.ownership import CancellationIntent
from svweave.execution.transport import BoundedStreams, feed_request


class Launcher(Protocol):
    stdin: BinaryIO | None
    stdout: BinaryIO | None
    stderr: BinaryIO | None

    def poll(self) -> int | None: ...


@dataclass(frozen=True)
class TransportOutcome:
    stdout: bytes
    stderr_tail: bytes
    launcher_rc: int | None
    reason: str
    cleanup_confirmed: bool
    io_complete: bool


def supervise(
    launcher: Launcher, payload: bytes, *, deadlines: Deadlines,
    handle: CancellationIntent, stdout_limit: int, stderr_limit: int,
    cancel_grace_seconds: float,
    reconcile: Callable[[bool, float], bool],
    record_cleanup: Callable[[bool], None],
    terminate_launcher: Callable[[], None],
) -> TransportOutcome:
    """Drain bounded stdio, apply phase deadlines and separate cleanup from cancel intent.

    ``reconcile(cancel, deadline)`` must verify all remote descendants terminal, even on
    launcher success. It runs in a bounded daemon thread: a hung scheduler query gives
    UNKNOWN cleanup, never success. The callback must bound each scheduler command
    by the supplied deadline and must never start a cancellation after that deadline. ``record_cleanup`` must durably persist that status
    before local launcher termination; persistence failure propagates after local teardown and leaves the
    caller responsible for remote recovery. The initial durable ledger stays pinned throughout.
    The overall duration includes cleanup: active supervision stops one cancellation grace
    before it, and no reconciliation starts after the total deadline. Primary timeout/exit
    reasons remain independent of the cleanup_confirmed flag.
    """
    if cancel_grace_seconds <= 0:
        raise ValueError("finite positive cancellation grace required")
    import math
    if (not math.isfinite(cancel_grace_seconds)
            or cancel_grace_seconds >= deadlines.overall_seconds):
        raise ValueError("finite cancellation grace smaller than overall duration required")
    if launcher.stdin is None or launcher.stdout is None or launcher.stderr is None:
        raise ValueError("launcher requires all three pipes")
    streams = BoundedStreams(stdout_limit, stderr_limit, handle.cancel)
    failures: list[BaseException] = []

    def guarded(action: Callable[[], None]) -> None:
        try:
            action()
        except BaseException as exc:
            failures.append(exc)
            handle.cancel()

    stdin, stdout, stderr = launcher.stdin, launcher.stdout, launcher.stderr
    threads = [threading.Thread(target=guarded, args=(action,), daemon=True) for action in (
        lambda: feed_request(stdin, payload), lambda: streams.drain_stdout(stdout),
        lambda: streams.drain_stderr(stderr),
    )]
    for thread in threads:
        thread.start()
    reason = "launcher_exit"
    overall_deadline = deadlines.submitted + deadlines.overall_seconds
    active_deadline = overall_deadline - cancel_grace_seconds
    try:
        while launcher.poll() is None:
            now = time.monotonic()
            expired = ("overall_timeout" if now >= active_deadline
                       else deadlines.expired_reason(now))
            if expired is not None:
                reason = expired
                handle.cancel()
                break
            if handle.cancelled:
                reason = "cancelled"
                break
            time.sleep(min(0.01, max(0.0, active_deadline - time.monotonic())))
        if reason == "launcher_exit" and launcher.poll() != 0:
            reason = "launcher_failure"
            handle.cancel()
    except BaseException:
        handle.cancel()
        raise
    finally:
        cleanup: list[bool] = []
        cleanup_deadline = min(
            time.monotonic() + cancel_grace_seconds,
            overall_deadline,
        )

        def verify_cleanup() -> None:
            try:
                cleanup.append(reconcile(handle.cancelled, cleanup_deadline) is True)
            except Exception:
                cleanup.append(False)

        verifier = threading.Thread(target=verify_cleanup, daemon=True)
        if cleanup_deadline > time.monotonic():
            verifier.start()
            verifier.join(max(0.0, cleanup_deadline - time.monotonic()))
        confirmed = bool(cleanup and cleanup[0] and not verifier.is_alive())
        try:
            record_cleanup(confirmed)
        finally:
            # The initial submission ledger stays pinned if persistence fails. Local
            # teardown still has to happen; it is never remote cleanup evidence.
            try:
                terminate_launcher()
            finally:
                for thread in threads:
                    thread.join(max(0.0, cleanup_deadline - time.monotonic()))
    io_complete = not any(thread.is_alive() for thread in threads) and not failures
    if streams.overflow:
        reason = "stdout_overflow"
    elif failures and reason in ("launcher_exit", "cancelled"):
        reason = "stdio_failure"
    elif reason == "launcher_exit":
        if failures:
            reason = "stdio_failure"
        elif not io_complete:
            reason = "stdio_incomplete"
        elif not confirmed:
            reason = "cleanup_unconfirmed"
    returncode = launcher.poll()
    if reason == "launcher_exit" and returncode != 0:
        reason = "launcher_failure"
    return TransportOutcome(
        streams.stdout if io_complete and not handle.cancelled and confirmed and returncode == 0 else b"",
        streams.stderr_tail, returncode, reason, confirmed, io_complete,
    )
