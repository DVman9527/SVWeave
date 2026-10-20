"""REV-I: fixes for the Codex whole-repository review (real worker process, real git)."""

from __future__ import annotations

import subprocess
import time
from pathlib import Path

import pytest

from svweave.adapters.slang.protocol import WorkerRequest
from svweave.adapters.slang.runner import run_worker
from svweave.application import diff_impact as di
from svweave.domain.errors import ErrorCode, SvwError

pytestmark = pytest.mark.integration


def test_REV_R01_worker_stdout_is_capped_and_the_worker_is_killed(tmp_path: Path) -> None:
    (tmp_path / "t.sv").write_text("module top; endmodule\n")
    req = WorkerRequest(snapshot_root=str(tmp_path), args=["--top", "top", "t.sv"])
    t0 = time.monotonic()
    out = run_worker(req, timeout_s=120, memory_mib=4096, test_fault="flood", max_stdout_bytes=2 << 20)
    assert out.kind == "resource_limit" and out.error_code is ErrorCode.RESOURCE_LIMIT
    assert "output exceeded 2 MiB" in out.message
    assert out.stdout == b""  # a truncated document is never handed to the parser
    assert time.monotonic() - t0 < 60  # killed, not waited out until the timeout


def test_REV_R02_a_normal_worker_run_is_unaffected_by_the_cap(tmp_path: Path) -> None:
    (tmp_path / "t.sv").write_text("module top; endmodule\n")
    req = WorkerRequest(snapshot_root=str(tmp_path), args=["--top", "top", "t.sv"])
    out = run_worker(req, timeout_s=120, memory_mib=4096)  # default cap: the worker memory limit
    assert out.kind == "ok" and out.stdout.startswith(b"{")


def _repo(tmp_path: Path) -> Path:
    env = {"PATH": "/usr/bin:/bin", "HOME": str(tmp_path), "GIT_CONFIG_NOSYSTEM": "1",
           "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t",
           "GIT_COMMITTER_EMAIL": "t@t"}
    r = tmp_path / "repo"
    r.mkdir()
    (r / "f.sv").write_text("module m; endmodule\n")

    def g(*a: str) -> None:
        subprocess.run(["git", "-C", str(r), *a], check=True, capture_output=True, env=env)

    g("init", "-q", "-b", "main")
    g("add", "-A")
    g("commit", "-q", "-m", "base")
    (r / "f.sv").write_text("// pad\n" * 40_000)  # a ~280 KB diff
    return r


def test_REV_G01_git_output_is_capped_while_reading_and_timeouts_are_enforced(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    r = _repo(tmp_path)
    args = ["diff", "-U0", "--no-color", "--no-ext-diff", "--no-textconv", "HEAD", "--"]
    assert len(di._git(r, args)) > 100_000  # unrestricted: the whole diff comes back
    monkeypatch.setattr(di, "MAX_DIFF_BYTES", 4096)
    with pytest.raises(SvwError) as e:
        di._git(r, args)
    assert e.value.code is ErrorCode.OUTPUT_LIMIT and e.value.details["reason"] == "diff_too_large"
    monkeypatch.setattr(di, "MAX_DIFF_BYTES", 64 << 20)
    monkeypatch.setattr(di, "GIT_TIMEOUT_S", 0)
    with pytest.raises(SvwError) as e2:
        di._git(r, args)
    assert e2.value.code is ErrorCode.BUILD_TIMEOUT and e2.value.details["reason"] == "git_timeout"
    monkeypatch.setattr(di, "GIT_TIMEOUT_S", 60)
    with pytest.raises(SvwError) as e3:
        di._git(r, ["diff", "no_such_revision_xyz", "--"])
    assert e3.value.code is ErrorCode.MISSING_INPUT and e3.value.details["reason"] == "git_failed"
