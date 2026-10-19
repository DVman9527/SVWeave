"""API-04: the real `svweave serve` process; stdout carries protocol frames only."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
from pathlib import Path
from typing import Any

import anyio
from mcp import Client
from mcp.client.stdio import StdioServerParameters

from tests.contract.conftest import Built

REPO = Path(__file__).resolve().parents[2]


def server_cmd(root: Path) -> list[str]:
    return [sys.executable, "-m", "svweave.cli", "--root", str(root), "serve", "--no-graph"]


def server_env() -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if k in ("PATH", "HOME", "XDG_CONFIG_HOME", "LANG")}
    env["PYTHONUNBUFFERED"] = "1"
    # the child must run the tree under test, not whatever is installed in the venv
    env["PYTHONPATH"] = os.pathsep.join(p for p in sys.path if p)
    return env


class RawStdio:
    """Hand-rolled line client so every byte of stdout can be inspected."""

    def __init__(self, root: Path, stderr_path: Path) -> None:
        self.stderr_f = open(stderr_path, "wb")  # noqa: SIM115
        self.p = subprocess.Popen(  # noqa: S603
            server_cmd(root),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=self.stderr_f,
            env=server_env(),
            cwd=REPO,
        )
        self.lines: list[str] = []
        self._cond = threading.Condition()
        self._t = threading.Thread(target=self._pump, daemon=True)
        self._t.start()

    def _pump(self) -> None:
        assert self.p.stdout is not None
        for raw in self.p.stdout:
            with self._cond:
                self.lines.append(raw.decode("utf-8"))
                self._cond.notify_all()

    def send(self, msg: dict[str, Any]) -> None:
        assert self.p.stdin is not None
        self.p.stdin.write((json.dumps(msg) + "\n").encode())
        self.p.stdin.flush()

    def response(self, rid: int, timeout: float = 60) -> dict[str, Any]:
        with self._cond:
            ok = self._cond.wait_for(
                lambda: any(json.loads(x).get("id") == rid for x in self.lines), timeout
            )
            assert ok, f"no response {rid}; got {self.lines}"
            return next(json.loads(x) for x in self.lines if json.loads(x).get("id") == rid)

    def close(self) -> int:
        assert self.p.stdin is not None
        self.p.stdin.close()
        try:
            rc = self.p.wait(timeout=20)
        except subprocess.TimeoutExpired:
            self.p.kill()
            rc = self.p.wait()
        self._t.join(5)
        self.stderr_f.close()
        return rc


def test_API_04_stdout_is_protocol_only(built: Built, tmp_path: Path) -> None:
    srv = RawStdio(built.ws.root, tmp_path / "stderr.log")
    try:
        srv.send(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": "2025-06-18",
                    "capabilities": {},
                    "clientInfo": {"name": "pytest", "version": "0"},
                },
            }
        )
        init = srv.response(1)
        assert init["result"]["serverInfo"]["name"] == "svweave"
        srv.send({"jsonrpc": "2.0", "method": "notifications/initialized"})
        srv.send({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
        tools = srv.response(2)["result"]["tools"]
        assert {t["name"] for t in tools} >= {"svw_status", "svw_hierarchy", "svw_evidence"}
        srv.send(
            {
                "jsonrpc": "2.0",
                "id": 3,
                "method": "tools/call",
                "params": {
                    "name": "svw_hierarchy",
                    "arguments": {"build_id": built.build_id, "limit": 3},
                },
            }
        )
        res = srv.response(3)["result"]
        assert res["isError"] is False
        env = res["structuredContent"]
        assert env["outcome"] == "OK" and len(env["data"]["items"]) == 3
        assert json.loads(res["content"][0]["text"]) == env
        srv.send(
            {
                "jsonrpc": "2.0",
                "id": 4,
                "method": "tools/call",
                "params": {"name": "svw_hierarchy", "arguments": {"build_id": "bad"}},
            }
        )
        assert srv.response(4)["result"]["isError"] is True
    finally:
        rc = srv.close()
    # every stdout line is a JSON-RPC frame; nothing else (no logs, no banners, no tracebacks)
    assert srv.lines, "server produced no output"
    for line in srv.lines:
        frame = json.loads(line)
        assert frame["jsonrpc"] == "2.0" and (
            "result" in frame or "error" in frame or "method" in frame
        ), line
    assert rc == 0
    err = (tmp_path / "stderr.log").read_text()
    assert "serving workspace on stdio" in err  # logs went to stderr
    assert "Traceback" not in err


def test_stray_print_in_a_handler_cannot_corrupt_stdout(built: Built, tmp_path: Path) -> None:
    """fd 1 is diverted while serving: a backend/child writing to stdout misses the wire."""
    code = (
        "import sys, builtins\n"
        "from svweave.gateway import tools as T\n"
        "orig = T._status\n"
        "def noisy(s, rid, a):\n"
        "    print('NOT-A-PROTOCOL-LINE'); import os; os.write(1, b'RAW-FD1-NOISE\\n')\n"
        "    return orig(s, rid, a)\n"
        "T.TOOLS['svw_status'] = type(T.TOOLS['svw_status'])(\n"
        "    'svw_status', 't', 'd', T.TOOLS['svw_status'].annotations, noisy,\n"
        "    T.TOOLS['svw_status'].input_schema, T.TOOLS['svw_status'].output_schema)\n"
        "from svweave.cli import main\n"
        f"sys.argv = ['svweave', '--root', {str(built.ws.root)!r}, 'serve', '--no-graph']\n"
        "main()\n"
    )
    p = subprocess.Popen(  # noqa: S603
        [sys.executable, "-c", code],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=server_env(),
        cwd=REPO,
    )
    assert p.stdin and p.stdout
    msgs = [
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": "2025-06-18",
                "capabilities": {},
                "clientInfo": {"name": "pytest", "version": "0"},
            },
        },
        {"jsonrpc": "2.0", "method": "notifications/initialized"},
        {
            "jsonrpc": "2.0",
            "id": 2,
            "method": "tools/call",
            "params": {"name": "svw_status", "arguments": {}},
        },
    ]
    p.stdin.write(("\n".join(json.dumps(m) for m in msgs) + "\n").encode())
    p.stdin.flush()
    got: list[str] = []
    while len(got) < 2:
        line = p.stdout.readline()
        assert line, "server closed early"
        got.append(line.decode())
    p.stdin.close()
    out_rest = p.stdout.read().decode()
    stderr = p.stderr.read().decode() if p.stderr else ""
    p.wait(timeout=20)
    for line in [*got, *out_rest.splitlines()]:
        assert json.loads(line)["jsonrpc"] == "2.0", line
    assert "RAW-FD1-NOISE" in stderr  # the handler really ran; its fd-1 output went to stderr
    assert "NOT-A-PROTOCOL-LINE" not in "".join(got) + out_rest
    assert "RAW-FD1-NOISE" not in "".join(got) + out_rest


def test_sdk_client_handshake_over_stdio_lists_tools_and_queries(built: Built) -> None:
    params = StdioServerParameters(
        command=sys.executable, args=server_cmd(built.ws.root)[1:], env=server_env(), cwd=str(REPO)
    )

    async def go() -> dict[str, Any]:
        async with Client(params) as c:
            names = {t.name for t in (await c.list_tools()).tools}
            r = await c.call_tool("svw_hierarchy", {"build_id": built.build_id, "limit": 2})
            assert r.structured_content is not None
            return {"names": names, "env": r.structured_content}

    out = anyio.run(go)
    assert (
        "svw_edit" in out["names"]
        and len(out["names"]) == 11
        and not any("approv" in n for n in out["names"])
    )
    assert out["env"]["outcome"] == "OK" and out["env"]["build_id"] == built.build_id


def test_server_subprocess_runs_the_tree_under_test() -> None:
    import svweave

    out = subprocess.run(  # noqa: S603
        [sys.executable, "-c", "import svweave; print(svweave.__file__)"],
        env=server_env(),
        cwd=REPO,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    assert Path(out).resolve() == Path(svweave.__file__).resolve()
