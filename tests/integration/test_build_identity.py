"""BLD-06/07/10 (semantic side), SNP-04 and plan 21.2 demo checks on the real worker."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from svweave.application import queries as Q
from svweave.application import toolchain
from svweave.domain.ids import explain_build_change
from tests.integration.demo_support import demo_ws
from tests.integration.helpers import (
    build,
    dbq,
    decl_rows,
    golden,
    lang_ws,
    opts,
)

pytestmark = pytest.mark.integration


def ident(w, env):  # type: ignore[no-untyped-def]
    b = dbq(w, "SELECT * FROM builds WHERE build_id=?", env.build_id)[0]
    return {
        "snapshot_id": b["snapshot_id"],
        "config_id": b["config_id"],
        "toolchain_id": b["toolchain_id"],
        "semantic_schema_version": b["semantic_schema_version"],
    }


def test_BLD_06_single_vs_per_file_compilation_unit_changes_the_result(tmp_path: Path) -> None:
    g = golden("bld06_cu")
    res = {}
    for mode in ("single", "per_file"):
        w = lang_ws(tmp_path / mode, "bld06_cu", "mod_b", compilation_units={"mode": mode})
        res[mode] = (w, build(w))
    (ws1, e1), (ws2, e2) = res["single"], res["per_file"]
    assert e1.analysis_coverage is not None and e2.analysis_coverage is not None
    assert e1.analysis_coverage.completeness == g["expect"]["single"]["completeness"]
    assert e2.analysis_coverage.completeness == g["expect"]["per_file"]["completeness"]
    assert e1.build_id != e2.build_id  # different CU semantics never share a build
    want = g["expect"]["per_file"]
    d = Q.diagnostics(opts(ws2), e2.build_id, "r")  # type: ignore[arg-type]
    errs = [x for x in d.data["items"] if x["is_error"]]
    assert [x["code"] for x in errs] == [want["code"]]
    assert errs[0]["location"]["start"] == want["start"]
    assert errs[0]["location"]["file"] == "macro_use.sv"
    assert not [
        x for x in Q.diagnostics(opts(ws1), e1.build_id, "r").data["items"] if x["is_error"]
    ]  # type: ignore[arg-type]


def test_BLD_07_include_dir_order_changes_selected_header_and_build_id(tmp_path: Path) -> None:
    g = golden("bld07_incdir")["expect"]
    out = {}
    for key, order in (("dir_a_first", ["dir_a", "dir_b"]), ("dir_b_first", ["dir_b", "dir_a"])):
        w = lang_ws(tmp_path / key, "bld07_incdir", "inc_top", include_dirs=order)
        env = build(w)
        assert env.build_id
        h = Q.hierarchy(opts(w), env.build_id, "r")
        params = {p["name"]: p["value"] for p in h.data["items"][0]["parameters"]}
        out[key] = (env, params, ident(w, env))
        assert params["P"] == g[key]["P"]
    assert out["dir_a_first"][0].build_id != out["dir_b_first"][0].build_id
    assert out["dir_a_first"][2]["config_id"] != out["dir_b_first"][2]["config_id"]
    assert out["dir_a_first"][2]["snapshot_id"] != out["dir_b_first"][2]["snapshot_id"]
    # the dependency report shows which header was actually read
    w = lang_ws(tmp_path / "dep", "bld07_incdir", "inc_top", include_dirs=["dir_b", "dir_a"])
    env = build(w)
    rep = json.loads(
        dbq(w, "SELECT dependency_report FROM build_reports WHERE build_id=?", env.build_id)[0][
            "dependency_report"
        ]
    )
    assert "dir_b/common.svh" in rep["files_read"] and "dir_a/common.svh" not in rep["files_read"]


def test_BLD_10_same_header_in_two_compilation_groups_keeps_both_occurrences(
    tmp_path: Path,
) -> None:
    g = golden("bld10_groups")["expect"]
    groups = [
        {
            "id": gid,
            "sources": [{"kind": "file", "path": f"{top}.sv"}],
            "include_dirs": ["inc"],
            "defines": [{"name": "W", "value": g["groups"][gid]["W"]}],
        }
        for gid, top in (("g1", "top1"), ("g2", "top2"))
    ]
    w = lang_ws(
        tmp_path,
        "bld10_groups",
        ["top1", "top2"],
        source_inputs=[],
        compilation_units={"mode": "explicit_groups", "groups": groups},
    )
    env = build(w)
    bid = env.build_id
    assert bid and env.analysis_coverage is not None
    assert env.analysis_coverage.completeness == "COMPLETE", env.data["reasons"]
    rows = [r for r in decl_rows(w, bid) if r["name"] == "WIDTH"]
    assert sorted(r["qualified_name"] for r in rows) == ["top1.WIDTH", "top2.WIDTH"]
    # same source occurrence (shared.svh), but NOT merged: distinct CU/macro context
    sh = g["shared_header"]
    assert {(r["file"], r["span_start"], r["span_end"]) for r in rows} == {
        ("inc/shared.svh", sh["start"], sh["end"])
    }
    assert len({r["declaration_id"] for r in rows}) == 2
    assert len({r["compilation_unit"] for r in rows}) == 2
    vals = {r["qualified_name"]: json.loads(r["attrs"])["value"] for r in rows}
    assert vals == {"top1.WIDTH": "8", "top2.WIDTH": "16"}
    # the group command files are adapter inputs: declared, hashed, and not reported as leaks
    rep = json.loads(
        dbq(w, "SELECT dependency_report FROM build_reports WHERE build_id=?", bid)[0][
            "dependency_report"
        ]
    )
    assert rep["ok"] and rep["violations"] == []


def test_SNP_04_parameter_define_and_toolchain_changes_alter_build_identity(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    base = demo_ws(tmp_path / "base", lanes=2)
    e0 = build(base)
    i0 = ident(base, e0)
    # same inputs again -> same build, reused (no new job), explained as unchanged
    e0b = build(base)
    assert e0b.build_id == e0.build_id and e0b.data["reused"] is True
    assert len(dbq(base, "SELECT 1 FROM jobs WHERE build_id=?", e0.build_id)) == 1
    # a parameter change: identical source bytes, different config -> different build
    p4 = demo_ws(tmp_path / "p4", lanes=4)
    e4 = build(p4)
    i4 = ident(p4, e4)
    assert i4["snapshot_id"] == i0["snapshot_id"] and e4.build_id != e0.build_id
    assert explain_build_change(i0, i4) == ["config_id"]
    # a define change
    d = demo_ws(tmp_path / "d", lanes=2, defines=[{"name": "SVW_DEMO", "value": "2"}])
    ed = build(d)
    assert ed.build_id not in (e0.build_id, e4.build_id)
    assert explain_build_change(i0, ident(d, ed)) == ["config_id"]
    # a toolchain/extractor change keeps snapshot+config but changes the build
    monkeypatch.setattr(toolchain, "EXTRACTOR_VERSION", "svweave-slang-extractor/test-bump")
    again = demo_ws(tmp_path / "tc", lanes=2)
    et = build(again)
    it = ident(again, et)
    assert (it["snapshot_id"], it["config_id"]) == (i0["snapshot_id"], i0["config_id"])
    assert et.build_id != e0.build_id
    assert explain_build_change(i0, it) == ["toolchain_id"]


def test_demo_21_2_lanes_data_w_and_actual_expression(tmp_path: Path) -> None:
    g = golden("demo_lanes")
    builds = {}
    for name, (lanes, dw) in {"L2": (2, 32), "L4": (4, 32), "W64": (2, 64)}.items():
        w = demo_ws(tmp_path / name, lanes=lanes, data_w=dw)
        env = build(w)
        assert env.build_id and env.analysis_coverage is not None
        assert env.analysis_coverage.completeness == "COMPLETE"
        builds[name] = (w, env)
        h = Q.hierarchy(opts(w), env.build_id, "r", limit=500)
        lane_items = [i for i in h.data["items"] if i["definition"] == "demo_lane"]
        assert len(lane_items) == lanes
        top = h.data["items"][0]
        pv = {p["name"]: p["value"] for p in top["parameters"]}
        assert pv == {"DATA_W": str(dw), "LANES": str(lanes)}
        for li in lane_items:  # each lane specialization carries DATA_W and the port widths
            assert {p["name"]: p["value"] for p in li["parameters"]} == {"DATA_W": str(dw)}
            c = Q.connections(opts(w), env.build_id, li["instance_id"], "r", port="data_i")
            assert c.data["items"][0]["actual"]["bit_width"] == dw
    assert len({e.build_id for _, e in builds.values()}) == 3
    w, env = builds["L2"]
    exp = g["data_i_actual"]
    c = Q.connections(opts(w), env.build_id, exp["instance"], "r", port=exp["formal"])
    it = c.data["items"][0]
    assert it["actual"]["text"] == exp["actual_text"]  # keeps the slice, not just `data_i`
    assert it["actual"]["kind"] == exp["kind"]
    assert [r["name"] for r in it["referenced_symbols"]] == exp["referenced"]
    loc = it["actual"]["location"]
    assert (loc["start"], loc["end"]) == (exp["start"], exp["end"])
    assert it["association_style"] == "named" and it["direction"] == "in"
    assert any(
        d["kind"] == "module" and d["name"] == "demo_top" for d in decl_rows(w, env.build_id)
    )  # type: ignore[arg-type]
