"""Fixtures for the MCP contract tests: a module-wide trusted-config home and a real demo build."""

from __future__ import annotations

import json
from collections.abc import Awaitable, Callable, Iterator
from pathlib import Path
from typing import Any

import anyio
import pytest
from mcp import Client

from svweave.application.build import run_build
from svweave.application.options import WorkspaceOptions
from svweave.application.session import Session
from svweave.gateway.tools import Gateway
from svweave.server import build_server
from tests.integration.demo_support import demo_ws


@pytest.fixture(scope="module")
def contract_home(tmp_path_factory: pytest.TempPathFactory) -> Iterator[Path]:
    mp = pytest.MonkeyPatch()
    xdg = tmp_path_factory.mktemp("xdg")
    mp.setenv("XDG_CONFIG_HOME", str(xdg))
    yield xdg
    mp.undo()


@pytest.fixture(autouse=True)
def _isolated_config_home(contract_home: Path) -> Path:  # overrides tests/conftest.py's
    return contract_home


class Built:
    def __init__(self, ws: Any, build_id: str, snapshot_id: str) -> None:
        self.ws, self.build_id, self.snapshot_id = ws, build_id, snapshot_id
        self.opts = WorkspaceOptions(root=ws.root)


@pytest.fixture(scope="module")
def built(contract_home: Path, tmp_path_factory: pytest.TempPathFactory) -> Built:
    ws = demo_ws(tmp_path_factory.mktemp("demo"))
    env = run_build(WorkspaceOptions(root=ws.root), "p", "req-test-build")
    assert env.build_id and env.snapshot_id and env.outcome.value == "OK", env.error
    return Built(ws, env.build_id, env.snapshot_id)


def make_gateway(opts: WorkspaceOptions, **kw: Any) -> Gateway:
    sess = Session(opts, use_graph=False, **kw)
    # every in-memory call() starts a fresh server whose end closes the session (cancelling
    # jobs); keep the session open across calls and close it explicitly with real_close()
    sess.real_close = sess.close  # type: ignore[attr-defined]
    sess.close = lambda: None  # type: ignore[method-assign]
    return Gateway(sess)


def mcp_run(gw: Gateway, fn: Callable[[Client], Awaitable[Any]]) -> Any:
    """Run ``fn(client)`` against an in-memory MCP client of a fresh server for ``gw``."""

    async def main() -> Any:
        async with Client(build_server(gw)) as c:
            return await fn(c)

    return anyio.run(main)


def call(gw: Gateway, name: str, args: dict[str, Any] | None = None) -> tuple[dict[str, Any], bool]:
    async def go(c: Client) -> tuple[dict[str, Any], bool]:
        r = await c.call_tool(name, args or {})
        assert r.structured_content is not None
        assert json.loads(r.content[0].text) == r.structured_content  # text block == structured
        return r.structured_content, bool(r.is_error)

    return mcp_run(gw, go)  # type: ignore[no-any-return]
