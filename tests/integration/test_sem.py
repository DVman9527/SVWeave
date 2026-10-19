"""SEM-01 .. SEM-10 on the real pinned pyslang worker (plan 20.3). Expectations come from the
hand-written goldens in fixtures/expected (see its README)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from svweave.application import queries as Q
from svweave.application.runner import exit_code
from svweave.domain.errors import ErrorCode
from tests.integration.helpers import (
    build,
    check_declarations,
    check_references,
    dbq,
    decl_by_qn,
    decl_rows,
    golden,
    lang_ws,
    opts,
    ref_rows,
)

pytestmark = pytest.mark.integration


def test_SEM_01_same_name_methods_in_two_packages_stay_separate(tmp_path: Path) -> None:
    w = lang_ws(tmp_path, "sem01_scopes", "sem01_top")
    env = build(w)
    assert env.analysis_coverage is not None and env.analysis_coverage.completeness == "COMPLETE"
    g = golden("sem01_scopes")
    bid = env.build_id
    assert bid
    check_declarations(w, bid, g)
    check_references(w, bid, g)
    rows = decl_rows(w, bid)
    assert len(decl_by_qn(rows, "pkg_a::agent::run_phase")) == 1
    assert len(decl_by_qn(rows, "pkg_b::agent::run_phase")) == 1
    assert len(decl_by_qn(rows, "pkg_a::agent::write")) == 1
    assert len(decl_by_qn(rows, "pkg_b::agent::write")) == 1
    # a bare name is never silently resolved: the query reports scoped candidates
    amb = Q.symbol(opts(w), bid, "r", name="run_phase")
    assert amb.error is not None and amb.error.code == ErrorCode.AMBIGUOUS_SYMBOL
    cands = {c["qualified_name"] for c in amb.data["candidates"]}
    assert cands == {"pkg_a::agent::run_phase", "pkg_b::agent::run_phase"}
    assert exit_code(amb) == 3
    # references of each `write` contain only its own call site
    for pkg, var in (("pkg_a", "a"), ("pkg_b", "b")):
        sid = decl_by_qn(rows, f"{pkg}::agent::write")[0]["symbol_id"]
        r = Q.references(opts(w), bid, sid, "r")
        locs = [i["location"] for i in r.data["items"]]
        assert len(locs) == 1 and locs[0]["file"] == "top.sv"
        text = (w.root / "top.sv").read_bytes()[locs[0]["start"] - 2 : locs[0]["end"]]
        assert text == f"{var}.write".encode()
        assert (
            r.data["items"][0]["target_declaration_id"]
            == decl_by_qn(rows, f"{pkg}::agent::write")[0]["declaration_id"]
        )


def test_SEM_02_parameterized_class_specializations_share_one_declaration(tmp_path: Path) -> None:
    w = lang_ws(tmp_path, "sem02_paramclass", "sem02_top")
    env = build(w)
    bid = env.build_id
    assert bid and env.analysis_coverage is not None
    g = golden("sem02_paramclass")
    check_declarations(w, bid, g)
    check_references(w, bid, g)
    rows = decl_rows(w, bid)
    assert len(decl_by_qn(rows, "box_pkg::box::width")) == 1  # one declaration, two specializations
    specs = dbq(w, "SELECT * FROM specializations WHERE build_id=? ORDER BY text", bid)
    want = {s["text"]: s for s in g["specializations"]}
    assert {s["text"] for s in specs} == set(want)
    cls_id = decl_by_qn(rows, "box_pkg::box", "class")[0]["declaration_id"]
    assert {s["class_declaration_id"] for s in specs} == {cls_id}
    assert len({s["symbol_id"] for s in specs}) == 2
    for s in specs:
        params = {p["name"]: p["value"] for p in json.loads(s["parameters"])}
        assert params == want[s["text"]]["parameters"]
    sym = Q.symbol(
        opts(w), bid, "r", symbol_id=decl_by_qn(rows, "box_pkg::box", "class")[0]["symbol_id"]
    )
    assert len(sym.data["symbol"]["specializations"]) == 2
    assert sym.data["symbol"]["attrs"]["generic"] is True


def test_SEM_03_lanes_generate_hierarchy_counts_and_paths(tmp_path: Path) -> None:
    from tests.integration.demo_support import demo_ws

    g = golden("demo_lanes")
    for variant in ("L2", "L4"):
        v = g["variants"][variant]
        w = demo_ws(tmp_path / variant, lanes=v["LANES"], data_w=v["DATA_W"])
        env = build(w)
        bid = env.build_id
        assert bid and env.analysis_coverage is not None
        assert env.analysis_coverage.completeness == "COMPLETE"
        h = Q.hierarchy(opts(w), bid, "r", limit=500)
        lanes = [i["path"] for i in h.data["items"] if i["definition"] == "demo_lane"]
        sva = [i["path"] for i in h.data["items"] if i["definition"] == "stream_hold_sva"]
        assert lanes == v["lane_paths"]
        assert sva == v["checker_paths"]
        a = Q.assertions(opts(w), bid, "r", entry_type="elaborated")
        assert a.data["total_matches"] == v["LANES"]


def test_SEM_04_ifdef_selects_branch_and_marks_inactive_source(tmp_path: Path) -> None:
    g = golden("sem04_ifdef")
    ids = {}
    for name, defines in (("default", []), ("USE_A", [{"name": "USE_A"}])):
        v = g["variants"][name]
        w = lang_ws(tmp_path / name, "sem04_ifdef", "sem04_top", defines=defines)
        env = build(w)
        bid = env.build_id
        assert bid
        ids[name] = bid
        h = Q.hierarchy(opts(w), bid, "r", limit=10)
        by_path = {i["path"]: i for i in h.data["items"]}
        assert by_path[g["instance_path"]]["definition"] == v["active_definition"]
        regions = dbq(w, "SELECT * FROM inactive_regions WHERE build_id=? ORDER BY ord", bid)
        cond = [r for r in regions if r["kind"] == "disabled_conditional_region"]
        assert cond and all(r["status"] == "inactive_for_profile" for r in regions)
        found = []
        for r in cond:
            data = json.loads(r["data"])
            assert data["directive"] in ("IfDefDirective", "ElseDirective")
            found += data["declarations"]
        inactive = v["inactive_modules"][0]
        assert [(d["kind"], d["name"], d["start"]) for d in found if d["kind"] == "module"] == [
            ("module", inactive["name"], inactive["start"])
        ]
        ne = g["never_instantiated"]
        never = [r for r in regions if r["kind"] == "definition_not_elaborated"]
        assert [(r["name"], r["span_start"], r["span_end"]) for r in never] == [
            (ne["name"], ne["start"], ne["end"])
        ]
        # the active module IS elaborated, the inactive one is not an instance anywhere
        assert not [i for i in h.data["items"] if i["definition"] == inactive["name"]]
    assert ids["default"] != ids["USE_A"]  # different define -> different build


def test_SEM_05_port_association_styles(tmp_path: Path) -> None:
    w = lang_ws(tmp_path, "sem05_ports", "sem05_top")
    env = build(w)
    bid = env.build_id
    assert bid and env.analysis_coverage is not None
    assert env.analysis_coverage.completeness == "COMPLETE"
    g = golden("sem05_ports")
    for inst, exp in g["bindings"].items():
        c = Q.connections(opts(w), bid, inst, "r")
        got = {i["formal_port"]: i for i in c.data["items"]}
        assert list(got) == g["ports"]  # declaration order of the formals
        for p in g["ports"]:
            assert got[p]["association_style"] == exp["style"][p], (inst, p)
            assert got[p]["actual"]["text"] == exp["actual"][p], (inst, p)
            assert got[p]["direction"] == g["directions"][p]
            assert got[p]["supported"] is True
        for p in exp.get("constant", []):
            assert got[p]["actual"]["is_constant"] is True
            assert got[p]["referenced_symbols"] == []
        for p in exp.get("open", []):
            assert got[p]["actual"]["text"] is None
        if "concat" in exp:
            d = got[exp["concat"]["port"]]
            assert d["actual"]["kind"] == "Concatenation"  # NOT collapsed to one signal
            assert [p["text"] for p in d["actual"]["parts"]] == exp["concat"]["parts"]
            assert [r["name"] for r in d["referenced_symbols"]] == exp["concat"]["parts"]
            loc = d["actual"]["location"]
            assert (loc["start"], loc["end"]) == (exp["concat"]["start"], exp["concat"]["end"])
    star = golden("sem05_ports")["star_span"]
    c = Q.connections(opts(w), bid, "sem05_top.u_star", "r", port="a")
    loc = c.data["items"][0]["actual"]["location"]
    assert (loc["start"], loc["end"]) == (star["start"], star["end"])  # the `.*` token
    assert c.data["items"][0]["actual"]["implicit"] is True


def test_SEM_06_interface_modport_instances_stay_distinct(tmp_path: Path) -> None:
    w = lang_ws(tmp_path, "sem06_iface", "sem06_top")
    env = build(w)
    bid = env.build_id
    assert bid and env.analysis_coverage is not None
    g = golden("sem06_iface")
    check_declarations(w, bid, g)
    h = Q.hierarchy(opts(w), bid, "r")
    assert [i["path"] for i in h.data["items"]] == g["instances"]
    rows = decl_rows(w, bid)
    assert (
        len(decl_by_qn(rows, "bus_if.data", "variable"))
        == g["same_name_signal"]["declaration_count"]
    )
    assert sum(1 for i in h.data["items"] if i["definition"] == "bus_if") == 2
    for inst, ports in g["interface_bindings"].items():
        c = Q.connections(opts(w), bid, inst, "r", port="b")
        it = c.data["items"][0]
        want_inst, want_mp = ports["b"]
        assert it["interface_instance"]["path"] == want_inst
        assert it["modport"] == want_mp
        assert it["port_kind"] == "InterfacePort"
    # a modport-qualified signal reference never resolves to the other interface instance's name
    src_paths = {
        Q.connections(opts(w), bid, i, "r", port="b").data["items"][0]["interface_instance"]["path"]
        for i in ("sem06_top.u_src_a", "sem06_top.u_src_b")
    }
    assert src_paths == {"sem06_top.u_bus_a", "sem06_top.u_bus_b"}
    # a virtual interface handle is a typed candidate: declared, but bound to no instance
    vif = decl_by_qn(rows, g["virtual_interface"]["field"], "field")[0]
    assert json.loads(vif["attrs"])["virtual_interface"] is True and "mp_src" in vif["type"]
    assert not [
        r
        for r in ref_rows(w, bid)
        if r["target_id"]
        in {
            d["declaration_id"]
            for d in rows
            if d["kind"] == "instance" and d["name"].startswith("u_bus")
        }
        and r["container_id"] == vif["declaration_id"]
    ]
    assert all(
        "virtual" not in json.dumps(Q.connections(opts(w), bid, i, "r").data).lower()
        for i in ("sem06_top.u_src_a", "sem06_top.u_dst_b")
    )


def test_SEM_07_errors_give_precise_diagnostics_and_partial(tmp_path: Path) -> None:
    w = lang_ws(tmp_path, "sem07_errors", "err_top")
    env = build(w)
    g = golden("sem07_errors")
    bid = env.build_id
    assert bid
    job = dbq(w, "SELECT * FROM jobs WHERE build_id=?", bid)[0]
    assert job["lifecycle"] == g["expect"]["job"]  # the job succeeded ...
    assert env.analysis_coverage is not None
    assert (
        env.analysis_coverage.completeness == g["expect"]["completeness"]
    )  # ... the design did not
    assert env.data["diagnostic_outcome"] == g["expect"]["diagnostic_outcome"]
    assert env.outcome == "PARTIAL" and env.error is not None
    assert env.error.code == ErrorCode.PARTIAL_ANALYSIS and exit_code(env) == 3
    assert env.data["published"] is False
    assert dbq(w, "SELECT * FROM published_pointer") == []
    d = Q.diagnostics(opts(w), bid, "r")
    errs = [x for x in d.data["items"] if x["is_error"]]
    assert len(errs) == g["expect"]["errors"]
    for want in g["diagnostics"]:
        hit = [e for e in errs if e["code"] == want["code"]]
        assert len(hit) == 1, want
        loc = hit[0]["location"]
        assert loc["file"] == want["file"]
        if want["code"] == "NoImplicitConversion":
            assert want["start"] <= loc["start"] <= want["end"]
        elif want["code"] == "ExpectedToken":
            assert loc["start"] == want["end"]  # reported right after `[7:0`
        else:
            assert loc["start"] == want["start"]
    un = [r for r in ref_rows(w, bid) if r["resolution_status"] == "candidate_source"]
    assert [
        (r["target_name"], r["unresolved_reason"], r["span_start"], r["span_end"]) for r in un
    ] == [(u["name"], u["reason"], u["start"], u["end"]) for u in g["unresolved"]]
    assert env.analysis_coverage.unresolved_count == 1
    assert any("PARTIAL" in x for x in env.warnings)


def test_SEM_08_virtual_call_has_static_target_and_dynamic_limitation(tmp_path: Path) -> None:
    w = lang_ws(tmp_path, "sem08_virtual", "sem08_top")
    env = build(w)
    bid = env.build_id
    assert bid and env.analysis_coverage is not None
    g = golden("sem08_virtual")
    check_declarations(w, bid, g)
    check_references(w, bid, g)
    rows = decl_rows(w, bid)
    by_id = {r["declaration_id"]: r for r in rows}
    dyn = [r for r in ref_rows(w, bid) if r["resolution_status"] == "dynamic_unresolved"]
    assert len(dyn) == 3
    for r in dyn:
        assert by_id[r["target_id"]]["qualified_name"] == "vpkg::animal::speak"
        assert r["unresolved_reason"] == "virtual_method_runtime_dispatch"
        cands = {
            by_id[c]["qualified_name"] for c in json.loads(r["context"])["override_candidates"]
        }
        assert cands == set(g["override_candidates"])  # listed, none picked as THE target
    assert any("dynamic_unresolved" in x for x in env.analysis_coverage.limitations)
    dog = decl_by_qn(rows, "vpkg::dog::speak")[0]
    refs = Q.references(opts(w), bid, dog["symbol_id"], "r")
    assert refs.data["total_matches"] == 3
    assert {i["via"] for i in refs.data["items"]} == {"virtual_dispatch_candidate"}
    assert {i["resolution_status"] for i in refs.data["items"]} == {"dynamic_unresolved"}


def test_SEM_09_escaped_names_and_generate_indices_do_not_collide(tmp_path: Path) -> None:
    w = lang_ws(tmp_path, "sem09_escaped", "sem09_top")
    env = build(w)
    bid = env.build_id
    assert bid and env.analysis_coverage is not None
    g = golden("sem09_escaped")
    insts = dbq(w, "SELECT * FROM instances WHERE build_id=? ORDER BY ord", bid)
    assert len(insts) == g["instance_count"]
    assert len({i["instance_id"] for i in insts}) == len(insts)
    got = [(json.loads(i["components"]), i["definition"]) for i in insts[1:]]
    for (comps, definition), want in zip(got, g["instances"], strict=True):
        assert [[c["name"], c["index"]] for c in comps] == want["components"]
        assert definition == want["definition"]
    # the naive '.'-split paths of the two leaves are identical, the identities are not
    a = next(
        i
        for i in insts
        if [c["name"] for c in json.loads(i["components"])] == g["naive_split_collision"]["a"]
    )
    b = next(
        i
        for i in insts
        if [c["name"] for c in json.loads(i["components"])] == g["naive_split_collision"]["b"]
    )
    naive = lambda i: ".".join(c["name"] for c in json.loads(i["components"]))  # noqa: E731
    assert naive(a) == naive(b) == g["naive_split_collision"]["same_joined_string"]
    assert a["instance_id"] != b["instance_id"] and a["parent_id"] != b["parent_id"]
    # queries address them by their display paths without ambiguity
    ha = Q.hierarchy(opts(w), bid, "r", instance=a["path"], depth=0)
    hb = Q.hierarchy(opts(w), bid, "r", instance=b["path"], depth=0)
    assert ha.data["items"][0]["instance_id"] == a["instance_id"]
    assert hb.data["items"][0]["instance_id"] == b["instance_id"]
    assert ha.data["items"][0]["path_components"][1]["name"] == "x.y"
    esc = g["escaped_instance_span"]
    decls = [d for d in decl_rows(w, bid) if d["kind"] == "instance" and d["name"] == "x.y"]
    assert [(d["span_start"], d["span_end"]) for d in decls] == [(esc["start"], esc["end"])]
    gen = [i for i in insts if i["generate_context"] != "[]"]
    assert [json.loads(i["generate_context"]) for i in gen][1] == ["g[0]", "h[1]"]


def test_SEM_10_unused_generic_and_library_body_are_reported_as_not_covered(tmp_path: Path) -> None:
    w = lang_ws(tmp_path, "sem10_unused", "sem10_top", library_dirs=["lib"])
    env = build(w)
    bid = env.build_id
    assert bid and env.analysis_coverage is not None
    g = golden("sem10_unused")
    check_declarations(w, bid, g)
    check_references(w, bid, g)
    gaps = {r["kind"] for r in dbq(w, "SELECT kind FROM coverage_gaps WHERE build_id=?", bid)}
    assert set(g["not_covered"]["gap_kinds"]) <= gaps
    helper = decl_by_qn(decl_rows(w, bid), "pkg10::helper")[0]
    r = Q.references(opts(w), bid, helper["symbol_id"], "r")
    assert r.data["total_matches"] == g["not_covered"]["reference_count_for_helper"]
    # the two sites in uncovered code are NOT in the result ...
    spans = {(i["location"]["file"], i["location"]["start"]) for i in r.data["items"]}
    for key in ("helper_2", "helper_3"):
        site = g["not_covered"][key]
        assert not any(f.endswith(site["file"]) and s == site["start"] for f, s in spans)
    # ... and the response says so instead of claiming it searched everything
    lim = " ".join(r.analysis_coverage.limitations if r.analysis_coverage else [])
    assert "generic class" in lim and "library member" in lim
    assert r.data["searched_scope"]["kind"].startswith("occurrences visited by slang")
    assert not any(
        x["kind"] == "value" and "lib_unused" in (x["file"] or "") for x in ref_rows(w, bid)
    )


def test_class_members_constraints_covergroups_static_inheritance_and_dpi(tmp_path: Path) -> None:
    w = lang_ws(tmp_path, "sem_classmembers", "cm_top")
    env = build(w)
    bid = env.build_id
    assert bid and env.analysis_coverage is not None
    assert env.analysis_coverage.completeness == "COMPLETE", env.data["reasons"]
    g = golden("sem_classmembers")
    check_declarations(w, bid, g)
    check_references(w, bid, g)
    rows = {(r["kind"], r["qualified_name"]): r for r in decl_rows(w, bid)}
    for e in g["declarations"]:
        attrs = json.loads(rows[(e["kind"], e["qualified_name"])]["attrs"])
        for k in ("rand_mode", "constructor", "static", "dpi_import", "dpi_spec"):
            if k in e:
                assert attrs[k] == e[k], (e["qualified_name"], k)
    ext = json.loads(rows[("class", "cm_pkg::ext_item")]["attrs"])
    assert ext["base_class"] == "base_item"
    assert ext["base_declaration_id"] == rows[("class", "cm_pkg::base_item")]["declaration_id"]
    assert ("covergroup", "cm_pkg::base_item::cg_item") in rows  # declared, never "evaluated"
    sym = Q.symbol(
        opts(w), bid, "r", symbol_id=rows[("constraint", "cm_pkg::ext_item::c_data")]["symbol_id"]
    )
    assert sym.data["symbol"]["kind"] == "constraint" and "satisf" not in json.dumps(sym.data)
