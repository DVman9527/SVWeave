"""ADR-013 with the REAL pyslang worker on the fixtures/uvm profile: tasks and constructors are
edited BY NAME (``slang_symbol``); no Serena is involved or counted. Fixtures are copied into a
temp workspace; the repository fixtures are never edited.
"""

from __future__ import annotations

import json

import pytest

from svweave.application import edit as E
from svweave.domain.errors import ErrorCode, SvwError
from tests.integration.helpers import UVM_SRC
from tests.integration.test_edt_flow import PKG, QN, ws  # noqa: F401  (fixture reuse)
from tests.unit._edit_fx import EditWs, typed_confirm

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not (UVM_SRC / "uvm_pkg.sv").is_file(), reason="pinned UVM library missing"),
]

NEW_RUN = (
    "task run_phase(uvm_phase phase);\n"
    '      `uvm_info("SVW_DRV", "driver by name", UVM_LOW)\n'
    "    endtask"
)
NEW_CTOR = (
    'function new(string name = "svw_driver", uvm_component parent = null);\n'
    "      super.new(name, parent);\n"
    "      // edited by name\n"
    "    endfunction"
)


def slang(qn: str, repl: str, **kw: str) -> dict[str, str]:
    return {"kind": "slang_symbol", "qualified_name": qn, "replacement": repl, **kw}


def flow(ws: EditWs, base, edits, key: str):  # type: ignore[no-untyped-def]  # noqa: F811
    e = E.prepare(ws.opts, "r", ws.req(base, edits, key, ["uvm_smoke"]), use_serena=False)
    tid = e.data["transaction_id"]
    v = E.validate(ws.opts, "r", transaction_id=tid)
    assert v.error is None, (v.error, v.data["validation"]["failures"])
    E.approve(ws.opts, "r", transaction_id=tid, patch_hash=v.data["patch_hash"],
              confirm=typed_confirm())
    return tid, v.data["patch_hash"], e


def test_edt13_task_and_constructor_by_name_end_to_end(ws: EditWs) -> None:  # noqa: F811
    base = ws.build("uvm_smoke")
    assert base.analysis_coverage.completeness.value == "COMPLETE"  # type: ignore[union-attr]
    orig = ws.read(PKG)
    t = ws.decl(base.build_id, QN + "svw_driver::run_phase")
    c = ws.decl(base.build_id, QN + "svw_driver::new")
    old_t, old_c = orig[t["full_start"]:t["full_end"]], orig[c["full_start"]:c["full_end"]]
    assert old_t.startswith(b"task run_phase") and old_t.endswith(b"endtask"), old_t
    assert old_c.startswith(b"function new") and old_c.endswith(b"endfunction"), old_c

    tid, h, prep = flow(ws, base, [
        slang(QN + "svw_driver::run_phase", NEW_RUN),
        slang(QN + "svw_driver::new", NEW_CTOR),
    ], "uvm-name-0001")
    assert ws.read(PKG) == orig, "nothing written before commit"
    recs = {r["qualified_name"]: r for r in prep.data["edits"]}
    for qn, kind, ctor in ((QN + "svw_driver::run_phase", "task", False),
                           (QN + "svw_driver::new", "function", True)):
        r = recs[qn]
        assert r["edit_path"] == "slang_symbol" and r["counted_as_serena_semantic_edit"] is False
        assert r["serena"] is None
        rs = r["resolved_symbol"]
        assert (rs["kind"], rs["constructor"], rs["file"]) == (kind, ctor, PKG)
        assert rs["build_id"] == base.build_id and len(rs["symbol_id"]) == 64
    assert prep.data["edit_paths"] == ["slang_symbol"]

    r = E.commit(ws.opts, "r", transaction_id=tid, expected_patch_hash=h, request_key="uvm-name-cm01")
    assert r.data["state"] == "POST_APPLY_VERIFIED" and r.error is None
    assert r.data["edit_paths"] == ["slang_symbol"]
    new = ws.read(PKG)
    expect = orig
    for s0, e0, repl in sorted(((t["full_start"], t["full_end"], NEW_RUN),
                                (c["full_start"], c["full_end"], NEW_CTOR)), reverse=True):
        expect = expect[:s0] + repl.encode() + expect[e0:]
    assert new == expect, "only the two declarations changed; the rest is byte-identical"
    # the sibling monitor declarations are intact
    for qn in ("svw_monitor::run_phase", "svw_monitor::new"):
        d0 = ws.decl(base.build_id, QN + qn)
        assert orig[d0["full_start"]:d0["full_end"]] in new
    pa = r.data["post_apply"]["uvm_smoke"]
    assert pa["snapshot_equals_candidate"] and pa["completeness"] == "COMPLETE"
    j = json.loads((ws.root / ".svweave/journal" / f"{tid}.json").read_text())
    assert j["status"] == "complete"
    # the stored plan record shows what was resolved
    rec = next(x for x in ws.txn_rows() if x["transaction_id"] == tid)
    assert "slang_symbol" in rec["record"] and "resolved_symbol" in rec["record"]


def test_edt13_bare_build_phase_is_ambiguous_and_nothing_is_selected(ws: EditWs) -> None:  # noqa: F811
    base = ws.build("uvm_smoke")
    orig = ws.read(PKG)
    with pytest.raises(SvwError) as ei:
        E.prepare(ws.opts, "r", ws.req(
            base, [slang("build_phase", "function void build_phase(uvm_phase phase); endfunction")],
            "uvm-name-0002", ["uvm_smoke"]), use_serena=False)
    assert ei.value.code is ErrorCode.AMBIGUOUS_SYMBOL
    qns = sorted(c["qualified_name"] for c in ei.value.details["candidates"])
    # the pinned UVM library declares more build_phase methods: all are listed, none is picked
    assert {QN + "svw_env::build_phase", QN + "svw_smoke_test::build_phase"} <= set(qns)
    assert len(qns) > 2 and "uvm_pkg::uvm_agent::build_phase" in qns
    assert ws.read(PKG) == orig and ws.txn_rows() == []


def test_edt13_source_changed_after_prepare_is_refused_by_cas(ws: EditWs) -> None:  # noqa: F811
    base = ws.build("uvm_smoke")
    tid, h, _ = flow(ws, base, [slang(QN + "svw_driver::run_phase", NEW_RUN)], "uvm-name-0003")
    (ws.root / PKG).write_text(ws.text(PKG) + "// human edit after prepare\n")
    human = ws.read(PKG)
    with pytest.raises(SvwError) as ei:
        E.commit(ws.opts, "r", transaction_id=tid, expected_patch_hash=h,
                 request_key="uvm-name-cm02")
    assert ei.value.code is ErrorCode.EDIT_CONFLICT
    assert ws.read(PKG) == human, "the human's version is untouched"


def test_edt13_uvm_info_macro_stays_allowed_in_a_replacement(ws: EditWs) -> None:  # noqa: F811
    """The UVM report macro (with its nested macros and conditionals) passes the macro-body walk."""
    base = ws.build("uvm_smoke")
    e = E.prepare(ws.opts, "r", ws.req(
        base, [slang(QN + "svw_monitor::run_phase", NEW_RUN.replace("SVW_DRV", "SVW_MON"))],
        "uvm-name-0004", ["uvm_smoke"]), use_serena=False)
    [rec] = e.data["edits"]
    assert rec["edit_path"] == "slang_symbol"
