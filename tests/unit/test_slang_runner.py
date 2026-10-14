"""Runner behaviour that does not need pyslang: faults fire before it is imported."""

from __future__ import annotations

import os
import re
import signal
import threading
import time
from pathlib import Path

import pytest

from svweave.adapters.slang.protocol import WorkerRequest
from svweave.adapters.slang.runner import WorkerHandle, run_worker, worker_env
from svweave.domain.errors import ErrorCode


def req(tmp_path: Path) -> WorkerRequest:
    return WorkerRequest(snapshot_root=str(tmp_path), args=[])


def pid_of(stderr: str) -> int:
    m = re.search(r"FAULT_CHILD_PID=(\d+)", stderr)
    assert m
    return int(m.group(1))


def sleepers() -> set[int]:
    """Host pids of the fault-injection ``sleep 3600`` descendants. The worker runs in its own PID
    namespace, so the pid it prints is not a host pid: look at the host process table instead."""
    out: set[int] = set()
    for d in Path("/proc").iterdir():
        if d.name.isdigit():
            try:
                if (d / "cmdline").read_bytes() == b"sleep\x003600\x00":
                    stat = (d / "stat").read_text().rsplit(")", 1)[1].split()[0]
                    if stat != "Z":
                        out.add(int(d.name))
            except OSError:
                pass
    return out


def no_new_sleepers(before: set[int]) -> bool:
    for _ in range(50):
        if not (sleepers() - before):
            return True
        time.sleep(0.1)
    return False


def test_timeout_reaps_the_whole_process_group(tmp_path: Path) -> None:
    before = sleepers()
    out = run_worker(req(tmp_path), timeout_s=1.0, memory_mib=4096, test_fault="hang")
    assert out.kind == "timeout" and out.error_code == ErrorCode.BUILD_TIMEOUT
    assert out.signal == signal.SIGKILL and 0.9 < out.wall_s < 10
    assert pid_of(out.stderr_tail) and no_new_sleepers(before)
    assert out.peak_rss_kib and out.peak_rss_kib > 1000  # rusage of exactly this child


@pytest.mark.parametrize(("fault", "sig"), [("abort", signal.SIGABRT), ("segv", signal.SIGSEGV)])
def test_native_crash_is_reported_not_raised(
    tmp_path: Path, fault: str, sig: signal.Signals
) -> None:
    out = run_worker(req(tmp_path), timeout_s=30, memory_mib=4096, test_fault=fault)
    assert out.kind == "crashed" and out.signal == sig and out.exit_code is None
    assert out.error_code == ErrorCode.BACKEND_UNAVAILABLE and "crashed" in out.message


def test_exit_code_and_memory_limit(tmp_path: Path) -> None:
    out = run_worker(req(tmp_path), timeout_s=30, memory_mib=4096, test_fault="exit3")
    assert out.kind == "exited" and out.exit_code == 3
    oom = run_worker(req(tmp_path), timeout_s=60, memory_mib=512, test_fault="oom")
    assert oom.kind == "resource_limit" and oom.error_code == ErrorCode.RESOURCE_LIMIT
    assert oom.resource_usage["memory_limit_mib"] == 512


def test_cancel_from_another_thread(tmp_path: Path) -> None:
    h = WorkerHandle()
    before = sleepers()
    res = {}
    t = threading.Thread(
        target=lambda: res.setdefault(
            "o",
            run_worker(req(tmp_path), timeout_s=60, memory_mib=4096, handle=h, test_fault="hang"),
        )
    )
    t.start()
    time.sleep(1.0)
    h.cancel()
    t.join(15)
    assert not t.is_alive() and res["o"].kind == "cancelled"
    assert pid_of(res["o"].stderr_tail) and no_new_sleepers(before)
    with pytest.raises(ProcessLookupError):
        os.killpg(h.pgid, 0)  # type: ignore[arg-type]


def test_cancel_before_start_never_runs_the_worker(tmp_path: Path) -> None:
    h = WorkerHandle()
    h._cancel.set()
    out = run_worker(req(tmp_path), timeout_s=30, memory_mib=4096, handle=h, test_fault="hang")
    assert out.kind == "cancelled" and out.wall_s < 5


def test_worker_gets_a_clean_environment_and_snapshot_cannot_shadow_modules(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SECRET_TOKEN", "x")
    assert set(worker_env()) == {
        "PATH",
        "LANG",
        "LC_ALL",
        "PYTHONDONTWRITEBYTECODE",
        "PYTHONUNBUFFERED",
    }
    (tmp_path / "json.py").write_text(
        "raise SystemExit(99)\n"
    )  # planted module in the snapshot root
    out = run_worker(req(tmp_path), timeout_s=30, memory_mib=4096, test_fault="exit3")
    assert (
        out.exit_code == 3
    )  # -I: cwd is not on sys.path, so the planted json.py was never imported


def test_normal_run_returns_a_result_document(tmp_path: Path) -> None:
    out = run_worker(req(tmp_path), timeout_s=60, memory_mib=4096)
    assert out.kind == "ok", out.stderr_tail
    import json

    doc = json.loads(out.stdout)
    assert doc["schema_version"] == "svw-extract-1" and doc["declarations"] == []  # no input files


# ------------------------------------------------------------------------------------ A2
SECRET = "OUTSIDE_SECRET_MARKER_9f3a"


def _leak_world(tmp_path: Path) -> WorkerRequest:
    snap = tmp_path / "snap" / "files"
    snap.mkdir(parents=True)
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.svh").write_text(f"`define {SECRET} 1\nthis is not valid sv {SECRET};\n")
    (snap / "t.sv").write_text(f'`include "{outside}/secret.svh"\nmodule t; endmodule\n')
    return WorkerRequest(snapshot_root=str(snap), args=["--top", "t", "t.sv"])


def test_a2_outside_absolute_include_cannot_be_read_by_the_sandboxed_worker(tmp_path: Path) -> None:
    out = run_worker(_leak_world(tmp_path), timeout_s=60, memory_mib=4096)
    assert out.kind == "ok", out.stderr_tail
    assert out.isolation == "bwrap" and out.isolation_warnings == ()
    assert SECRET not in out.stdout.decode() and SECRET not in out.stderr_tail
    import json

    doc = json.loads(out.stdout)
    text = json.dumps(doc["diagnostics"]) + json.dumps(doc["driver_messages"])
    assert "secret.svh" in text and "No such file" in text or "could not" in text.lower()
    assert not any(SECRET in json.dumps(d) for d in doc["diagnostics"])


def test_a2_control_the_same_probe_reads_the_file_without_the_sandbox(tmp_path: Path) -> None:
    """Negative control: proves the probe above can see the file when no sandbox is in place."""
    from svweave.security.sandbox import HostCapability, SandboxRunner

    no_bwrap = SandboxRunner(allow_degraded=True,
                             capability=HostCapability(False, None, None, "forced for the control"))
    out = run_worker(_leak_world(tmp_path), timeout_s=60, memory_mib=4096, sandbox=no_bwrap)
    assert out.kind == "ok", out.stderr_tail
    assert out.isolation == "degraded_no_isolation" and out.isolation_warnings
    assert out.resource_usage["isolation"] == "degraded_no_isolation"
    assert SECRET in out.stdout.decode()  # the outside file WAS read


def test_a2_without_bwrap_the_worker_fails_closed_unless_degraded_is_explicit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from svweave.security.sandbox import HostCapability, SandboxRunner

    monkeypatch.delenv("SVWEAVE_ALLOW_DEGRADED_WORKER", raising=False)
    cap = HostCapability(False, None, None, "no user namespaces")
    out = run_worker(req(tmp_path), timeout_s=30, memory_mib=4096, test_fault="exit3",
                     sandbox=SandboxRunner(capability=cap))
    assert out.kind == "exited" and out.error_code == ErrorCode.BACKEND_UNAVAILABLE
    assert out.exit_code is None and "not started" in out.message and out.isolation == "UNAVAILABLE"


def test_a2_worker_spec_is_read_only_and_networkless(tmp_path: Path) -> None:
    from svweave.adapters.slang.runner import worker_sandbox_spec
    from svweave.security.sandbox import build_bwrap_argv

    r = _leak_world(tmp_path)
    spec = worker_sandbox_spec(r)
    argv = build_bwrap_argv(spec, ["/bin/true"])
    assert "--unshare-net" in argv and not spec.rw_paths and "--bind" not in argv
    assert str(tmp_path / "outside") not in argv
    ro = [argv[i + 1] for i, a in enumerate(argv) if a == "--ro-bind"]
    assert r.snapshot_root in ro


def test_effective_error_severity_counts_as_error(tmp_path: Path) -> None:
    """format-too-many-args is a warning-option diagnostic that slang reports at error severity;
    isError() is False for it, so the document must derive is_error from the effective severity."""
    import json

    snap = tmp_path / "snap" / "files"
    snap.mkdir(parents=True)
    (snap / "t.sv").write_text(
        'module t; string s; initial s = $sformatf("done", 5); endmodule\n'
    )
    out = run_worker(
        WorkerRequest(snapshot_root=str(snap), args=["--top", "t", "t.sv"]),
        timeout_s=60,
        memory_mib=4096,
    )
    assert out.kind == "ok", out.stderr_tail
    diags = {d["code"]: d for d in json.loads(out.stdout)["diagnostics"]}
    d = diags["FormatTooManyArgs"]
    assert d["severity"] == "error" and d["is_error"] is True


def test_resource_usage_is_the_workers_own_not_the_launchers(tmp_path: Path) -> None:
    """wait4 on the launcher sees only bwrap (about 60 MB and 0 s for any build); the worker reports
    its own getrusage on its last stderr line and the runner must use that."""
    out = run_worker(req(tmp_path), timeout_s=60, memory_mib=4096)
    assert out.kind == "ok", out.stderr_tail
    assert out.resource_usage["rusage_source"] == "worker"
    assert "SVW_RUSAGE" not in out.stderr_tail
    # pyslang alone is tens of MB resident; the bwrap launcher is a few MB
    assert out.peak_rss_kib and out.peak_rss_kib > 30_000
    assert out.user_cpu_s and out.user_cpu_s > 0.05  # importing pyslang costs CPU; bwrap's is ~0


def test_a_killed_worker_falls_back_to_the_launcher_figures(tmp_path: Path) -> None:
    out = run_worker(req(tmp_path), timeout_s=1.0, memory_mib=4096, test_fault="hang")
    assert out.kind == "timeout" and out.resource_usage["rusage_source"] == "launcher"


@pytest.mark.parametrize(
    "line",
    [
        b'SVW_RUSAGE {"maxrss_kib":-1,"user_s":1,"sys_s":1}',
        b'SVW_RUSAGE {"maxrss_kib":"9","user_s":1,"sys_s":1}',
        b'SVW_RUSAGE {"maxrss_kib":true,"user_s":1,"sys_s":1}',
        b'SVW_RUSAGE {"maxrss_kib":1e30,"user_s":1,"sys_s":1}',
        b'SVW_RUSAGE {"maxrss_kib":1,"user_s":1}',
        b"SVW_RUSAGE [1,2,3]",
        b"SVW_RUSAGE not json",
    ],
)
def test_rusage_line_is_validated_and_bad_lines_are_left_alone(line: bytes) -> None:
    from svweave.adapters.slang.protocol import parse_rusage_line

    err = b"before\n" + line + b"\nafter"
    rest, usage = parse_rusage_line(err)
    assert usage is None and rest == err


def test_rusage_line_is_removed_and_only_the_last_marker_counts() -> None:
    from svweave.adapters.slang.protocol import parse_rusage_line

    err = (
        b'SVW_RUSAGE {"maxrss_kib":1,"user_s":1,"sys_s":1}\nnoise\n'
        b'SVW_RUSAGE {"maxrss_kib":5,"user_s":2.5,"sys_s":0.5}'
    )
    rest, usage = parse_rusage_line(err)
    assert usage == {"maxrss_kib": 5.0, "user_s": 2.5, "sys_s": 0.5}
    assert b"SVW_RUSAGE {" in rest and b"noise" in rest and b"maxrss_kib\":5" not in rest


def test_parameter_errors_on_an_encrypted_module_are_linked_to_the_envelope(tmp_path: Path) -> None:
    """A protected module's parameters live inside the envelope slang skips, so overriding one reports
    ParameterDoesNotExist. The ingest verdict must say these are likely encryption artifacts, and must
    NOT claim that for a missing parameter on an ordinary module."""
    import json

    from svweave.adapters.slang.ingest import _protected_parameter_errors

    snap = tmp_path / "snap" / "files"
    snap.mkdir(parents=True)
    (snap / "t.sv").write_text(
        "module prot (input clk);\n"
        "`pragma protect begin_protected\n"
        '`pragma protect data_method = "aes256-cbc"\n'
        '`pragma protect encrypt_agent = "X"\n'
        "`pragma protect key_block\nAAAA\n"
        "`pragma protect data_block\nBBBB\n"
        "`pragma protect end_protected\nendmodule\n"
        "module plain (input clk); endmodule\n"
        "module top;\n"
        "  prot #(.W(1)) u1 (.clk(1'b0));\n"
        "  plain #(.W(1)) u2 (.clk(1'b0));\n"
        "endmodule\n"
    )
    out = run_worker(
        WorkerRequest(snapshot_root=str(snap), args=["--top", "top", "--timescale", "1ns/1ps", "t.sv"]),
        timeout_s=60,
        memory_mib=4096,
    )
    assert out.kind == "ok", out.stderr_tail
    doc = json.loads(out.stdout)
    codes = [d["code"] for d in doc["diagnostics"]]
    assert "ProtectedEnvelope" in codes and codes.count("ParameterDoesNotExist") == 2
    assert _protected_parameter_errors(doc) == {"prot": 1}  # `plain` is an ordinary error


def test_missing_timescale_is_diagnosed_per_file_without_a_default(tmp_path: Path) -> None:
    """The ingest verdict tells the user to set language_options.timescale when this fires (and the
    default timescale makes it go away)."""
    import json

    snap = tmp_path / "snap" / "files"
    snap.mkdir(parents=True)
    (snap / "a.sv").write_text("`timescale 1ns/1ps\nmodule a; endmodule\n")
    (snap / "b.sv").write_text("module b; endmodule\n")

    def codes(extra: list[str]) -> list[str]:
        out = run_worker(
            WorkerRequest(snapshot_root=str(snap), args=[*extra, "--top", "a", "--top", "b", "a.sv", "b.sv"]),
            timeout_s=60,
            memory_mib=4096,
        )
        assert out.kind == "ok", out.stderr_tail
        return [d["code"] for d in json.loads(out.stdout)["diagnostics"]]

    assert "MissingTimeScale" in codes([])
    assert "MissingTimeScale" not in codes(["--timescale", "1ns/1ps"])
