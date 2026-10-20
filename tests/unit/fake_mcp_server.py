"""Tiny hand-rolled MCP stdio server used ONLY by unit tests (never as a backend stand-in).

Usage: python fake_mcp_server.py MODE [PROTOCOL] [SIZE]
MODE: ok | banner | midstream_garbage | exit_on_call | slow_call | daemon_child
      huge_banner  (SIZE bytes on stdout without a newline before the handshake)
      huge_line    (on tools/call: SIZE bytes without a newline, then hang)
      big_result   (on tools/call: a valid result whose text is SIZE characters)
      sized_line   (on tools/call: a valid result line of exactly SIZE bytes, newline excluded)
      crlf_line    (as sized_line, but terminated by CRLF)
      spont_flood  (after tools/list: SIZE bytes without a newline, unprompted, then hang)
      mixed        (tools/call x="slow": never answers; x="big": a SIZE-character result)
"""

from __future__ import annotations

import json
import sys
import time

mode = sys.argv[1] if len(sys.argv) > 1 else "ok"
proto = sys.argv[2] if len(sys.argv) > 2 else "2025-11-25"
size = int(sys.argv[3]) if len(sys.argv) > 3 else 0


def flood(n: int) -> None:
    """Write n bytes of non-newline data (blocks once the reader stops reading), then hang."""
    sys.stdout.flush()
    block = b"x" * (1024 * 1024)
    left = n
    while left > 0:
        k = min(left, len(block))
        sys.stdout.buffer.write(block[:k])
        sys.stdout.buffer.flush()
        left -= k
    time.sleep(120)
    sys.exit(0)


if mode == "huge_banner":
    flood(size)

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
        if mode == "spont_flood":
            time.sleep(0.3)
            flood(size)
    elif method == "tools/call":
        if mode == "exit_on_call":
            sys.exit(3)
        if mode == "slow_call":
            time.sleep(30)
        if mode == "huge_line":
            flood(size)
        if mode == "mixed":
            x = (msg.get("params") or {}).get("arguments", {}).get("x")
            if x == "slow":
                continue  # never answered
            if x == "big":
                send({"jsonrpc": "2.0", "id": rid,
                      "result": {"content": [{"type": "text", "text": "z" * size}], "isError": False}})
                continue
        if mode == "crlf_line":
            frame = {"jsonrpc": "2.0", "id": rid,
                     "result": {"content": [{"type": "text", "text": ""}], "isError": False}}
            text = "y" * (size - len(json.dumps(frame)))
            body = json.dumps({"jsonrpc": "2.0", "id": rid,
                               "result": {"content": [{"type": "text", "text": text}], "isError": False}})
            sys.stdout.write(body + "\r\n")
            sys.stdout.flush()
            continue
        if mode in ("big_result", "sized_line"):
            text = "y" * size
            if mode == "sized_line":
                frame = {"jsonrpc": "2.0", "id": rid,
                         "result": {"content": [{"type": "text", "text": ""}], "isError": False}}
                text = "y" * (size - len(json.dumps(frame)))
            send({"jsonrpc": "2.0", "id": rid,
                  "result": {"content": [{"type": "text", "text": text}], "isError": False}})
            continue
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
