"""Tiny hand-rolled MCP stdio server used ONLY by unit tests (never as a backend stand-in).

Usage: python fake_mcp_server.py MODE [PROTOCOL]
MODE: ok | banner | midstream_garbage | exit_on_call | slow_call
"""

from __future__ import annotations

import json
import sys
import time

mode = sys.argv[1] if len(sys.argv) > 1 else "ok"
proto = sys.argv[2] if len(sys.argv) > 2 else "2025-11-25"

if mode == "daemon_child":
    import subprocess

    # like cbm: forks a daemon that detaches into its own session and outlives the server
    subprocess.Popen([sys.executable, "-I", "-c", "import time; time.sleep(300)", "svw-fake-daemon"],
                     start_new_session=True, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL)

if mode == "banner":
    sys.stdout.write("Welcome to fake backend v1 -- listening on stdio\n")
    sys.stdout.flush()
sys.stderr.write(
    "fake backend starting API_TOKEN=supersecretvalue123 at /home/someone/private/repo\n"
)
sys.stderr.flush()

TOOL = {
    "name": "echo",
    "description": "echo",
    "inputSchema": {"type": "object", "properties": {"x": {"type": "string"}}},
}


def send(obj: dict) -> None:
    sys.stdout.write(json.dumps(obj) + "\n")
    sys.stdout.flush()


for line in sys.stdin:
    line = line.strip()
    if not line:
        continue
    msg = json.loads(line)
    method = msg.get("method")
    rid = msg.get("id")
    if method == "initialize":
        send(
            {
                "jsonrpc": "2.0",
                "id": rid,
                "result": {
                    "protocolVersion": proto,
                    "capabilities": {"tools": {}},
                    "serverInfo": {"name": "fake", "version": "9.9"},
                },
            }
        )
    elif method == "tools/list":
        send({"jsonrpc": "2.0", "id": rid, "result": {"tools": [TOOL]}})
    elif method == "tools/call":
        if mode == "exit_on_call":
            sys.exit(3)
        if mode == "slow_call":
            time.sleep(30)
        if mode == "midstream_garbage":
            sys.stdout.write("DEBUG: not a protocol line\n")
            sys.stdout.flush()
        send(
            {
                "jsonrpc": "2.0",
                "id": rid,
                "result": {"content": [{"type": "text", "text": "pong"}], "isError": False},
            }
        )
