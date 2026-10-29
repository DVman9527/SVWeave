"""GLB-09: two actual SDK stdio gateways, without native backend execution.

Unlike the launcher unit tests this exercises process startup, initialization,
request validation, protocol-only stdout and cleanup. It is not a native backend
or real Claude/Codex connection qualification test.
"""

from __future__ import annotations

import json
import os
import selectors
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

from svweave.config.trusted import trusted_dir
from tests.conftest import Ws, profile

REPO = Path(__file__).resolve().parents[2]


def _probe(root: Path, other: Path, launch_dir: Path, config_home: Path) -> dict[str, Any]:
    # No inherited project/toolchain overrides, tokens, client config or user home.
    env = {
        "HOME": str(launch_dir / "home"),
        "XDG_CONFIG_HOME": str(config_home),
        "LANG": "C.UTF-8",
        "PATH": os.defpath,
        "PYTHONPATH": str(REPO / "src"),
        "SVWEAVE_PROJECT_ROOT": str(root),
    }
    process = subprocess.Popen(
        [sys.executable, "-m", "svweave.launcher", "--no-graph"],
        cwd=launch_dir,
        env=env,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        bufsize=1,
    )
    assert process.stdin is not None and process.stdout is not None and process.stderr is not None
    frames: list[dict[str, Any]] = []
    selector = selectors.DefaultSelector()
    selector.register(process.stdout, selectors.EVENT_READ)

    def exchange(message: dict[str, Any]) -> dict[str, Any]:
        assert process.stdin is not None and process.stdout is not None
        process.stdin.write(json.dumps(message) + "\n")
        process.stdin.flush()
        if "id" not in message:
            return {}
        while True:
            assert selector.select(15), f"timeout: {root.name}, request {message['id']}"
            line = process.stdout.readline()
            assert line, f"gateway closed early: {root.name}, exit={process.poll()}"
            frame = json.loads(line)
            frames.append(frame)
            assert frame["jsonrpc"] == "2.0"
            if frame.get("id") == message["id"]:
                return frame

    try:
        initialized = exchange(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": "2025-06-18",
                    "capabilities": {"roots": {"listChanged": True}},
                    "clientInfo": {"name": "global-launcher-contract", "version": "0"},
                },
            }
        )
        assert initialized["result"]["serverInfo"]["name"] == "svweave"
        exchange({"jsonrpc": "2.0", "method": "notifications/initialized"})
        listed = exchange({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
        assert len(listed["result"]["tools"]) == 14
        # Client roots capability and request arguments cannot select another project.
        # These calls fail schema validation before status or any sandbox/backend probe.
        for number, field in enumerate(("root", "workspace", "repo"), 3):
            result = exchange(
                {
                    "jsonrpc": "2.0",
                    "id": number,
                    "method": "tools/call",
                    "params": {"name": "svw_status", "arguments": {field: str(other)}},
                }
            )["result"]
            assert result["isError"] is True
            envelope = result["structuredContent"]
            assert envelope["workspace_id"] == root.name
            assert envelope["error"]["code"] == "INVALID_CONFIG"
        process.stdin.close()
        process.wait(timeout=15)
        remaining = process.stdout.read()
        stderr = process.stderr.read()
        assert process.returncode == 0, stderr
        assert not remaining.strip()  # all stdout bytes were protocol frames above
        assert "Traceback" not in stderr
        assert "serving workspace on stdio" in stderr
        assert not (root / ".svweave" / "backends").exists()
        return {
            "root": root,
            "frames": len(frames),
            "key": (trusted_dir(root) / "cursor.key").read_bytes(),
        }
    finally:
        selector.close()
        if process.poll() is None:
            process.kill()
            process.wait(timeout=15)
        for stream in (process.stdin, process.stdout, process.stderr):
            if stream is not None:
                stream.close()


def test_GLB_09_two_actual_stdio_processes_keep_independent_workspace_binding(
    tmp_path: Path,
    _isolated_config_home: Path,
) -> None:
    roots = [tmp_path / "project-a", tmp_path / "project-b"]
    for root in roots:
        ws = Ws(root)
        ws.config({"p": profile()}, project={"id": root.name, "root": ".", "default_profile": "p"})
        ws.policy()
    launch_dir = tmp_path / "unrelated-cwd"
    (launch_dir / "home").mkdir(parents=True)
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [
            pool.submit(_probe, root, roots[1 - index], launch_dir, _isolated_config_home)
            for index, root in enumerate(roots)
        ]
        outcomes = [future.result(timeout=60) for future in futures]
    assert [outcome["frames"] for outcome in outcomes] == [5, 5]
    assert outcomes[0]["key"] != outcomes[1]["key"]
    assert not (launch_dir / ".svweave").exists()
    assert not list((launch_dir / "home").iterdir())
