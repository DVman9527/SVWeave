"""`svweave build` / `svweave query ...` end to end through the CLI (plan 15.1, 15.4)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from click.testing import CliRunner

from svweave.cli import cli
from svweave.domain.envelope import Envelope
from tests.integration.helpers import lang_ws

pytestmark = pytest.mark.integration


def run(root: Path, *args: str) -> tuple[int, dict[str, Any]]:
    res = CliRunner().invoke(cli, ["--root", str(root), "--compact", *args])
    assert res.stdout.strip(), res.output
    data = json.loads(res.stdout)
    Envelope.model_validate(data)  # every stdout payload is a valid envelope
    return res.exit_code, data


def test_build_then_queries_through_the_cli(tmp_path: Path) -> None:
    w = lang_ws(tmp_path, "sem01_scopes", "sem01_top")
    code, b = run(w.root, "build", "--profile", "p")
    assert code == 0 and b["outcome"] == "OK" and b["data"]["published"] is True
    bid = b["build_id"]
    code, again = run(w.root, "build", "--profile", "p")
    assert code == 0 and again["data"]["reused"] is True and again["build_id"] == bid
    code, h = run(
        w.root, "query", "hierarchy", "--build", bid, "--top", "sem01_top", "--depth", "0"
    )
    assert code == 0 and [i["path"] for i in h["data"]["items"]] == ["sem01_top"]
    assert h["provenance"]["evidence_kind"] == "HDL_STATIC_RESOLVED" and h["freshness"] == "CURRENT"
    code, s = run(w.root, "query", "symbol", "--build", bid, "--name", "run_phase")
    assert code == 3 and s["error"]["code"] == "AMBIGUOUS_SYMBOL"
    sid = s["data"]["candidates"][0]["symbol_id"]
    code, r = run(w.root, "query", "references", "--build", bid, "--symbol", sid, "--limit", "5")
    assert code == 0 and r["data"]["total_matches"] == 1
    code, one = run(w.root, "query", "symbol", "--build", bid, "--file", "top.sv", "--byte", "33")
    assert code == 0 and one["data"]["symbol"]["qualified_name"] == "sem01_top.a"
    code, d = run(w.root, "query", "diagnostics", "--build", bid)
    assert code == 0 and d["data"]["total_matches"] == 0
    code, a = run(w.root, "query", "assertions", "--build", bid)
    assert code == 0 and a["data"]["execution_status"] == "NOT_RUN"
    # invalid usage / unknown build
    code, bad = run(w.root, "query", "hierarchy", "--build", bid, "--limit", "9999")
    assert code == 2 and bad["error"]["code"] == "INVALID_CONFIG"
    code, unk = run(w.root, "query", "hierarchy", "--build", "0" * 64)
    assert code == 3 and unk["error"]["code"] == "MISSING_INPUT"
    code, two = run(w.root, "query", "symbol", "--build", bid)
    assert code == 2


def test_build_exit_code_3_for_errors_and_connections_query(tmp_path: Path) -> None:
    w = lang_ws(tmp_path / "e", "sem07_errors", "err_top")
    code, b = run(w.root, "build", "--profile", "p")
    assert code == 3 and b["outcome"] == "PARTIAL" and b["error"]["code"] == "PARTIAL_ANALYSIS"
    assert b["data"]["diagnostic_outcome"] == "HAS_ERRORS" and b["data"]["published"] is False
    w2 = lang_ws(tmp_path / "p", "sem05_ports", "sem05_top")
    code, b2 = run(w2.root, "build", "--profile", "p")
    assert code == 0
    code, c = run(
        w2.root,
        "query",
        "connections",
        "--build",
        b2["build_id"],
        "--instance",
        "sem05_top.u_concat",
        "--port",
        "d",
    )
    assert code == 0 and c["data"]["items"][0]["actual"]["parts"][0]["text"] == "hi"


def test_build_blocked_dependency_leak_exits_3(tmp_path: Path) -> None:
    visible = Path("/etc/nsswitch.conf")  # outside the snapshot but exposed by the worker sandbox
    if not visible.is_file():
        pytest.skip("no /etc/nsswitch.conf on this host")
    w = lang_ws(tmp_path / "w", "sem01_scopes", "sem01_top")
    top = w.root / "top.sv"
    top.write_text(f'`include "{visible}"\n' + top.read_text())
    code, b = run(w.root, "build", "--profile", "p")
    assert code == 3 and b["outcome"] == "BLOCKED" and b["data"]["published"] is False


def test_build_with_missing_input_is_blocked_before_any_worker_runs(tmp_path: Path) -> None:
    w = lang_ws(tmp_path, "sem01_scopes", "sem01_top")
    (w.root / "files.f").write_text("pkg_a.sv\nmissing.sv\n")
    code, b = run(w.root, "build", "--profile", "p")
    assert code == 3 and b["error"]["code"] == "MISSING_INPUT" and b["outcome"] == "BLOCKED"
    assert not list((w.root / ".svweave").glob("runs/*"))
