"""UVM-01 .. UVM-03 against the REAL pinned UVM 2020.3.1 library (plan 16.1, 21.5)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from svweave.application import queries as Q
from tests.conftest import Ws
from tests.integration.helpers import (
    UVM_SRC,
    check_declarations,
    dbq,
    decl_by_qn,
    decl_rows,
    file_matches,
    golden,
    opts,
    ref_rows,
    uvm_ws,
)

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not (UVM_SRC / "uvm_pkg.sv").is_file(), reason="pinned UVM library missing"),
]


@pytest.fixture(scope="module")
def xdg_home(tmp_path_factory: pytest.TempPathFactory) -> Path:
    return tmp_path_factory.mktemp("xdg")


@pytest.fixture(autouse=True)
def _isolated_config_home(xdg_home: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Module-wide trusted-policy home (overrides the per-test one in tests/conftest.py)."""
    monkeypatch.setenv("XDG_CONFIG_HOME", str(xdg_home))
    return xdg_home


def _make(
    tmp_path_factory: pytest.TempPathFactory, xdg_home: Path, name: str, files: list[str], top: str
) -> tuple[Ws, object]:
    from tests.integration.helpers import build

    tmp = tmp_path_factory.mktemp(name)
    with pytest.MonkeyPatch.context() as mp:
        mp.setenv("XDG_CONFIG_HOME", str(xdg_home))
        w = uvm_ws(tmp, files, top)
        return w, build(w)


@pytest.fixture(scope="module")
def base(tmp_path_factory: pytest.TempPathFactory, xdg_home: Path) -> tuple[Ws, object]:
    return _make(
        tmp_path_factory,
        xdg_home,
        "uvm_base",
        ["svw_demo_uvm_pkg.sv", "svw_uvm_top.sv"],
        "svw_uvm_top",
    )


@pytest.fixture(scope="module")
def ext(tmp_path_factory: pytest.TempPathFactory, xdg_home: Path) -> tuple[Ws, object]:
    return _make(
        tmp_path_factory,
        xdg_home,
        "uvm_ext",
        ["svw_uvm_override_pkg.sv", "svw_uvm_cfgdb_pkg.sv", "svw_uvm_ext_top.sv"],
        "svw_uvm_ext_top",
    )


def test_UVM_01_real_uvm_static_compile_finds_classes_and_methods(base: tuple[Ws, object]) -> None:
    w, env = base
    bid = env.build_id  # type: ignore[attr-defined]
    cov = env.analysis_coverage  # type: ignore[attr-defined]
    assert bid and cov.completeness == "COMPLETE", env.data["reasons"]  # type: ignore[attr-defined]
    assert env.data["diagnostic_outcome"] == "NO_ERRORS"  # type: ignore[attr-defined]
    g = golden("uvm_base")
    check_declarations(w, bid, g)
    rows = decl_rows(w, bid)
    # the two run_phase methods are separate declarations with separate scopes
    rp = [r for r in rows if r["name"] == "run_phase" and r["file"].endswith("svw_demo_uvm_pkg.sv")]
    assert sorted(r["qualified_name"] for r in rp) == [
        "svw_demo_uvm_pkg::svw_driver::run_phase",
        "svw_demo_uvm_pkg::svw_monitor::run_phase",
    ]
    amb = Q.symbol(opts(w), bid, "r", name="run_phase")  # UVM itself has many more
    assert amb.error is not None and amb.data["candidate_count"] > 2
    # inheritance from the real library, whose source is the pinned external root
    for cls, base_cls in g["bases"].items():
        d = decl_by_qn(rows, cls, "class")[0]
        attrs = json.loads(d["attrs"])
        assert (
            attrs["base_class"].startswith(base_cls.split("::")[-1])
            or base_cls in attrs["base_class"]
        ), attrs
        assert attrs["base_declaration_id"] is not None
    uc = decl_by_qn(rows, "uvm_pkg::uvm_component", "class")[0]
    assert uc["file"].startswith("@uvm/") and uc["file"].endswith(".svh")  # not a fake stub
    drv_base = decl_by_qn(rows, "uvm_pkg::uvm_driver", "class")[0]
    assert drv_base["file"].startswith("@uvm/")
    # `uvm_info macro expansions keep their origin: spelled in the library, expanded in the fixture
    mac = [
        r
        for r in ref_rows(w, bid)
        if json.loads(r["loc"]).get("macro", {}).get("name") == "uvm_info"
    ]
    assert mac
    exp_files = {json.loads(r["loc"])["macro"]["expansion"]["file"] for r in mac}
    assert any(file_matches(f, "fixtures/uvm/svw_demo_uvm_pkg.sv") for f in exp_files)
    assert any(json.loads(r["loc"])["file"].startswith("@uvm/") for r in mac)
    # `run_test` call from the top module resolves into uvm_pkg
    rt = g["run_test_call"]
    hit = [
        r
        for r in ref_rows(w, bid)
        if r["span_start"] == rt["start"] and file_matches(r["file"], "fixtures/uvm/svw_uvm_top.sv")
    ]
    assert len(hit) == 1 and hit[0]["resolution_status"] == "resolved_static"
    assert any(
        d["declaration_id"] == hit[0]["target_id"] and d["file"].startswith("@uvm/") for d in rows
    )
    # more than 150 real library files were read and all were inventoried (no leak)
    rep = json.loads(
        dbq(w, "SELECT dependency_report FROM build_reports WHERE build_id=?", bid)[0][
            "dependency_report"
        ]
    )
    assert rep["ok"] and sum(1 for f in rep["files_read"] if f.startswith("_ext/uvm/")) > 150
    # env/drv/mon are class relationships, not an elaborated instance tree
    h = Q.hierarchy(opts(w), bid, "r")
    assert [i["path"] for i in h.data["items"]] == ["svw_uvm_top"]
    assert "uvm_test_top" not in json.dumps(h.data)


def test_UVM_02_factory_overrides_are_listed_call_sites_not_a_topology(
    ext: tuple[Ws, object],
) -> None:
    w, env = ext
    bid = env.build_id  # type: ignore[attr-defined]
    assert bid and env.analysis_coverage.completeness == "COMPLETE", env.data["reasons"]  # type: ignore[attr-defined]
    g = golden("uvm_factory")
    check_declarations(w, bid, g)
    rows = decl_rows(w, bid)
    by_id = {r["declaration_id"]: r for r in rows}
    refs = ref_rows(w, bid)
    for call in g["override_calls"]:
        hit = [
            r
            for r in refs
            if r["kind"] == "call"
            and r["span_start"] == call["start"]
            and r["span_end"] == call["end"]
            and file_matches(r["file"], "fixtures/uvm/" + call["file"])
        ]
        assert len(hit) == 1, call
        r = hit[0]
        tgt = by_id[r["target_id"]]
        assert tgt["qualified_name"] == f"uvm_pkg::uvm_component::{call['callee']}"
        assert tgt["file"].startswith("@uvm/")
        assert by_id[r["container_id"]]["qualified_name"] == f"{call['in_class']}::build_phase"
        ctx = json.loads(r["context"])
        assert ctx["args"] == call["args"]
        assert r["resolution_status"] == "resolved_static"
    # the two test classes call different overrides; nothing claims which one wins at runtime
    sym = decl_by_qn(rows, "uvm_pkg::uvm_component::set_type_override_by_type")[0]
    q = Q.references(opts(w), bid, sym["symbol_id"], "r")
    assert q.data["total_matches"] == 1
    lim = " ".join(q.analysis_coverage.limitations)  # type: ignore[union-attr]
    assert "no runtime" in lim.lower() or "runtime" in lim.lower()
    assert q.provenance.evidence_kind == "HDL_STATIC_RESOLVED"
    dumped = json.dumps(q.model_dump(mode="json")).lower()
    assert "topology" not in dumped and "effective" not in dumped


def test_UVM_03_config_db_sites_keep_type_wildcard_and_context(ext: tuple[Ws, object]) -> None:
    w, env = ext
    bid = env.build_id  # type: ignore[attr-defined]
    assert bid
    g = golden("uvm_config_db")
    refs = ref_rows(w, bid)
    seen_specs: dict[str, set[str]] = {"set": set(), "get": set()}
    for c in g["calls"]:
        hit = [
            r
            for r in refs
            if r["kind"] == "call"
            and r["span_start"] == c["start"]
            and file_matches(r["file"], "fixtures/uvm/" + c["file"])
        ]
        assert len(hit) == 1, c
        ctx = json.loads(hit[0]["context"])
        # the callee is the specialization uvm_config_db#(<type>)::op, so the type is retained
        spec = ctx["specialization_path"]
        assert spec.startswith("uvm_pkg::uvm_config_db#(") and spec.endswith(f")::{c['op']}")
        assert c["type"] in spec
        seen_specs[c["op"]].add(spec)
        args = ctx["args"]
        assert args[1] == c["inst"] and args[2] == c["field"]  # instance path / field name literals
        if c["op"] == "set":
            assert args[3] == c["value"]
            assert ("*" in c["inst"]) == c["wildcard"]  # wildcard scope is visible in the context
        assert hit[0]["resolution_status"] == "resolved_static"  # a call site, NOT a matched get
    # different types are different specializations (set<int> != set<int unsigned>)
    assert len(seen_specs["set"]) == 3 and len(seen_specs["get"]) == 2
    # the declaration is shared; the sites are separate occurrences
    rows = decl_rows(w, bid)
    set_decl = [r for r in rows if r["qualified_name"] == "uvm_pkg::uvm_config_db::set"]
    assert len(set_decl) == 1
    q = Q.references(opts(w), bid, set_decl[0]["symbol_id"], "r")
    mine = [i for i in q.data["items"] if i["location"]["file"].endswith("svw_uvm_cfgdb_pkg.sv")]
    assert len(mine) == sum(1 for c in g["calls"] if c["op"] == "set")
    dumped = json.dumps(q.data).lower()
    assert "matched" not in dumped and "hit" not in dumped


@pytest.fixture(scope="module")
def static_calls(tmp_path_factory: pytest.TempPathFactory, xdg_home: Path) -> tuple[Ws, object]:
    return _make(
        tmp_path_factory, xdg_home, "uvm_static_calls", ["svw_uvm_static_calls.sv"],
        "svw_uvm_static_calls_top",
    )


def test_UVM_02_03_named_arguments_and_same_name_negative(static_calls: tuple[Ws, object]) -> None:
    """Exercises the real gateway, worker, ingestion and references query; no direct parser."""
    w, env = static_calls
    bid = env.build_id  # type: ignore[attr-defined]
    assert bid and env.data["diagnostic_outcome"] == "NO_ERRORS"  # type: ignore[attr-defined]
    rows = decl_rows(w, bid)
    by_id = {r["declaration_id"]: r for r in rows}
    calls = [r for r in ref_rows(w, bid) if r["kind"] == "call"
             and r["file"].endswith("svw_uvm_static_calls.sv")]
    relevant = [r for r in calls if json.loads(r["context"]).get("uvm")]
    assert len(relevant) == 8
    for r in relevant:
        ctx = json.loads(r["context"])
        meta = ctx["uvm"]
        assert meta["runtime_resolution"] == "not_computed"
        assert meta["evidence"] == "static_call_site"
        assert meta["syntax_available"] and not meta["arguments_truncated"]
        assert all(a["association_issue"] is None for a in meta["arguments"])
        assert by_id[r["target_id"]]["file"].startswith("@uvm/")
        assert meta["declaration"] == by_id[r["target_id"]]["qualified_name"]
    cfg = [json.loads(r["context"])["uvm"] for r in relevant
           if json.loads(r["context"])["uvm"]["api"] == "config_db"]
    assert {m["operation"] for m in cfg} == {"set", "get"}
    setter = next(m for m in cfg if m["operation"] == "set")
    assert "int" in setter["specialization_path"]
    assert {a["formal"]: a["expression"] for a in setter["arguments"]} == {
        "value": "32", "field_name": '"width"', "cntxt": "this", "inst_name": '"env.*"',
    }
    creates = [json.loads(r["context"])["uvm"] for r in relevant
               if json.loads(r["context"])["uvm"]["operation"] == "create"]
    assert len(creates) == 2
    named = next(m for m in creates if m["arguments"][0]["kind"] == "named")
    assert [a["formal"] for a in named["arguments"]] == ["parent", "name"]
    ordered = next(m for m in creates if m["arguments"][0]["kind"] == "ordered")
    assert ordered["arguments"][1]["kind"] == "empty"
    assert ordered["arguments"][1]["formal"] == "parent"
    assert ordered["arguments"][1]["expression"] is None
    negatives = [r for r in calls if r["target_id"] in by_id
                 and "::local_db::" in by_id[r["target_id"]]["qualified_name"]]
    assert len(negatives) == 2
    assert all("uvm" not in json.loads(r["context"]) for r in negatives)
    # Metadata survives ingestion and the public references query.
    declaration = next(r for r in rows if r["qualified_name"] == "uvm_pkg::uvm_config_db::set")
    response = Q.references(opts(w), bid, declaration["symbol_id"], "r")
    assert any(i["context"].get("uvm") == setter for i in response.data["items"])
