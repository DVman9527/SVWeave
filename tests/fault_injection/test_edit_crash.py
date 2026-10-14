"""Fault injection for the commit path (plan 12.5): EDT-07, EDT-08, recovery modes.

The crash hook is a keyword argument of ``commit`` (never configuration, environment or MCP).
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

import pytest

from svweave.application import edit as E
from svweave.domain.errors import ErrorCode, SvwError
from svweave.editing.journal import SimulatedCrash
from tests.unit._edit_fx import EditWs, make_ws, prepared, to_approval

TWO = [("rtl/pkg.sv", '"task hello"', '"task hello 2"'), ("rtl/top.sv", "q <= q + 4'd1;", "q <= q + 4'd2;")]


@pytest.fixture
def ws(tmp_path: Path) -> EditWs:
    return make_ws(tmp_path)


def two_file_txn(ws: EditWs, key: str = "two-file-0001") -> tuple[str, str, Any]:
    base = ws.build()
    tid, h = to_approval(ws, base, [ws.range_edit(*t) for t in TWO], serena=False, key=key)
    return tid, h, base


def crash(ws: EditWs, tid: str, h: str, after: int = 1) -> None:
    with pytest.raises(SimulatedCrash):
        E.commit(ws.opts, "r", transaction_id=tid, expected_patch_hash=h, request_key="ck-0000001",
                 crash_after_files=after)


def journal(ws: EditWs, tid: str) -> dict[str, Any]:
    out: dict[str, Any] = json.loads((ws.root / ".svweave/journal" / f"{tid}.json").read_text())
    return out


def state(ws: EditWs, tid: str) -> str:
    return next(r["state"] for r in ws.txn_rows() if r["transaction_id"] == tid)


def err(fn: Any, *a: Any, **kw: Any) -> SvwError:
    with pytest.raises(SvwError) as ei:
        fn(*a, **kw)
    return ei.value


def test_edt07_crash_between_files_leaves_a_journal_and_blocks_all_new_writes(ws: EditWs) -> None:
    tid, h, base = two_file_txn(ws)
    crash(ws, tid, h)
    j = journal(ws, tid)
    assert j["status"] == "in_progress"
    assert [f["status"] for f in j["files"]] == ["applied", "pending"]
    assert state(ws, tid) == "COMMITTING"
    # exactly one file is new, the other is original
    assert b"task hello 2" in ws.read("rtl/pkg.sv") and b"4'd1;" in ws.read("rtl/top.sv")
    # every new prepare and commit is blocked
    e = err(E.prepare, ws.opts, "r", ws.req(base, [ws.range_edit("rtl/other.sv", "module other", "module o2")]),
            use_serena=False)
    assert e.code is ErrorCode.RECOVERY_REQUIRED and tid in e.details["transactions"]
    e2 = err(E.commit, ws.opts, "r", transaction_id=tid, expected_patch_hash=h, request_key="ck-0000002")
    assert e2.code is ErrorCode.RECOVERY_REQUIRED
    # deleting the journal file does not unblock (the DB still says COMMITTING)
    (ws.root / ".svweave/journal" / f"{tid}.json").unlink()
    assert err(E.prepare, ws.opts, "r", ws.req(base, [ws.range_edit("rtl/other.sv", "module other", "module o2")]),
               use_serena=False).code is ErrorCode.RECOVERY_REQUIRED


def test_edt07_unreadable_journal_fails_closed(ws: EditWs) -> None:
    tid, h, base = two_file_txn(ws)
    crash(ws, tid, h)
    (ws.root / ".svweave/journal" / f"{tid}.json").write_text("{corrupt")
    assert err(E.prepare, ws.opts, "r", ws.req(base, [ws.range_edit("rtl/other.sv", "module other", "module o2")]),
               use_serena=False).code is ErrorCode.RECOVERY_REQUIRED


def test_journal_is_written_before_the_first_source_byte_changes(
    ws: EditWs, monkeypatch: pytest.MonkeyPatch
) -> None:
    tid, h, _ = two_file_txn(ws)
    seen: list[bytes] = []
    import svweave.editing.journal as jr

    real = jr.apply_files

    def spy(ctx: Any, journal: Any, payloads: Any, **kw: Any) -> None:
        assert (ctx.state_dir / "journal" / f"{tid}.json").is_file()
        seen.append((ctx.project_root / "rtl/pkg.sv").read_bytes())
        real(ctx, journal, payloads, **kw)

    monkeypatch.setattr(jr, "apply_files", spy)
    E.commit(ws.opts, "r", transaction_id=tid, expected_patch_hash=h, request_key="ck-0000001")
    assert b'"task hello"' in seen[0]  # still the original when the journal already existed


def test_crash_before_the_first_file_recovers_to_not_applied(ws: EditWs) -> None:
    tid, h, base = two_file_txn(ws)
    crash(ws, tid, h, after=0)
    r = E.recover(ws.opts, "r", transaction_id=tid)
    assert r.data["recovery"]["block_cleared"] and state(ws, tid) == "ABORTED"
    assert {f["verdict"] for f in r.data["recovery"]["files"]} == {"not_applied"}
    assert b'"task hello"' in ws.read("rtl/pkg.sv")
    # not blocked any more
    prepared(ws, base, [ws.range_edit("rtl/other.sv", "module other", "module o2")], serena=False,
             key="after-recovery-1")


def test_mixed_state_stays_blocked_until_resolved(ws: EditWs) -> None:
    tid, h, base = two_file_txn(ws)
    crash(ws, tid, h)
    r = E.recover(ws.opts, "r", transaction_id=tid)
    assert not r.data["recovery"]["block_cleared"] and r.error.code == "RECOVERY_REQUIRED"  # type: ignore[union-attr]
    assert {f["logical_path"]: f["verdict"] for f in r.data["recovery"]["files"]} == {
        "rtl/pkg.sv": "applied", "rtl/top.sv": "not_applied"}
    assert state(ws, tid) == "RECOVERY_REQUIRED"
    assert err(E.prepare, ws.opts, "r", ws.req(base, [ws.range_edit("rtl/other.sv", "module other", "module o2")]),
               use_serena=False).code is ErrorCode.RECOVERY_REQUIRED


def test_roll_forward_finishes_the_commit_and_post_apply_runs_via_replay(ws: EditWs) -> None:
    tid, h, _ = two_file_txn(ws)
    crash(ws, tid, h)
    r = E.recover(ws.opts, "r", transaction_id=tid, mode="roll_forward")
    assert r.data["recovery"]["block_cleared"]
    assert state(ws, tid) == "POST_APPLY_VERIFIED", "roll-forward runs the post-apply verification"
    assert r.data["post_apply_status"] == "VERIFIED"
    assert b"task hello 2" in ws.read("rtl/pkg.sv") and b"4'd2;" in ws.read("rtl/top.sv")
    c = E.commit(ws.opts, "r", transaction_id=tid, expected_patch_hash=h, request_key="ck-0000001")
    assert c.data["state"] == "POST_APPLY_VERIFIED"
    assert c.data["post_apply"]["p"]["snapshot_equals_candidate"]


def test_rollback_restores_only_files_that_still_equal_the_after_hash(ws: EditWs) -> None:
    tid, h, base = two_file_txn(ws)
    orig = ws.read("rtl/pkg.sv")
    crash(ws, tid, h)
    r = E.recover(ws.opts, "r", transaction_id=tid, mode="rollback")
    assert r.data["recovery"]["block_cleared"] and state(ws, tid) == "ABORTED"
    assert ws.read("rtl/pkg.sv") == orig


def test_edt08_human_edit_after_crash_is_never_overwritten(ws: EditWs) -> None:
    tid, h, base = two_file_txn(ws)
    crash(ws, tid, h)
    human = ws.read("rtl/pkg.sv") + b"// the human kept working here\n"
    (ws.root / "rtl/pkg.sv").write_bytes(human)
    for mode in (None, "rollback", "roll_forward"):
        r = E.recover(ws.opts, "r", transaction_id=tid, mode=mode)
        files = {f["logical_path"]: f for f in r.data["recovery"]["files"]}
        assert files["rtl/pkg.sv"]["verdict"] == "conflict"
        assert files["rtl/pkg.sv"]["action"] == "kept_current_version"
        assert not r.data["recovery"]["block_cleared"]
        assert ws.read("rtl/pkg.sv") == human, f"mode {mode} overwrote the human's version"
        assert b"4'd1;" in ws.read("rtl/top.sv"), "no partial roll-forward when a conflict exists"
    cand = ws.root / files["rtl/pkg.sv"]["candidate_copy"]
    assert cand.is_file() and cand.name == "pkg.sv.candidate" and b"task hello 2" in cand.read_bytes()
    assert cand.read_bytes() != human
    assert err(E.prepare, ws.opts, "r", ws.req(base, [ws.range_edit("rtl/other.sv", "module other", "module o2")]),
               use_serena=False).code is ErrorCode.RECOVERY_REQUIRED
    # explicit human resolution: keep what is on disk, clear the block
    r = E.recover(ws.opts, "r", transaction_id=tid, mode="accept_current")
    assert r.data["recovery"]["block_cleared"] and state(ws, tid) == "CONFLICT"
    assert ws.read("rtl/pkg.sv") == human
    assert journal(ws, tid)["status"] == "resolved"
    assert err(E.prepare, ws.opts, "r", ws.req(base, [ws.range_edit("rtl/other.sv", "module other", "module o2")]),
               use_serena=False).code is ErrorCode.STALE_SNAPSHOT  # blocked no more; base is stale
    fresh = ws.build()
    prepared(ws, fresh, [ws.range_edit("rtl/other.sv", "module other", "module o2")], serena=False,
             key="after-accept-001")


def test_edt08_unrelated_third_state_is_a_conflict_not_a_guess(ws: EditWs) -> None:
    tid, h, _ = two_file_txn(ws)
    crash(ws, tid, h)
    (ws.root / "rtl/top.sv").write_text("// replaced wholesale by someone\n")
    r = E.recover(ws.opts, "r", transaction_id=tid, mode="roll_forward")
    files = {f["logical_path"]: f["verdict"] for f in r.data["recovery"]["files"]}
    assert files == {"rtl/pkg.sv": "applied", "rtl/top.sv": "conflict"}
    assert ws.text("rtl/top.sv") == "// replaced wholesale by someone\n"


def test_failure_during_apply_after_some_files_requires_recovery(
    ws: EditWs, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A second file that changes between the precondition check and its replace (a racing
    editor) must stop the commit with RECOVERY_REQUIRED, not roll back blindly."""
    tid, h, _ = two_file_txn(ws)
    import svweave.editing.journal as jr

    real = jr.write_journal
    calls = {"n": 0}

    def racing(ctx: Any, j: Any) -> None:
        real(ctx, j)
        calls["n"] += 1
        if calls["n"] == 2:  # right after file #1 was journalled as applied
            (ctx.project_root / "rtl/top.sv").write_text("// racing human edit\n")

    monkeypatch.setattr(jr, "write_journal", racing)
    e = err(E.commit, ws.opts, "r", transaction_id=tid, expected_patch_hash=h, request_key="ck-0000001")
    assert e.code is ErrorCode.RECOVERY_REQUIRED and e.details["applied"] == 1
    assert ws.text("rtl/top.sv") == "// racing human edit\n"
    assert b"task hello 2" in ws.read("rtl/pkg.sv"), "applied file is not blindly rolled back"
    assert state(ws, tid) == "RECOVERY_REQUIRED"


def test_workspace_lock_busy_refuses_commit_without_state_change(ws: EditWs) -> None:
    from svweave.storage.lock import workspace_lock

    tid, h, _ = two_file_txn(ws)
    with workspace_lock(ws.root / ".svweave", exclusive=True):
        e = err(E.commit, ws.opts, "r", transaction_id=tid, expected_patch_hash=h,
                request_key="ck-0000001")
    assert e.code is ErrorCode.WORKSPACE_UNSTABLE and state(ws, tid) == "APPROVED"
    assert os.path.exists(ws.root / "rtl/pkg.sv") and b'"task hello"' in ws.read("rtl/pkg.sv")
