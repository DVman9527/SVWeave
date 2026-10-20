"""Bounded MCP stdio client transport (security: backend output must not exhaust gateway memory).

The official SDK's ``mcp.client.stdio.stdio_client`` reads the server's stdout as
``buffer + chunk`` until it sees a newline, with no length limit: a backend that writes one huge
line (or never writes the newline) grows the gateway's memory until it is stopped, and a per-call
timeout does not stop the reader from accumulating.

:func:`bounded_stdio_client` keeps the SDK's contract (same process launch in a new session /
process group, same anyio memory object streams of ``SessionMessage | Exception``, same stdin
writer, same JSON-RPC parsing, same shutdown sequence: close stdin, grace, SIGTERM then SIGKILL the
process group) and replaces only the stdout reader with one that reads raw bytes and never holds
more than ``max_message_bytes`` plus one read chunk. A line longer than the limit is a protocol
error: the reader stops reading, terminates the backend's process group at once and delivers a
:class:`MessageTooLarge` to the session (the client turns it into an ``SvwError``).

The launch / shutdown helpers are the SDK's own (``mcp`` is pinned by hash in
``toolchain.lock.json``); if a future SDK renames them the import fails loudly instead of silently
falling back to the unbounded reader.
"""

from __future__ import annotations

import logging
import sys
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager, suppress
from dataclasses import dataclass
from typing import TextIO

import anyio
import anyio.lowlevel
from mcp.client._transport import TransportStreams
from mcp.client.stdio import (
    _WRITER_FLUSH_TIMEOUT,
    StdioServerParameters,
    _aclose_all,
    _create_platform_compatible_process,
    _drain_stdout,
    _get_executable_command,
    _parse_line,
    _stop_server_process,
    _terminate_process_tree,
    get_default_environment,
)
from mcp.shared.message import SessionMessage

logger = logging.getLogger(__name__)

#: Largest JSON-RPC message (one stdout line, newline excluded) accepted from a backend.
#: Why 16 MiB never cuts a legitimate response from the pinned backends (measured 2026-10-20):
#: * cbm 0.11.0 refuses on its own side to send any response above its transport limit of
#:   10,485,744 bytes ("response too large: N bytes exceeds the 10485744 byte transport limit");
#:   the largest real responses seen were ``get_code_snippet(source_mode="full")`` lines of
#:   7.7 MB (7 MiB single-line module) and 10.9 MB (refused by cbm itself).
#: * Serena 1.7.0 truncates every tool answer to ``default_max_tool_answer_chars`` = 150,000
#:   characters (gateway Serena config), i.e. < 2 MB on the wire even fully ``\uXXXX``-escaped.
#: * ``initialize`` / ``tools/list`` are tens of KB.
#: The ceiling is ~1.6x cbm's own cap; anything above it is not a response a pinned backend sends.
MAX_MESSAGE_BYTES = 16 * 1024 * 1024

#: Bytes requested from the pipe per read (the buffer never exceeds the limit plus this).
READ_CHUNK_BYTES = 64 * 1024


class MessageTooLarge(Exception):
    """A backend stdout line exceeded ``limit_bytes`` (delivered to the session as a value)."""

    def __init__(self, limit_bytes: int, received_bytes: int) -> None:
        super().__init__(
            f"backend message exceeds {limit_bytes} bytes (at least {received_bytes} bytes without "
            "a complete line)"
        )
        self.limit_bytes = limit_bytes
        self.received_bytes = received_bytes


@dataclass
class TransportStats:
    """Reader instrumentation (diagnostics and tests): what the gateway actually buffered."""

    peak_buffer_bytes: int = 0
    max_line_bytes: int = 0
    lines: int = 0
    overflow: MessageTooLarge | None = None


@asynccontextmanager
async def bounded_stdio_client(
    server: StdioServerParameters,
    errlog: TextIO = sys.stderr,
    *,
    max_message_bytes: int = MAX_MESSAGE_BYTES,
    stats: TransportStats | None = None,
) -> AsyncGenerator[TransportStreams, None]:
    """Drop-in for ``mcp.client.stdio.stdio_client`` with a per-message length limit."""
    if max_message_bytes < 1:
        raise ValueError("max_message_bytes must be positive")
    if "\n".encode(server.encoding) != b"\n":
        raise ValueError(f"encoding {server.encoding!r} is not newline-compatible with byte framing")
    st = stats if stats is not None else TransportStats()
    command = await _get_executable_command(server.command)

    process = await _create_platform_compatible_process(
        command=command,
        args=server.args,
        env=get_default_environment() | (server.env or {}),
        errlog=errlog,
        cwd=server.cwd,
    )

    # As in the SDK: no awaits between the spawn and the task group.
    read_stream_writer, read_stream = anyio.create_memory_object_stream[SessionMessage | Exception](0)
    write_stream, write_stream_reader = anyio.create_memory_object_stream[SessionMessage](0)

    shutting_down = False
    writer_done = anyio.Event()

    def decode(raw: bytes) -> SessionMessage | Exception:
        try:
            text = raw.decode(server.encoding, errors=server.encoding_error_handler)
        except UnicodeDecodeError as exc:
            return exc  # surfaced to the session like a parse error (protocol violation)
        return _parse_line(text)

    async def overflow(received: int) -> None:
        exc = MessageTooLarge(max_message_bytes, received)
        st.overflow = exc
        # Stop the source FIRST (process group, SIGTERM then SIGKILL): delivering the exception goes
        # through a zero-capacity stream and blocks until someone receives, so a consumer that is
        # not reading must never be able to keep a flooding backend alive.
        with anyio.CancelScope(shield=True):
            await _terminate_process_tree(process)
        with suppress(anyio.ClosedResourceError, anyio.BrokenResourceError):
            await read_stream_writer.send(exc)

    async def stdout_reader() -> None:
        assert process.stdout, "Opened process is missing stdout"
        stdout = process.stdout
        buf = bytearray()
        scanned = 0  # bytes of buf already known to contain no newline
        try:
            async with read_stream_writer:
                try:
                    while True:
                        try:
                            chunk = await stdout.receive(READ_CHUNK_BYTES)
                        except anyio.EndOfStream:
                            return  # a trailing partial line is discarded, as in the SDK
                        buf += chunk
                        st.peak_buffer_bytes = max(st.peak_buffer_bytes, len(buf))
                        while True:
                            nl = buf.find(b"\n", scanned)
                            if nl < 0:
                                scanned = len(buf)
                                break
                            # the limit is the payload: a CRLF terminator's '\r' is not counted
                            size = nl - 1 if nl > 0 and buf[nl - 1] == 0x0D else nl
                            if size > max_message_bytes:
                                await overflow(nl)
                                return
                            line = bytes(buf[:nl])
                            del buf[: nl + 1]
                            scanned = 0
                            st.lines += 1
                            st.max_line_bytes = max(st.max_line_bytes, nl)
                            try:
                                await read_stream_writer.send(decode(line))
                            except (anyio.ClosedResourceError, anyio.BrokenResourceError):
                                return  # the session is gone; only the drain below remains
                        # one byte of slack for a '\r' whose '\n' has not arrived yet
                        if len(buf) > max_message_bytes + 1:
                            await overflow(len(buf))
                            return
                finally:
                    buf.clear()
                    await _drain_stdout(process)  # discards: memory stays bounded
        except anyio.ClosedResourceError:
            pass  # our own shutdown closed the stdout stream under the read
        except (anyio.BrokenResourceError, ConnectionError):
            # Teardown noise during shutdown, a real failure otherwise; either way the session
            # sees clean closure when the read stream closes (as in the SDK).
            if not shutting_down:
                logger.exception("Reading from the MCP server's stdout failed mid-session")

    async def stdin_writer() -> None:
        assert process.stdin, "Opened process is missing stdin"
        try:
            async with write_stream_reader:
                async for session_message in write_stream_reader:
                    json = session_message.message.model_dump_json(by_alias=True, exclude_unset=True)
                    data = (json + "\n").encode(encoding=server.encoding, errors=server.encoding_error_handler)
                    await process.stdin.send(data)
        except (anyio.ClosedResourceError, anyio.BrokenResourceError, OSError):
            await read_stream_writer.aclose()
        finally:
            writer_done.set()

    async def shutdown() -> None:
        read_stream.close()
        write_stream.close()
        with anyio.move_on_after(_WRITER_FLUSH_TIMEOUT) as flush_scope:
            await writer_done.wait()
        if flush_scope.cancelled_caught:
            await anyio.lowlevel.cancel_shielded_checkpoint()
        await _stop_server_process(process)
        await _aclose_all(read_stream, write_stream, read_stream_writer, write_stream_reader)
        await anyio.lowlevel.checkpoint()

    async with anyio.create_task_group() as tg:
        tg.start_soon(stdout_reader)
        tg.start_soon(stdin_writer)
        try:
            yield read_stream, write_stream
        finally:
            shutting_down = True
            with anyio.CancelScope(shield=True):
                await shutdown()
            tg.cancel_scope.cancel()
    await anyio.lowlevel.cancel_shielded_checkpoint()
