"""ADR-013: name-based task / function / constructor edits located by slang (kind ``slang_symbol``).

Real pyslang builds (no mocks): the semantic database decides what a name resolves to. Every
refusal fails closed; the write path is the SAME exact-range CAS / approval / journal path.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from svweave.adapters.serena.guard import tree_hashes
from svweave.application import edit as E
from svweave.domain.envelope import Envelope
from svweave.domain.errors import ErrorCode, SvwError
from svweave.editing import plan as P
from svweave.editing.models import PrepareRequest
from tests.unit._edit_fx import EditWs, make_ws, sha, to_approval

NEW_T = 'task t();\n      $display("task by name");\n    endtask'
NEW_CTOR = 'function new();\n      $display("ctor by name");\n    endfunction'
EXTERN_PKG = """\
package p;
  class c;
    extern function int ef(int x);
    function int g(int x);
      return x * 2;
    endfunction
  endclass
  function int c::ef(int x);
    return x + 1;
  endfunction
endpackage
"""


@pytest.fixture
def ws(tmp_path: Path) -> EditWs:
    return make_ws(tmp_path)


def slang(qn: str | None, repl: str, **kw: Any) -> dict[str, Any]:
    d: dict[str, Any] = {"kind": "slang_symbol", "replacement": repl}
    if qn is not None:
        d["qualified_name"] = qn
    d.update(kw)
    return d


def prep(ws: EditWs, base: Envelope, edits: list[dict[str, Any]]) -> Envelope:
    return E.prepare(ws.opts, "r-prep", ws.req(base, edits), use_serena=False)


def err(fn: Any, *a: Any, **kw: Any) -> SvwError:
    with pytest.raises(SvwError) as ei:
        fn(*a, **kw)
    return ei.value


def declaration_bytes(ws: EditWs, base: Envelope, qn: str) -> tuple[bytes, int, int]:
    d = ws.decl(base.build_id or "", qn)
    return ws.read("rtl/pkg.sv")[d["full_start"]:d["full_end"]], d["full_start"], d["full_end"]


def resolve_directly(ws: EditWs, build_id: str, snapshot_id: str, edits: list[dict[str, Any]]) -> Any:
    ctx = ws.w.ctx()
    rec, _ = E._base_rec(ctx, snapshot_id)
    req = PrepareRequest.model_validate(
        {"base_build_id": build_id, "snapshot_id": snapshot_id, "edits": edits,
         "request_key": "key-direct-0001"}
    )
    with ws.db() as db:
        return P.resolve_edits(ctx, db, build_id, rec, req.edits)


# ------------------------------------------------------------------------------ model / schema
def test_model_accepts_name_or_symbol_id_and_rejects_both_none_or_extra() -> None:
    base = {"base_build_id": "a" * 64, "snapshot_id": "b" * 64, "request_key": "key-model-0001"}

    def mk(edit: dict[str, Any]) -> PrepareRequest:
        return PrepareRequest.model_validate({**base, "edits": [edit]})

    mk(slang("p::c::t", "x"))
    mk(slang(None, "x", symbol_id="c" * 64, expected_sha256="d" * 64))
    for bad in (
        slang(None, "x"),  # neither selector
        slang("p::c::t", "x", symbol_id="c" * 64),  # both
        slang("p::c::t", "x", expected_sha256="nothex"),
        slang("p::c::t", "x", symbol_id="c" * 64) | {"qualified_name": "p::c::t"},
        slang("p::c::t", "x", approved=True),  # no approval field exists
        slang("p::c::t", "x", logical_path="rtl/pkg.sv"),
    ):
        with pytest.raises(ValidationError):
            mk(bad)


def test_mcp_input_schema_accepts_and_rejects_slang_symbol() -> None:
    import jsonschema

    from svweave.gateway.schemas import INPUT

    schema = INPUT["svw_edit"]
    base = {"action": "prepare", "base_build_id": "a" * 64, "snapshot_id": "b" * 64,
            "request_key": "key-schema-0001"}
    ok = [slang("p::c::t", "x"), slang(None, "x", symbol_id="c" * 64)]
    for edit in ok:
        jsonschema.validate({**base, "edits": [edit]}, schema)
    for bad in (slang(None, "x"), slang("p::c::t", "x", symbol_id="c" * 64),
                slang("p::c::t", "x", approved=True)):
        with pytest.raises(jsonschema.ValidationError):
            jsonschema.validate({**base, "edits": [bad]}, schema)


# ------------------------------------------------------------------------------ happy paths
def test_task_and_constructor_by_name_are_planned_as_labelled_exact_range(ws: EditWs) -> None:
    base = ws.build()
    before = tree_hashes(ws.root / "rtl")
    t_old, ts, te = declaration_bytes(ws, base, "p::c::t")
    n_old, ns, ne = declaration_bytes(ws, base, "p::c::new")
    assert t_old.startswith(b"task t") and t_old.endswith(b"endtask")
    assert n_old.startswith(b"function new") and n_old.endswith(b"endfunction")
    e = prep(ws, base, [slang("p::c::t", NEW_T), slang("p::c::new", NEW_CTOR,
                                                       expected_sha256=sha(n_old))])
    assert e.data["edit_paths"] == ["slang_symbol"]
    assert tree_hashes(ws.root / "rtl") == before
    recs = {r["qualified_name"]: r for r in e.data["edits"]}
    t, n = recs["p::c::t"], recs["p::c::new"]
    for r, (s0, e0), old in ((t, (ts, te), t_old), (n, (ns, ne), n_old)):
        assert r["edit_path"] == "slang_symbol"
        assert r["counted_as_serena_semantic_edit"] is False and r["serena"] is None
        assert r["expected_sha256"] == sha(old)
        rs = r["resolved_symbol"]
        assert (rs["start_byte"], rs["end_byte"]) == (s0, e0) == (r["start_byte"], r["end_byte"])
        assert rs["build_id"] == base.build_id and rs["file"] == "rtl/pkg.sv"
        assert rs["symbol_id"] == ws.decl(base.build_id or "", rs["qualified_name"])["symbol_id"]
    assert t["resolved_symbol"]["constructor"] is False and t["resolved_symbol"]["kind"] == "task"
    assert n["resolved_symbol"]["constructor"] is True and n["resolved_symbol"]["kind"] == "function"
    cand = (ws.root / e.data["candidate_dir"] / "rtl/pkg.sv").read_bytes()
    orig = ws.read("rtl/pkg.sv")
    assert ne <= ts
    assert cand == orig[:ns] + NEW_CTOR.encode() + orig[ne:ts] + NEW_T.encode() + orig[te:]
    assert b"task by name" in cand and b"ctor by name" in cand


def test_symbol_id_and_suffix_name_resolve_to_the_same_declaration(ws: EditWs) -> None:
    base = ws.build()
    d = ws.decl(base.build_id or "", "p::c::t")
    [a] = resolve_directly(ws, base.build_id or "", base.snapshot_id or "",
                           [slang(None, NEW_T, symbol_id=d["symbol_id"])])[0]
    [b] = resolve_directly(ws, base.build_id or "", base.snapshot_id or "",
                           [slang("c::t", NEW_T)])[0]
    assert (a.start, a.end, a.declaration_id) == (b.start, b.end, b.declaration_id)


def test_full_flow_commit_changes_only_the_declaration_bytes(ws: EditWs) -> None:
    base = ws.build()
    orig = ws.read("rtl/pkg.sv")
    _, ts, te = declaration_bytes(ws, base, "p::c::t")
    tid, h = to_approval(ws, base, [slang("p::c::t", NEW_T)], serena=False)
    r = E.commit(ws.opts, "r-c", transaction_id=tid, expected_patch_hash=h, request_key="slang-commit-1")
    assert r.data["state"] == "POST_APPLY_VERIFIED" and r.data["edit_paths"] == ["slang_symbol"]
    assert ws.read("rtl/pkg.sv") == orig[:ts] + NEW_T.encode() + orig[te:]


# ------------------------------------------------------------------------------ refusals
def test_bare_name_matching_several_is_ambiguous_and_lists_scoped_candidates(ws: EditWs) -> None:
    base = ws.build()
    before = tree_hashes(ws.root / "rtl")
    e = err(prep, ws, base, [slang("f", "function int f(int x); return 0; endfunction")])
    assert e.code is ErrorCode.AMBIGUOUS_SYMBOL and e.details["reason"] == "ambiguous_name"
    qns = sorted(c["qualified_name"] for c in e.details["candidates"])
    assert qns == ["p::c::f", "p::d::f"]
    assert all(len(c["symbol_id"]) == 64 for c in e.details["candidates"])
    assert tree_hashes(ws.root / "rtl") == before and ws.txn_rows() == []
    # scoped name resolves
    ok = prep(ws, base, [slang("d::f", "function int f(int x);\n      return x - 2;\n    endfunction")])
    assert ok.data["edits"][0]["qualified_name"] == "p::d::f"


def test_unknown_name_is_missing_input(ws: EditWs) -> None:
    base = ws.build()
    assert err(prep, ws, base, [slang("p::c::nope", NEW_T)]).code is ErrorCode.MISSING_INPUT
    assert err(prep, ws, base, [slang(None, NEW_T, symbol_id="e" * 64)]).code \
        is ErrorCode.MISSING_INPUT


def test_symbol_id_of_a_non_subroutine_is_refused(ws: EditWs) -> None:
    base = ws.build()
    d = ws.decl(base.build_id or "", "p::c")
    e = err(prep, ws, base, [slang(None, NEW_T, symbol_id=d["symbol_id"])])
    assert e.code is ErrorCode.POLICY_DENIED and e.details["reason"] == "not_a_subroutine"


def test_macro_generated_declaration_is_refused(ws: EditWs) -> None:
    base = ws.build()
    e = err(prep, ws, base, [slang("p::e::hh", "function int hh(); return 1; endfunction")])
    assert e.code is ErrorCode.POLICY_DENIED and e.details["reason"] in (
        "macro_expansion", "macro_definition_edit_unsupported", "no_full_span")


def test_extern_method_resolves_to_the_out_of_block_definition(tmp_path: Path) -> None:
    """slang stores ONE declaration for an extern method: the out-of-block definition (verified
    with a real build), so the name edits that body and never the prototype."""
    ws = make_ws(tmp_path)
    ws.w.write("rtl/pkg.sv", EXTERN_PKG)
    base = ws.build()
    assert base.build_id, base.error
    old = ws.read("rtl/pkg.sv")
    d = ws.decl(base.build_id, "p::c::ef")
    assert old[d["full_start"]:d["full_end"]].startswith(b"function int c::ef")
    e = prep(ws, base, [slang("p::c::ef", "function int c::ef(int x);\n    return 0;\n  endfunction")])
    assert e.data["edits"][0]["edit_path"] == "slang_symbol"
    cand = (ws.root / e.data["candidate_dir"] / "rtl/pkg.sv").read_bytes()
    assert b"extern function int ef(int x);" in cand and b"return 0;" in cand


def test_extern_prototype_span_is_refused(tmp_path: Path) -> None:
    """Fail closed if the stored span ever covers a prototype: simulated by pointing the stored
    full span of the declaration at the ``extern`` line."""
    ws = make_ws(tmp_path)
    ws.w.write("rtl/pkg.sv", EXTERN_PKG)
    base = ws.build()
    data = ws.read("rtl/pkg.sv")
    proto = b"extern function int ef(int x);"
    ps = data.index(proto)
    for lo, hi in ((ps + len(b"extern ") , ps + len(proto)), (ps, ps + len(proto))):
        con = sqlite3.connect(ws.root / ".svweave" / "svweave.db")
        con.execute("UPDATE declarations SET full_start=?, full_end=? WHERE build_id=? "
                    "AND qualified_name='p::c::ef'", (lo, hi, base.build_id))
        con.commit()
        con.close()
        e = err(prep, ws, base, [slang("p::c::ef", "function int ef(int x); return 0; endfunction")])
        assert e.code is ErrorCode.POLICY_DENIED and e.details["reason"] == "extern_prototype", lo


def test_expected_sha256_mismatch_is_a_conflict_and_match_is_accepted(ws: EditWs) -> None:
    base = ws.build()
    old, _, _ = declaration_bytes(ws, base, "p::c::t")
    e = err(prep, ws, base, [slang("p::c::t", NEW_T, expected_sha256=sha(b"other"))])
    assert e.code is ErrorCode.EDIT_CONFLICT and e.details["reason"] == "expected_bytes_mismatch"
    prep(ws, base, [slang("p::c::t", NEW_T, expected_sha256=sha(old))])


@pytest.mark.parametrize("repl", [
    "task t(); $display(1);",  # no endtask
    "function int t(); return 1; endfunction",  # kind change
    "// c\ntask t(); endtask",  # not starting at the keyword
    "task t(); endtask\nmodule m; endmodule",  # trailing junk
])
def test_replacement_must_be_a_whole_declaration_of_the_same_kind(ws: EditWs, repl: str) -> None:
    base = ws.build()
    e = err(prep, ws, base, [slang("p::c::t", repl)])
    assert e.code is ErrorCode.POLICY_DENIED and e.details["reason"] == "replacement_shape"


def test_end_label_is_accepted_in_the_replacement(ws: EditWs) -> None:
    base = ws.build()
    prep(ws, base, [slang("p::c::t", 'task t();\n    endtask : t')])


def test_two_edits_of_the_same_declaration_overlap(ws: EditWs) -> None:
    base = ws.build()
    e = err(prep, ws, base, [slang("p::c::t", NEW_T), slang("c::t", NEW_T)])
    assert e.code is ErrorCode.POLICY_DENIED and e.details["reason"] == "overlapping_edits"


def test_read_only_root_is_refused(tmp_path: Path) -> None:
    ws = make_ws(tmp_path, root_writable=False)
    base = ws.build()
    e = err(prep, ws, base, [slang("p::c::t", NEW_T)])
    assert e.code is ErrorCode.POLICY_DENIED and e.details["reason"] == "root_not_writable"


def test_path_outside_allowed_paths_is_refused(tmp_path: Path) -> None:
    ws = make_ws(tmp_path, editing={"allowed_paths": ["rtl/top.sv"]})
    base = ws.build()
    e = err(prep, ws, base, [slang("p::c::t", NEW_T)])
    assert e.code is ErrorCode.POLICY_DENIED and e.details["reason"] == "path_not_allowed"


def test_generated_input_is_refused(ws: EditWs) -> None:
    import yaml

    p = ws.root / "svweave.yaml"
    m = yaml.safe_load(p.read_text())
    m["profiles"]["p"]["generated_inputs"] = [{"path": "rtl/pkg.sv"}]
    p.write_text(yaml.safe_dump(m, sort_keys=False))
    base = ws.build()
    e = err(prep, ws, base, [slang("p::c::t", NEW_T)])
    assert e.code is ErrorCode.POLICY_DENIED and e.details["reason"] == "role_not_editable"


def test_partial_build_refuses_name_resolution_at_prepare_and_in_the_resolver(ws: EditWs) -> None:
    base = ws.build()
    con = sqlite3.connect(ws.root / ".svweave" / "svweave.db")
    con.execute("UPDATE builds SET completeness='PARTIAL' WHERE build_id=?", (base.build_id,))
    con.commit()
    con.close()
    assert err(prep, ws, base, [slang("p::c::t", NEW_T)]).code is ErrorCode.PARTIAL_ANALYSIS
    # the resolver itself also refuses (it does not rely on the caller having checked)
    e = err(resolve_directly, ws, base.build_id or "", base.snapshot_id or "",
            [slang("p::c::t", NEW_T)])
    assert e.code is ErrorCode.PARTIAL_ANALYSIS and e.details["reason"] == "base_not_complete"


def test_stale_workspace_refuses_prepare(ws: EditWs) -> None:
    base = ws.build()
    (ws.root / "rtl/pkg.sv").write_text(ws.text("rtl/pkg.sv") + "// human edit\n")
    assert err(prep, ws, base, [slang("p::c::t", NEW_T)]).code is ErrorCode.STALE_SNAPSHOT


def test_resolver_refuses_a_build_that_belongs_to_another_snapshot(ws: EditWs) -> None:
    """The name -> declaration lookup is only valid in the build OF the base snapshot (M14)."""
    old = ws.build()
    (ws.root / "rtl/pkg.sv").write_text(ws.text("rtl/pkg.sv") + "// moved on\n")
    new = ws.build()
    assert old.snapshot_id != new.snapshot_id
    e = err(resolve_directly, ws, old.build_id or "", new.snapshot_id or "",
            [slang("p::c::t", NEW_T)])
    assert e.code is ErrorCode.STALE_SNAPSHOT and e.details["reason"] == "build_snapshot_mismatch"
    # the matching pair resolves
    resolve_directly(ws, new.build_id or "", new.snapshot_id or "", [slang("p::c::t", NEW_T)])


def test_source_changed_after_prepare_blocks_commit_by_cas(ws: EditWs) -> None:
    base = ws.build()
    tid, h = to_approval(ws, base, [slang("p::c::t", NEW_T)], serena=False)
    (ws.root / "rtl/pkg.sv").write_text(ws.text("rtl/pkg.sv") + "// human\n")
    human = ws.read("rtl/pkg.sv")
    e = err(E.commit, ws.opts, "r-c", transaction_id=tid, expected_patch_hash=h,
            request_key="slang-commit-2")
    assert e.code is ErrorCode.EDIT_CONFLICT
    assert ws.read("rtl/pkg.sv") == human


# ------------------------------------------------------------------------------ review round 2
MULTI_TOP = """\
function automatic int gt(); return 7; endfunction
interface ifc; task t(); endtask endinterface
module top(input logic clk, output logic [3:0] q);
  import p::*;
  task t(); $display("MODULE task t"); endtask
  always_ff @(posedge clk) q <= q + 4'd1;
endmodule
"""
MULTI_PKG = """\
package p;
  class c;
    task t();
      $display("class task");
    endtask
  endclass
endpackage
"""


@pytest.fixture
def multi(tmp_path: Path) -> EditWs:
    ws = make_ws(tmp_path)
    ws.w.write("rtl/top.sv", MULTI_TOP)
    ws.w.write("rtl/pkg.sv", MULTI_PKG)
    return ws


def test_b1_bare_name_shared_by_module_interface_and_class_is_ambiguous(multi: EditWs) -> None:
    base = multi.build()
    assert base.build_id, base.error
    e = err(prep, multi, base, [slang("t", "task t(); endtask")])
    assert e.code is ErrorCode.AMBIGUOUS_SYMBOL and e.details["reason"] == "ambiguous_name"
    assert sorted(c["qualified_name"] for c in e.details["candidates"]) == ["ifc.t", "p::c::t", "top.t"]
    # a partial suffix respects both separators and is unique per scope
    for name, qn in (("c::t", "p::c::t"), ("p::c::t", "p::c::t"), ("ifc.t", "ifc.t"),
                     ("top.t", "top.t"), ("top::t", "top.t"), ("ifc::t", "ifc.t")):
        [r], _ = resolve_directly(multi, base.build_id or "", base.snapshot_id or "",
                                  [slang(name, "task t(); endtask")])
        assert r.qualified_name == qn, name


def test_b2_module_interface_and_unit_scope_subroutines_can_be_named(multi: EditWs) -> None:
    base = multi.build()
    gt = "function automatic int gt(); return 8; endfunction"
    for name in ("gt", "$unit::gt"):
        [r], _ = resolve_directly(multi, base.build_id or "", base.snapshot_id or "",
                                  [slang(name, gt)])
        assert r.qualified_name == "gt"
    # $unit:: roots the name: it never matches a nested declaration
    e = err(prep, multi, base, [slang("$unit::t", "task t(); endtask")])
    assert e.code is ErrorCode.MISSING_INPUT
    assert "pkg::cls::name" in e.message
    # a module task goes through the whole validation
    new = 'task t(); $display("MODULE task edited"); endtask'
    tid, h = to_approval(multi, base, [slang("top.t", new)], serena=False)
    r = E.commit(multi.opts, "r", transaction_id=tid, expected_patch_hash=h, request_key="b2-commit-01")
    assert r.data["state"] == "POST_APPLY_VERIFIED"
    assert b"MODULE task edited" in multi.read("rtl/top.sv")


@pytest.mark.parametrize("repl", [
    "task t(); // endtask",  # tail hidden in a line comment
    "task t(); /* endtask */",
    'task t(); $display("endtask");',
    "task t(); /* endtask",  # unterminated block comment
    'task t(); $display("endtask);\nendtask',  # unterminated string
    "virtual task t(); endtask",  # leading qualifier: the stored span excludes it
    "static task t(); endtask",
    "task t(); endtask\n  endclass\n  class injected;\n    function int sneaky(); return 42; endfunction\n"
    "    task t2();\n    endtask",  # class boundary injection
    "task t(); endtask task u(); endtask",  # two declarations
    "task t(); endtask endpackage package z; task u(); endtask",
])
def test_b3_replacement_must_be_exactly_one_declaration(ws: EditWs, repl: str) -> None:
    base = ws.build()
    e = err(prep, ws, base, [slang("p::c::t", repl)])
    assert e.code is ErrorCode.POLICY_DENIED and e.details["reason"] == "replacement_shape"


def test_b3_duplicate_static_qualifier_is_refused(ws: EditWs) -> None:
    ws.w.write("rtl/pkg.sv", "package p;\n  class c;\n    static function int sf(); return 1; endfunction\n"
                            "  endclass\nendpackage\n")
    base = ws.build()
    e = err(prep, ws, base, [slang("p::c::sf", "static function int sf(); return 2; endfunction")])
    assert e.code is ErrorCode.POLICY_DENIED and e.details["reason"] == "replacement_shape"
    ok = prep(ws, base, [slang("p::c::sf", "function int sf(); return 2; endfunction")])
    cand = (ws.root / ok.data["candidate_dir"] / "rtl/pkg.sv").read_text()
    assert "static function int sf(); return 2;" in cand and "static static" not in cand


def test_masking_keeps_length_and_blanks_comments_and_strings() -> None:
    src = b'task t(); // c "x"\n $display("a//b\\"c"); /* e\nf */ endtask'
    m = P.mask_comments_strings(src)
    assert m is not None and len(m) == len(src) and m.count(b"\n") == src.count(b"\n")
    assert b"endtask" in m and b"//" not in m and b'"' not in m
    assert P.mask_comments_strings(b"/* open") is None and P.mask_comments_strings(b'"open') is None


def _validate_failures(ws: EditWs, base: Envelope, edit: dict[str, Any]) -> tuple[Envelope, list[str]]:
    e = prep(ws, base, [edit])
    v = E.validate(ws.opts, "r-val", transaction_id=e.data["transaction_id"])
    return v, [f["reason"] for f in v.data["validation"]["failures"]]


def test_b3_renamed_constructor_is_a_scope_escape_and_cannot_be_approved(ws: EditWs) -> None:
    base = ws.build()
    v, reasons = _validate_failures(
        ws, base, slang("p::c::new", "function void not_a_ctor();\n    endfunction"))
    assert "slang_symbol_scope_escape" in reasons
    assert v.error is not None and v.error.code == ErrorCode.POLICY_DENIED.value
    assert v.data["validation"]["auto_committable"] is False
    from tests.unit._edit_fx import typed_confirm

    tid = v.data["transaction_id"] if "transaction_id" in v.data else \
        ws.txn_rows()[-1]["transaction_id"]
    e = err(E.approve, ws.opts, "r-appr", transaction_id=tid,
            patch_hash=v.data["patch_hash"], confirm=typed_confirm())
    assert e.code is ErrorCode.APPROVAL_REQUIRED
    assert b"not_a_ctor" not in ws.read("rtl/pkg.sv")


def test_b3_renamed_task_is_a_scope_escape(ws: EditWs) -> None:
    base = ws.build()
    v, reasons = _validate_failures(ws, base, slang("p::c::t", "task t_renamed(); endtask"))
    assert "slang_symbol_scope_escape" in reasons
    problems = [f for f in v.data["validation"]["failures"]
                if f["reason"] == "slang_symbol_scope_escape"][0]["problems"]
    assert any(p.get("problem") == "target_not_unique" for p in problems)


def test_b3_contained_edit_passes_the_scope_check(ws: EditWs) -> None:
    base = ws.build()
    v, reasons = _validate_failures(ws, base, slang("p::c::t", NEW_T.replace("by name", "ok")))
    assert v.error is None and reasons == []


def test_b3_scope_escape_detector_sees_added_declarations(ws: EditWs) -> None:
    """Direct test of the decisive check: a candidate build that gained a class is rejected."""
    base = ws.build()
    p = ws.root / "rtl/pkg.sv"
    p.write_text(ws.text("rtl/pkg.sv") + "class injected; task z(); endtask endclass\n")
    cand = ws.build()
    t = ws.decl(base.build_id or "", "p::c::t")
    target = {"qualified_name": "p::c::t", "kind": "task", "file": "rtl/pkg.sv", "constructor": False,
              "start_byte": t["full_start"], "end_byte": t["full_end"]}
    with ws.db() as db:
        problems = P.slang_scope_escape(db, base.build_id or "", cand.build_id or "", [target])
        assert problems and problems[-1]["problem"] == "declarations_outside_target_changed"
        assert any("injected" in k[0] for k in problems[-1]["added"])
        assert P.slang_scope_escape(db, base.build_id or "", base.build_id or "", [target]) == []


# ------------------------------------------------------------------------------ preprocessor escapes
TOP6 = """\
module top(input logic clk, output logic [3:0] q);
  import p::*;
  task mtask();
    $display("m");
  endtask
  always_ff @(posedge clk) q <= q + 4'd1;
endmodule
"""
EVIL_MOD = (
    'task mtask();\n    $display("m");\n`define SVW_E end``task\n  `SVW_E\n'
    '  initial $display("INJECTED OUTSIDE THE TASK");\n`define SVW_Z endtask'
)
EVIL_REDEF = 'task t();\n      $display("task hello");\n`undef BUMP\n`define BUMP 1000\n    endtask'
EVIL_CLASS = (
    'task t();\n      $display("task hello");\n`define SVW_E end``task\n    `SVW_E\n'
    "    rand int injected_prop;\n`define SVW_Z endtask"
)


@pytest.mark.parametrize("repl", [
    EVIL_MOD,
    EVIL_REDEF,
    EVIL_CLASS,
    "task t(); endtask `define X 1",
    "task t();\n`ifdef NEVER\n$display(1);\n`endif\nendtask",
    "task t();\n`include \"x.svh\"\nendtask",
    "task t();\n`timescale 1ns/1ps\nendtask",
    "task t();\n`pragma protect begin\nendtask",
    "task t();\n`undefineall\nendtask",
    "task t();\n`default_nettype none\nendtask",
    "task t(); $display(`\"a`\"); endtask",
    "task t(); int x = a``b; endtask",
])
def test_r1_directives_pasting_and_stringification_are_refused(ws: EditWs, repl: str) -> None:
    base = ws.build()
    e = err(prep, ws, base, [slang("p::c::t", repl)])
    assert e.code is ErrorCode.POLICY_DENIED and e.details["reason"] == "replacement_directive"
    assert ws.txn_rows() == []


def test_r1_macro_end_task_escape_on_a_module_task_is_refused(tmp_path: Path) -> None:
    ws = make_ws(tmp_path, top=TOP6)
    base = ws.build()
    before = ws.read("rtl/top.sv")
    e = err(prep, ws, base, [slang("top.mtask", EVIL_MOD)])
    assert e.details["reason"] == "replacement_directive"
    assert ws.read("rtl/top.sv") == before


def test_r2_macro_redefinition_cannot_change_later_code(tmp_path: Path) -> None:
    ws = make_ws(tmp_path, top=TOP6.replace("q + 4'd1", "q + 4'(`BUMP)"))
    base = ws.build()
    assert base.build_id, base.error
    e = err(prep, ws, base, [slang("p::c::t", EVIL_REDEF)])
    assert e.details["reason"] == "replacement_directive"


def test_r1_backticks_in_comments_and_strings_are_not_directives(ws: EditWs) -> None:
    base = ws.build()
    ok = prep(ws, base, [slang(
        "p::c::t", 'task t();\n  // `define X, `ifdef Y\n  $display("`undef `` `\\"");\nendtask')])
    assert ok.data["edits"][0]["edit_path"] == "slang_symbol"


def test_r1_macro_invocation_rules(tmp_path: Path) -> None:
    ws = make_ws(tmp_path)
    ws.w.write("rtl/defs.svh", "`define BUMP 1\n`define MKF function int hh(); return 0; endfunction\n`define SHOW(x) $display(x)\n`define OUTER `SHOW(\"o\")\n"
                               "`define CLOSER $display(1); endtask\n`define VIA_CLOSER `CLOSER\n")
    base = ws.build()
    assert base.build_id, base.error
    body = "task t();\n  %s\nendtask"
    # defined, no closing keyword, also transitively: allowed
    for use in ("`SHOW(\"hi\");", "`OUTER;", "int a = `BUMP;", "$display(`__FILE__, `__LINE__);"):
        resolve_directly(ws, base.build_id or "", base.snapshot_id or "", [slang("p::c::t", body % use)])
    # unknown macro, closing keyword directly, or through another macro: refused, distinct from shape
    for use in ("`NOT_DEFINED;", "`CLOSER", "`VIA_CLOSER"):
        e = err(resolve_directly, ws, base.build_id or "", base.snapshot_id or "",
                [slang("p::c::t", body % use)])
        assert e.code is ErrorCode.POLICY_DENIED and e.details["reason"] == "replacement_directive", use


def test_r1_span_equality_backstop_rejects_a_candidate_whose_target_span_differs(ws: EditWs) -> None:
    """Slang-backed backstop (M21): the candidate's target span must be exactly the replacement."""
    base = ws.build()
    p = ws.root / "rtl/pkg.sv"
    p.write_text(ws.text("rtl/pkg.sv").replace("task t();", "task t(); // shifted\n   ", 1))
    cand = ws.build()
    t = ws.decl(base.build_id or "", "p::c::t")
    target = {"qualified_name": "p::c::t", "kind": "task", "file": "rtl/pkg.sv", "constructor": False,
              "start_byte": t["full_start"], "end_byte": t["full_end"],
              "new_start": t["full_start"], "new_end": t["full_end"]}
    with ws.db() as db:
        problems = P.slang_scope_escape(db, base.build_id or "", cand.build_id or "", [target])
        assert any(x.get("problem") == "span_mismatch" for x in problems)
        assert P.slang_scope_escape(db, base.build_id or "", base.build_id or "", [target]) == []


def test_r1_contained_edit_has_exact_span_after_validate(ws: EditWs) -> None:
    base = ws.build()
    v, reasons = _validate_failures(ws, base, slang("p::c::t", NEW_T))
    assert v.error is None and reasons == []


def test_m18_non_declaration_span_is_refused_with_the_specific_reason(ws: EditWs) -> None:
    """Point the stored span at bytes that are not a declaration: the refusal must be the shape
    check itself (M18), not a crash further down."""
    base = ws.build()
    d = ws.decl(base.build_id or "", "p::c::t")
    con = sqlite3.connect(ws.root / ".svweave" / "svweave.db")
    con.execute("UPDATE declarations SET full_start=?, full_end=? WHERE build_id=? "
                "AND qualified_name='p::c::t'", (d["full_start"] + 5, d["full_end"], base.build_id))
    con.commit()
    con.close()
    e = err(prep, ws, base, [slang("p::c::t", NEW_T)])
    assert e.code is ErrorCode.POLICY_DENIED and e.details["reason"] == "unexpected_declaration_shape"


def test_n2_state_changing_directive_inside_a_macro_body_is_refused(tmp_path: Path) -> None:
    """A pre-existing macro that `include`s a file doing `undef / `define must not be invoked from
    a replacement (it would change what LATER code sees); conditionals in bodies stay fine."""
    ws = make_ws(tmp_path)
    ws.w.write("rtl/defs.svh",
               "`define BUMP 1\n`define MKF function int hh(); return 0; endfunction\n"
               '`define INC_BUMP `include "bump.svh"\n'
               "`define OUTER `INC_BUMP\n"
               "`define COND `ifdef BUMP $display(1); `else $display(2); `endif\n"
               "`define REDEF `define BUMP 5\n"
               "`define TS `timescale 1ns/1ps\n")
    ws.w.write("rtl/bump.svh", "`undef BUMP\n`define BUMP 1000\n")
    base = ws.build()
    assert base.build_id, base.error
    sid, bid = base.snapshot_id or "", base.build_id or ""
    body = "task t();\n  %s\nendtask"
    for use in ("`INC_BUMP", "`OUTER", "`REDEF", "`TS"):  # directly and transitively
        e = err(resolve_directly, ws, bid, sid, [slang("p::c::t", body % use)])
        assert e.code is ErrorCode.POLICY_DENIED and e.details["reason"] == "replacement_directive", use
        assert "state-changing directive" in e.message, use
    resolve_directly(ws, bid, sid, [slang("p::c::t", body % "`COND")])  # conditionals are fine


def test_n2_state_directive_set_is_exactly_the_non_conditional_ones() -> None:
    assert P._STATE_DIRECTIVES == P._FORBIDDEN_DIRECTIVES - P._CONDITIONALS
    for d in (b"define", b"undef", b"undefineall", b"include", b"resetall", b"default_nettype",
              b"timescale", b"line", b"pragma", b"celldefine", b"endcelldefine",
              b"unconnected_drive", b"nounconnected_drive", b"begin_keywords", b"end_keywords"):
        assert d in P._STATE_DIRECTIVES, d
    assert not (P._CONDITIONALS & P._STATE_DIRECTIVES)
