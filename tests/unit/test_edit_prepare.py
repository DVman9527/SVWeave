"""Prepare (plan 12.2, 12.4): EDT-03, EDT-10, SEC-01, candidate isolation, idempotency, routing."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from svweave.adapters.serena.guard import tree_hashes
from svweave.application import edit as E
from svweave.domain.errors import ErrorCode, SvwError
from svweave.domain.states import TransactionState as S
from tests.unit._edit_fx import EditWs, fake_factory, make_ws, prepared, sha, spans_for

NEW_G = "function int g(int x);\n      return x * 3;\n    endfunction"


@pytest.fixture
def ws(tmp_path: Path) -> EditWs:
    return make_ws(tmp_path)


def err(fn, *a, **kw) -> SvwError:  # type: ignore[no-untyped-def]
    with pytest.raises(SvwError) as ei:
        fn(*a, **kw)
    return ei.value


# ---------------------------------------------------------------------------- happy path
def test_prepare_creates_independent_byte_copy_and_never_touches_the_original(ws: EditWs) -> None:
    base = ws.build()
    before = tree_hashes(ws.root / "rtl")
    snap_files = ws.root / ".svweave" / "snapshots" / base.snapshot_id / "files"
    e = prepared(ws, base, [ws.sym_edit(base.build_id, "p::c::g", NEW_G)])
    tid = e.data["transaction_id"]
    assert e.data["state"] == "CANDIDATE_READY" and e.data["edit_paths"] == ["serena_symbol"]
    assert tree_hashes(ws.root / "rtl") == before, "original must be untouched"
    cand = ws.root / ".svweave" / "candidates" / tid
    assert "x * 3" in (cand / "rtl" / "pkg.sv").read_text()
    # real byte copies: separate inodes, no extra links, no symlinks anywhere
    for p in cand.rglob("*"):
        assert not p.is_symlink()
        if p.is_file():
            assert os.stat(p).st_nlink == 1
            if p.name != "verible.filelist" and (snap_files / p.relative_to(cand)).exists():
                assert os.stat(p).st_ino != os.stat(snap_files / p.relative_to(cand)).st_ino
    assert os.stat(cand / "rtl" / "top.sv").st_ino != os.stat(ws.root / "rtl" / "top.sv").st_ino
    # untouched sibling function / class in the candidate
    c_txt = (cand / "rtl" / "pkg.sv").read_text()
    assert c_txt.count("x * 3") == 1 and "return x - 1;" in c_txt


def test_edit_path_is_recorded_per_edit_and_serena_edit_is_labelled(ws: EditWs) -> None:
    base = ws.build()
    e = prepared(ws, base, [ws.sym_edit(base.build_id, "p::c::g", NEW_G)])
    [rec] = e.data["edits"]
    assert rec["edit_path"] == "serena_symbol" and rec["counted_as_serena_semantic_edit"] is True
    assert rec["serena"]["name_path"] == "p/c/g"


def test_task_and_constructor_use_exact_range_even_with_serena(ws: EditWs) -> None:
    base = ws.build()
    t_body = 'task t();\n      $display("task edited");\n    endtask'
    ctor = "function new();\n      $display(\"x\");\n    endfunction"
    # the fake Serena knows no task / constructor symbol (like Verible, ADR-012)
    factory, made = fake_factory(ws, spans_for(ws, base.build_id, "p::c::g"))
    req = ws.req(base, [ws.sym_edit(base.build_id, "p::c::t", t_body, "task"),
                        ws.sym_edit(base.build_id, "p::c::new", ctor, "function")])
    e = E.prepare(ws.opts, "r", req, serena_factory=factory)
    recs = {r["qualified_name"]: r for r in e.data["edits"]}
    assert recs["p::c::t"]["edit_path"] == "exact_range"
    assert recs["p::c::new"]["edit_path"] == "exact_range"
    assert not any(r["counted_as_serena_semantic_edit"] for r in recs.values())
    assert "task edited" in ws.root.joinpath(".svweave/candidates", e.data["transaction_id"],
                                             "rtl/pkg.sv").read_text()
    assert made, "Serena was consulted for the function declaration"


def test_function_without_serena_capability_is_labelled_exact_range(ws: EditWs) -> None:
    base = ws.build()
    e = prepared(ws, base, [ws.sym_edit(base.build_id, "p::c::g", NEW_G)], serena=False)
    [rec] = e.data["edits"]
    assert rec["edit_path"] == "exact_range" and "unavailable" in rec["reason"]
    assert any("Serena capability unavailable" in w for w in e.warnings)


def test_exact_range_edit_and_other_file_untouched(ws: EditWs) -> None:
    base = ws.build()
    e = prepared(ws, base, [ws.range_edit("rtl/pkg.sv", '"task hello"', '"task hello 2"')],
                 serena=False)
    cand = ws.root / ".svweave/candidates" / e.data["transaction_id"]
    assert '"task hello 2"' in (cand / "rtl/pkg.sv").read_text()
    assert (cand / "rtl/other.sv").read_bytes() == ws.read("rtl/other.sv")


# ---------------------------------------------------------------------------- EDT-03
def test_edt03_bare_name_is_never_accepted_and_candidates_are_scoped(ws: EditWs) -> None:
    base = ws.build()
    for ident in ("f", "p::c::f"):  # two classes both have a function f; qualified name != id
        e = err(E.prepare, ws.opts, "r", ws.req(base, [
            {"kind": "symbol_replacement", "declaration_id": ident, "new_body": "x"}]),
            use_serena=False)
        assert e.code is ErrorCode.AMBIGUOUS_SYMBOL
        cands = e.details["candidates"]
        assert cands and all("declaration_id" in c and "qualified_name" in c for c in cands)
    e = err(E.prepare, ws.opts, "r", ws.req(base, [
        {"kind": "symbol_replacement", "declaration_id": "f", "new_body": "x"}]), use_serena=False)
    assert {c["qualified_name"] for c in e.details["candidates"]} == {"p::c::f", "p::d::f"}
    assert ws.txn_rows() == [], "a request refused at planning time leaves no transaction behind"


def test_edt03_unknown_declaration_id(ws: EditWs) -> None:
    base = ws.build()
    e = err(E.prepare, ws.opts, "r", ws.req(base, [
        {"kind": "symbol_replacement", "declaration_id": "0" * 64, "new_body": "x"}]),
        use_serena=False)
    assert e.code is ErrorCode.MISSING_INPUT


def test_edt03_macro_expansion_location_is_refused(ws: EditWs) -> None:
    base = ws.build()
    # symbol replacement on a declaration that only exists through a macro body
    e = err(E.prepare, ws.opts, "r", ws.req(base, [
        ws.sym_edit(base.build_id, "p::e::hh", "function int hh(); return 1; endfunction")]),
        use_serena=False)
    assert e.code is ErrorCode.POLICY_DENIED and e.details["reason"] == "macro_expansion"
    # exact range over the macro body bytes in the header
    data = ws.read("rtl/defs.svh")
    old = b"return 0;"
    pos = data.index(old)
    e2 = err(E.prepare, ws.opts, "r", ws.req(base, [
        {"kind": "exact_range", "logical_path": "rtl/defs.svh", "start_byte": pos,
         "end_byte": pos + len(old), "expected_sha256": sha(old), "replacement": "return 1;"}]),
        use_serena=False)
    assert e2.code is ErrorCode.POLICY_DENIED
    assert e2.details["reason"] == "macro_definition_edit_unsupported"
    assert ws.read("rtl/defs.svh") == data


def test_serena_symbol_that_is_not_the_chosen_declaration_is_ambiguous(ws: EditWs) -> None:
    base = ws.build()
    wrong = {"p/c/g": tuple(ws.decl(base.build_id, "p::d::f")[k] for k in ("full_start", "full_end")),
             "c/g": tuple(ws.decl(base.build_id, "p::d::f")[k] for k in ("full_start", "full_end"))}
    factory, _ = fake_factory(ws, wrong)  # type: ignore[arg-type]
    e = err(E.prepare, ws.opts, "r", ws.req(base, [ws.sym_edit(base.build_id, "p::c::g", NEW_G)]),
            serena_factory=factory)
    assert e.code is ErrorCode.AMBIGUOUS_SYMBOL and e.details["reason"] == "serena_span_mismatch"


# ---------------------------------------------------------------------------- stale / partial base
def test_edt02_prepare_refuses_a_stale_base_after_source_or_header_edit(ws: EditWs) -> None:
    base = ws.build()
    edit = ws.range_edit("rtl/pkg.sv", '"task hello"', '"x"')
    (ws.root / "rtl" / "other.sv").write_text("// human edit\nmodule other; endmodule\n")
    e = err(E.prepare, ws.opts, "r", ws.req(base, [edit]), use_serena=False)
    assert e.code is ErrorCode.STALE_SNAPSHOT
    assert "rtl/other.sv" in str(e.details["changed"])
    (ws.root / "rtl" / "other.sv").write_text(ws.read("rtl/other.sv").decode().replace("// human edit\n", "// other file, must stay untouched\n"))
    # a dependency header edited
    (ws.root / "rtl" / "defs.svh").write_text(ws.text("rtl/defs.svh") + "// touched\n")
    e2 = err(E.prepare, ws.opts, "r", ws.req(base, [edit]), use_serena=False)
    assert e2.code is ErrorCode.STALE_SNAPSHOT


def test_new_dependency_file_makes_the_base_stale(ws: EditWs) -> None:
    base = ws.build()
    (ws.root / "rtl" / "added.svh").write_text("// new candidate header\n")  # lookup search space
    e = err(E.prepare, ws.opts, "r", ws.req(base, [ws.range_edit("rtl/pkg.sv", '"task hello"', '"x"')]),
            use_serena=False)
    assert e.code is ErrorCode.STALE_SNAPSHOT


def test_partial_base_build_is_refused(tmp_path: Path) -> None:
    from tests.unit._edit_fx import BAD_TOP

    bad = make_ws(tmp_path, top=BAD_TOP)
    base = bad.build()
    assert base.analysis_coverage.completeness.value == "PARTIAL"  # type: ignore[union-attr]
    e = err(E.prepare, bad.opts, "r", bad.req(base, [bad.range_edit("rtl/pkg.sv", '"task hello"', '"x"')]),
            use_serena=False)
    assert e.code is ErrorCode.PARTIAL_ANALYSIS


def test_snapshot_must_belong_to_the_build(ws: EditWs) -> None:
    base = ws.build()
    req = ws.req(base, [ws.range_edit("rtl/pkg.sv", '"task hello"', '"x"')])
    req["snapshot_id"] = "e" * 64
    assert err(E.prepare, ws.opts, "r", req, use_serena=False).code is ErrorCode.STALE_SNAPSHOT


# ---------------------------------------------------------------------------- policy / EDT-10 / SEC-01
def test_editing_is_off_by_default(tmp_path: Path) -> None:
    off = make_ws(tmp_path, enabled=False)
    base = off.build()
    e = err(E.prepare, off.opts, "r", off.req(base, [off.range_edit("rtl/pkg.sv", '"task hello"', '"x"')]),
            use_serena=False)
    assert e.code is ErrorCode.POLICY_DENIED and e.details["reason"] == "editing_disabled"
    assert off.txn_rows() == []


def test_path_outside_allowed_paths_is_denied(tmp_path: Path) -> None:
    ws2 = make_ws(tmp_path, editing={"allowed_paths": ["rtl/pkg.sv"]})
    base = ws2.build()
    e = err(E.prepare, ws2.opts, "r", ws2.req(base, [ws2.range_edit("rtl/other.sv", "module other", "module oth")]),
            use_serena=False)
    assert e.code is ErrorCode.POLICY_DENIED and e.details["reason"] == "path_not_allowed"


def test_non_writable_source_root_is_denied(ws: EditWs) -> None:
    base = ws.build()
    e = err(E.prepare, ws.opts, "r", ws.req(base, [ws.range_edit("ro/lib.sv", "module lib", "module lib2")]),
            use_serena=False)
    assert e.code is ErrorCode.POLICY_DENIED and e.details["reason"] == "root_not_writable"


@pytest.mark.parametrize("path", ["../outside.sv", "/etc/passwd", "rtl/../../x.sv", "rtl/new_file.sv",
                                  "@uvm/uvm_pkg.sv", "rtl/", ""])
def test_edt10_sec01_unknown_new_or_escaping_paths_are_refused(ws: EditWs, path: str) -> None:
    base = ws.build()
    edit = {"kind": "exact_range", "logical_path": path or "x", "start_byte": 0, "end_byte": 0,
            "expected_sha256": sha(b""), "replacement": "module n; endmodule\n"}
    e = err(E.prepare, ws.opts, "r", ws.req(base, [edit]), use_serena=False)
    assert e.code in (ErrorCode.POLICY_DENIED, ErrorCode.PATH_NOT_ALLOWED)
    assert not (ws.root / "rtl" / "new_file.sv").exists()
    assert not Path("/etc/passwd.svw").exists()


def test_sec01_symlink_target_is_refused(ws: EditWs) -> None:
    base = ws.build()
    edit = ws.range_edit("rtl/other.sv", "module other", "module oth")
    data = ws.read("rtl/other.sv")
    (ws.root / "rtl" / "real.sv").write_bytes(data)
    (ws.root / "rtl" / "other.sv").unlink()
    os.symlink("real.sv", ws.root / "rtl" / "other.sv")  # identical bytes: snapshot still CURRENT
    e = err(E.prepare, ws.opts, "r", ws.req(base, [edit]), use_serena=False)
    assert e.code in (ErrorCode.POLICY_DENIED, ErrorCode.PATH_NOT_ALLOWED, ErrorCode.STALE_SNAPSHOT)
    assert (ws.root / "rtl" / "real.sv").read_bytes() == data


def test_sec01_hardlinked_target_is_refused(ws: EditWs, tmp_path: Path) -> None:
    base = ws.build()
    edit = ws.range_edit("rtl/other.sv", "module other", "module oth")
    os.link(ws.root / "rtl" / "other.sv", tmp_path / "alias.sv")
    e = err(E.prepare, ws.opts, "r", ws.req(base, [edit]), use_serena=False)
    assert e.code is ErrorCode.POLICY_DENIED and e.details["reason"] == "hardlinked"
    assert (tmp_path / "alias.sv").read_bytes() == ws.read("rtl/other.sv")


def test_binary_and_wrong_hash_and_bounds(ws: EditWs) -> None:
    base = ws.build()
    bad_hash = ws.range_edit("rtl/pkg.sv", '"task hello"', '"x"')
    bad_hash["expected_sha256"] = "0" * 64
    assert err(E.prepare, ws.opts, "r", ws.req(base, [bad_hash]), use_serena=False).code is ErrorCode.EDIT_CONFLICT
    oob = ws.range_edit("rtl/pkg.sv", '"task hello"', '"x"')
    oob["end_byte"] = 10**9
    assert err(E.prepare, ws.opts, "r", ws.req(base, [oob]), use_serena=False).code is ErrorCode.EDIT_CONFLICT
    nul = ws.range_edit("rtl/pkg.sv", '"task hello"', "a\x00b")
    assert err(E.prepare, ws.opts, "r", ws.req(base, [nul]), use_serena=False).code is ErrorCode.POLICY_DENIED


def test_overlapping_and_mixed_edits_are_refused(ws: EditWs) -> None:
    base = ws.build()
    a = ws.range_edit("rtl/pkg.sv", '"task hello"', '"x"')
    b = ws.range_edit("rtl/pkg.sv", 'hello"', 'bye"')
    assert err(E.prepare, ws.opts, "r", ws.req(base, [a, b]), use_serena=False).details["reason"] == "overlapping_edits"
    mixed = [ws.sym_edit(base.build_id, "p::c::g", NEW_G), ws.range_edit("rtl/pkg.sv", '"task hello"', '"x"')]
    factory, _ = fake_factory(ws, spans_for(ws, base.build_id, "p::c::g"))
    e = err(E.prepare, ws.opts, "r", ws.req(base, mixed), serena_factory=factory)
    assert e.details["reason"] == "mixed_edit_paths_same_file"
    for r in ws.txn_rows():
        assert r["state"] == "ABORTED"
    assert list((ws.root / ".svweave" / "candidates").iterdir()) == []


# ---------------------------------------------------------------------------- EDT-06 at prepare
def test_edt06_serena_touching_an_extra_file_rejects_the_candidate(ws: EditWs) -> None:
    base = ws.build()
    before = tree_hashes(ws.root / "rtl")
    e = err(prepared, ws, base, [ws.sym_edit(base.build_id, "p::c::g", NEW_G)], mode="write_other")
    assert e.code is ErrorCode.POLICY_DENIED
    assert tree_hashes(ws.root / "rtl") == before
    assert ws.txn_rows()[0]["state"] == "ABORTED"


# ---------------------------------------------------------------------------- idempotency
def test_prepare_idempotency_same_payload_same_txn_different_payload_conflict(ws: EditWs) -> None:
    base = ws.build()
    edit = ws.range_edit("rtl/pkg.sv", '"task hello"', '"x"')
    e1 = prepared(ws, base, [edit], serena=False, key="idem-key-0001")
    e2 = prepared(ws, base, [edit], serena=False, key="idem-key-0001")
    assert e1.data["transaction_id"] == e2.data["transaction_id"] and e2.data["idempotent_replay"]
    assert len(ws.txn_rows()) == 1
    other = ws.range_edit("rtl/pkg.sv", '"task hello"', '"y"')
    c = err(prepared, ws, base, [other], serena=False, key="idem-key-0001")
    assert c.code is ErrorCode.IDEMPOTENCY_CONFLICT
    assert len(ws.txn_rows()) == 1
    e3 = prepared(ws, base, [other], serena=False, key="idem-key-0002")  # a new key is a new txn
    assert e3.data["transaction_id"] != e1.data["transaction_id"]


def test_invalid_request_is_rejected_before_anything_happens(ws: EditWs) -> None:
    base = ws.build()
    req = ws.req(base, [ws.range_edit("rtl/pkg.sv", '"task hello"', '"x"')])
    req["approved"] = True
    e = err(E.prepare, ws.opts, "r", req, use_serena=False)
    assert e.code is ErrorCode.INVALID_CONFIG
    assert ws.txn_rows() == []
    assert S.PREPARED.value  # states exist; nothing persisted for a rejected request


def test_serena_span_wider_than_the_declaration_keeps_the_extra_qualifier_bytes(ws: EditWs) -> None:
    """Serena's symbol may start at a qualifier (``virtual``) that slang's span excludes: the
    replacement must not swallow it."""
    base = ws.build()
    d = ws.decl(base.build_id, "p::c::g")
    wide = {k: (d["full_start"] - 4, d["full_end"] + 1) for k in ("p/c/g", "c/g")}
    factory, _ = fake_factory(ws, wide)
    before = ws.read("rtl/pkg.sv")
    e = E.prepare(ws.opts, "r", ws.req(base, [ws.sym_edit(base.build_id, "p::c::g", NEW_G)]),
                  serena_factory=factory)
    cand = (ws.root / ".svweave/candidates" / e.data["transaction_id"] / "rtl/pkg.sv").read_bytes()
    pre, post = before[d["full_start"] - 4:d["full_start"]], before[d["full_end"]:d["full_end"] + 1]
    assert cand == before[:d["full_start"]] + NEW_G.encode() + before[d["full_end"]:]
    assert pre == b"    " and post == b"\n"
