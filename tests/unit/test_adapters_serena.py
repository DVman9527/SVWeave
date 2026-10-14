"""Unit tests: Serena adapter planning/edit-routing/guards with scripted fakes, byte-offset
conversion, candidate guard, exact-range patch and config generation."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from typing import Any

import pytest

from svweave.adapters.serena import SerenaAdapter, apply_exact_range
from svweave.adapters.serena import config as scfg
from svweave.adapters.serena import offsets as off
from svweave.adapters.serena.guard import (
    CandidateGuard,
    EditOutcome,
    changed_region,
    diff_hashes,
    safe_candidate_file,
    tree_hashes,
)
from svweave.domain.errors import ErrorCode, SvwError
from svweave.orchestration.capability import (
    SERENA_CORE_FEATURES,
    SERENA_FEATURES,
    SERENA_FORBIDDEN,
    compare_capabilities,
    derive_serena_edit_subset,
)
from svweave.security.sandbox import HostCapability, SandboxRunner
from tests.unit._fakes import FakeSession, jtext, toolchain, tools_from_report

UVM = "uvm/pkg.sv"
SRC = (
    "package p;\n"
    "  class c1;\n"
    "    virtual function void build_phase(int x);\n"
    '      $display("drv");\n'
    "    endfunction\n"
    "    virtual task run_phase(int x);\n"
    '      $display("task");\n'
    "    endtask\n"
    "  endclass\n"
    "  class c2;\n"
    "    virtual function void build_phase(int x);\n"
    '      $display("other");\n'
    "    endfunction\n"
    "  endclass\n"
    "endpackage\n"
)
FN_BODY = 'virtual function void build_phase(int x);\n      $display("drv");\n    endfunction'


def lines_of(text: str, needle: str) -> tuple[int, int]:
    a = text.index(needle)
    return text.count("\n", 0, a), text.count("\n", 0, a + len(needle))


def sym(name_path: str, kind: str, body: str | None = None, rel: str = UVM, lines: tuple[int, int] | None = None) -> dict[str, Any]:
    s, e = lines or (lines_of(SRC, body) if body else (0, 0))
    d: dict[str, Any] = {"name_path": name_path, "kind": kind, "relative_path": rel,
                         "body_location": {"start_line": s, "end_line": e}}
    if body is not None:
        d["body"] = body
    return d


@pytest.fixture
def cand(tmp_path: Path) -> Path:
    c = tmp_path / "cand"
    (c / "uvm").mkdir(parents=True)
    (c / UVM).write_text(SRC)
    (c / "other.sv").write_text("module o; endmodule\n")
    return c


def make(tmp_path: Path, cand: Path, *, bwrap: bool = True) -> tuple[SerenaAdapter, FakeSession]:
    tc = toolchain()
    cap = HostCapability(True, "/usr/bin/bwrap", "x", None) if bwrap else HostCapability(False, None, None, "no userns")
    ad = SerenaAdapter(tc, tmp_path / "state", cand, SandboxRunner(allow_degraded=True, capability=cap))
    fake = FakeSession(tools_from_report(tc, "serena"))
    ad._session = fake
    ad.comparison = compare_capabilities("serena", fake.tools, tc.pinned_tool_hashes("serena"),
                                         SERENA_FEATURES, SERENA_CORE_FEATURES, forbidden=SERENA_FORBIDDEN)
    from svweave.security.sandbox import IsolationMode
    ad.isolation = IsolationMode.BWRAP if bwrap else IsolationMode.DEGRADED_NO_ISOLATION
    ad.guard.baseline()
    return ad, fake


def script_find(fake: FakeSession, results: dict[str, list[dict[str, Any]]]) -> None:
    fake.handlers["find_symbol"] = lambda a: jtext(
        [r for r in results.get(a["name_path_pattern"], []) if a.get("include_body") or "body" not in r]
        if not a.get("include_body") else results.get(a["name_path_pattern"], []))


FUNCS = {
    "c1/build_phase": [sym("p/c1/build_phase", "Function", FN_BODY)],
    "c1/run_phase": [],
    "build_phase": [sym("p/c1/build_phase", "Function", FN_BODY),
                    sym("p/c2/build_phase", "Function", None, lines=(10, 12))],
    "c1": [sym("p/c1", "Class", None, lines=(1, 8))],
    "p": [sym("p", "File", None, lines=(0, 14)), sym("p", "Package", None, lines=(0, 14))],
}


# ------------------------------------------------------------------------------- offsets
def test_lsp_utf16_columns_vs_utf8_bytes_with_cjk_and_astral() -> None:
    line = "ab中😀cd\n".encode()  # a b 中(3B,1u) 😀(4B,2u) c d
    assert off.lsp_position_to_byte(line, 0, 0) == 0
    assert off.lsp_position_to_byte(line, 0, 2) == 2          # before 中
    assert off.lsp_position_to_byte(line, 0, 3) == 5          # after 中 (1 unit, 3 bytes)
    assert off.lsp_position_to_byte(line, 0, 5) == 9          # after 😀 (2 units, 4 bytes)
    assert off.lsp_position_to_byte(line, 0, 6) == 10
    assert off.lsp_position_to_byte(line, 0, 99) == len(line) - 1  # clamp to end of content
    assert off.lsp_position_to_byte(line, 0, 3, "utf-8") == 3      # utf-8 character == byte count
    assert off.lsp_position_to_byte(line, 0, 4, "utf-32") == 9


def test_line_terminators_lf_crlf_cr() -> None:
    data = b"one\r\ntwo\rthree\nfour"
    assert off.line_starts(data) == [0, 5, 9, 15]
    assert off.lsp_position_to_byte(data, 1, 2) == 7
    assert off.lsp_position_to_byte(data, 3, 4) == len(data)
    assert off.line_end(data, off.line_starts(data), 0) == 3
    with pytest.raises(ValueError):
        off.lsp_position_to_byte(data, 4, 0)
    with pytest.raises(ValueError):
        off.lsp_position_to_byte(data, -1, 0)


def test_locate_body_exact_unique_and_absent() -> None:
    data = SRC.encode()
    s, e = lines_of(SRC, FN_BODY)
    span = off.locate_body(data, s, e, FN_BODY)
    assert span is not None and data[span[0]:span[1]].decode() == FN_BODY
    assert off.locate_body(data, 0, 14, FN_BODY) == span
    assert off.locate_body(data, s, e, "not there") is None
    twice = b"x\nfoo\nfoo\n"
    assert off.locate_body(twice, 0, 2, "foo") is None  # not unique -> no guess
    assert off.locate_body(data, s, e, "") is None
    with pytest.raises(ValueError):
        off.line_window(data, 5, 2)


def test_locate_body_with_multibyte_text_gives_byte_offsets() -> None:
    text = "// 注釋 😀\nfunction f;\n  x = 1;\nendfunction\n"
    data = text.encode()
    body = "function f;\n  x = 1;\nendfunction"
    span = off.locate_body(data, 1, 3, body)
    assert span is not None and data[span[0]:span[1]] == body.encode()
    assert span[0] == len("// 注釋 😀\n".encode()) != len("// 注釋 😀\n")


# ------------------------------------------------------------------------------- planning
def test_plan_function_is_serena_and_task_ctor_class_are_not(tmp_path: Path, cand: Path) -> None:
    ad, fake = make(tmp_path, cand)
    script_find(fake, FUNCS)
    p = ad.plan_symbol_edit(cand, "c1/build_phase", UVM)
    assert p.edit_path == "serena_symbol" and p.symbol is not None and p.symbol.span_precision == "exact"
    t = ad.plan_symbol_edit(cand, "c1/run_phase", UVM)
    assert t.edit_path == "exact_range" and t.symbol is None and "task" in t.reason
    k = ad.plan_symbol_edit(cand, "c1", UVM)
    assert k.edit_path == "exact_range" and "Class" in k.reason


def test_plan_refuses_ambiguous_and_lists_candidates(tmp_path: Path, cand: Path) -> None:
    ad, fake = make(tmp_path, cand)
    script_find(fake, FUNCS)
    with pytest.raises(SvwError) as ei:
        ad.plan_symbol_edit(cand, "build_phase", UVM)
    assert ei.value.code is ErrorCode.AMBIGUOUS_SYMBOL
    assert [c["name_path"] for c in ei.value.details["candidates"]] == ["p/c1/build_phase", "p/c2/build_phase"]


def test_plan_ignores_file_pseudo_symbols_and_honours_absolute_pattern(tmp_path: Path, cand: Path) -> None:
    ad, fake = make(tmp_path, cand)
    script_find(fake, {**FUNCS, "/c1/build_phase": FUNCS["c1/build_phase"]})
    only_pkg = ad.plan_symbol_edit(cand, "p", UVM)
    assert only_pkg.symbol is not None and only_pkg.symbol.kind == "Package"
    # a leading '/' means "this exact name path from the top": 'p/c1/build_phase' != 'c1/build_phase'
    assert ad.plan_symbol_edit(cand, "/c1/build_phase", UVM).edit_path == "exact_range"


def test_plan_detects_stale_serena_view(tmp_path: Path, cand: Path) -> None:
    ad, fake = make(tmp_path, cand)
    stale = FN_BODY.replace('"drv"', '"old"')
    script_find(fake, {"c1/build_phase": [sym("p/c1/build_phase", "Function", stale, lines=lines_of(SRC, FN_BODY))]})
    with pytest.raises(SvwError) as ei:
        ad.plan_symbol_edit(cand, "c1/build_phase", UVM)
    assert ei.value.code is ErrorCode.EDIT_CONFLICT and ei.value.details["reason"] == "serena_stale_view"


def test_edit_subset_comes_from_probe_checks_not_assumption() -> None:
    rep = {"checks": [{"check": "find_symbol svw_env/build_phase (function method)", "pass": True},
                      {"check": "find_symbol svw_driver/run_phase (task method)", "pass": False}]}
    s = derive_serena_edit_subset(rep)
    assert s.kinds == frozenset({"Function"})
    both = derive_serena_edit_subset({"checks": [{"check": "x (function method)", "pass": True},
                                                 {"check": "y (task method)", "pass": True}]})
    assert both.kinds == frozenset({"Function", "Task"})
    assert derive_serena_edit_subset({}).kinds == frozenset()
    assert derive_serena_edit_subset(toolchain().backend_report("serena")).kinds == frozenset({"Function"})


# ------------------------------------------------------------------------------- INT-03 unit
def test_every_call_checks_the_active_candidate(tmp_path: Path, cand: Path) -> None:
    ad, fake = make(tmp_path, cand)
    other = tmp_path / "other_cand"
    other.mkdir()
    for call in (
        lambda: ad.find_symbol(other, "x"),
        lambda: ad.find_referencing_symbols(other, "x", UVM),
        lambda: ad.get_symbols_overview(other, UVM),
        lambda: ad.plan_symbol_edit(other, "x", UVM),
        lambda: ad.replace_symbol_body(other, "x", UVM, "b"),
        lambda: ad.find_symbol(tmp_path / "does-not-exist", "x"),
    ):
        with pytest.raises(SvwError) as ei:
            call()
        assert ei.value.code is ErrorCode.STALE_SNAPSHOT
        assert ei.value.details["reason"] == "active_project_mismatch"
    assert not fake.calls
    link = tmp_path / "alias"
    os.symlink(cand, link)
    script_find(fake, FUNCS)
    assert ad.find_symbol(link, "c1")  # same realpath = same project


def test_relative_paths_cannot_escape_or_follow_symlinks(tmp_path: Path, cand: Path) -> None:
    ad, fake = make(tmp_path, cand)
    os.symlink("/etc/passwd", cand / "uvm" / "evil.sv")
    os.symlink(tmp_path, cand / "linkdir")
    for bad in ("../x.sv", "/etc/passwd", "uvm/evil.sv", "linkdir/a.sv", "uvm", "", "a\x00b", "uvm//pkg.sv"):
        with pytest.raises(SvwError) as ei:
            ad.find_symbol(cand, "c1", bad)
        assert ei.value.code is ErrorCode.PATH_NOT_ALLOWED, bad
    assert not fake.calls


# ------------------------------------------------------------------------------- editing
def edit_handler(cand: Path, *, also_touch: str | None = None, widen: bool = False,
                 reply: str = "OK") -> Any:
    def h(a: dict[str, Any]) -> str:
        p = cand / a["relative_path"]
        text = p.read_text()
        new = text.replace(FN_BODY, a["body"])
        if widen:
            new = new.replace("endpackage", "endpackage // widened")
        p.write_text(new)
        if also_touch:
            (cand / also_touch).write_text("// surprise edit\n")
        return reply

    return h


def test_function_edit_ok_and_confined(tmp_path: Path, cand: Path) -> None:
    ad, fake = make(tmp_path, cand)
    script_find(fake, FUNCS)
    fake.handlers["replace_symbol_body"] = edit_handler(cand)
    out = ad.replace_symbol_body(cand, "c1/build_phase", UVM, FN_BODY.replace('"drv"', '"drv2"'),
                                 expected_body_sha256=hashlib.sha256(FN_BODY.encode()).hexdigest())
    assert out.ok and out.edit_path == "serena_symbol" and out.counted_as_serena_semantic_edit
    assert out.changed_files == (UVM,) and out.unexpected_files == ()
    out.raise_if_surprising()
    assert '"drv2"' in (cand / UVM).read_text() and '"other"' in (cand / UVM).read_text()


def test_edt06_extra_file_changed_by_backend_is_flagged(tmp_path: Path, cand: Path) -> None:
    ad, fake = make(tmp_path, cand)
    script_find(fake, FUNCS)
    fake.handlers["replace_symbol_body"] = edit_handler(cand, also_touch="other.sv")
    out = ad.replace_symbol_body(cand, "c1/build_phase", UVM, FN_BODY.replace('"drv"', '"d"'))
    assert not out.ok and out.unexpected_files == ("other.sv",)
    with pytest.raises(SvwError) as ei:
        out.raise_if_surprising()
    assert ei.value.code is ErrorCode.POLICY_DENIED
    assert ei.value.details["unexpected_files"] == ["other.sv"]


def test_edt06_new_file_created_by_backend_is_flagged(tmp_path: Path, cand: Path) -> None:
    ad, fake = make(tmp_path, cand)
    script_find(fake, FUNCS)
    fake.handlers["replace_symbol_body"] = edit_handler(cand, also_touch="brand_new.sv")
    out = ad.replace_symbol_body(cand, "c1/build_phase", UVM, FN_BODY.replace('"drv"', '"d"'))
    assert out.unexpected_files == ("brand_new.sv",)


def test_edit_outside_symbol_span_is_flagged(tmp_path: Path, cand: Path) -> None:
    ad, fake = make(tmp_path, cand)
    script_find(fake, FUNCS)
    fake.handlers["replace_symbol_body"] = edit_handler(cand, widen=True)
    out = ad.replace_symbol_body(cand, "c1/build_phase", UVM, FN_BODY.replace('"drv"', '"d"'))
    assert not out.within_target and not out.ok
    with pytest.raises(SvwError):
        out.raise_if_surprising()


def test_edit_that_changes_nothing_is_not_ok(tmp_path: Path, cand: Path) -> None:
    ad, fake = make(tmp_path, cand)
    script_find(fake, FUNCS)
    fake.handlers["replace_symbol_body"] = lambda a: "OK"
    out = ad.replace_symbol_body(cand, "c1/build_phase", UVM, FN_BODY)
    assert not out.ok and out.changed_files == ()


def test_unconfirmed_edit_and_expected_hash_mismatch(tmp_path: Path, cand: Path) -> None:
    ad, fake = make(tmp_path, cand)
    script_find(fake, FUNCS)
    fake.handlers["replace_symbol_body"] = edit_handler(cand, reply="Error: no such symbol")
    with pytest.raises(SvwError) as ei:
        ad.replace_symbol_body(cand, "c1/build_phase", UVM, FN_BODY.replace("drv", "z"))
    assert ei.value.details["reason"] == "edit_not_confirmed"
    ad2, fake2 = make(tmp_path / "b", cand)
    script_find(fake2, FUNCS)
    with pytest.raises(SvwError) as ei2:
        ad2.replace_symbol_body(cand, "c1/build_phase", UVM, "x", expected_body_sha256="0" * 64)
    assert ei2.value.code is ErrorCode.EDIT_CONFLICT and not any(n == "replace_symbol_body" for n, _ in fake2.calls)


def test_task_and_ambiguous_never_reach_serena_replace(tmp_path: Path, cand: Path) -> None:
    ad, fake = make(tmp_path, cand)
    script_find(fake, FUNCS)
    fake.handlers["replace_symbol_body"] = edit_handler(cand)
    with pytest.raises(SvwError) as ei:
        ad.replace_symbol_body(cand, "c1/run_phase", UVM, "x")
    assert ei.value.code is ErrorCode.UNSUPPORTED_OPTION and ei.value.details["edit_path"] == "exact_range"
    with pytest.raises(SvwError) as ei2:
        ad.replace_symbol_body(cand, "build_phase", UVM, "x")
    assert ei2.value.code is ErrorCode.AMBIGUOUS_SYMBOL
    assert not any(n == "replace_symbol_body" for n, _ in fake.calls)


def test_symbol_edit_refused_without_isolation(tmp_path: Path, cand: Path) -> None:
    ad, fake = make(tmp_path, cand, bwrap=False)
    script_find(fake, FUNCS)
    fake.handlers["replace_symbol_body"] = edit_handler(cand)
    with pytest.raises(SvwError) as ei:
        ad.replace_symbol_body(cand, "c1/build_phase", UVM, FN_BODY.replace("drv", "z"))
    assert ei.value.code is ErrorCode.POLICY_DENIED and ei.value.details["reason"] == "isolation_unavailable"
    assert ad.isolation.value == "degraded_no_isolation"
    ad.allow_edit_without_isolation = True
    assert ad.replace_symbol_body(cand, "c1/build_phase", UVM, FN_BODY.replace("drv", "z")).ok


def test_dep02_disabled_symbol_edit_feature(tmp_path: Path, cand: Path) -> None:
    ad, fake = make(tmp_path, cand)
    fake._tools = tools_from_report(toolchain(), "serena", tamper={"replace_symbol_body": "0" * 64})
    ad.comparison = compare_capabilities("serena", fake.tools, toolchain().pinned_tool_hashes("serena"),
                                         SERENA_FEATURES, SERENA_CORE_FEATURES, forbidden=SERENA_FORBIDDEN)
    script_find(fake, FUNCS)
    with pytest.raises(SvwError) as ei:
        ad.replace_symbol_body(cand, "c1/build_phase", UVM, "x")
    assert ei.value.code is ErrorCode.CAPABILITY_MISMATCH and ei.value.details["feature"] == "symbol_edit"
    assert ad.find_symbol(cand, "c1")  # reading still works


def test_forbidden_tools_exposed_by_backend_fail_the_comparison() -> None:
    tc = toolchain()
    live = tools_from_report(tc, "serena")
    live["execute_shell_command"] = live["find_symbol"]
    cmp = compare_capabilities("serena", live, tc.pinned_tool_hashes("serena"), SERENA_FEATURES,
                               SERENA_CORE_FEATURES, forbidden=SERENA_FORBIDDEN)
    assert not cmp.ok and cmp.forbidden_present == ("execute_shell_command",)


def test_allowlist_blocks_other_serena_tools(tmp_path: Path, cand: Path) -> None:
    ad, fake = make(tmp_path, cand)
    for t in ("execute_shell_command", "create_text_file", "activate_project", "write_memory"):
        with pytest.raises(SvwError) as ei:
            ad._call(t, {}, "find_symbol")
        assert ei.value.code is ErrorCode.POLICY_DENIED


def test_dead_serena_is_unavailable(tmp_path: Path, cand: Path) -> None:
    ad, fake = make(tmp_path, cand)
    fake.up = False
    with pytest.raises(SvwError) as ei:
        ad.find_symbol(cand, "c1")
    assert ei.value.code is ErrorCode.BACKEND_UNAVAILABLE and ei.value.details["reason"] == "process_exited"
    assert not ad.alive


def test_bad_find_symbol_output(tmp_path: Path, cand: Path) -> None:
    ad, fake = make(tmp_path, cand)
    fake.handlers["find_symbol"] = lambda a: "Error: language server crashed"
    with pytest.raises(SvwError) as ei:
        ad.find_symbol(cand, "c1")
    assert ei.value.details["reason"] == "bad_output"


def test_references_are_parsed_as_lsp_resolved(tmp_path: Path, cand: Path) -> None:
    ad, fake = make(tmp_path, cand)
    fake.handlers["find_referencing_symbols"] = lambda a: jtext({
        "other.sv": {"Namespace": [{"name_path": "o/g", "body_location": {"start_line": 3, "end_line": 9},
                                    "content_around_reference": "..  3: x"}]}})
    [r] = ad.find_referencing_symbols(cand, "c1", UVM)
    assert (r.relative_path, r.kind_group, r.start_line, r.evidence_kind) == ("other.sv", "Namespace", 3, "LSP_RESOLVED")


# ------------------------------------------------------------------------------- guard / exact range
def test_changed_region_minimal_span() -> None:
    assert changed_region(b"abcdef", b"abcdef") is None
    assert changed_region(b"abcdef", b"abXYef") == ((2, 4), (2, 4))
    assert changed_region(b"abcdef", b"abef") == ((2, 4), (2, 2))
    assert changed_region(b"abef", b"abcdef") == ((2, 2), (2, 4))
    assert changed_region(b"", b"x") == ((0, 0), (0, 1))


def test_tree_hash_diff_and_guard(cand: Path) -> None:
    g = CandidateGuard(cand)
    g.baseline()
    assert not g.changed()
    (cand / "other.sv").write_text("x")
    (cand / "n.sv").write_text("n")
    (cand / "uvm" / "pkg.sv").unlink()
    ch = g.changed()
    assert ch.modified == ("other.sv",) and ch.added == ("n.sv",) and ch.removed == ("uvm/pkg.sv",)
    assert ch.files == ("n.sv", "other.sv", "uvm/pkg.sv") and bool(ch)
    assert diff_hashes({"a": "1"}, {"a": "1"}).files == ()
    os.symlink("other.sv", cand / "ln")
    assert tree_hashes(cand)["ln"] == "symlink:other.sv"
    os.symlink("uvm", cand / "lnd")
    assert tree_hashes(cand)["lnd"] == "symlink:uvm"


def test_exact_range_patch_ok_conflict_and_safety(cand: Path) -> None:
    data = (cand / UVM).read_bytes()
    old = b'$display("task");'
    a = data.index(old)
    h = hashlib.sha256(old).hexdigest()
    os.chmod(cand / UVM, 0o640)
    out = apply_exact_range(cand, UVM, a, a + len(old), h, b'$display("task2");')
    assert out.edit_path == "exact_range" and not out.counted_as_serena_semantic_edit and out.ok
    assert out.old_span == (a, a + len(old)) and out.new_span == (a, a + len(old) + 1)
    assert (cand / UVM).read_bytes() == data[:a] + b'$display("task2");' + data[a + len(old):]
    assert (cand / UVM).stat().st_mode & 0o777 == 0o640
    assert not list(cand.rglob("*.svw-*"))
    before = (cand / UVM).read_bytes()
    for args, reason in (((0, 5, "0" * 64, b"x"), "expected_bytes_mismatch"),
                         ((5, 3, h, b"x"), "range_out_of_bounds"),
                         ((0, 10**9, h, b"x"), "range_out_of_bounds")):
        with pytest.raises(SvwError) as ei:
            apply_exact_range(cand, UVM, *args)
        assert ei.value.code is ErrorCode.EDIT_CONFLICT and ei.value.details["reason"] == reason
    assert (cand / UVM).read_bytes() == before
    os.symlink(cand / "other.sv", cand / "uvm" / "ln.sv")
    for bad in ("../x", "uvm/ln.sv", "uvm", "/abs"):
        with pytest.raises(SvwError) as ei2:
            apply_exact_range(cand, bad, 0, 0, hashlib.sha256(b"").hexdigest(), b"x")
        assert ei2.value.code is ErrorCode.PATH_NOT_ALLOWED
    assert safe_candidate_file(cand, UVM).name == "pkg.sv"


def test_edit_outcome_ok_requires_change_and_confinement() -> None:
    base: dict[str, Any] = {"edit_path": "exact_range", "relative_path": "a", "old_span": (0, 1), "new_span": (0, 1),
                            "counted_as_serena_semantic_edit": False}
    assert EditOutcome(changed_files=("a",), unexpected_files=(), within_target=True, **base).ok
    assert not EditOutcome(changed_files=(), unexpected_files=(), within_target=True, **base).ok
    assert not EditOutcome(changed_files=("a", "b"), unexpected_files=("b",), within_target=True, **base).ok


# ------------------------------------------------------------------------------- config
def test_candidate_with_serena_dir_or_missing_is_rejected(tmp_path: Path) -> None:
    c = tmp_path / "c"
    c.mkdir()
    assert scfg.check_candidate(c) == c.resolve()
    (c / ".serena").mkdir()
    with pytest.raises(SvwError) as ei:
        scfg.check_candidate(c)
    assert ei.value.code is ErrorCode.POLICY_DENIED and ei.value.details["reason"] == "candidate_has_serena_dir"
    d = tmp_path / "d"
    d.mkdir()
    os.symlink("/nonexistent", d / ".serena")
    with pytest.raises(SvwError):
        scfg.check_candidate(d)
    with pytest.raises(SvwError) as ei2:
        scfg.check_candidate(tmp_path / "missing")
    assert ei2.value.code is ErrorCode.MISSING_INPUT


def test_serena_config_trusts_exactly_the_candidate_and_disables_ui(tmp_path: Path) -> None:
    import yaml

    cand = tmp_path / 'weird "name" \\ dir'
    cfgd = yaml.safe_load(scfg.serena_config_yml(tmp_path / "proj", cand))
    assert cfgd["trusted_project_path_patterns"] == [str(cand)]
    assert cfgd["web_dashboard"] is False and cfgd["web_dashboard_open_on_launch"] is False
    assert cfgd["gui_log_window"] is False and cfgd["record_tool_usage"] is False
    assert cfgd["project_serena_folder_location"] == str(tmp_path / "proj") + "/$projectFolderName/.serena"
    assert {"execute_shell_command", "create_text_file", "activate_project"} <= set(cfgd["excluded_tools"])
    ctx = yaml.safe_load(scfg.CONTEXT_YML)
    assert set(ctx["fixed_tools"]) == {"get_symbols_overview", "find_symbol", "find_referencing_symbols",
                                       "find_declaration", "replace_symbol_body", "get_diagnostics_for_file"}
    assert ctx["single_project"] is True
    proj = yaml.safe_load(scfg.PROJECT_YML)
    assert proj["language_servers"] == ["systemverilog"] and proj["activation_command"] is None


def test_prepare_writes_only_into_state_never_into_candidate(tmp_path: Path, cand: Path) -> None:
    before = tree_hashes(cand)
    paths = scfg.SerenaPaths(tmp_path / "st" / scfg.candidate_id(cand))
    paths.prepare(cand)
    assert (paths.serena_home / "serena_config.yml").is_file() and paths.context_file.is_file()
    assert (paths.projects_dir / cand.name / ".serena" / "project.yml").is_file()
    assert tree_hashes(cand) == before
    env = paths.env(Path("/v/bin"), Path("/venv"))
    assert env["PATH"].startswith("/v/bin:/venv/bin:") and env["HOME"] == str(paths.home)
    assert set(env) <= {"PATH", "HOME", "SERENA_HOME", "XDG_CONFIG_HOME", "XDG_CACHE_HOME", "XDG_DATA_HOME",
                        "LANG", "PYTHONNOUSERSITE", "UV_NO_SYNC"}


def test_verible_filelist_lists_sources_sorted(cand: Path) -> None:
    (cand / "a.svh").write_text("")
    (cand / "readme.txt").write_text("")
    files = scfg.write_verible_filelist(cand)
    assert files == ["a.svh", "other.sv", "uvm/pkg.sv"]
    assert (cand / "verible.filelist").read_text() == "a.svh\nother.sv\nuvm/pkg.sv\n"
    assert "verible.filelist" in scfg.GENERATED_FILES


def test_python_prefix_to_bind(tmp_path: Path) -> None:
    v = tmp_path / "venv" / "bin"
    v.mkdir(parents=True)
    os.symlink("/usr/local/bin/python3.12", v / "python")
    assert scfg.python_prefix_to_bind(tmp_path / "venv") == Path("/usr/local")
    (tmp_path / "v2" / "bin").mkdir(parents=True)
    os.symlink("/usr/bin/python3", tmp_path / "v2" / "bin" / "python")
    real = os.path.realpath("/usr/bin/python3")
    expect = None if real.startswith(("/usr/bin/", "/usr/lib/", "/usr/lib64/")) else Path(real).parents[1]
    assert scfg.python_prefix_to_bind(tmp_path / "v2") == expect


def test_candidate_id_is_stable_per_realpath(tmp_path: Path, cand: Path) -> None:
    link = tmp_path / "ln"
    os.symlink(cand, link)
    assert scfg.candidate_id(link) == scfg.candidate_id(cand) != scfg.candidate_id(tmp_path)
    json.dumps(scfg.candidate_id(cand))
