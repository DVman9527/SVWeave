"""Unit tests must never launch a real backend (cbm / Serena): they use fakes. A guard fails any
test that tries, instead of silently leaking sandboxed processes."""

from __future__ import annotations

from typing import Any

import pytest

from svweave.adapters.mcp_client import McpClient
from svweave.security.sandbox import SandboxRunner

REAL_BACKENDS = ("codebase-memory-mcp", "serena")


def _is_real(argv: list[str]) -> bool:
    return any(a.rsplit("/", 1)[-1] in REAL_BACKENDS for a in argv)


@pytest.fixture(autouse=True)
def _no_real_backends(monkeypatch: pytest.MonkeyPatch) -> None:
    real_start = McpClient.start
    real_run_once = SandboxRunner.run_once

    async def start(self: McpClient) -> Any:
        if _is_real(self.launch.argv):
            raise AssertionError(f"unit test tried to launch a REAL backend: {self.launch.argv[-6:]}")
        return await real_start(self)

    def run_once(self: SandboxRunner, spec: Any, inner_argv: Any, **kw: Any) -> Any:
        would_run = self.capability.available or self.allow_degraded  # else wrap() refuses first
        if would_run and _is_real(list(inner_argv)):
            raise AssertionError(f"unit test tried to run a REAL backend CLI: {list(inner_argv)[:3]}")
        return real_run_once(self, spec, inner_argv, **kw)

    monkeypatch.setattr(McpClient, "start", start)
    monkeypatch.setattr(SandboxRunner, "run_once", run_once)


@pytest.fixture
def cand(tmp_path: Any) -> Any:
    """A small SystemVerilog candidate directory (shared by the Serena unit tests)."""
    from tests.unit.test_adapters_serena import SRC, UVM

    c = tmp_path / "cand"
    (c / "uvm").mkdir(parents=True)
    (c / UVM).write_text(SRC)
    (c / "other.sv").write_text("module o; endmodule\n")
    return c
