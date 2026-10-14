"""EDT flows with the REAL backends (slang worker + Serena in bwrap) on the fixtures/uvm profile.

Fixtures are copied into a temp workspace: the repository fixtures are never edited. One backend
at a time, always torn down.
"""

from __future__ import annotations

import hashlib
import json
import shutil
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from svweave.application import edit as E
from svweave.domain.errors import ErrorCode, SvwError
from svweave.orchestration.registry import BackendRegistry
from svweave.orchestration.toolchain import Toolchain
from tests.conftest import FIXTURES, Ws, profile
from tests.integration.conftest import skip_or_fail
from tests.integration.helpers import UVM_SRC
from tests.unit._edit_fx import EditWs, typed_confirm

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not (UVM_SRC / "uvm_pkg.sv").is_file(), reason="pinned UVM library missing"),
]
PKG = "fixtures/uvm/svw_demo_uvm_pkg.sv"
QN = "svw_demo_uvm_pkg::"


@pytest.fixture
def ws(tmp_path: Path) -> EditWs:
    root = tmp_path / "ws"
    (root / "fixtures" / "uvm").mkdir(parents=True)
    for f in (FIXTURES / "uvm").glob("*.sv"):
        shutil.copy(f, root / "fixtures" / "uvm" / f.name)
    w = Ws(root)
    prof = profile(
        source_inputs=[{"kind": "file", "path": "@uvm/uvm_pkg.sv"},
                       {"kind": "file", "path": "fixtures/uvm/svw_demo_uvm_pkg.sv"},
                       {"kind": "file", "path": "fixtures/uvm/svw_uvm_top.sv"}],
        top_modules=["svw_uvm_top"], include_dirs=["@uvm"],
    )
    w.config(
        {"uvm_smoke": prof},
        source_roots=[
            {"id": "project", "path": ".", "include": ["**"], "exclude": ["**/.svweave/**"],
             "writable": False},
            {"id": "uvmfx", "path": "fixtures/uvm", "include": ["**"], "exclude": [],
             "writable": True},
            {"id": "uvm", "include": ["**"], "exclude": []},
        ],
        editing={"enabled": True, "allowed_paths": ["fixtures/uvm/**"], "max_files_per_patch": 5,
                 "max_changed_lines": 40, "require_human_approval": True,
                 "validate_profiles": ["uvm_smoke"]},
    )
    w.policy(allowed_roots=[str(root.resolve()), str(UVM_SRC.resolve())],
             writable_paths=["fixtures/uvm/**"], editing_enabled=True,
             allow_test_approvals=True)
    w.local([{"id": "uvm", "path": str(UVM_SRC)}])
    return EditWs(root, w)


@pytest.fixture
def registry(ws: EditWs) -> Iterator[BackendRegistry]:
    tc = Toolchain.load(Path(__file__).resolve().parents[2])
    if not tc.serena_exe.is_file():
        skip_or_fail("serena venv not installed")
    reg = BackendRegistry.for_workspace(ws.w.ctx(), tc)
    try:
        yield reg
    finally:
        reg.shutdown()


def body_of(ws: EditWs, bid: str, qn: str) -> tuple[dict[str, Any], str]:
    d = ws.decl(bid, QN + qn)
    data = ws.read(PKG)
    return d, data[d["full_start"]:d["full_end"]].decode()


def pipeline(ws: EditWs, base: Any, edits: list[dict[str, Any]], registry: BackendRegistry,
             key: str) -> tuple[str, str, Any]:
    e = E.prepare(ws.opts, "r", ws.req(base, edits, key, ["uvm_smoke"]), registry=registry)
    tid = e.data["transaction_id"]
    registry.shutdown()  # one heavy backend at a time: Serena is gone before slang runs
    v = E.validate(ws.opts, "r", transaction_id=tid)
    assert v.error is None, (v.error, v.data["validation"]["failures"])
    E.approve(ws.opts, "r", transaction_id=tid, patch_hash=v.data["patch_hash"],
              confirm=typed_confirm())
    return tid, v.data["patch_hash"], e


def test_edt01_serena_function_edit_flow_end_to_end(ws: EditWs, registry: BackendRegistry) -> None:
    base = ws.build("uvm_smoke")
    assert base.analysis_coverage.completeness.value == "COMPLETE"  # type: ignore[union-attr]
    orig = ws.read(PKG)
    d, old_body = body_of(ws, base.build_id, "svw_env::build_phase")
    assert '"drv"' in old_body
    new_body = old_body.replace('"drv"', '"drv_edited"')
    edit = {"kind": "symbol_replacement", "declaration_id": d["declaration_id"], "new_body": new_body}
    tid, h, prep = pipeline(ws, base, [edit], registry, "uvm-fn-0001")
    # a REAL Serena semantic edit, applied in the candidate only
    [rec] = prep.data["edits"]
    assert rec["edit_path"] == "serena_symbol" and rec["counted_as_serena_semantic_edit"]
    assert rec["serena"]["within_target"] and rec["serena"]["name_path"].endswith("svw_env/build_phase")
    assert ws.read(PKG) == orig, "nothing written to the original before commit"

    r = E.commit(ws.opts, "r", transaction_id=tid, expected_patch_hash=h, request_key="uvm-commit-01")
    assert r.data["state"] == "POST_APPLY_VERIFIED" and r.error is None
    new = ws.read(PKG)
    assert new.replace(b'"drv_edited"', b'"drv"') == orig, "only that function's literal changed"
    # the other build_phase (svw_smoke_test) and both run_phase tasks are intact
    for qn in ("svw_smoke_test::build_phase", "svw_driver::run_phase", "svw_monitor::run_phase"):
        d0 = ws.decl(base.build_id, QN + qn)
        seg0 = orig[d0["full_start"]:d0["full_end"]]
        assert seg0 in new, qn
    pa = r.data["post_apply"]["uvm_smoke"]
    assert pa["snapshot_equals_candidate"] and pa["completeness"] == "COMPLETE"
    assert pa["old_build_freshness"] == "HISTORICAL"
    assert r.data["edit_paths"] == ["serena_symbol"]
    j = json.loads((ws.root / ".svweave/journal" / f"{tid}.json").read_text())
    assert j["status"] == "complete"
    assert not (ws.root / ".svweave/candidates" / tid).exists()


def test_edt01b_task_log_text_goes_through_exact_range_and_monitor_is_untouched(
    ws: EditWs, registry: BackendRegistry
) -> None:
    base = ws.build("uvm_smoke")
    orig = ws.read(PKG)
    old = b'"driver fixture"'
    pos = orig.index(old)
    assert orig.count(old) == 1
    edit = {"kind": "exact_range", "logical_path": PKG, "start_byte": pos,
            "end_byte": pos + len(old), "expected_sha256": hashlib.sha256(old).hexdigest(),
            "replacement": '"driver fixture edited"'}
    tid, h, prep = pipeline(ws, base, [edit], registry, "uvm-task-0001")
    [rec] = prep.data["edits"]
    assert rec["edit_path"] == "exact_range" and not rec["counted_as_serena_semantic_edit"]
    r = E.commit(ws.opts, "r", transaction_id=tid, expected_patch_hash=h, request_key="uvm-commit-02")
    assert r.data["state"] == "POST_APPLY_VERIFIED"
    new = ws.read(PKG)
    assert new == orig.replace(old, b'"driver fixture edited"')
    mon = ws.decl(base.build_id, QN + "svw_monitor::run_phase")
    assert orig[mon["full_start"]:mon["full_end"]] in new
    assert b'"monitor fixture"' in new


def test_task_symbol_replacement_is_routed_to_exact_range_per_adr012(
    ws: EditWs, registry: BackendRegistry
) -> None:
    base = ws.build("uvm_smoke")
    d, old_body = body_of(ws, base.build_id, "svw_driver::run_phase")
    new_body = old_body.replace("driver fixture", "driver fixture via task")
    e = E.prepare(ws.opts, "r", ws.req(base, [{"kind": "symbol_replacement",
                                               "declaration_id": d["declaration_id"],
                                               "new_body": new_body}], "uvm-task-sym-1",
                                       ["uvm_smoke"]), registry=registry)
    [rec] = e.data["edits"]
    assert rec["edit_path"] == "exact_range" and rec["counted_as_serena_semantic_edit"] is False
    assert rec["decl_kind"] == "task"


def test_bare_method_name_is_ambiguous_even_with_real_serena(
    ws: EditWs, registry: BackendRegistry
) -> None:
    base = ws.build("uvm_smoke")
    with pytest.raises(SvwError) as ei:
        E.prepare(ws.opts, "r", ws.req(base, [{"kind": "symbol_replacement",
                                               "declaration_id": "build_phase", "new_body": "x"}],
                                       "uvm-bare-0001", ["uvm_smoke"]), registry=registry)
    assert ei.value.code is ErrorCode.AMBIGUOUS_SYMBOL
    names = {c["qualified_name"] for c in ei.value.details["candidates"]}
    assert {QN + "svw_env::build_phase", QN + "svw_smoke_test::build_phase"} <= names


def test_edt02_human_edit_after_approval_is_a_conflict(ws: EditWs, registry: BackendRegistry) -> None:
    base = ws.build("uvm_smoke")
    orig = ws.read(PKG)
    old = b'"driver fixture"'
    pos = orig.index(old)
    edit = {"kind": "exact_range", "logical_path": PKG, "start_byte": pos, "end_byte": pos + len(old),
            "expected_sha256": hashlib.sha256(old).hexdigest(), "replacement": '"driver fixture 2"'}
    tid, h, _ = pipeline(ws, base, [edit], registry, "uvm-conf-0001")
    top = ws.root / "fixtures/uvm/svw_uvm_top.sv"
    human = top.read_text() + "// human edit of a different, still relevant source\n"
    top.write_text(human)
    with pytest.raises(SvwError) as ei:
        E.commit(ws.opts, "r", transaction_id=tid, expected_patch_hash=h, request_key="uvm-commit-03")
    assert ei.value.code is ErrorCode.EDIT_CONFLICT
    assert top.read_text() == human and ws.read(PKG) == orig
