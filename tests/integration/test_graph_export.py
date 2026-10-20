"""GRF-I: `svweave graph export` on the real demo design (real pyslang, no backend servers)."""

from __future__ import annotations

import json
import re
import stat
from pathlib import Path
from typing import Any

import pytest

from tests.integration.demo_support import demo_ws
from tests.integration.test_cli_semantic import run

pytestmark = pytest.mark.integration


def _page_data(path: Path) -> dict[str, Any]:
    m = re.search(r'<script id="data" type="application/json">(.*?)</script>', path.read_text(), re.S)
    assert m
    data: dict[str, Any] = json.loads(m.group(1))
    return data


def _built(tmp_path: Path, **kw: Any) -> tuple[Path, str]:
    w = demo_ws(tmp_path, **kw)
    code, b = run(w.root, "build", "--profile", "p")
    assert code == 0 and b["outcome"] == "OK"
    return w.root, str(b["build_id"])


def test_GRF_I01_export_writes_one_private_self_contained_file(tmp_path: Path) -> None:
    root, bid = _built(tmp_path)
    code, env = run(root, "graph", "export", "--build", bid)
    assert code == 0 and env["outcome"] == "OK" and env["freshness"] == "CURRENT"
    d = env["data"]
    assert d["served"] is False and d["path"] == f".svweave/exports/graph-{bid[:12]}.html"
    f = root / d["path"]
    assert f.is_file() and stat.S_IMODE(f.stat().st_mode) == 0o600
    assert f.stat().st_size == d["bytes"]
    assert any("not a netlist" in x or "netlist" in x for x in env["analysis_coverage"]["limitations"])
    data = _page_data(f)
    paths = [n["path"] for n in data["nodes"]]
    assert paths[0] == "demo_top" and len(paths) == 5 and data["stats"]["instances_total"] == 5
    assert data["meta"]["build_id"] == bid and data["meta"]["evidence_kind"] == "HDL_STATIC_RESOLVED"
    by_path = {n["path"]: n["id"] for n in data["nodes"]}
    edges = {(e["from"], e["to"], e["signal"]) for e in data["edges"]}
    top, lane0 = by_path["demo_top"], by_path["demo_top.g_lane[0].u_lane"]
    assert (top, lane0, "demo_top.clk") in edges  # top port -> child input
    assert (lane0, top, "demo_top.data_o") in edges  # child output -> top port
    assert not any(e["signal"].endswith(".lane") for e in data["edges"])  # genvar is not a signal


def test_GRF_I02_graph_follows_the_named_build_configuration(tmp_path: Path) -> None:
    r2, b2 = _built(tmp_path / "a", lanes=2)
    r3, b3 = _built(tmp_path / "b", lanes=3)
    n2 = _page_data(r2 / run(r2, "graph", "export", "--build", b2)[1]["data"]["path"])["nodes"]
    n3 = _page_data(r3 / run(r3, "graph", "export", "--build", b3)[1]["data"]["path"])["nodes"]
    assert len(n2) == 5 and len(n3) == 7  # LANES=3 elaborates one more lane (+ its SVA bind)


def test_GRF_I03_limits_hubs_and_refusals(tmp_path: Path) -> None:
    root, bid = _built(tmp_path, lanes=4)
    code, env = run(root, "graph", "export", "--build", bid, "--max-fanout", "2", "--name", "hubs")
    assert code == 0 and any("listed as hubs" in w for w in env["warnings"])
    data = _page_data(root / env["data"]["path"])
    assert "demo_top.clk" in [h["signal"] for h in data["hubs"]]
    assert not any(e["signal"] == "demo_top.clk" for e in data["edges"])
    code, env = run(root, "graph", "export", "--build", bid, "--max-instances", "3", "--name", "cut")
    assert code == 0 and env["data"]["truncated"] is True
    assert any(w.startswith("truncated:") for w in env["warnings"])
    cut = _page_data(root / env["data"]["path"])
    assert [n["depth"] for n in cut["nodes"]] == sorted(n["depth"] for n in cut["nodes"])[: len(cut["nodes"])]
    for bad in (["--name", "../x"], ["--name", "a/b"], ["--name", ".hidden"], ["--max-fanout", "1"],
                ["--max-instances", "0"]):
        code, e = run(root, "graph", "export", "--build", bid, *bad)
        assert code == 2 and e["error"]["code"] == "INVALID_CONFIG", bad
    code, e = run(root, "graph", "export", "--build", "0" * 64)
    assert code == 3 and e["error"]["code"] == "MISSING_INPUT"
    assert not (root.parent / "x.html").exists()
