"""Native benchmark reporting helpers tested without launching any native worker."""
from __future__ import annotations

import importlib.util
import json
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest


@pytest.fixture
def benchmark(monkeypatch: pytest.MonkeyPatch):
    root = Path(__file__).resolve().parents[2]
    monkeypatch.syspath_prepend(str(root / "scripts"))
    spec = importlib.util.spec_from_file_location("build_benchmark", root / "scripts/benchmark_incremental_build.py")
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _raw(**over):
    data = {
        "completeness": "COMPLETE", "diagnostic_outcome": "NO_ERRORS", "worker_isolation": "bwrap",
        "reused": False, "job_id": "job", "resource_usage": {"isolation": "bwrap"},
        "update": {"semantic_action": "full_profile_analysis"},
        "verified_rerun": True, "semantic_result_sha256": "digest", **over,
    }
    return {"exit": 0, "stdout": json.dumps({"outcome": "OK", "error": None, "data": data})}


def test_benchmark_requires_actual_isolated_complete_worker(benchmark) -> None:
    assert benchmark._validate_result(_raw(), update=True, rerun=True)["data"]["job_id"] == "job"
    for changed in ({"reused": True}, {"worker_isolation": "degraded"},
                    {"completeness": "PARTIAL"}, {"verified_rerun": False},
                    {"update": {"semantic_action": "exact_build_reuse"}}):
        with pytest.raises(RuntimeError):
            benchmark._validate_result(_raw(**changed), update=True, rerun=True)


def test_native_benchmark_refuses_non_ci_execution(monkeypatch: pytest.MonkeyPatch, benchmark) -> None:
    monkeypatch.delenv("CI", raising=False)
    monkeypatch.setattr("sys.argv", ["benchmark"])
    with pytest.raises(SystemExit) as err:
        benchmark.main()
    assert err.value.code == 2


def test_semantic_digest_retains_semantic_ids_ignores_execution_only(tmp_path: Path, benchmark) -> None:
    state = tmp_path / ".svweave"
    state.mkdir()
    with sqlite3.connect(state / "svweave.db") as db:
        for table in benchmark.TABLES:
            db.execute(f'CREATE TABLE "{table}" (build_id TEXT, semantic_id TEXT, job_id TEXT, diagnostic_id INTEGER, stats TEXT)')
            db.execute(f'INSERT INTO "{table}" VALUES (?,?,?,?,?)',
                       ("build", "symbol-1", "job-1", 1, json.dumps({"worker_wall_s": 1.0, "count": 1})))
    before = benchmark.semantic_digests(tmp_path, "build")
    with sqlite3.connect(state / "svweave.db") as db:
        db.execute("UPDATE diagnostics SET job_id='job-2',diagnostic_id=2")
        db.execute("UPDATE build_reports SET job_id='job-2',stats=?", (json.dumps({"worker_wall_s": 99.0, "count": 1}),))
    assert benchmark.semantic_digests(tmp_path, "build") == before
    with sqlite3.connect(state / "svweave.db") as db:
        db.execute("UPDATE declarations SET semantic_id='symbol-2'")
    assert benchmark.semantic_digests(tmp_path, "build") != before


def test_controller_drops_secrets_and_degraded_flags(tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
                                                     benchmark) -> None:
    monkeypatch.setenv("SECRET_TOKEN", "must-not-leak")
    monkeypatch.setenv("SVWEAVE_ALLOW_DEGRADED_WORKER", "1")
    monkeypatch.setenv("PYTHONSTARTUP", "/untrusted/startup.py")
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    monkeypatch.setenv("SVWEAVE_TOOLCHAIN_ROOT", "/trusted/toolchain")
    env = benchmark._environment(tmp_path / "root")
    assert "SECRET_TOKEN" not in env and "SVWEAVE_ALLOW_DEGRADED_WORKER" not in env
    assert "PYTHONSTARTUP" not in env
    assert env["SVWEAVE_TOOLCHAIN_ROOT"] == "/trusted/toolchain"
    assert env["SVWEAVE_REQUIRE_BACKENDS"] == "1"


@pytest.mark.parametrize(("code", "timeout", "reason"), [
    ("import time; time.sleep(10)", 0.1, "cli_timeout"),
    ("print('x'*100000)", 2.0, "cli_output_limit"),
])
def test_controller_bounds_and_reaps_python_child(code: str, timeout: float, reason: str,
                                                   monkeypatch: pytest.MonkeyPatch, benchmark) -> None:
    monkeypatch.setattr(benchmark, "MAX_OUTPUT_BYTES", 4096)
    proc = subprocess.Popen([sys.executable, "-c", code], stdin=subprocess.DEVNULL,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, start_new_session=True)
    out, err, failure = benchmark._bounded_output(proc, timeout)
    assert failure == reason and proc.poll() is not None
    assert len(out.encode()) + len(err.encode()) <= 4096


def test_runtime_cleanup_handles_readonly_tree_and_marks_failure(tmp_path: Path,
                                                                monkeypatch: pytest.MonkeyPatch,
                                                                benchmark) -> None:
    from svweave.snapshot import capture

    work = tmp_path / "runtime"
    work.mkdir()
    item = work / "snapshot"
    item.write_bytes(b"synthetic")
    item.chmod(0o444)
    work.chmod(0o555)
    report = {"status": "PASS"}
    benchmark._cleanup_workspace(work, report)
    assert not work.exists() and report["runtime_workspace_removed"] is True

    def denied(path):
        raise PermissionError("injected cleanup failure")

    monkeypatch.setattr(capture, "remove_snapshot_dir", denied)
    with pytest.raises(PermissionError):
        benchmark._cleanup_workspace(work, report)
    assert report["status"] == "FAIL" and report["runtime_workspace_removed"] is False
