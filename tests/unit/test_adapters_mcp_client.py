"""Unit tests for the shared MCP client helper using a hand-rolled fake stdio server.

API-04 (non-protocol stdout), API-05 (protocol revision pin) and process-failure mapping.
"""

from __future__ import annotations

import os
import sys
import time
from pathlib import Path

import pytest

from svweave.adapters.mcp_client import McpLaunch, McpSession, tool_schema_sha256
from svweave.domain.errors import ErrorCode, SvwError
from svweave.security.redaction import Redactor

FAKE = str(Path(__file__).with_name("fake_mcp_server.py"))


def launch(
    tmp_path: Path,
    mode: str = "ok",
    proto: str = "2025-11-25",
    expected: str | None = "2025-11-25",
    call_timeout: float = 20.0,
) -> McpLaunch:
    return McpLaunch(
        backend="fake",
        argv=[sys.executable, "-I", FAKE, mode, proto],
        env={"PATH": "/usr/bin:/bin"},
        stderr_log=tmp_path / "logs" / "fake.stderr.log",
        expected_protocol=expected,
        init_timeout=20,
        call_timeout=call_timeout,
        redactor=Redactor([tmp_path]),
    )


def test_ok_records_init_and_tool_hashes(tmp_path: Path) -> None:
    with McpSession(launch(tmp_path)) as s:
        assert s.init is not None
        assert s.init.protocol_version == "2025-11-25"
        assert (s.init.server_name, s.init.server_version) == ("fake", "9.9")
        t = s.tools["echo"]
        assert len(t.schema_sha256) == 64 and len(t.input_schema_sha256) == 64
        assert t.schema_sha256 == tool_schema_sha256(
            {"name": "echo", "inputSchema": t.input_schema, "outputSchema": None}
        )
        assert s.alive and s.pid is not None
        r = s.call_tool("echo", {"x": "1"})
        assert r.text == "pong" and not r.is_error
    assert not s.alive


def test_api04_banner_on_stdout_is_protocol_error(tmp_path: Path) -> None:
    s = McpSession(launch(tmp_path, "banner"))
    with pytest.raises(SvwError) as ei:
        s.start()
    e = ei.value
    assert e.code is ErrorCode.BACKEND_UNAVAILABLE
    assert e.details["reason"] == "protocol_error"
    assert e.details["protocol_error"]["kind"] == "non_protocol_stdout"
    assert "Welcome to fake backend" in e.details["protocol_error"]["sample"]
    s.stop()


def test_api04_garbage_midstream_fails_the_call(tmp_path: Path) -> None:
    s = McpSession(launch(tmp_path, "midstream_garbage"))
    s.start()
    try:
        with pytest.raises(SvwError) as ei:
            s.call_tool("echo", {})
        assert ei.value.details["reason"] == "protocol_error"
        assert s.violation is not None
        assert not s.alive
    finally:
        s.stop()


def test_api05_protocol_mismatch_is_capability_mismatch(tmp_path: Path) -> None:
    s = McpSession(launch(tmp_path, proto="2024-11-05", expected="2025-11-25"))
    with pytest.raises(SvwError) as ei:
        s.start()
    assert ei.value.code is ErrorCode.CAPABILITY_MISMATCH
    assert ei.value.details["expected"] == "2025-11-25"
    assert ei.value.details["actual"] == "2024-11-05"
    s.stop()


def test_api05_unknown_protocol_revision_is_mismatch(tmp_path: Path) -> None:
    s = McpSession(launch(tmp_path, proto="2099-01-01", expected="2025-11-25"))
    with pytest.raises(SvwError) as ei:
        s.start()
    assert ei.value.code is ErrorCode.CAPABILITY_MISMATCH
    s.stop()


def test_matching_protocol_passes_but_no_pin_means_no_check(tmp_path: Path) -> None:
    with McpSession(launch(tmp_path, proto="2024-11-05", expected=None)) as s:
        assert s.init is not None and s.init.protocol_version == "2024-11-05"


def test_backend_exit_mid_call_is_unavailable(tmp_path: Path) -> None:
    s = McpSession(launch(tmp_path, "exit_on_call"))
    s.start()
    try:
        with pytest.raises(SvwError) as ei:
            s.call_tool("echo", {})
        assert ei.value.code is ErrorCode.BACKEND_UNAVAILABLE
        assert ei.value.details["reason"] in ("process_exited", "mcp_error", "client_error")
        assert not s.alive
    finally:
        s.stop()


def test_call_timeout_is_unavailable_timeout(tmp_path: Path) -> None:
    s = McpSession(launch(tmp_path, "slow_call", call_timeout=1.0))
    s.start()
    try:
        with pytest.raises(SvwError) as ei:
            s.call_tool("echo", {}, timeout=1.0)
        assert ei.value.code is ErrorCode.BACKEND_UNAVAILABLE
        assert ei.value.details["reason"] in ("timeout", "mcp_error")
    finally:
        s.stop()


def test_spawn_failure_reports_spawn_failed(tmp_path: Path) -> None:
    lc = launch(tmp_path)
    lc.argv = [str(tmp_path / "does-not-exist")]
    s = McpSession(lc)
    with pytest.raises(SvwError) as ei:
        s.start()
    assert ei.value.details["reason"] == "spawn_failed"
    s.stop()


def test_stderr_goes_to_private_log_and_is_redacted_in_errors(tmp_path: Path) -> None:
    lc = launch(tmp_path, "banner")
    s = McpSession(lc)
    with pytest.raises(SvwError) as ei:
        s.start()
    s.stop()
    raw = lc.stderr_log.read_text()
    assert "supersecretvalue123" in raw  # raw log is private
    assert oct(lc.stderr_log.stat().st_mode & 0o777) == "0o600"
    tail = ei.value.details["stderr_tail"]
    assert "supersecretvalue123" not in tail
    assert "/home/someone/private/repo" not in tail


def test_unknown_tool_is_capability_mismatch(tmp_path: Path) -> None:
    with McpSession(launch(tmp_path)) as s, pytest.raises(SvwError) as ei:
        s.call_tool("delete_project", {})
    assert ei.value.code is ErrorCode.CAPABILITY_MISMATCH


def _procs_with(marker: str) -> list[int]:
    out = []
    for d in os.listdir("/proc"):
        if d.isdigit():
            try:
                if marker.encode() in Path(f"/proc/{d}/cmdline").read_bytes():
                    out.append(int(d))
            except OSError:
                continue
    return out


def test_detached_daemon_child_does_not_outlive_the_session(tmp_path: Path) -> None:
    """cbm forks a --cbm-daemon-internal child that setsid()s out of the process group; stop()
    must reap it too (token sweep), in plain and in env -i (degraded) launches."""
    for wrapped in (False, True):
        lc = launch(tmp_path / str(wrapped), "daemon_child")
        if wrapped:
            lc.argv = ["/usr/bin/env", "-i", "PATH=/usr/bin:/bin", *lc.argv]
        s = McpSession(lc)
        s.start()
        deadline = time.monotonic() + 10
        while not _procs_with("svw-fake-daemon") and time.monotonic() < deadline:
            time.sleep(0.1)
        assert _procs_with("svw-fake-daemon"), "fixture: daemon child never appeared"
        s.stop()
        gone = time.monotonic() + 3
        while _procs_with("svw-fake-daemon") and time.monotonic() < gone:
            time.sleep(0.1)
        assert not _procs_with("svw-fake-daemon"), f"daemon child survived stop() (env -i={wrapped})"


def test_unit_guard_refuses_real_backend_launch(tmp_path: Path) -> None:
    lc = launch(tmp_path)
    lc.argv = ["/opt/x/codebase-memory-mcp"]
    with pytest.raises(AssertionError, match="REAL backend"):
        McpSession(lc).start()


def test_log_rotates_at_size_cap_and_refuses_links(tmp_path: Path) -> None:
    lc = launch(tmp_path)
    lc.log_max_bytes = 100
    lc.stderr_log.parent.mkdir(parents=True)
    lc.stderr_log.write_text("x" * 500)
    with McpSession(lc):
        pass
    assert lc.stderr_log.with_name(lc.stderr_log.name + ".1").read_text() == "x" * 500
    assert lc.stderr_log.stat().st_size < 500
    victim = tmp_path / "victim.txt"
    victim.write_text("keep")
    lc2 = launch(tmp_path / "b")
    lc2.stderr_log.parent.mkdir(parents=True)
    lc2.stderr_log.symlink_to(victim)
    with pytest.raises(SvwError) as ei:
        McpSession(lc2).start()
    assert ei.value.code is ErrorCode.POLICY_DENIED and victim.read_text() == "keep"


def test_f4_degraded_env_i_launch_is_detected_alive_and_callable(tmp_path: Path) -> None:
    """env -i drops the pid-lookup token across exec: degraded mode used to report alive=False."""
    from svweave.security.sandbox import HostCapability, SandboxRunner, SandboxSpec

    runner = SandboxRunner(allow_degraded=True, capability=HostCapability(False, None, None, "forced"))
    w = runner.wrap(SandboxSpec(env={"PATH": "/usr/bin:/bin"}), [sys.executable, "-I", FAKE, "ok", "2025-11-25"])
    assert w.argv[:2] == ["/usr/bin/env", "-i"]
    lc = launch(tmp_path)
    lc.argv, lc.env = w.argv, w.env
    with McpSession(lc) as s:
        assert s.pid is not None and s.alive
        assert s.call_tool("echo", {}).text == "pong"
    assert not s.alive
