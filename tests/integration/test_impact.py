"""IMP-I: `svweave query impact` on the real demo design (real pyslang)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from tests.integration.demo_support import demo_ws
from tests.integration.test_cli_semantic import run

pytestmark = pytest.mark.integration


def _build(tmp_path: Path, **kw: Any) -> tuple[Path, str]:
    w = demo_ws(tmp_path, **kw)
    code, b = run(w.root, "build", "--profile", "p")
    assert code == 0
    return w.root, str(b["build_id"])


def test_IMP_I01_module_impact(tmp_path: Path) -> None:
    root, bid = _build(tmp_path)
    code, env = run(root, "query", "impact", "--build", bid, "--name", "demo_lane")
    assert code == 0 and env["outcome"] == "OK" and env["freshness"] == "CURRENT"
    d = env["data"]
    assert d["target"]["kind"] == "module" and d["summary"]["instances"] == 2
    assert sorted(i["path"] for i in d["instances"]) == [
        "demo_top.g_lane[0].u_lane",
        "demo_top.g_lane[1].u_lane",
    ]
    assert all(i["parent_path"] == "demo_top" for i in d["instances"])
    assert [(x["qualified_name"], x["level"], x["via_instances"]) for x in d["dependents"]] == [
        ("demo_top", 1, 2)
    ]
    assert [x["qualified_name"] for x in d["dependencies"]] == ["stream_hold_sva"]
    assert d["references_summary"]["total"] == 1 and d["port_count"] == 8
    assert "static impact" in d["note"] and d["truncated_lists"] == []
    assert env["provenance"]["evidence_kind"] == "HDL_STATIC_RESOLVED"


def test_IMP_I02_port_impact_merges_the_implicit_net_and_lists_connection_sites(
    tmp_path: Path,
) -> None:
    root, bid = _build(tmp_path)
    code, env = run(root, "query", "impact", "--build", bid, "--name", "demo_lane.valid_i")
    assert code == 0, env.get("error")  # port + implicit net are NOT an ambiguity here
    d = env["data"]
    assert d["target"]["kind"] == "port"
    assert sorted(a["kind"] for a in d["target"]["same_entity"]) == ["net", "port"]
    assert d["summary"]["connection_sites"] == 2 and d["summary"]["distinct_signals"] == 1
    conn = {c["instance_path"]: c for c in d["connections"]}
    assert conn["demo_top.g_lane[0].u_lane"]["actual"] == "valid_i[lane]"
    assert "demo_top.valid_i" in conn["demo_top.g_lane[1].u_lane"]["signals"]
    assert d["owner"]["qualified_name"] == "demo_lane"
    assert [x["qualified_name"] for x in d["dependents"]] == ["demo_top"]


def test_IMP_I03_top_port_shows_where_its_signal_goes(tmp_path: Path) -> None:
    root, bid = _build(tmp_path)
    code, env = run(root, "query", "impact", "--build", bid, "--name", "demo_top.valid_i")
    d = env["data"]
    assert code == 0 and d["summary"]["connection_sites"] == 0  # the top is not instantiated
    assert d["summary"]["uses_as_actual"] == 2
    assert sorted(u["instance_path"] for u in d["signal_uses"]) == [
        "demo_top.g_lane[0].u_lane",
        "demo_top.g_lane[1].u_lane",
    ]


def test_IMP_I04_instance_impact_and_config_dependence(tmp_path: Path) -> None:
    root, bid = _build(tmp_path / "a", lanes=2)
    code, env = run(root, "query", "impact", "--build", bid, "--instance", "demo_top.g_lane[0].u_lane")
    d = env["data"]
    assert code == 0 and d["target"]["type"] == "instance"
    assert [a["path"] for a in d["ancestors"]] == ["demo_top"]
    assert d["descendants"] == [{"definition": "stream_hold_sva", "instances": 1}]
    assert d["summary"]["port_connections"] == 8 and len(d["connections"]) == 8
    r3, b3 = _build(tmp_path / "b", lanes=3)
    _, e3 = run(r3, "query", "impact", "--build", b3, "--name", "demo_lane")
    assert e3["data"]["summary"]["instances"] == 3  # the answer follows the named build


def test_IMP_I05_ambiguity_missing_and_bad_arguments(tmp_path: Path) -> None:
    root, bid = _build(tmp_path)
    code, env = run(root, "query", "impact", "--build", bid, "--name", "valid_i")
    assert code == 3 and env["error"]["code"] == "AMBIGUOUS_SYMBOL"  # demo_lane.valid_i, demo_top...
    assert len(env["error"]["details"]["candidates"]) >= 2
    code, env = run(root, "query", "impact", "--build", bid, "--name", "no_such_thing")
    assert code == 3 and env["error"]["code"] == "MISSING_INPUT"
    code, env = run(root, "query", "impact", "--build", bid, "--instance", "demo_top.nope")
    assert code == 3 and env["error"]["code"] == "MISSING_INPUT"
    for bad in (
        [],  # no target
        ["--name", "demo_lane", "--instance", "demo_top"],  # two targets
        ["--name", "demo_lane", "--depth", "0"],
        ["--name", "demo_lane", "--depth", "99"],
        ["--name", "demo_lane", "--limit", "9999"],
        ["--file", "fixtures/demo/rtl/demo_lane.sv"],  # file without byte
    ):
        code, env = run(root, "query", "impact", "--build", bid, *bad)
        assert code == 2 and env["error"]["code"] == "INVALID_CONFIG", bad
    code, env = run(root, "query", "impact", "--build", "0" * 64, "--name", "demo_lane")
    assert code == 3 and env["error"]["code"] == "MISSING_INPUT"


def test_IMP_I06_limit_truncates_lists_but_keeps_totals(tmp_path: Path) -> None:
    root, bid = _build(tmp_path, lanes=4)
    code, env = run(root, "query", "impact", "--build", bid, "--name", "demo_lane", "--limit", "2")
    d = env["data"]
    assert code == 0 and len(d["instances"]) == 2 and d["instances_total"] == 4
    assert any(w.startswith("instances: 2 of 4") for w in env["warnings"])
