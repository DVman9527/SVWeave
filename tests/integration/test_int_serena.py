"""INT-02, INT-03, INT-04 (Serena side), EDT-06 hook, DEP-02 (live) against real Serena + Verible."""

from __future__ import annotations

import hashlib
import os
import signal
import time
from collections.abc import Iterator
from pathlib import Path

import pytest

from svweave.adapters.serena import SerenaAdapter, apply_exact_range
from svweave.adapters.serena.guard import tree_hashes
from svweave.domain.errors import ErrorCode, SvwError
from svweave.orchestration.capability import (
    SERENA_CORE_FEATURES,
    SERENA_FEATURES,
    compare_capabilities,
)
from svweave.orchestration.toolchain import Toolchain
from svweave.security.sandbox import IsolationMode, SandboxRunner
from tests.integration.conftest import REPO, copy_fixture_dirs, skip_or_fail

pytestmark = pytest.mark.integration

UVM = "uvm/svw_demo_uvm_pkg.sv"


@pytest.fixture(scope="module")
def candidate(tmp_path_factory: pytest.TempPathFactory) -> Path:
    return copy_fixture_dirs(tmp_path_factory.mktemp("cand") / "candidate")


@pytest.fixture(scope="module")
def serena(
    toolchain: Toolchain,
    sandbox: SandboxRunner,
    candidate: Path,
    tmp_path_factory: pytest.TempPathFactory,
) -> Iterator[SerenaAdapter]:
    if not toolchain.serena_exe.is_file():
        skip_or_fail("serena venv not installed")
    ad = SerenaAdapter(toolchain, tmp_path_factory.mktemp("serena-state"), candidate, sandbox,
                       protected_paths=(REPO / "fixtures",))
    ad.start()
    try:
        yield ad
    finally:
        ad.stop()


def test_serena_runs_isolated_with_exact_tool_surface(serena: SerenaAdapter, toolchain: Toolchain) -> None:
    assert serena.isolation is IsolationMode.BWRAP and not serena.warnings
    assert serena.comparison is not None and serena.comparison.ok, serena.comparison
    assert serena.init_info is not None
    assert serena.init_info.protocol_version == toolchain.pinned_protocol("serena")
    assert set(serena.tool_infos) == {
        "get_symbols_overview", "find_symbol", "find_referencing_symbols", "find_declaration",
        "replace_symbol_body", "get_diagnostics_for_file",
    }
    assert (serena.candidate / "verible.filelist").is_file()
    assert not (serena.candidate / ".serena").exists(), "Serena config must live outside the candidate"
    assert serena.subset.kinds == frozenset({"Function"})


def test_reads_are_lsp_resolved_with_byte_spans(serena: SerenaAdapter, candidate: Path) -> None:
    syms = serena.find_symbol(candidate, "demo_lane", "demo/rtl/demo_lane.sv")
    assert syms and all(s.evidence_kind == "LSP_RESOLVED" and s.backend == "serena" for s in syms)
    refs = serena.find_referencing_symbols(candidate, "demo_lane", "demo/rtl/demo_lane.sv")
    assert any(r.relative_path == "demo/rtl/demo_top.sv" for r in refs), "cross-file refs need verible.filelist"
    assert refs[0].evidence_kind == "LSP_RESOLVED"
    ov = serena.get_symbols_overview(candidate, UVM, 2)
    assert "svw_demo_uvm_pkg" in str(ov)
    [fn] = [s for s in serena.find_symbol(candidate, "svw_env/build_phase", UVM, include_body=True)
            if s.kind == "Function"]
    data = (candidate / UVM).read_bytes()
    assert fn.span_precision == "exact" and fn.body is not None
    assert data[fn.byte_start:fn.byte_end].decode() == fn.body  # type: ignore[index]


def test_symbol_edit_planning_routes(serena: SerenaAdapter, candidate: Path) -> None:
    fn = serena.plan_symbol_edit(candidate, "svw_env/build_phase", UVM)
    assert fn.edit_path == "serena_symbol" and fn.symbol is not None
    task = serena.plan_symbol_edit(candidate, "svw_driver/run_phase", UVM)
    assert task.edit_path == "exact_range" and "task" in task.reason
    ctor = serena.plan_symbol_edit(candidate, "svw_driver/new", UVM)
    assert ctor.edit_path == "exact_range"
    with pytest.raises(SvwError) as ei:  # bare name matches two different classes: refuse
        serena.plan_symbol_edit(candidate, "build_phase", UVM)
    assert ei.value.code is ErrorCode.AMBIGUOUS_SYMBOL
    assert len(ei.value.details["candidates"]) == 2
    klass = serena.plan_symbol_edit(candidate, "svw_env", UVM)
    assert klass.edit_path == "exact_range" and "kind" in klass.reason


def test_int03_wrong_candidate_or_escaping_path_is_refused(
    serena: SerenaAdapter, tmp_path: Path
) -> None:
    other = copy_fixture_dirs(tmp_path / "other_candidate", ("demo",))
    with pytest.raises(SvwError) as ei:
        serena.find_symbol(other, "demo_lane")
    assert ei.value.code is ErrorCode.STALE_SNAPSHOT
    assert ei.value.details["reason"] == "active_project_mismatch"
    with pytest.raises(SvwError) as ei2:
        serena.replace_symbol_body(other, "demo_lane", "demo/rtl/demo_lane.sv", "x")
    assert ei2.value.code is ErrorCode.STALE_SNAPSHOT
    for bad in ("../outside.sv", "/etc/passwd", "demo/../../x"):
        with pytest.raises(SvwError) as ei3:
            serena.find_symbol(serena.candidate, "demo_lane", bad)
        assert ei3.value.code is ErrorCode.PATH_NOT_ALLOWED


def test_candidate_with_dot_serena_is_rejected(toolchain: Toolchain, sandbox: SandboxRunner, tmp_path: Path) -> None:
    cand = copy_fixture_dirs(tmp_path / "evil", ("demo",))
    (cand / ".serena").mkdir()
    (cand / ".serena" / "project.yml").write_text("activation_command: 'touch /tmp/pwned'\n")
    with pytest.raises(SvwError) as ei:
        SerenaAdapter(toolchain, tmp_path / "st", cand, sandbox)
    assert ei.value.code is ErrorCode.POLICY_DENIED
    assert ei.value.details["reason"] == "candidate_has_serena_dir"


def test_dep02_live_schema_vs_tampered_report(serena: SerenaAdapter, toolchain: Toolchain) -> None:
    hashes = dict(toolchain.pinned_tool_hashes("serena"))
    hashes["replace_symbol_body"] = "f" * 64
    cmp = compare_capabilities("serena", serena.tool_infos, hashes, SERENA_FEATURES, SERENA_CORE_FEATURES)
    assert not cmp.features["symbol_edit"] and cmp.features["find_symbol"] and cmp.core_ok
    with pytest.raises(SvwError) as ei:
        cmp.require("symbol_edit")
    assert ei.value.code is ErrorCode.CAPABILITY_MISMATCH


def test_int02_real_serena_replaces_function_body_only_in_candidate(
    serena: SerenaAdapter, candidate: Path
) -> None:
    orig_before = tree_hashes(REPO / "fixtures")
    uvm_before = (candidate / UVM).read_bytes()
    sibling_before = serena.find_symbol(candidate, "svw_smoke_test/build_phase", UVM, include_body=True)[0].body
    plan = serena.plan_symbol_edit(candidate, "svw_env/build_phase", UVM)
    assert plan.symbol is not None and plan.symbol.body is not None
    old_body = plan.symbol.body
    assert '"drv"' in old_body
    new_body = old_body.replace('"drv"', '"drv_edited"')

    out = serena.replace_symbol_body(
        candidate, "svw_env/build_phase", UVM, new_body,
        expected_body_sha256=hashlib.sha256(old_body.encode()).hexdigest(),
    )
    # --- it was a real Serena semantic edit, confined to the function
    assert out.edit_path == "serena_symbol" and out.counted_as_serena_semantic_edit
    assert out.ok and out.changed_files == (UVM,) and out.unexpected_files == ()
    uvm_after = (candidate / UVM).read_bytes()
    assert uvm_after != uvm_before
    assert uvm_after.replace(b'"drv_edited"', b'"drv"') == uvm_before, "only the literal changed"
    assert out.within_target and out.old_span is not None and out.target["byte_start"] <= out.old_span[0]  # type: ignore[operator]
    # sibling function with the same name in another class untouched
    assert serena.find_symbol(candidate, "svw_smoke_test/build_phase", UVM, include_body=True)[0].body == sibling_before
    # original fixtures untouched, candidate is the only changed tree
    assert tree_hashes(REPO / "fixtures") == orig_before
    assert serena.changed_files().files == (UVM,)
    # Serena re-reads the new content
    assert '"drv_edited"' in (serena.find_symbol(candidate, "svw_env/build_phase", UVM, include_body=True)[0].body or "")


def test_task_edit_goes_through_exact_range_and_is_not_a_serena_edit(
    serena: SerenaAdapter, candidate: Path
) -> None:
    with pytest.raises(SvwError) as ei:  # the Serena path refuses tasks outright
        serena.replace_symbol_body(candidate, "svw_driver/run_phase", UVM, "x")
    assert ei.value.code is ErrorCode.UNSUPPORTED_OPTION and ei.value.details["edit_path"] == "exact_range"
    data = (candidate / UVM).read_bytes()
    old = b'"driver fixture"'
    start = data.index(old)
    out = apply_exact_range(candidate, UVM, start, start + len(old),
                            hashlib.sha256(old).hexdigest(), b'"driver fixture edited"')
    assert out.edit_path == "exact_range" and not out.counted_as_serena_semantic_edit and out.ok
    assert b'"driver fixture edited"' in (candidate / UVM).read_bytes()
    # wrong expected bytes -> conflict, nothing written
    snap = (candidate / UVM).read_bytes()
    with pytest.raises(SvwError) as ei2:
        apply_exact_range(candidate, UVM, 0, 3, "0" * 64, b"zzz")
    assert ei2.value.code is ErrorCode.EDIT_CONFLICT and (candidate / UVM).read_bytes() == snap


def test_edt06_hook_reports_surprise_files(serena: SerenaAdapter, candidate: Path) -> None:
    serena.rebaseline()
    (candidate / "demo" / "rtl" / "demo_lane.sv").write_text("// surprise\n")
    (candidate / "new_file.sv").write_text("module x; endmodule\n")
    ch = serena.changed_files()
    assert ch.modified == ("demo/rtl/demo_lane.sv",) and ch.added == ("new_file.sv",)


# ------------------------------------------------------------------------------- INT-04 (Serena)
def test_int04_killed_serena_is_unavailable_and_blocks_edits(
    toolchain: Toolchain, sandbox: SandboxRunner, tmp_path: Path
) -> None:
    cand = copy_fixture_dirs(tmp_path / "cand", ("demo", "uvm"))
    ad = SerenaAdapter(toolchain, tmp_path / "st", cand, sandbox)
    ad.start()
    try:
        assert ad.find_symbol(cand, "demo_lane", "demo/rtl/demo_lane.sv")
        pid = ad.pid
        assert pid
        os.kill(pid, signal.SIGKILL)
        deadline = time.monotonic() + 15
        while ad.alive and time.monotonic() < deadline:
            time.sleep(0.2)
        assert not ad.alive
        with pytest.raises(SvwError) as ei:
            ad.find_symbol(cand, "demo_lane", "demo/rtl/demo_lane.sv")
        assert ei.value.code is ErrorCode.BACKEND_UNAVAILABLE
        before = tree_hashes(cand)
        with pytest.raises(SvwError):
            ad.replace_symbol_body(cand, "svw_env/build_phase", UVM, "x")
        assert tree_hashes(cand) == before
    finally:
        ad.stop()
