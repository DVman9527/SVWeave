"""Scripted stand-ins for an upstream MCP session (UNIT tests only: error branches)."""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

from svweave.adapters.mcp_client import InitInfo, ToolInfo, ToolResult
from svweave.domain.errors import ErrorCode, SvwError
from svweave.orchestration.toolchain import Toolchain

REPO = Path(__file__).resolve().parents[2]

Handler = Callable[[dict[str, Any]], "str | ToolResult"]


def toolchain() -> Toolchain:
    return Toolchain.load(REPO)


def tools_from_report(tc: Toolchain, key: str, *, tamper: dict[str, str] | None = None) -> dict[str, ToolInfo]:
    out: dict[str, ToolInfo] = {}
    for name, h in tc.pinned_tool_hashes(key).items():
        h = (tamper or {}).get(name, h)
        out[name] = ToolInfo(name, {}, None, None, schema_sha256=h, input_schema_sha256="0" * 64)
    return out


class FakeSession:
    """Implements the ``ToolCaller`` protocol. ``handlers[tool]`` returns text or a ToolResult."""

    def __init__(self, tools: dict[str, ToolInfo], protocol: str = "2025-11-25") -> None:
        self._tools = tools
        self._init = InitInfo(protocol, "fake", "1.0", {}, 0.0)
        self.handlers: dict[str, Handler] = {}
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.up = True
        self.stopped = False
        self.raise_on_call: SvwError | None = None

    @property
    def tools(self) -> dict[str, ToolInfo]:
        return self._tools

    @property
    def init(self) -> InitInfo | None:
        return self._init

    @property
    def alive(self) -> bool:
        return self.up

    def call_tool(self, name: str, arguments: dict[str, Any], timeout: float | None = None) -> ToolResult:
        self.calls.append((name, dict(arguments)))
        if self.raise_on_call is not None:
            raise self.raise_on_call
        h = self.handlers.get(name)
        if h is None:
            raise SvwError(ErrorCode.CAPABILITY_MISMATCH, f"fake has no handler for {name}", {})
        r = h(arguments)
        if isinstance(r, ToolResult):
            return r
        return ToolResult(name, r, False, None, 0.0)

    def stop(self) -> None:
        self.stopped = True
        self.up = False


def jtext(obj: Any) -> str:
    return json.dumps(obj)
