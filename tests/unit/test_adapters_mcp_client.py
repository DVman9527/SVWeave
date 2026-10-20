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


# ---------------------------------------------------------------------- bounded backend output
# security.md 6: a backend must not be able to grow the gateway's memory with one huge stdout line
# (no newline) or one huge tool result. Small limits keep the tests fast; the floods are many times
# larger than the limit, and the reader's own instrumentation proves what was actually buffered.

MIB = 1024 * 1024
READ_CHUNK = 64 * 1024  # bounded_stdio.READ_CHUNK_BYTES


def sized(tmp_path: Path, mode: str, size: int, **limits: int) -> McpLaunch:
    lc = launch(tmp_path, mode, call_timeout=8.0)
    lc.argv = [*lc.argv, str(size)]
    for k, v in limits.items():
        setattr(lc, k, v)
    return lc


def _status_kib(key: str) -> int:
    for line in Path("/proc/self/status").read_text().splitlines():
        if line.startswith(key + ":"):
            return int(line.split()[1])
    raise AssertionError(f"no {key}")


def _reset_peak_rss() -> bool:
    """Reset VmHWM (the PEAK resident set) so a later reading measures this test's peak; RSS after
    the fact cannot fail (the buffer is freed by then)."""
    try:
        Path("/proc/self/clear_refs").write_text("5")
    except OSError:
        return False
    return True


def _gone(s: McpSession, pid: int | None) -> bool:
    from svweave.adapters.mcp_client import _token_pids

    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        pid_gone = pid is None or not Path(f"/proc/{pid}").exists()
        if pid_gone and not _token_pids(s._client.token):
            return True
        time.sleep(0.05)
    return False


def test_sec_oversized_line_without_newline_is_bounded_protocol_error(tmp_path: Path) -> None:
    limit, flood = 1 * MIB, 64 * MIB
    s = McpSession(sized(tmp_path, "huge_line", flood, max_message_bytes=limit))
    s.start()
    pid = s.pid
    assert pid is not None
    measured = _reset_peak_rss()
    hwm0 = _status_kib("VmHWM")
    try:
        t0 = time.monotonic()
        with pytest.raises(SvwError) as ei:
            s.call_tool("echo", {})
        elapsed = time.monotonic() - t0
        e = ei.value
        assert e.code is ErrorCode.BACKEND_UNAVAILABLE
        assert e.details["reason"] == "message_too_large"
        pe = e.details["protocol_error"]
        assert pe["kind"] == "message_too_large" and pe["limit_bytes"] == limit
        assert limit < pe["received_bytes_at_least"] <= limit + READ_CHUNK
        assert elapsed < 6.0, f"call took {elapsed:.1f}s (must fail on the limit, not the timeout)"
        st = s.transport_stats
        assert st.overflow is not None
        # the client never held more than the limit plus one read chunk of the 64 MiB flood
        assert st.peak_buffer_bytes <= limit + READ_CHUNK
        if measured:  # the PEAK, not the RSS after the buffer was freed (that could never fail)
            assert (_status_kib("VmHWM") - hwm0) * 1024 < 24 * MIB
        assert not s.alive
        assert _gone(s, pid), "backend still running after the oversized message"
    finally:
        s.stop()


def test_sec_oversized_line_during_handshake_fails_start_and_kills_backend(tmp_path: Path) -> None:
    limit = 1 * MIB
    s = McpSession(sized(tmp_path, "huge_banner", 32 * MIB, max_message_bytes=limit))
    t0 = time.monotonic()
    with pytest.raises(SvwError) as ei:
        s.start()
    assert time.monotonic() - t0 < 10.0
    assert ei.value.code is ErrorCode.BACKEND_UNAVAILABLE
    assert ei.value.details["reason"] == "message_too_large"
    assert s.transport_stats.peak_buffer_bytes <= limit + READ_CHUNK
    assert _gone(s, None)
    s.stop()


def test_sec_tool_result_text_over_limit_is_output_limit(tmp_path: Path) -> None:
    limit_chars = 256 * 1024
    s = McpSession(sized(tmp_path, "big_result", 4 * MIB, max_result_chars=limit_chars))
    s.start()
    pid = s.pid
    try:
        t0 = time.monotonic()
        with pytest.raises(SvwError) as ei:
            s.call_tool("echo", {})
        assert time.monotonic() - t0 < 6.0
        e = ei.value
        assert e.code is ErrorCode.OUTPUT_LIMIT
        assert e.details["reason"] == "result_too_large"
        pe = e.details["protocol_error"]
        assert pe == {"kind": "result_too_large", "tool": "echo", "limit_chars": limit_chars,
                      "size_chars": 4 * MIB}
        assert not s.alive
        assert _gone(s, pid), "backend still running after the oversized result"
        with pytest.raises(SvwError) as again:
            s.call_tool("echo", {})
        assert again.value.code is ErrorCode.BACKEND_UNAVAILABLE
    finally:
        s.stop()


def test_sec_messages_and_results_just_under_the_limits_pass(tmp_path: Path) -> None:
    limit = 1 * MIB
    # a response line of exactly the limit (newline excluded) is accepted, and the session lives on
    with McpSession(sized(tmp_path / "line", "sized_line", limit, max_message_bytes=limit)) as s:
        r = s.call_tool("echo", {})
        assert not r.is_error and set(r.text) == {"y"} and len(r.text) > limit - 200
        assert s.transport_stats.max_line_bytes == limit and s.transport_stats.overflow is None
        assert s.alive
        r2 = s.call_tool("echo", {})
        assert r2.text == r.text
    # a result text of exactly the result limit is accepted
    with McpSession(sized(tmp_path / "res", "big_result", limit, max_result_chars=limit)) as s:
        r = s.call_tool("echo", {})
        assert len(r.text) == limit and s.violation is None and s.alive


def test_sec_default_limits_sit_above_the_pinned_backends_own_caps() -> None:
    """The defaults must never cut a legitimate response: cbm 0.11.0 refuses on its own side any
    response above 10,485,744 bytes; Serena answers are capped at 150,000 characters."""
    from svweave.adapters.bounded_stdio import MAX_MESSAGE_BYTES
    from svweave.adapters.mcp_client import MAX_RESULT_TEXT_CHARS

    lc = McpLaunch(backend="x", argv=["x"], env={}, stderr_log=Path("/nonexistent"))
    assert lc.max_message_bytes == MAX_MESSAGE_BYTES == 16 * MIB
    assert lc.max_result_chars == MAX_RESULT_TEXT_CHARS == 16 * MIB
    assert MAX_MESSAGE_BYTES > 10_485_744 * 1.5
    assert MAX_RESULT_TEXT_CHARS > 10_485_744


# ----------------------------------------------------------------- review follow-ups (2026-10-20)
def test_sec_overflow_kills_the_backend_even_if_nobody_reads_the_stream(tmp_path: Path) -> None:
    """The transport must not wait for a consumer before stopping a flooding backend."""
    import anyio
    from mcp.client.stdio import StdioServerParameters

    from svweave.adapters.bounded_stdio import TransportStats, bounded_stdio_client

    fake = str(Path(__file__).with_name("fake_mcp_server.py"))
    marker = str(32 * MIB + 7)  # a unique argv to find the child in /proc

    def alive() -> bool:
        for d in Path("/proc").iterdir():
            if d.name.isdigit():
                try:
                    if marker.encode() in (d / "cmdline").read_bytes():
                        return True
                except OSError:
                    pass
        return False

    async def go() -> tuple[bool, bool]:
        st = TransportStats()
        params = StdioServerParameters(command=sys.executable, args=[fake, "huge_banner", "2025-11-25", marker])
        async with bounded_stdio_client(params, max_message_bytes=1 * MIB, stats=st) as (_read, _write):
            seen_alive = False
            for _ in range(100):  # never receive from _read: nobody consumes the overflow
                await anyio.sleep(0.05)
                seen_alive = seen_alive or alive()
                if st.overflow is not None and not alive():
                    return seen_alive, True
            return seen_alive, False

    _seen, gone = anyio.run(go)
    assert gone, "a flooding backend stayed alive while the overflow waited for a reader"
    assert not alive()


def test_sec_crlf_terminator_is_not_counted_and_one_byte_over_is_refused(tmp_path: Path) -> None:
    limit = 256 * 1024
    with McpSession(sized(tmp_path / "ok", "crlf_line", limit, max_message_bytes=limit)) as s:
        r = s.call_tool("echo", {})  # payload exactly the limit, then \r\n: accepted
        assert not r.is_error and s.alive and s.transport_stats.overflow is None
    s = McpSession(sized(tmp_path / "over", "crlf_line", limit + 1, max_message_bytes=limit))
    s.start()
    try:
        with pytest.raises(SvwError) as ei:
            s.call_tool("echo", {})
        assert ei.value.details["reason"] == "message_too_large"
    finally:
        s.stop()


def test_sec_a_complete_line_one_byte_over_the_limit_is_refused(tmp_path: Path) -> None:
    """The complete-line branch (newline present), not only the no-newline flood."""
    limit = 256 * 1024
    s = McpSession(sized(tmp_path, "sized_line", limit + 1, max_message_bytes=limit))
    s.start()
    pid = s.pid
    try:
        with pytest.raises(SvwError) as ei:
            s.call_tool("echo", {})
        assert ei.value.details["reason"] == "message_too_large"
        assert ei.value.details["protocol_error"]["received_bytes_at_least"] == limit + 1
        assert _gone(s, pid)
    finally:
        s.stop()


def test_sec_only_the_oversized_call_gets_output_limit_others_see_backend_terminated(
    tmp_path: Path,
) -> None:
    import threading

    s = McpSession(sized(tmp_path, "mixed", 300_000, max_result_chars=100_000))
    s.start()
    out: dict[str, SvwError] = {}

    def call(tag: str) -> None:
        try:
            s.call_tool("echo", {"x": tag})
        except SvwError as e:
            out[tag] = e

    try:
        slow = threading.Thread(target=call, args=("slow",))
        slow.start()
        time.sleep(0.5)  # the slow call is in flight
        call("big")
        slow.join(30)
        big, other = out["big"], out["slow"]
        assert big.code is ErrorCode.OUTPUT_LIMIT and big.details["reason"] == "result_too_large"
        assert big.details["protocol_error"]["size_chars"] == 300_000
        # the bystander was not "too large": its backend was terminated under it
        assert other.code is ErrorCode.BACKEND_UNAVAILABLE, other.details
        assert other.details["reason"] == "backend_terminated"
    finally:
        s.stop()


def test_sec_a_spontaneous_flood_is_reported_and_reaped_by_the_adapter(tmp_path: Path) -> None:
    from types import SimpleNamespace

    from svweave.adapters.codebase_memory.adapter import CbmAdapter
    from svweave.adapters.serena.adapter import SerenaAdapter

    s = McpSession(sized(tmp_path, "spont_flood", 8 * MIB, max_message_bytes=1 * MIB))
    s.start()
    try:
        for _ in range(100):  # nothing is in flight: the backend floods on its own
            if not s.alive:
                break
            time.sleep(0.05)
        assert not s.alive and (s.violation or {}).get("kind") == "message_too_large"
        for cls in (CbmAdapter, SerenaAdapter):
            stopped: list[bool] = []
            fake_session = SimpleNamespace(alive=False, violation=s.violation,
                                           violation_error=s.violation_error,
                                           stop=lambda stopped=stopped: stopped.append(True))
            fake = SimpleNamespace(_session=fake_session, _failure=None)
            with pytest.raises(SvwError) as ei:
                cls._require_running(fake)  # type: ignore[arg-type]
            # was reason "protocol_error" and no stop(): detached daemons outlived the failure
            assert ei.value.details["reason"] == "message_too_large", cls.__name__
            assert stopped == [True], cls.__name__
    finally:
        s.stop()
