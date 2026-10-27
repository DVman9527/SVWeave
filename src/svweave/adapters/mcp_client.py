"""Upstream MCP client helper shared by the cbm and Serena adapters (plan 14.1, 14.2, 7.4).

* official ``mcp`` SDK only (no hand-written JSON-RPC framing);
* launch is an argument array, never a shell string; the environment is an explicit allowlist
  (the caller supplies every variable; the launch argv is prefixed with ``env -i`` semantics by the
  sandbox runner so nothing is inherited);
* the backend's stderr goes to a private log file; stdout carries protocol messages only. Any
  stdout line that is not a protocol message is a *protocol error* (test API-04) and is reported as
  ``BACKEND_UNAVAILABLE`` with ``details.reason == "protocol_error"``;
* the negotiated protocol revision must equal the revision pinned in ``capability_report.json``
  (test API-05); a different revision is ``CAPABILITY_MISMATCH``, never "use latest";
* shutdown follows the SDK sequence (close stdin, grace, then kill the whole process group);
* backend output is bounded (security.md 6): the stdio transport is the SDK contract with a
  length-limited reader (:mod:`svweave.adapters.bounded_stdio`); a stdout line longer than
  ``McpLaunch.max_message_bytes`` is ``BACKEND_UNAVAILABLE`` / ``reason == "message_too_large"``
  and a tool result whose text exceeds ``McpLaunch.max_result_chars`` is ``OUTPUT_LIMIT`` /
  ``reason == "result_too_large"``. Either way the backend is terminated (process group + token
  sweep) before the error is raised.

The async :class:`McpClient` is wrapped by the synchronous :class:`McpSession` (runs on a shared
event-loop thread) because the application layer and CLI are synchronous.
"""

from __future__ import annotations

import asyncio
import atexit
import contextlib
import json
import os
import signal
import threading
import time
import uuid
import weakref
from collections.abc import Awaitable, Callable, Coroutine
from dataclasses import dataclass, field
from pathlib import Path
from types import TracebackType
from typing import Any, Protocol, TypeVar

import anyio
import mcp_types as types
from mcp import MCPError
from mcp.client.session import ClientSession
from mcp.client.stdio import StdioServerParameters

from svweave.adapters.bounded_stdio import (
    MAX_MESSAGE_BYTES,
    MessageTooLarge,
    TransportStats,
    bounded_stdio_client,
)
from svweave.domain.errors import ErrorCode, SvwError
from svweave.domain.ids import canonical_json, sha256_hex
from svweave.security import safeio
from svweave.security.redaction import Redactor

T = TypeVar("T")

TOKEN_ENV = "SVWEAVE_BACKEND_TOKEN"
CLIENT_NAME = "svweave-gateway"

#: Largest concatenated text of one tool result handed to the adapters (characters). Decoded text
#: is never longer than the JSON line it came in, so with the default transport limit this is a
#: second, transport-independent guard (it also bounds the join of many content items and stays in
#: force if ``max_message_bytes`` is raised). Real results stay below cbm's own 10,485,744-byte
#: response cap and Serena's 150,000-character answer cap (see ``bounded_stdio.MAX_MESSAGE_BYTES``).
MAX_RESULT_TEXT_CHARS = 16 * 1024 * 1024

#: Violation kinds that mean "backend output too large": the backend is terminated at once.
SIZE_VIOLATIONS = frozenset({"message_too_large", "result_too_large"})


def tool_schema_sha256(tool: dict[str, Any]) -> str:
    """Per-tool schema digest; identical to the M0 probe (name + inputSchema + outputSchema)."""
    core = {
        "name": tool.get("name"),
        "inputSchema": tool.get("inputSchema"),
        "outputSchema": tool.get("outputSchema"),
    }
    return sha256_hex(canonical_json(core))


@dataclass(frozen=True)
class ToolInfo:
    name: str
    input_schema: dict[str, Any]
    output_schema: dict[str, Any] | None
    annotations: dict[str, Any] | None
    schema_sha256: str
    input_schema_sha256: str


@dataclass(frozen=True)
class InitInfo:
    protocol_version: str
    server_name: str
    server_version: str
    capabilities: dict[str, Any]
    initialize_s: float


@dataclass(frozen=True)
class ToolResult:
    tool: str
    text: str
    is_error: bool
    structured: dict[str, Any] | None
    elapsed_s: float

    def json(self) -> Any:
        """Parse the text content as JSON (``None`` if it is not JSON)."""
        try:
            return json.loads(self.text)
        except (ValueError, TypeError):
            return None


@dataclass
class McpLaunch:
    """How to start one upstream backend. ``argv`` is the FINAL argv (sandbox prefix included)."""

    backend: str
    argv: list[str]
    env: dict[str, str]
    stderr_log: Path
    cwd: Path | None = None
    #: Pinned protocol revision from capability_report.json; ``None`` disables the check.
    expected_protocol: str | None = None
    init_timeout: float = 60.0
    call_timeout: float = 120.0
    redactor: Redactor = field(default_factory=Redactor)
    log_max_bytes: int = 4 * 1024 * 1024
    #: Longest stdout line (one JSON-RPC message) accepted from the backend, in bytes.
    max_message_bytes: int = MAX_MESSAGE_BYTES
    #: Longest concatenated text of one tool result, in characters.
    max_result_chars: int = MAX_RESULT_TEXT_CHARS


def _leaf(exc: BaseException) -> BaseException:
    while isinstance(exc, BaseExceptionGroup) and exc.exceptions:
        exc = exc.exceptions[0]
    return exc


def _proc_ppid(pid: int) -> int | None:
    try:
        with open(f"/proc/{pid}/stat") as f:
            return int(f.read().rsplit(")", 1)[1].split()[1])
    except (OSError, ValueError, IndexError):
        return None


def _proc_state(pid: int) -> str | None:
    try:
        with open(f"/proc/{pid}/stat") as f:
            return f.read().rsplit(")", 1)[1].split()[0]
    except (OSError, IndexError):
        return None


def find_backend_pid(token: str) -> int | None:
    """Direct child of this process whose environment carries ``token`` (None if gone)."""
    me = os.getpid()
    needle = f"{TOKEN_ENV}={token}".encode()
    for d in os.listdir("/proc"):
        if not d.isdigit() or _proc_ppid(int(d)) != me:
            continue
        try:
            with open(f"/proc/{d}/environ", "rb") as f:
                if needle in f.read().split(b"\0"):
                    return int(d)
        except OSError:
            continue
    return None


def _token_pids(token: str) -> list[int]:
    """Every process of this user whose environment carries ``token`` (backend + daemon children,
    including ones that detached with setsid and would survive a process-group kill)."""
    needle = f"{TOKEN_ENV}={token}".encode()
    out: list[int] = []
    for d in os.listdir("/proc"):
        if not d.isdigit() or int(d) == os.getpid():
            continue
        try:
            with open(f"/proc/{d}/environ", "rb") as f:
                if needle in f.read().split(b"\0"):
                    out.append(int(d))
        except OSError:
            continue
    return out


def kill_token_processes(token: str) -> int:
    """SIGKILL everything still carrying ``token``; returns how many were signalled."""
    pids = _token_pids(token)
    for pid in pids:
        with contextlib.suppress(OSError):
            os.kill(pid, signal.SIGKILL)
    return len(pids)


#: Clients that were started and not yet stopped; swept at interpreter exit so a forgotten
#: adapter can never leave a backend (or its daemon) behind.
_LIVE: weakref.WeakSet[McpClient] = weakref.WeakSet()


@atexit.register
def _sweep_live_clients() -> None:
    for c in list(_LIVE):
        kill_token_processes(c.token)


class McpClient:
    """One upstream MCP server over stdio. Create, ``await start()``, call, ``await stop()``."""

    def __init__(self, launch: McpLaunch) -> None:
        self.launch = launch
        self.token = uuid.uuid4().hex
        self.init: InitInfo | None = None
        self.tools: dict[str, ToolInfo] = {}
        self._violation: dict[str, Any] | None = None
        self._session: ClientSession | None = None
        self._owner: asyncio.Task[None] | None = None
        self._stop: asyncio.Event | None = None
        self._ready: asyncio.Future[None] | None = None
        self._inflight: set[anyio.CancelScope] = set()
        self._dead_reason: str | None = None
        self._stopped = False
        self._pid: int | None = None
        self.transport_stats = TransportStats()

    @property
    def violation(self) -> dict[str, Any] | None:
        # The transport records overflow before terminating the process, then delivers the
        # exception asynchronously. Expose its reason even while that delivery is pending.
        overflow = self.transport_stats.overflow
        if self._violation is None and overflow is not None:
            return {
                "kind": "message_too_large",
                "limit_bytes": overflow.limit_bytes,
                "received_bytes_at_least": overflow.received_bytes,
            }
        return self._violation

    @violation.setter
    def violation(self, value: dict[str, Any] | None) -> None:
        self._violation = value

    # ------------------------------------------------------------------ errors
    def _err(self, code: ErrorCode, message: str, reason: str, **extra: Any) -> SvwError:
        details: dict[str, Any] = {
            "backend": self.launch.backend,
            "reason": reason,
            "stderr_tail": self.launch.redactor.tail_file(self.launch.stderr_log, 2000),
            **extra,
        }
        return SvwError(code, message, details)

    def _violation_error(self) -> SvwError:
        kind = (self.violation or {}).get("kind")
        if kind == "message_too_large":
            return self._err(
                ErrorCode.BACKEND_UNAVAILABLE,
                f"{self.launch.backend}: backend wrote a protocol message larger than "
                f"{self.launch.max_message_bytes} bytes (protocol error); backend terminated",
                "message_too_large",
                protocol_error=self.violation or {},
            )
        if kind == "result_too_large":
            # Only the call whose result was too large gets OUTPUT_LIMIT (built in call_tool);
            # every OTHER call on this session just lost its backend.
            return self._err(
                ErrorCode.BACKEND_UNAVAILABLE,
                f"{self.launch.backend}: backend terminated after another call's result exceeded "
                f"{self.launch.max_result_chars} characters",
                "backend_terminated",
                protocol_error=self.violation or {},
            )
        return self._err(
            ErrorCode.BACKEND_UNAVAILABLE,
            f"{self.launch.backend}: backend wrote non-protocol data to stdout (protocol error)",
            "protocol_error",
            protocol_error=self.violation or {},
        )

    # ------------------------------------------------------------------ message handler
    async def _on_message(self, message: object) -> None:
        if isinstance(message, MessageTooLarge) and self._violation is None:
            self.violation = {
                "kind": "message_too_large",
                "limit_bytes": message.limit_bytes,
                "received_bytes_at_least": message.received_bytes,
            }
            for scope in list(self._inflight):
                scope.cancel()
            return
        if isinstance(message, Exception) and self.violation is None:
            sample = ""
            errors = getattr(message, "errors", None)
            if callable(errors):
                try:
                    sample = str(errors()[0].get("input", ""))
                except Exception:  # defensive: only used for a diagnostic sample
                    sample = ""
            if not sample:
                sample = str(message)
            self.violation = {
                "kind": "non_protocol_stdout",
                "sample": self.launch.redactor.text(sample[:200]),
            }
            for scope in list(self._inflight):
                scope.cancel()

    # ------------------------------------------------------------------ lifecycle
    async def start(self) -> InitInfo:
        if self._owner is not None:
            raise RuntimeError("McpClient already started")
        loop = asyncio.get_running_loop()
        self._stop = asyncio.Event()
        self._ready = loop.create_future()
        self._owner = loop.create_task(self._run(), name=f"mcp-{self.launch.backend}")
        _LIVE.add(self)
        try:
            await self._ready
        except BaseException:
            await self.stop()
            raise
        assert self.init is not None
        return self.init

    async def _guard(self, make: Callable[[], Awaitable[T]], timeout: float, what: str) -> T:
        scope = anyio.CancelScope()
        self._inflight.add(scope)
        try:
            with scope:
                try:
                    with anyio.fail_after(timeout):
                        result = await make()
                except TimeoutError:
                    raise self._err(
                        ErrorCode.BACKEND_UNAVAILABLE,
                        f"{self.launch.backend}: {what} timed out after {timeout:.0f}s",
                        "timeout",
                        operation=what,
                    ) from None
            if scope.cancelled_caught or self.violation is not None:
                raise self._violation_error()
            return result
        except SvwError:
            raise
        except MCPError as e:
            if self.violation is not None:
                raise self._violation_error() from e
            raise self._err(
                ErrorCode.BACKEND_UNAVAILABLE,
                f"{self.launch.backend}: {what} failed: {e}",
                "process_exited" if not self.alive else "mcp_error",
                operation=what,
            ) from e
        except Exception as e:
            if self.violation is not None:
                raise self._violation_error() from e
            if isinstance(e, RuntimeError) and "protocol version" in str(e).lower():
                raise self._wrap_start_error(e) from e
            raise self._err(
                ErrorCode.BACKEND_UNAVAILABLE,
                f"{self.launch.backend}: {what} failed: {type(e).__name__}: {e}",
                "process_exited" if not self.alive else "client_error",
                operation=what,
            ) from e
        finally:
            self._inflight.discard(scope)

    async def _run(self) -> None:
        assert self._ready is not None and self._stop is not None
        la = self.launch
        la.stderr_log.parent.mkdir(parents=True, exist_ok=True)
        # Opened by the HOST before launch (no-follow, regular file only, rotated at a size cap);
        # the log lives outside every path the backend can write.
        try:
            fd = safeio.open_append_log(la.stderr_log, la.log_max_bytes)
        except BaseException as e:
            if not self._ready.done():
                self._ready.set_exception(e)
            self._stopped = True
            return
        errlog = os.fdopen(fd, "a")
        try:
            env = {**la.env, TOKEN_ENV: self.token}
            argv = list(la.argv)
            if argv[:2] == ["/usr/bin/env", "-i"]:
                # `env -i` execs the backend with ONLY the listed variables: carry the pid-lookup
                # token across that exec, otherwise degraded (unsandboxed) mode looks dead.
                argv.insert(2, f"{TOKEN_ENV}={self.token}")
            params = StdioServerParameters(
                command=argv[0], args=argv[1:], env=env, cwd=la.cwd
            )
            try:
                async with bounded_stdio_client(  # noqa: SIM117
                    params,
                    errlog=errlog,
                    max_message_bytes=la.max_message_bytes,
                    stats=self.transport_stats,
                ) as (r, w):
                    async with ClientSession(
                        r,
                        w,
                        read_timeout_seconds=la.call_timeout,
                        message_handler=self._on_message,
                        client_info=types.Implementation(name=CLIENT_NAME, version="0.1.0"),
                    ) as session:
                        self._session = session
                        await self._handshake(session)
                        if not self._ready.done():
                            self._ready.set_result(None)
                        await self._stop.wait()
            except BaseException as e:
                leaf = _leaf(e)
                err = leaf if isinstance(leaf, SvwError) else self._wrap_start_error(leaf)
                self._dead_reason = f"{type(leaf).__name__}: {leaf}"[:300]
                if not self._ready.done():
                    self._ready.set_exception(err)
                elif not isinstance(leaf, asyncio.CancelledError):
                    pass  # session died after start; callers see it through alive/_guard
        finally:
            self._stopped = True
            errlog.close()

    def _wrap_start_error(self, e: BaseException) -> SvwError:
        if self.violation is not None:
            return self._violation_error()
        if isinstance(e, FileNotFoundError | PermissionError | NotADirectoryError):
            return self._err(
                ErrorCode.BACKEND_UNAVAILABLE,
                f"{self.launch.backend}: cannot spawn backend: {e}",
                "spawn_failed",
            )
        if isinstance(e, RuntimeError) and "protocol version" in str(e).lower():
            return self._err(
                ErrorCode.CAPABILITY_MISMATCH,
                f"{self.launch.backend}: unsupported protocol revision offered by the backend: {e}",
                "protocol_version_unsupported",
                expected=self.launch.expected_protocol,
            )
        return self._err(
            ErrorCode.BACKEND_UNAVAILABLE,
            f"{self.launch.backend}: backend failed to start: {type(e).__name__}: {e}",
            "start_failed",
        )

    async def _handshake(self, session: ClientSession) -> None:
        la = self.launch
        t0 = time.monotonic()
        res = await self._guard(session.initialize, la.init_timeout, "initialize")
        info = InitInfo(
            protocol_version=str(res.protocol_version),
            server_name=res.server_info.name,
            server_version=res.server_info.version,
            capabilities=res.capabilities.model_dump(mode="json", by_alias=True, exclude_none=True),
            initialize_s=round(time.monotonic() - t0, 3),
        )
        self.init = info
        if la.expected_protocol is not None and info.protocol_version != la.expected_protocol:
            raise self._err(
                ErrorCode.CAPABILITY_MISMATCH,
                f"{la.backend}: negotiated protocol {info.protocol_version} differs from pinned "
                f"{la.expected_protocol}",
                "protocol_version_mismatch",
                expected=la.expected_protocol,
                actual=info.protocol_version,
            )
        tools: dict[str, ToolInfo] = {}
        cursor: str | None = None
        while True:
            page = await self._guard(
                lambda c=cursor: session.list_tools(  # type: ignore[misc]
                    params=types.PaginatedRequestParams(cursor=c) if c else None
                ),
                la.init_timeout,
                "tools/list",
            )
            for t in page.tools:
                d = t.model_dump(mode="json", by_alias=True, exclude_none=True)
                tools[t.name] = ToolInfo(
                    name=t.name,
                    input_schema=d.get("inputSchema", {}),
                    output_schema=d.get("outputSchema"),
                    annotations=d.get("annotations"),
                    schema_sha256=tool_schema_sha256(d),
                    input_schema_sha256=sha256_hex(canonical_json(d.get("inputSchema"))),
                )
            cursor = page.next_cursor
            if not cursor:
                break
        self.tools = tools

    # ------------------------------------------------------------------ liveness
    @property
    def pid(self) -> int | None:
        if self._pid is None:
            self._pid = find_backend_pid(self.token)
        return self._pid

    @property
    def alive(self) -> bool:
        if self._stopped or self._session is None or self._dead_reason is not None:
            return False
        if self.violation is not None:
            return False
        pid = self.pid
        if pid is None:
            return False
        state = _proc_state(pid)
        return state is not None and state not in ("Z", "X")

    # ------------------------------------------------------------------ calls
    async def call_tool(
        self, name: str, arguments: dict[str, Any], *, timeout: float | None = None
    ) -> ToolResult:
        if self._session is None or self._stopped:
            raise self._err(ErrorCode.BACKEND_UNAVAILABLE, f"{self.launch.backend}: not running", "not_running")
        if name not in self.tools:
            raise self._err(
                ErrorCode.CAPABILITY_MISMATCH,
                f"{self.launch.backend}: tool {name!r} not present in tools/list",
                "tool_missing",
                tool=name,
            )
        t = timeout or self.launch.call_timeout
        t0 = time.monotonic()
        session = self._session
        try:
            res = await self._guard(
                lambda: session.call_tool(name, arguments, read_timeout_seconds=t), t + 5, f"tools/call {name}"
            )
        except SvwError:
            if (self.violation or {}).get("kind") in SIZE_VIOLATIONS:
                await self.stop()  # the transport already killed the group; reap and close
            raise
        texts: list[str] = [x for c in res.content if (x := getattr(c, "text", None)) is not None]
        # Check the size BEFORE concatenating (no second copy of an oversized result).
        size = sum(len(x) for x in texts) + max(len(texts) - 1, 0)
        if size > self.launch.max_result_chars:
            if self.violation is None:
                self.violation = {
                    "kind": "result_too_large",
                    "tool": name,
                    "limit_chars": self.launch.max_result_chars,
                    "size_chars": size,
                }
            del res, texts
            err = self._err(  # THIS call's own error, with its own figures
                ErrorCode.OUTPUT_LIMIT,
                f"{self.launch.backend}: tool result text exceeds {self.launch.max_result_chars} "
                "characters; backend terminated",
                "result_too_large",
                protocol_error={"kind": "result_too_large", "tool": name,
                                "limit_chars": self.launch.max_result_chars, "size_chars": size},
            )
            await self.stop()
            raise err
        text = "\n".join(texts)
        return ToolResult(
            tool=name,
            text=text,
            is_error=bool(res.is_error),
            structured=res.structured_content,
            elapsed_s=round(time.monotonic() - t0, 3),
        )

    async def stop(self) -> None:
        if self._owner is None:
            return
        if self._stop is not None:
            self._stop.set()
        try:
            await asyncio.wait_for(asyncio.shield(self._owner), timeout=30)
        except (TimeoutError, asyncio.CancelledError):
            self._owner.cancel()
            await asyncio.gather(self._owner, return_exceptions=True)
        except BaseException:
            pass
        self._stopped = True
        # The SDK killed the process group; a daemon that detached (setsid) is still ours to reap.
        kill_token_processes(self.token)
        _LIVE.discard(self)


# ---------------------------------------------------------------------- sync facade
class LoopThread:
    """A daemon thread running one asyncio event loop; sync code submits coroutines to it."""

    def __init__(self) -> None:
        self._loop = asyncio.new_event_loop()
        self._ready = threading.Event()
        self._thread = threading.Thread(target=self._main, name="svweave-mcp-loop", daemon=True)
        self._thread.start()
        self._ready.wait()

    def _main(self) -> None:
        asyncio.set_event_loop(self._loop)
        self._loop.call_soon(self._ready.set)
        self._loop.run_forever()

    def run(self, coro: Coroutine[Any, Any, T], timeout: float | None = None) -> T:
        fut = asyncio.run_coroutine_threadsafe(coro, self._loop)
        try:
            return fut.result(timeout)
        except TimeoutError:
            fut.cancel()
            raise SvwError(
                ErrorCode.BACKEND_UNAVAILABLE, "operation timed out", {"reason": "timeout"}
            ) from None


_SHARED: LoopThread | None = None
_SHARED_LOCK = threading.Lock()


def shared_loop() -> LoopThread:
    global _SHARED
    with _SHARED_LOCK:
        if _SHARED is None:
            _SHARED = LoopThread()
        return _SHARED


class ToolCaller(Protocol):
    """What adapters need from an upstream session (real :class:`McpSession` or a test fake)."""

    @property
    def tools(self) -> dict[str, ToolInfo]: ...

    @property
    def init(self) -> InitInfo | None: ...

    @property
    def alive(self) -> bool: ...

    def call_tool(
        self, name: str, arguments: dict[str, Any], timeout: float | None = None
    ) -> ToolResult: ...

    def stop(self) -> None: ...


class McpSession:
    """Synchronous wrapper around :class:`McpClient`."""

    def __init__(self, launch: McpLaunch, loop: LoopThread | None = None) -> None:
        self._loop = loop or shared_loop()
        self._client = McpClient(launch)
        self.launch = launch

    def start(self) -> InitInfo:
        return self._loop.run(self._client.start(), timeout=self.launch.init_timeout * 3 + 30)

    @property
    def tools(self) -> dict[str, ToolInfo]:
        return self._client.tools

    @property
    def init(self) -> InitInfo | None:
        return self._client.init

    @property
    def alive(self) -> bool:
        return self._client.alive

    @property
    def pid(self) -> int | None:
        return self._client.pid

    @property
    def violation(self) -> dict[str, Any] | None:
        return self._client.violation

    def violation_error(self) -> SvwError:
        """The error a caller should see for this session's recorded violation (for adapters that
        notice it later, e.g. a backend that flooded while no call was in flight)."""
        return self._client._violation_error()

    @property
    def transport_stats(self) -> TransportStats:
        return self._client.transport_stats

    def call_tool(
        self, name: str, arguments: dict[str, Any], timeout: float | None = None
    ) -> ToolResult:
        t = timeout or self.launch.call_timeout
        return self._loop.run(self._client.call_tool(name, arguments, timeout=t), timeout=t + 30)

    def stop(self) -> None:
        self._loop.run(self._client.stop(), timeout=60)

    def __enter__(self) -> McpSession:
        self.start()
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.stop()
