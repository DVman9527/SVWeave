"""A3: recovery roll-forward applies the same gates as a normal commit (approval, editing
permission, policy, toolchain/config, unchanged dependencies), tolerating files that already sit
at their after-hash."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml

from svweave.application import edit as E
from svweave.domain.errors import ErrorCode, SvwError
from tests.fault_injection.test_edit_crash import crash, state, two_file_txn
from tests.unit._edit_fx import EditWs, make_ws


@pytest.fixture
def ws(tmp_path: Path) -> EditWs:
    return make_ws(tmp_path)


def roll(ws: EditWs, tid: str) -> Any:
    return E.recover(ws.opts, "r", transaction_id=tid, mode="roll_forward")


def refused(ws: EditWs, tid: str) -> SvwError:
    top = ws.read("rtl/top.sv")
    with pytest.raises(SvwError) as ei:
        roll(ws, tid)
    assert ws.read("rtl/top.sv") == top, "roll-forward wrote a file although a gate refused"
    assert b"4'd1;" in ws.read("rtl/top.sv")
    return ei.value


def test_a3_roll_forward_without_a_valid_approval_is_refused(ws: EditWs) -> None:
    tid, h, _ = two_file_txn(ws)
    crash(ws, tid, h)
    (ws.trusted() / "approvals" / f"{tid}.json").unlink()
    e = refused(ws, tid)
    assert e.code is ErrorCode.APPROVAL_REQUIRED and e.details["reason"] == "approval_missing"
    assert state(ws, tid) == "RECOVERY_REQUIRED"


def test_a3_roll_forward_with_editing_disabled_is_refused(ws: EditWs) -> None:
    tid, h, _ = two_file_txn(ws)
    crash(ws, tid, h)
    p = ws.root / "svweave.yaml"
    m = yaml.safe_load(p.read_text())
    m["editing"]["enabled"] = False  # the trusted policy still allows editing; the manifest turns it off
    p.write_text(yaml.safe_dump(m, sort_keys=False))
    e = refused(ws, tid)
    assert e.code is ErrorCode.POLICY_DENIED and e.details["reason"] == "editing_disabled"


def test_a3_roll_forward_after_a_toolchain_change_is_refused(
    ws: EditWs, monkeypatch: pytest.MonkeyPatch
) -> None:
    tid, h, _ = two_file_txn(ws)
    crash(ws, tid, h)
    monkeypatch.setattr(E, "compute_toolchain", lambda root: ("f" * 64, {}, root))
    e = refused(ws, tid)
    assert e.code is ErrorCode.EDIT_CONFLICT and e.details["reason"] == "toolchain_changed"


def test_a3_roll_forward_after_a_config_change_is_refused(ws: EditWs) -> None:
    tid, h, _ = two_file_txn(ws)
    crash(ws, tid, h)
    p = ws.root / "svweave.yaml"
    m = yaml.safe_load(p.read_text())
    m["profiles"]["p"]["defines"] = [{"name": "PROBE_DEF", "value": "1"}]
    p.write_text(yaml.safe_dump(m, sort_keys=False))
    e = refused(ws, tid)
    assert e.code is ErrorCode.EDIT_CONFLICT and e.details["reason"] == "config_changed"


def test_a3_roll_forward_after_a_dependency_changed_is_refused(ws: EditWs) -> None:
    tid, h, _ = two_file_txn(ws)
    crash(ws, tid, h)
    (ws.root / "rtl/defs.svh").write_text("`define BUMP 7\n")  # an include dependency
    e = refused(ws, tid)
    assert e.code is ErrorCode.EDIT_CONFLICT and e.details["reason"] == "workspace_changed"


def test_a3_roll_forward_with_a_newly_narrowed_path_policy_is_refused(ws: EditWs) -> None:
    tid, h, _ = two_file_txn(ws)
    crash(ws, tid, h)
    p = ws.root / "svweave.yaml"
    m = yaml.safe_load(p.read_text())
    m["editing"]["allowed_paths"] = ["rtl/pkg.sv"]
    p.write_text(yaml.safe_dump(m, sort_keys=False))
    e = refused(ws, tid)
    # exactly what recheck_edit_policy raises (denied("path_not_allowed")); the HMAC approval
    # binding would also block the write but with APPROVAL_REQUIRED, so this pins the recheck
    assert e.code is ErrorCode.POLICY_DENIED and e.details["reason"] == "path_not_allowed"


def test_a3_tampered_candidate_bytes_are_not_rolled_forward(ws: EditWs) -> None:
    tid, h, _ = two_file_txn(ws)
    crash(ws, tid, h)
    p = ws.root / ".svweave/candidates" / tid / "rtl/top.sv"
    p.write_bytes(p.read_bytes() + b"// sneaked\n")
    top = ws.read("rtl/top.sv")
    with pytest.raises(SvwError):
        roll(ws, tid)
    assert ws.read("rtl/top.sv") == top


def test_a3_roll_forward_still_works_with_all_gates_green(ws: EditWs) -> None:
    tid, h, _ = two_file_txn(ws)
    crash(ws, tid, h)
    r = roll(ws, tid)
    assert r.data["recovery"]["block_cleared"] and b"4'd2;" in ws.read("rtl/top.sv")
