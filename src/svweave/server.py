"""``svweave serve --transport stdio``: the single external MCP server (plan 6.1, 14.1).

* uses the official MCP SDK for framing; no hand-written JSON-RPC;
* bound to ONE workspace given at start; requests carry no filesystem roots;
* stdout carries protocol frames only (the SDK also diverts fd 1 to stderr while serving, so a
  stray ``print`` cannot corrupt the stream); all logs go to stderr and to the session log files;
* protocol errors (unknown tool) are JSON-RPC errors, tool execution errors are ``isError``
  results, PARTIAL domain results are ordinary results; every result carries the envelope as
  structured content plus a text JSON block.
"""

from __future__ import annotations

import json
import logging
import sys
from contextlib import asynccontextmanager
from typing import Any

import anyio
import anyio.to_thread
import mcp_types as types
from mcp.server.lowlevel import Server
from mcp.server.stdio import stdio_server
from mcp.shared.exceptions import MCPError

from svweave import __version__
from svweave.application.events import EventLog
from svweave.application.options import WorkspaceOptions
from svweave.application.session import Session
from svweave.gateway.tools import Gateway, ToolSpec, UnknownToolError
from svweave.security.redaction import Redactor

SERVER_NAME = "svweave"
INSTRUCTIONS = (
    "SVWeave: build-aware SystemVerilog/UVM analysis. Start with svw_status, then svw_build "
    "(returns a job_id; poll svw_job). Semantic tools need build_id, svw_search needs snapshot_id. "
    "Read outcome/freshness/analysis_coverage of every envelope; PARTIAL or truncated results "
    "never prove absence. Editing (svw_edit) exists but is disabled unless both svweave.yaml "
    "and the trusted policy enable it; a commit needs a human approval bound to the patch hash."
)


def _annotations(spec: ToolSpec) -> types.ToolAnnotations:
    a = spec.annotations
    return types.ToolAnnotations(
        title=spec.title,
        read_only_hint=a.get("readOnlyHint"),
        destructive_hint=a.get("destructiveHint"),
        idempotent_hint=a.get("idempotentHint"),
        open_world_hint=a.get("openWorldHint"),
    )


def build_server(gateway: Gateway) -> Server[Any]:
    async def on_list_tools(
        ctx: Any, params: types.PaginatedRequestParams | None
    ) -> types.ListToolsResult:
        return types.ListToolsResult(
            tools=[
                types.Tool(
                    name=t.name,
                    title=t.title,
                    description=t.description,
                    input_schema=t.input_schema,
                    output_schema=t.output_schema,
                    annotations=_annotations(t),
                )
                for t in gateway.tools()
            ]
        )

    async def on_call_tool(ctx: Any, params: types.CallToolRequestParams) -> types.CallToolResult:
        try:
            out = await anyio.to_thread.run_sync(gateway.call, params.name, params.arguments)
        except UnknownToolError:
            raise MCPError(types.INVALID_PARAMS, f"unknown tool: {params.name[:64]}") from None
        payload = out.envelope.model_dump(mode="json")
        return types.CallToolResult(
            content=[
                types.TextContent(
                    text=json.dumps(payload, separators=(",", ":"), ensure_ascii=False)
                )
            ],
            structured_content=payload,
            is_error=out.is_error,
        )

    @asynccontextmanager
    async def lifespan(_: Server[Any]) -> Any:
        try:
            yield {}
        finally:
            await anyio.to_thread.run_sync(gateway.session.close)

    return Server(
        SERVER_NAME,
        version=__version__,
        instructions=INSTRUCTIONS,
        lifespan=lifespan,
        on_list_tools=on_list_tools,
        on_call_tool=on_call_tool,
    )


def configure_logging(log_file: Any = None) -> None:
    """stderr only (+ optional file). Never stdout."""
    root = logging.getLogger("svweave")
    root.setLevel(logging.INFO)
    for h in list(root.handlers):
        root.removeHandler(h)
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
    sh = logging.StreamHandler(sys.stderr)
    sh.setFormatter(fmt)
    root.addHandler(sh)
    if log_file is not None:
        fh = logging.FileHandler(log_file, encoding="utf-8")
        fh.setFormatter(fmt)
        root.addHandler(fh)
    root.propagate = False


def make_gateway(opts: WorkspaceOptions, *, use_graph: bool = True) -> Gateway:
    ctx = opts.load()  # fail fast (config/policy errors) before serving
    events = EventLog.for_session(ctx.state_dir, Redactor([ctx.project_root]))
    session = Session(opts, use_graph=use_graph, events=events)
    session.cursor_key()  # create the signing key now so read tools never write
    return Gateway(session, events)


async def _serve(server: Server[Any]) -> None:
    async with stdio_server() as (read_stream, write_stream):
        await server.run(read_stream, write_stream, server.create_initialization_options())


def serve_stdio(opts: WorkspaceOptions, *, use_graph: bool = True) -> None:
    gateway = make_gateway(opts, use_graph=use_graph)
    assert gateway.events is not None
    configure_logging(gateway.events.path.with_name("server.log"))
    logging.getLogger("svweave.gateway").info("serving workspace on stdio (graph=%s)", use_graph)
    try:
        anyio.run(_serve, build_server(gateway))
    finally:
        gateway.session.close()
