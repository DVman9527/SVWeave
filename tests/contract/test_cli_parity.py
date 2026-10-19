"""SVW-024: the same query through the CLI and through MCP yields the same normalized envelope."""

from __future__ import annotations

import json
from typing import Any

import pytest
from click.testing import CliRunner

from svweave.cli import cli
from tests.contract.conftest import Built, call, make_gateway


def cli_env(root: Any, *args: str) -> tuple[int, dict[str, Any]]:
    r = CliRunner().invoke(cli, ["--root", str(root), "--compact", *args])
    assert r.stdout.strip(), r.output
    return r.exit_code, json.loads(r.stdout)


def normalize(env: dict[str, Any]) -> dict[str, Any]:
    e = json.loads(json.dumps(env))
    e.pop("request_id", None)
    return e


def cases(b: Built) -> list[tuple[str, list[str], str, dict[str, Any]]]:
    bid = b.build_id
    return [
        (
            "hierarchy",
            ["query", "hierarchy", "--build", bid, "--limit", "4"],
            "svw_hierarchy",
            {"build_id": bid, "limit": 4},
        ),
        (
            "hierarchy-top",
            ["query", "hierarchy", "--build", bid, "--top", "demo_top", "--depth", "1"],
            "svw_hierarchy",
            {"build_id": bid, "top": "demo_top", "depth": 1},
        ),
        (
            "assertions",
            ["query", "assertions", "--build", bid, "--limit", "5"],
            "svw_assertions",
            {"build_id": bid, "limit": 5},
        ),
        (
            "assertions-bind",
            ["query", "assertions", "--build", bid, "--kind", "bind"],
            "svw_assertions",
            {"build_id": bid, "kind": "bind"},
        ),
        ("evidence", ["query", "evidence", "--build", bid], "svw_evidence", {"build_id": bid}),
        (
            "search",
            [
                "query",
                "search",
                "--snapshot",
                b.snapshot_id,
                "--query",
                "demo_lane",
                "--no-graph",
                "--limit",
                "3",
            ],
            "svw_search",
            {"snapshot_id": b.snapshot_id, "query": "demo_lane", "limit": 3},
        ),
        (
            "missing-build",
            ["query", "hierarchy", "--build", "0" * 64],
            "svw_hierarchy",
            {"build_id": "0" * 64},
        ),
    ]


@pytest.mark.parametrize("idx", range(7))
def test_cli_and_mcp_return_the_same_envelope(built: Built, idx: int) -> None:
    label, cli_args, tool, mcp_args = cases(built)[idx]
    code, via_cli = cli_env(built.ws.root, *cli_args)
    via_mcp, is_err = call(make_gateway(built.opts), tool, mcp_args)
    assert normalize(via_cli) == normalize(via_mcp), label
    assert is_err == (via_cli["error"] is not None and via_cli["outcome"] != "PARTIAL"), label
    assert (code == 0) == (via_cli["error"] is None), label


def test_symbol_references_connections_parity(built: Built) -> None:
    gw = make_gateway(built.opts)
    h, _ = call(gw, "svw_hierarchy", {"build_id": built.build_id})
    inst = h["data"]["items"][1]["instance_id"]
    _, c1 = cli_env(
        built.ws.root,
        "query",
        "connections",
        "--build",
        built.build_id,
        "--instance",
        inst,
        "--limit",
        "5",
    )
    c2, _ = call(
        gw, "svw_connections", {"build_id": built.build_id, "instance_id": inst, "limit": 5}
    )
    assert normalize(c1) == normalize(c2)
    _, s1 = cli_env(
        built.ws.root, "query", "symbol", "--build", built.build_id, "--name", "demo_top"
    )
    s2, _ = call(gw, "svw_symbol", {"build_id": built.build_id, "name": "demo_top"})
    assert normalize(s1) == normalize(s2)
    cands = s2["data"].get("candidates") or [s2["data"].get("declaration")]
    sid = (cands[0] or {}).get("symbol_id") if cands else None
    if sid:
        _, r1 = cli_env(
            built.ws.root,
            "query",
            "references",
            "--build",
            built.build_id,
            "--symbol",
            sid,
            "--limit",
            "5",
        )
        r2, _ = call(
            gw, "svw_references", {"build_id": built.build_id, "symbol_id": sid, "limit": 5}
        )
        assert normalize(r1) == normalize(r2)
