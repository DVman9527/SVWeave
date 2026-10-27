"""SRUN transport unit tests. No Slurm or sandbox process is started."""
from __future__ import annotations

import hashlib
import io
import os
from pathlib import Path

import pytest
from pydantic import ValidationError

from svweave.domain.errors import SvwError
from svweave.execution.deadlines import Deadlines
from svweave.execution.ledger import ExecutionLedger, job_requires_reconciliation, pinned_snapshots
from svweave.execution.models import AttemptRecord, Ownership, Phase, SchedulerObservation
from svweave.execution.ownership import cancellation_target
from svweave.execution.remote_bootstrap import verify_immutable_files
from svweave.execution.transport import BoundedStreams, feed_request


def own(**changes: object) -> Ownership:
    data: dict[str, object] = dict(attempt_id="a", nonce="n", cluster="c", uid=os.getuid(), job_id="12", step_id="3", submitted_at="2026-10-27T00:00:00Z")
    data.update(changes)
    return Ownership.model_validate(data)


def record(**changes: object) -> AttemptRecord:
    data: dict[str, object] = dict(attempt_id="a", job_id="job", snapshot_id="snapshot", execution_fingerprint="0" * 64, owner_host="host", owner_boot_id="boot", owner_pid=1, owner_start_identity="start")
    data.update(changes)
    return AttemptRecord.model_validate(data)


def test_SRUN_09_exact_borrowed_step_only() -> None:
    owner = own()
    observation = SchedulerObservation(ownership=owner, state="RUNNING", terminal=False, observed_at_monotonic=10.0)
    assert cancellation_target(owner, observation, now=11.0) == "12.3"
    for altered in (own(job_id="13"), own(step_id="4"), own(uid=os.getuid() + 1), own(nonce="different"), own(submitted_at="later")):
        with pytest.raises(SvwError):
            cancellation_target(altered, observation, now=11.0)
    with pytest.raises(SvwError):
        cancellation_target(owner, observation, now=20.0)
    with pytest.raises(ValidationError):
        own(step_id="batch")
    with pytest.raises(ValidationError):
        own(step_id=None)


def test_SRUN_17_unknown_cleanup_pins_and_corruption_blocks_gc(tmp_path: Path) -> None:
    root = tmp_path / "execution"
    root.mkdir(mode=0o700)
    ledger = ExecutionLedger(root)
    ledger.save(record(submission_started=True))
    assert pinned_snapshots(tmp_path) == {"snapshot"}
    assert job_requires_reconciliation(tmp_path, "job")
    ledger.save(record(submission_started=True, cleanup_confirmed=True))
    assert pinned_snapshots(tmp_path) == set()
    (root / "a.json").write_text("corrupt")
    assert pinned_snapshots(tmp_path) is None
    assert job_requires_reconciliation(tmp_path, "job")


def test_SRUN_11_ledger_symlink_fifo_rejected(tmp_path: Path) -> None:
    root = tmp_path / "execution"
    root.mkdir(mode=0o700)
    ledger = ExecutionLedger(root)
    os.mkfifo(root / "a.json", mode=0o600)
    with pytest.raises(ValueError):
        ledger.read("a")
    (root / "a.json").unlink()
    (root / "a.json").symlink_to(tmp_path / "other")
    with pytest.raises(OSError):
        ledger.read("a")


def test_SRUN_17_verified_requires_cleanup_and_hash() -> None:
    with pytest.raises(ValidationError):
        record(phase=Phase.VERIFIED)


def test_SRUN_13_bounded_streams_reject_partial_json() -> None:
    cancelled: list[bool] = []
    streams = BoundedStreams(5, 3, lambda: cancelled.append(True))
    streams.drain_stdout(io.BytesIO(b"123456789"))
    streams.drain_stdout(io.BytesIO(b"flood"))
    streams.drain_stderr(io.BytesIO(b"123456"))
    assert streams.overflow and streams.stdout == b""
    assert streams.stderr_tail == b"456" and cancelled == [True]


def test_SRUN_07_deadline_phases_are_not_inferred() -> None:
    deadlines = Deadlines(0, 10, 3, 5, 100)
    assert deadlines.expired_reason(11) is None
    deadlines.scheduler_pending = True
    assert deadlines.expired_reason(11) == "queue_timeout"
    deadlines.scheduler_started = 12
    assert deadlines.expired_reason(16) == "startup_timeout"
    deadlines.worker_started = 14
    assert deadlines.expired_reason(20) == "execution_timeout"
    assert deadlines.expired_reason(101) == "overall_timeout"


def test_SRUN_06_immutable_input_hashes(tmp_path: Path) -> None:
    source = tmp_path / "input"
    source.write_bytes(b"module x; endmodule")
    source.chmod(0o444)
    digest = hashlib.sha256(source.read_bytes()).hexdigest()
    assert verify_immutable_files(tmp_path, {"input": digest}) == {"input": digest}
    with pytest.raises(SvwError):
        verify_immutable_files(tmp_path, {"input": "0" * 64})
    (tmp_path / "link").symlink_to(source)
    with pytest.raises(SvwError):
        verify_immutable_files(tmp_path, {"link": digest})
    source.chmod(0o644)
    with pytest.raises(SvwError):
        verify_immutable_files(tmp_path, {"input": digest})


def test_SRUN_13_feed_short_writes_and_failure() -> None:
    class ShortWriter(io.BytesIO):
        def write(self, payload: bytes | bytearray) -> int:
            return super().write(payload[:2])
        def close(self) -> None:
            self.saved = self.getvalue()
            super().close()
    stream = ShortWriter()
    feed_request(stream, b"abcdef")
    assert stream.saved == b"abcdef" and stream.closed
    class Broken(ShortWriter):
        def write(self, payload: bytes | bytearray) -> int:
            raise BrokenPipeError("disconnected")
    broken = Broken()
    with pytest.raises(BrokenPipeError):
        feed_request(broken, b"request")
    assert broken.closed


def test_SRUN_02_21_no_spawn_even_with_self_asserted_qualification(monkeypatch: pytest.MonkeyPatch) -> None:
    import subprocess

    from svweave.config.execution import (
        ExecutionPolicy,
        ExecutionSelection,
        SlurmSiteProfile,
        resolve_execution,
    )
    from svweave.execution.slurm import ensure_available, preview_argv

    def no_spawn(*args: object, **kwargs: object) -> None:
        pytest.fail("unqualified provider attempted a process")
    monkeypatch.setattr(subprocess, "Popen", no_spawn)
    selection = ExecutionSelection(provider="srun", site_profile="site", allocation_id="123")
    site = SlurmSiteProfile(cluster="cluster", srun_path="/usr/bin/srun", scancel_path="/usr/bin/scancel", scontrol_path="/usr/bin/scontrol", python_path="/shared/runtime/python", bootstrap_path="/shared/runtime/bootstrap.py", shared_root="/shared/input data", qualification_digest="0" * 64)
    for approved in (False, True):
        execution = resolve_execution(selection, ExecutionPolicy(scheduler_network_approved=approved, site_profiles={"site": site}))
        with pytest.raises(SvwError):
            ensure_available(execution)
    argv = preview_argv(execution, request_path=Path("/shared/input data/attempt/request.json"))
    assert "--jobid=123" in argv and "--ntasks=1" in argv and "--export=NONE" in argv
    assert argv[-1] == "/shared/input data/attempt/request.json"
    with pytest.raises(SvwError):
        preview_argv(execution, request_path=Path("/elsewhere/request.json"))


def test_SRUN_11_sidecar_cannot_supply_cancellation_authority() -> None:
    from svweave.execution.remote_bootstrap import ControlRecord, validate_control
    owner = own()
    record = ControlRecord(protocol_version="svweave-slurm-1", attempt_id="a", nonce="n", request_sha256="0" * 64, ownership=owner, bootstrap_sha256="1" * 64, toolchain_sha256="2" * 64, sandbox_verified=True, remote_ready=True)
    args = dict(attempt_id="a", nonce="n", request_sha256="0" * 64, ownership=owner, bootstrap_sha256="1" * 64, toolchain_sha256="2" * 64)
    validate_control(record, **args)
    for field, value in (("nonce", "fake"), ("request_sha256", "3" * 64), ("ownership", own(step_id="4")), ("sandbox_verified", False)):
        with pytest.raises(SvwError):
            validate_control(record.model_copy(update={field: value}), **args)


def test_SRUN_13_supervisor_keeps_cleanup_distinct() -> None:
    import time

    from svweave.execution.ownership import CancellationIntent
    from svweave.execution.supervisor import supervise

    class FakeLauncher:
        def __init__(self) -> None:
            self.stdin = io.BytesIO()
            self.stdout = io.BytesIO(b'{"result": 1}')
            self.stderr = io.BytesIO(b"log")
        def poll(self) -> int:
            return 0
    events: list[object] = []
    result = supervise(FakeLauncher(), b"request", deadlines=Deadlines(time.monotonic(), 1, 1, 1, 2), handle=CancellationIntent(), stdout_limit=1024, stderr_limit=64, cancel_grace_seconds=0.1, reconcile=lambda cancel, deadline: False, record_cleanup=lambda confirmed: events.append(confirmed), terminate_launcher=lambda: events.append("local_terminated"))
    assert not result.cleanup_confirmed and result.stdout == b""
    assert result.reason == "cleanup_unconfirmed" and events == [False, "local_terminated"]


def test_SRUN_07_supervisor_timeout_cancels_then_persists_before_local_stop() -> None:
    import time

    from svweave.execution.ownership import CancellationIntent
    from svweave.execution.supervisor import supervise

    class FakeLauncher:
        stdin = io.BytesIO()
        stdout = io.BytesIO(b"partial")
        stderr = io.BytesIO()
        def poll(self) -> None:
            return None
    events: list[object] = []
    deadlines = Deadlines(time.monotonic() - 10, 1, 1, 1, 20, scheduler_pending=True)
    def reconcile(cancel: bool, deadline: float) -> bool:
        events.append(cancel)
        return True
    result = supervise(FakeLauncher(), b"request", deadlines=deadlines, handle=CancellationIntent(), stdout_limit=1024, stderr_limit=64, cancel_grace_seconds=0.1, reconcile=reconcile, record_cleanup=lambda confirmed: events.append(confirmed), terminate_launcher=lambda: events.append("stop"))
    assert result.reason == "queue_timeout" and result.cleanup_confirmed and result.stdout == b""
    assert events == [True, True, "stop"]


@pytest.mark.parametrize("returncode", [0, 1, 137])
def test_SRUN_12_supervisor_requires_zero_exit_and_complete_drains(returncode: int) -> None:
    import time

    from svweave.execution.ownership import CancellationIntent
    from svweave.execution.supervisor import supervise

    class Delayed(io.BytesIO):
        def read(self, size: int | None = -1) -> bytes:
            time.sleep(0.01)
            return super().read(size)
    class FakeLauncher:
        def __init__(self) -> None:
            self.stdin = io.BytesIO()
            self.stdout = Delayed(b'{"result": 1}')
            self.stderr = io.BytesIO()
        def poll(self) -> int:
            return returncode
    result = supervise(FakeLauncher(), b"request", deadlines=Deadlines(time.monotonic(), 1, 1, 1, 2), handle=CancellationIntent(), stdout_limit=1024, stderr_limit=64, cancel_grace_seconds=0.5, reconcile=lambda cancel, deadline: True, record_cleanup=lambda confirmed: None, terminate_launcher=lambda: None)
    assert result.io_complete and result.cleanup_confirmed
    assert result.stdout == (b'{"result": 1}' if returncode == 0 else b"")
    assert result.reason == ("launcher_exit" if returncode == 0 else "launcher_failure")


def test_SRUN_10_supervisor_tears_down_local_on_ledger_write_failure() -> None:
    import time

    from svweave.execution.ownership import CancellationIntent
    from svweave.execution.supervisor import supervise

    class FakeLauncher:
        stdin = io.BytesIO()
        stdout = io.BytesIO(b"{}")
        stderr = io.BytesIO()
        def poll(self) -> int:
            return 0
    stopped: list[bool] = []
    def fail_record(confirmed: bool) -> None:
        raise OSError("disk full")
    with pytest.raises(OSError, match="disk full"):
        supervise(FakeLauncher(), b"request", deadlines=Deadlines(time.monotonic(), 1, 1, 1, 2), handle=CancellationIntent(), stdout_limit=1024, stderr_limit=64, cancel_grace_seconds=0.1, reconcile=lambda cancel, deadline: False, record_cleanup=fail_record, terminate_launcher=lambda: stopped.append(True))
    assert stopped == [True]


def test_SRUN_04_remote_probe_is_fresh_and_never_degrades(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import sys

    from svweave.adapters.slang.protocol import WorkerRequest
    from svweave.execution import remote_bootstrap as remote
    from svweave.security.sandbox import HostCapability

    snapshot = tmp_path / "snapshot"
    snapshot.mkdir()
    source = snapshot / "a.sv"
    source.write_bytes(b"module a; endmodule")
    source.chmod(0o444)
    digest = hashlib.sha256(source.read_bytes()).hexdigest()
    req = remote.BootstrapRequest(attempt_id="a", nonce="n", worker=WorkerRequest(snapshot_root=str(snapshot), args=[]), input_hashes={"snapshot/a.sv": digest}, memory_mib=128, execution_seconds=10)
    deployment = remote.BootstrapDeployment(shared_root=str(tmp_path), runtime_root=str(tmp_path), python_path=os.path.realpath(sys.executable), bwrap_path="/usr/bin/bwrap", runtime_hashes={"snapshot/a.sv": digest})
    probed: list[bool] = []
    def unavailable() -> HostCapability:
        probed.append(True)
        return HostCapability(False, "/usr/bin/bwrap", "test", "mock unavailable")
    monkeypatch.setattr(remote, "_verify_runtime_closure", lambda *args, **kwargs: None)  # isolate probe unit
    monkeypatch.setenv("SVWEAVE_ALLOW_DEGRADED_WORKER", "1")
    with pytest.raises(SvwError, match="bwrap"):
        remote.prepare_worker(req, deployment, probe=unavailable)
    assert probed == [True]
    # A missing hash must stop before even the fresh sandbox capability probe.
    source2 = snapshot / "b.sv"
    source2.write_text("module b; endmodule")
    with pytest.raises(SvwError, match="hash manifest"):
        remote.prepare_worker(req, deployment, probe=unavailable)
    assert probed == [True]


def test_SRUN_11_control_private_bounded_immutable(tmp_path: Path) -> None:
    from svweave.execution.control import ControlStore
    from svweave.execution.remote_bootstrap import ControlRecord

    tmp_path.chmod(0o700)
    store = ControlStore(tmp_path)
    record = ControlRecord(protocol_version="svweave-slurm-1", attempt_id="a", nonce="n", request_sha256="0" * 64, ownership=own(), bootstrap_sha256="1" * 64, toolchain_sha256="2" * 64, sandbox_verified=True, remote_ready=True)
    store.publish(record)
    assert store.read("a") == record
    with pytest.raises(FileExistsError):
        store.publish(record)
    (tmp_path / "a.json").unlink()
    os.mkfifo(tmp_path / "a.json", 0o600)
    with pytest.raises(ValueError):
        store.read("a")
    (tmp_path / "a.json").unlink()
    (tmp_path / "a.json").write_bytes(b"x" * (65536 + 1))
    (tmp_path / "a.json").chmod(0o600)
    with pytest.raises(ValueError, match="limit"):
        store.read("a")


def test_SRUN_10_ledger_rejects_identity_and_safety_regression(tmp_path: Path) -> None:
    tmp_path.chmod(0o700)
    ledger = ExecutionLedger(tmp_path)
    ledger.save(record(submission_started=True))
    with pytest.raises(ValueError, match="regress"):
        ledger.save(record())
    with pytest.raises(ValueError, match="immutable"):
        ledger.save(record(submission_started=True, snapshot_id="other"))
    assert ledger.read("a").pins_inputs


def test_SRUN_04_runtime_closure_rejects_unrelated_inventory(tmp_path: Path) -> None:
    from svweave.execution.remote_bootstrap import _verify_runtime_closure, runtime_tree_digest
    from svweave.security.sandbox import SandboxSpec

    source = tmp_path / "runtime"
    source.mkdir()
    (source / "python").write_bytes(b"pinned executable")
    before = runtime_tree_digest(source)
    (source / "python").write_bytes(b"changed executable")
    assert runtime_tree_digest(source) != before
    with pytest.raises(SvwError, match="exact sandbox mounts"):
        _verify_runtime_closure(SandboxSpec(ro_paths=(source,)), tmp_path / "inputs", {str(source): before})


def test_SRUN_12_remote_lifecycle_uses_compute_limits_not_launcher_oom_heuristic(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from svweave.adapters.slang import runner
    from svweave.adapters.slang.protocol import WorkerRequest
    from svweave.config.execution import (
        ExecutionPolicy,
        ExecutionSelection,
        ResolvedExecution,
        SlurmSiteProfile,
        resolve_execution,
    )
    from svweave.execution import remote_bootstrap as remote

    site = SlurmSiteProfile(cluster="cluster", srun_path="/usr/bin/srun", scancel_path="/usr/bin/scancel", scontrol_path="/usr/bin/scontrol", python_path="/trusted/python", bootstrap_path="/trusted/bootstrap.py", shared_root=str(tmp_path), executable_hashes={"/trusted/python": "0" * 64, "/usr/bin/bwrap": "1" * 64, "/trusted/bootstrap.py": "2" * 64})
    execution = resolve_execution(ExecutionSelection(provider="srun", site_profile="site", allocation_id="1"), ExecutionPolicy(scheduler_network_approved=True, site_profiles={"site": site}))
    # UNIT ONLY: simulate qualification/preparation to test lifecycle argument wiring.
    monkeypatch.setattr(ResolvedExecution, "require_srun_authorized", lambda self: site)
    sentinel = object()
    monkeypatch.setattr(remote, "_prepare_worker", lambda *args: (None, sentinel))
    received: dict[str, object] = {}
    def fake_run(req: WorkerRequest, **kwargs: object) -> runner.WorkerOutcome:
        received.update(kwargs)
        return runner.WorkerOutcome(kind="resource_limit", signal=9, resource_usage={"rusage_source": "launcher"})
    monkeypatch.setattr(runner, "run_worker", fake_run)
    req = remote.BootstrapRequest(attempt_id="a", nonce="n", worker=WorkerRequest(snapshot_root=str(tmp_path / "snapshot"), args=[]), input_hashes={}, memory_mib=128, execution_seconds=10)
    deployment = remote.BootstrapDeployment(shared_root=str(tmp_path), runtime_root="/", python_path="/trusted/python", bwrap_path="/usr/bin/bwrap", runtime_hashes={"trusted/python": "0" * 64, "usr/bin/bwrap": "1" * 64, "trusted/bootstrap.py": "2" * 64})
    outcome = remote.run_bootstrap(req, deployment, execution=execution, max_stdout_bytes=1024)
    assert received["allow_degraded"] is False and received["sandbox"] is sentinel
    assert received["memory_mib"] == 128 and received["timeout_s"] == 10 and received["max_stdout_bytes"] == 1024
    assert outcome.kind == "crashed" and "UNKNOWN" in outcome.message
    assert outcome.resource_usage["rusage_source"] == "remote_launcher"


def test_SRUN_04_shared_runtime_is_not_mistaken_for_verified_input(tmp_path: Path) -> None:
    from svweave.execution.remote_bootstrap import _verify_runtime_closure
    from svweave.security.sandbox import SandboxSpec

    shared_runtime = tmp_path / "runtime"
    shared_runtime.mkdir()
    with pytest.raises(SvwError, match="exact sandbox mounts"):
        _verify_runtime_closure(SandboxSpec(ro_paths=(shared_runtime,)), tmp_path, {})


def test_SRUN_04_runtime_inventory_is_structurally_unambiguous(tmp_path: Path) -> None:
    from svweave.execution.remote_bootstrap import runtime_tree_digest

    tree = tmp_path / "runtime"
    tree.mkdir()
    (tree / "a").write_bytes(b"one")
    (tree / "b").write_bytes(b"two")
    original = runtime_tree_digest(tree)
    # Embedding a serialized record delimiter/header in source bytes cannot impersonate
    # another entry: each entry contains an independently computed file SHA-256.
    (tree / "a").write_bytes(b"one\x00END\x00./b\x0033188\x000\x003\x00two")
    (tree / "b").unlink()
    assert runtime_tree_digest(tree) != original


def test_SRUN_11_bootstrap_entrypoint_binds_private_control_to_exact_request(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from svweave.adapters.slang.protocol import WorkerRequest
    from svweave.adapters.slang.runner import WorkerOutcome
    from svweave.config.execution import (
        ExecutionPolicy,
        ExecutionSelection,
        ResolvedExecution,
        SlurmSiteProfile,
        resolve_execution,
    )
    from svweave.execution import remote_bootstrap as remote
    from svweave.execution.control import ControlStore

    site = SlurmSiteProfile(cluster="c", srun_path="/usr/bin/srun", scancel_path="/usr/bin/scancel", scontrol_path="/usr/bin/scontrol", python_path="/trusted/python", bootstrap_path="/trusted/bootstrap.py", shared_root=str(tmp_path), executable_hashes={"/trusted/bootstrap.py": "1" * 64})
    execution = resolve_execution(ExecutionSelection(provider="srun", site_profile="site", allocation_id="12"), ExecutionPolicy(scheduler_network_approved=True, site_profiles={"site": site}))
    monkeypatch.setattr(ResolvedExecution, "require_srun_authorized", lambda self: site)
    monkeypatch.setenv("SLURM_JOB_ID", "12")
    monkeypatch.setenv("SLURM_STEP_ID", "3")
    def fake_lifecycle(*args: object, **kwargs: object) -> WorkerOutcome:
        kwargs["on_ready"]()  # type: ignore[operator]
        return WorkerOutcome(kind="ok", stdout=b"{}")
    monkeypatch.setattr(remote, "run_bootstrap", fake_lifecycle)
    request = remote.BootstrapRequest(attempt_id="a", nonce="n", worker=WorkerRequest(snapshot_root=str(tmp_path / "snapshot"), args=[]), input_hashes={}, memory_mib=128, execution_seconds=10)
    deployment = remote.BootstrapDeployment(shared_root=str(tmp_path), runtime_root="/trusted", python_path="/trusted/python", bwrap_path="/usr/bin/bwrap", runtime_hashes={})
    control = tmp_path / "control"
    control.mkdir(mode=0o700)
    payload = request.model_dump_json().encode()
    result = remote.bootstrap_once(payload, deployment, execution=execution, ownership=own(), control_directory=control, bootstrap_sha256="1" * 64, toolchain_sha256="2" * 64)
    assert result.ok
    assert ControlStore(control).read("a").request_sha256 == hashlib.sha256(payload).hexdigest()
    monkeypatch.setenv("SLURM_STEP_ID", "4")
    with pytest.raises(SvwError, match="identity"):
        remote.bootstrap_once(payload, deployment, execution=execution, ownership=own(), control_directory=control, bootstrap_sha256="1" * 64, toolchain_sha256="2" * 64)
