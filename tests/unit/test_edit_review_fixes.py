"""Regression tests for the M4 independent-review findings F1..F11 (+ surviving mutants M7b, M9,
M12 and the M3b reviewer's notes). Each test fails without its fix."""

from __future__ import annotations

import json
import os
import stat
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
import yaml

from svweave.application import edit as E
from svweave.domain.errors import ErrorCode, SvwError
from svweave.domain.states import TransactionState as S
from svweave.editing import approval as A
from svweave.editing import journal as jr
from svweave.editing.plan import define_spans
from svweave.editing.store import TxnStore
from tests.unit._edit_fx import (
    EditWs,
    make_ws,
    prepared,
    tamper_candidate,
    to_approval,
    typed_confirm,
)

TASK = ('"task hello"', '"task hello 2"')


@pytest.fixture
def ws(tmp_path: Path) -> EditWs:
    return make_ws(tmp_path)


def C(ws: EditWs, tid: str, h: str, key: str = "commit-key-01", **kw: Any) -> Any:
    return E.commit(ws.opts, "r", transaction_id=tid, expected_patch_hash=h, request_key=key, **kw)


def err(fn: Any, *a: Any, **kw: Any) -> SvwError:
    with pytest.raises(SvwError) as ei:
        fn(*a, **kw)
    return ei.value


def st(ws: EditWs, tid: str) -> str:
    return next(r["state"] for r in ws.txn_rows() if r["transaction_id"] == tid)


def edit_manifest(ws: EditWs, fn: Any) -> None:
    p = ws.root / "svweave.yaml"
    m = yaml.safe_load(p.read_text())
    fn(m)
    p.write_text(yaml.safe_dump(m, sort_keys=False))


def published(ws: EditWs) -> str:
    with ws.db() as db:
        return str(db.conn.execute("SELECT build_id FROM published_pointer").fetchone()[0])


# ----------------------------------------------------------------------------------- F1
def test_f1_config_change_after_approval_is_an_edit_conflict(ws: EditWs) -> None:
    base = ws.build()
    before = ws.read("rtl/pkg.sv")
    tid, h = to_approval(ws, base, [ws.range_edit("rtl/pkg.sv", *TASK)], serena=False)
    edit_manifest(ws, lambda m: m["profiles"]["p"].__setitem__("defines", [{"name": "PROBE_DEF", "value": "1"}]))
    e = err(C, ws, tid, h)
    assert e.code is ErrorCode.EDIT_CONFLICT and e.details["reason"] == "config_changed"
    assert ws.read("rtl/pkg.sv") == before and st(ws, tid) == "CONFLICT"
    assert not (ws.root / ".svweave/journal" / f"{tid}.json").exists()


def test_f1_config_ids_are_bound_into_the_approval(ws: EditWs) -> None:
    base = ws.build()
    tid, h = to_approval(ws, base, [ws.range_edit("rtl/pkg.sv", *TASK)], serena=False)
    rec = json.loads((ws.trusted() / "approvals" / f"{tid}.json").read_text())
    assert set(rec["config_ids"]) == {"p"} and len(rec["config_ids"]["p"]) == 64
    with ws.db() as db:  # tampering with the recorded config ids invalidates the approval
        TxnStore(db).update(tid, lambda r: r.__setitem__("config_ids", {"p": "0" * 64}))
    e = err(C, ws, tid, h)
    assert e.details["reason"] == "approval_binding_mismatch" and e.details["field"] == "config_ids"


# ----------------------------------------------------------------------------------- F2
def test_f2_post_apply_identity_failure_is_not_verified_and_publishes_nothing(
    ws: EditWs, monkeypatch: pytest.MonkeyPatch
) -> None:
    base = ws.build()
    tid, h = to_approval(ws, base, [ws.range_edit("rtl/pkg.sv", *TASK)], serena=False)
    real = jr.apply_files

    def apply_then_race(ctx: Any, j: Any, payloads: Any, **kw: Any) -> None:
        real(ctx, j, payloads, **kw)
        (ctx.project_root / "rtl/other.sv").write_text("// someone else, right after the commit\n")

    monkeypatch.setattr(jr, "apply_files", apply_then_race)
    e = err(C, ws, tid, h)
    assert e.code is ErrorCode.EDIT_CONFLICT and e.details["post_apply_status"] == "FAILED"
    assert "ALREADY WRITTEN" in e.message
    assert st(ws, tid) == "APPLIED", "never reported as verified"
    assert b"task hello 2" in ws.read("rtl/pkg.sv")
    assert published(ws) == base.build_id, "nothing was published before the identity check"
    with ws.db() as db:
        assert db.conn.execute("SELECT COUNT(*) FROM builds").fetchone()[0] == 2  # no post-apply build


def test_f2_post_apply_build_must_be_complete_and_error_free(
    ws: EditWs, monkeypatch: pytest.MonkeyPatch
) -> None:
    base = ws.build()
    tid, h = to_approval(ws, base, [ws.range_edit("rtl/pkg.sv", *TASK)], serena=False)
    real = E.run_build_on_record
    calls = {"n": 0}

    def sabotage(opts: Any, prof: Any, rec: Any, rid: str, **kw: Any) -> Any:
        env = real(opts, prof, rec, rid, **kw)
        calls["n"] += 1
        with ws.db() as db:
            db.conn.execute("UPDATE builds SET diagnostic_outcome='HAS_ERRORS' WHERE build_id=?",
                            (env.build_id,))
        return env

    monkeypatch.setattr(E, "run_build_on_record", sabotage)
    e = err(C, ws, tid, h)
    assert e.code is ErrorCode.PARTIAL_ANALYSIS and e.details["post_apply_status"] == "FAILED"
    assert e.details["reasons"][0]["reason"] == "post_apply_build_not_clean"
    assert st(ws, tid) == "APPLIED" and published(ws) != "" and published(ws) == base.build_id
    monkeypatch.setattr(E, "run_build_on_record", real)  # a replay re-verifies from APPLIED
    with ws.db() as db:
        db.conn.execute("UPDATE builds SET diagnostic_outcome='NO_ERRORS'")
    ok = C(ws, tid, h)
    assert ok.data["state"] == "POST_APPLY_VERIFIED" and ok.data["post_apply_status"] == "VERIFIED"
    assert published(ws) != base.build_id


# ----------------------------------------------------------------------------------- F3
def crash_before_committing(ws: EditWs, tid: str, h: str, monkeypatch: pytest.MonkeyPatch) -> Any:
    orig = TxnStore.update

    def upd(self: TxnStore, txn_id: str, mutate: Any = None, *, to: Any = None, **kw: Any) -> Any:
        if to is S.COMMITTING:
            raise jr.SimulatedCrash("crash after the journal, before COMMITTING")
        return orig(self, txn_id, mutate, to=to, **kw)

    monkeypatch.setattr(TxnStore, "update", upd)
    with pytest.raises(jr.SimulatedCrash):
        C(ws, tid, h)
    monkeypatch.setattr(TxnStore, "update", orig)
    return orig


def test_f3_crash_between_journal_and_committing_rolls_forward_cleanly(
    ws: EditWs, monkeypatch: pytest.MonkeyPatch
) -> None:
    base = ws.build()
    tid, h = to_approval(ws, base, [ws.range_edit("rtl/pkg.sv", *TASK)], serena=False)
    crash_before_committing(ws, tid, h, monkeypatch)
    assert st(ws, tid) == "APPROVED" and b'"task hello"' in ws.read("rtl/pkg.sv")
    assert err(E.prepare, ws.opts, "r", ws.req(base, [ws.range_edit("rtl/other.sv", "module other", "module o")]),
               use_serena=False).code is ErrorCode.RECOVERY_REQUIRED
    r = E.recover(ws.opts, "r", transaction_id=tid, mode="roll_forward")  # no INTERNAL error
    assert r.data["recovery"]["block_cleared"] and r.data["state"] == "POST_APPLY_VERIFIED"
    assert b"task hello 2" in ws.read("rtl/pkg.sv")
    j = json.loads((ws.root / ".svweave/journal" / f"{tid}.json").read_text())
    assert j["status"] == "resolved"
    nb = ws.build()
    prepared(ws, nb, [ws.range_edit("rtl/top.sv", "q <= q + 4'd1;", "q <= q + 4'd3;")], serena=False)


def test_f3_journal_is_resolved_only_after_the_state_update_succeeded(
    ws: EditWs, monkeypatch: pytest.MonkeyPatch
) -> None:
    base = ws.build()
    tid, h = to_approval(ws, base, [ws.range_edit("rtl/pkg.sv", *TASK)], serena=False)
    orig = crash_before_committing(ws, tid, h, monkeypatch)

    def failing(self: TxnStore, txn_id: str, mutate: Any = None, *, to: Any = None, **kw: Any) -> Any:
        if to is S.ABORTED:
            raise RuntimeError("state store went away")
        return orig(self, txn_id, mutate, to=to, **kw)

    monkeypatch.setattr(TxnStore, "update", failing)
    with pytest.raises(RuntimeError):
        E.recover(ws.opts, "r", transaction_id=tid)  # nothing applied -> would abort
    monkeypatch.setattr(TxnStore, "update", orig)
    j = json.loads((ws.root / ".svweave/journal" / f"{tid}.json").read_text())
    assert j["status"] == "in_progress", "the block must stay until the state update succeeded"
    # and the recovery can simply be run again
    r = E.recover(ws.opts, "r", transaction_id=tid)
    assert r.data["recovery"]["block_cleared"] and st(ws, tid) == "ABORTED"


def test_f3_state_already_final_with_open_journal_only_closes_the_journal(ws: EditWs) -> None:
    base = ws.build()
    tid, h = to_approval(ws, base, [ws.range_edit("rtl/pkg.sv", *TASK)], serena=False)
    C(ws, tid, h)
    p = ws.root / ".svweave/journal" / f"{tid}.json"
    j = json.loads(p.read_text())
    j["status"] = "in_progress"  # crash between the state update and the journal write
    p.write_text(json.dumps(j))
    r = E.recover(ws.opts, "r", transaction_id=tid)
    assert r.data["recovery"]["block_cleared"] and st(ws, tid) == "POST_APPLY_VERIFIED"
    assert json.loads(p.read_text())["status"] == "resolved"


# ----------------------------------------------------------------------------------- F4
def test_f4_configured_validate_profiles_are_a_floor(tmp_path: Path) -> None:
    from tests.conftest import profile

    q = profile(source_inputs=[{"kind": "file", "path": "rtl/pkg.sv"},
                               {"kind": "file", "path": "rtl/top.sv"}],
                top_modules=["top"], include_dirs=["rtl"], defines=[{"name": "BUMP2"}])
    ws2 = make_ws(tmp_path, extra_profiles={"q": q}, editing={"validate_profiles": ["p", "q"]})
    base = ws2.build()
    e = prepared(ws2, base, [ws2.range_edit("rtl/pkg.sv", *TASK)], serena=False, profiles=["p"])
    assert e.data["profiles"] == ["p", "q"], "the request cannot drop a configured profile"
    e2 = prepared(ws2, base, [ws2.range_edit("rtl/pkg.sv", '"task hello"', '"x"')], serena=False,
                  profiles=[], key="empty-profiles-1")
    assert e2.data["profiles"] == ["p", "q"]


# ----------------------------------------------------------------------------------- F5
def test_f5_test_approvals_need_the_trusted_policy_flag(tmp_path: Path) -> None:
    off = make_ws(tmp_path, allow_test=False)
    base = off.build()
    e = prepared(off, base, [off.range_edit("rtl/pkg.sv", *TASK)], serena=False)
    tid = e.data["transaction_id"]
    h = E.validate(off.opts, "r", transaction_id=tid).data["patch_hash"]
    x = err(E.approve, off.opts, "r", transaction_id=tid, patch_hash=h, confirm=typed_confirm())
    assert x.details["reason"] == "test_approvals_not_allowed"
    # a perfectly signed test-method record is refused at commit as well
    ctx = off.w.ctx()
    txn = E.TxnStore(E.Database(off.root / ".svweave" / "svweave.db")).get(tid)
    A.write_approval(ctx.trusted_dir, E._profiles_binding(txn, ctx), method="test_token")
    c = err(C, off, tid, h)
    assert c.code is ErrorCode.APPROVAL_REQUIRED and c.details["reason"] == "test_approvals_not_allowed"
    assert b'"task hello"' in off.read("rtl/pkg.sv")
    # the human-typed TTY method needs no flag
    A.write_approval(ctx.trusted_dir, E._profiles_binding(txn, ctx), method="tty")
    assert C(off, tid, h).data["state"] == "POST_APPLY_VERIFIED"


def test_f5_flag_default_is_false_and_results_mark_unqualified_approvals(ws: EditWs) -> None:
    from svweave.config.models import Policy

    assert Policy.model_validate({"schema_version": "1.0", "allowed_roots": ["/x"]}).allow_test_approvals is False
    base = ws.build()
    tid, h = to_approval(ws, base, [ws.range_edit("rtl/pkg.sv", *TASK)], serena=False)
    r = C(ws, tid, h)
    assert r.data["approval_method"] == "test_token" and r.data["approval_qualified"] is False


# ----------------------------------------------------------------------------------- F6
def test_f6_abort_during_commit_is_refused_and_never_internal(
    ws: EditWs, monkeypatch: pytest.MonkeyPatch
) -> None:
    base = ws.build()
    tid, h = to_approval(ws, base, [ws.range_edit("rtl/pkg.sv", *TASK)], serena=False)
    seen: list[SvwError] = []
    orig = jr.write_journal

    def wj(ctx: Any, j: Any) -> None:
        orig(ctx, j)
        if not seen:
            try:
                E.abort(ws.opts, "r-abort", transaction_id=tid)
            except SvwError as e:
                seen.append(e)

    monkeypatch.setattr(jr, "write_journal", wj)
    r = C(ws, tid, h)
    assert seen and seen[0].code is ErrorCode.EDIT_CONFLICT
    assert r.data["state"] == "POST_APPLY_VERIFIED" and b"task hello 2" in ws.read("rtl/pkg.sv")


def test_f6_abort_refused_when_committing_recovering_or_applied(
    ws: EditWs, monkeypatch: pytest.MonkeyPatch
) -> None:
    base = ws.build()
    tid, h = to_approval(ws, base, [ws.range_edit("rtl/pkg.sv", *TASK),
                                    ws.range_edit("rtl/top.sv", "q <= q + 4'd1;", "q <= q + 4'd2;")],
                         serena=False)
    with pytest.raises(jr.SimulatedCrash):
        C(ws, tid, h, crash_after_files=1)
    for _ in range(2):  # COMMITTING, then RECOVERY_REQUIRED
        assert err(E.abort, ws.opts, "r", transaction_id=tid).code is ErrorCode.EDIT_CONFLICT
        E.recover(ws.opts, "r", transaction_id=tid)
    assert err(E.abort, ws.opts, "r", transaction_id=tid).code is ErrorCode.EDIT_CONFLICT
    assert st(ws, tid) == "RECOVERY_REQUIRED" and (ws.root / ".svweave/candidates" / tid).exists()


def test_f6_abort_of_an_unfinished_journal_in_approved_state_is_refused(
    ws: EditWs, monkeypatch: pytest.MonkeyPatch
) -> None:
    base = ws.build()
    tid, h = to_approval(ws, base, [ws.range_edit("rtl/pkg.sv", *TASK)], serena=False)
    crash_before_committing(ws, tid, h, monkeypatch)
    assert err(E.abort, ws.opts, "r", transaction_id=tid).code is ErrorCode.EDIT_CONFLICT
    assert st(ws, tid) == "APPROVED"


# ----------------------------------------------------------------------------------- F7
def test_f7_commit_key_cannot_be_reused_for_another_transaction(ws: EditWs) -> None:
    base = ws.build()
    t1, h1 = to_approval(ws, base, [ws.range_edit("rtl/pkg.sv", *TASK)], serena=False)
    C(ws, t1, h1, key="shared-key-1")
    base2 = ws.build()
    t2, h2 = to_approval(ws, base2, [ws.range_edit("rtl/top.sv", "q <= q + 4'd1;", "q <= q + 4'd2;")],
                         serena=False)
    e = err(C, ws, t2, h2, key="shared-key-1")
    assert e.code is ErrorCode.IDEMPOTENCY_CONFLICT and e.details["transaction_id"] == t1
    assert b"4'd1;" in ws.read("rtl/top.sv")
    assert C(ws, t2, h2, key="other-key-22").data["state"] == "POST_APPLY_VERIFIED"


def test_f7_aborted_prepare_key_keeps_its_payload(ws: EditWs) -> None:
    base = ws.build()
    a = prepared(ws, base, [ws.range_edit("rtl/pkg.sv", *TASK)], serena=False, key="reuse-key-1")
    E.abort(ws.opts, "r", transaction_id=a.data["transaction_id"])
    other = [ws.range_edit("rtl/top.sv", "q <= q + 4'd1;", "q <= q + 4'd2;")]
    assert err(prepared, ws, base, other, serena=False, key="reuse-key-1").code is ErrorCode.IDEMPOTENCY_CONFLICT
    # the SAME payload may be prepared again (a fresh transaction)
    again = prepared(ws, base, [ws.range_edit("rtl/pkg.sv", *TASK)], serena=False, key="reuse-key-1")
    assert again.data["transaction_id"] != a.data["transaction_id"]


# ----------------------------------------------------------------------------------- F8
def test_f8_define_directive_edits_are_refused(ws: EditWs) -> None:
    base = ws.build()
    for old, new in (("`define BUMP 1", "`define BUMP 2"), ("BUMP 1", "BUMP 3"), (" 1\n", " 2\n")):
        e = err(E.prepare, ws.opts, "r", ws.req(base, [ws.range_edit("rtl/defs.svh", old, new)]),
                use_serena=False)
        assert e.code is ErrorCode.POLICY_DENIED
        assert e.details["reason"] == "macro_definition_edit_unsupported", old
    assert ws.txn_rows() == []


def test_f8_define_spans_cover_continuation_lines() -> None:
    d = b"x\n`define A 1\ny\n  `define B(a) \\\n  a + \\\n  2\nz\n"
    spans = [d[a:b] for a, b in define_spans(d)]
    assert spans == [b"`define A 1", b"  `define B(a) \\\n  a + \\\n  2"]


# ----------------------------------------------------------------------------------- F9
def test_f9_approval_key_must_be_private_and_owned(tmp_path: Path) -> None:
    t = tmp_path / "trusted-new"
    assert A.approval_key(t, create=True) is not None
    assert stat.S_IMODE(os.stat(t).st_mode) == 0o700
    assert stat.S_IMODE(os.stat(t / A.KEY_FILENAME).st_mode) == 0o600
    os.chmod(t / A.KEY_FILENAME, 0o644)
    e = err(A.approval_key, t, create=False)
    assert e.code is ErrorCode.POLICY_DENIED and e.details["reason"] == "approval_key_unsafe"
    os.chmod(t / A.KEY_FILENAME, 0o600)
    (t / A.KEY_FILENAME).unlink()
    os.symlink("/etc/hostname", t / A.KEY_FILENAME)
    assert err(A.approval_key, t, create=False).code is ErrorCode.POLICY_DENIED


def test_f9_trusted_dir_inside_the_project_tree_is_refused(ws: EditWs) -> None:
    fake = SimpleNamespace(trusted_dir=ws.root / ".trusted", manifest_dir=ws.root, project_root=ws.root)
    assert err(E._check_trusted_location, fake).code is ErrorCode.POLICY_DENIED
    E._check_trusted_location(ws.w.ctx())  # the real one is outside


def test_f9_unsafe_key_blocks_commit(ws: EditWs) -> None:
    base = ws.build()
    tid, h = to_approval(ws, base, [ws.range_edit("rtl/pkg.sv", *TASK)], serena=False)
    os.chmod(ws.trusted() / A.KEY_FILENAME, 0o666)
    assert err(C, ws, tid, h).details["reason"] == "approval_key_unsafe"
    assert b'"task hello"' in ws.read("rtl/pkg.sv")


# ----------------------------------------------------------------------------------- F10 / M12
def test_f10_m12_commit_time_policy_narrowing_is_policy_denied(ws: EditWs) -> None:
    base = ws.build()
    tid, h = to_approval(ws, base, [ws.range_edit("rtl/pkg.sv", *TASK)], serena=False)
    edit_manifest(ws, lambda m: m["editing"].__setitem__("allowed_paths", ["ro/**"]))
    e = err(C, ws, tid, h)
    assert e.code is ErrorCode.POLICY_DENIED and e.details["reason"] == "path_not_allowed"
    assert b'"task hello"' in ws.read("rtl/pkg.sv")
    assert not (ws.root / ".svweave/journal" / f"{tid}.json").exists()


def test_m12_precommit_path_check_is_independent_of_the_freeze_check(
    ws: EditWs, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Even if the candidate re-freeze did not look at the policy, the per-file pre-journal check
    must still refuse a path that is no longer allowed."""
    base = ws.build()
    tid, h = to_approval(ws, base, [ws.range_edit("rtl/pkg.sv", *TASK)], serena=False)
    edit_manifest(ws, lambda m: m["editing"].__setitem__("allowed_paths", ["ro/**"]))
    real = E.path_allowed
    calls = {"n": 0}

    def late(ctx: Any, logical: str) -> bool:  # lets the first (early) check and freeze pass
        calls["n"] += 1
        return True if calls["n"] <= 2 else real(ctx, logical)

    monkeypatch.setattr(E, "path_allowed", late)
    e = err(C, ws, tid, h)
    assert e.code is ErrorCode.POLICY_DENIED
    assert b'"task hello"' in ws.read("rtl/pkg.sv")


# ----------------------------------------------------------------------------------- F11
@pytest.mark.parametrize("text", ["// slang lint_off\n", "`pragma diagnostic ignore=\"x\"\n",
                                  "// pragma protect begin\n", "// verible lint_on\n"])
def test_f11_diagnostic_control_directives_are_rejected(ws: EditWs, text: str) -> None:
    base = ws.build()
    e = err(E.prepare, ws.opts, "r", ws.req(base, [ws.range_edit(
        "rtl/top.sv", "always_ff", text + "  always_ff")]), use_serena=False)
    assert e.code is ErrorCode.POLICY_DENIED
    assert "diagnostic_control_change" in str(e.details["failures"])
    assert ws.txn_rows()[0]["state"] == "ABORTED"


def test_f11_tampered_candidate_is_rejected_at_validate_and_benign_comments_pass(ws: EditWs) -> None:
    base = ws.build()
    e = prepared(ws, base, [ws.range_edit("rtl/pkg.sv", *TASK)], serena=False)
    tid = e.data["transaction_id"]
    tamper_candidate(ws, tid, "rtl/pkg.sv", lambda b: b + b"// slang lint_off\n")
    v = E.validate(ws.opts, "r", transaction_id=tid)
    assert "diagnostic_control_change" in {f["reason"] for f in v.data["validation"]["failures"]}
    ok = prepared(ws, base, [ws.range_edit("rtl/top.sv", "always_ff", "// a plain comment\n  always_ff")],
                  serena=False, key="benign-key-01")
    assert ok.data["state"] == "CANDIDATE_READY"


# ----------------------------------------------------------------------------------- M7b / M9
def test_m7b_the_pre_journal_cas_refuses_before_any_journal_exists(
    ws: EditWs, monkeypatch: pytest.MonkeyPatch
) -> None:
    base = ws.build()
    tid, h = to_approval(ws, base, [ws.range_edit("rtl/pkg.sv", *TASK)], serena=False)
    monkeypatch.setattr(E, "_stale", lambda ctx, rec: None)  # freshness check "passes"
    (ws.root / "rtl/pkg.sv").write_text(ws.text("rtl/pkg.sv") + "// human\n")
    human = ws.read("rtl/pkg.sv")
    monkeypatch.setattr(jr, "apply_files", lambda *a, **k: pytest.fail("apply must not start"))
    e = err(C, ws, tid, h)
    assert e.details["reason"] == "expected_hash_mismatch"
    assert not (ws.root / ".svweave/journal" / f"{tid}.json").exists(), "refused BEFORE the journal"
    assert st(ws, tid) == "CONFLICT" and ws.read("rtl/pkg.sv") == human


def test_m9_post_apply_checks_the_bytes_on_disk(ws: EditWs, monkeypatch: pytest.MonkeyPatch) -> None:
    base = ws.build()
    tid, h = to_approval(ws, base, [ws.range_edit("rtl/pkg.sv", *TASK)], serena=False)

    def lying_apply(ctx: Any, journal: Any, payloads: Any, **kw: Any) -> None:
        for f in journal.files:  # claims success without writing anything
            f.status = "applied"

    monkeypatch.setattr(jr, "apply_files", lying_apply)
    e = err(C, ws, tid, h)
    assert e.code is ErrorCode.EDIT_CONFLICT and e.details["reasons"][0]["reason"] == "post_apply_bytes_differ"
    assert st(ws, tid) == "APPLIED" and e.details["post_apply_status"] == "FAILED"
    assert published(ws) == base.build_id


# ----------------------------------------------------------------------------------- M3b notes
def test_validation_builds_take_the_exclusive_build_lock(ws: EditWs) -> None:
    from svweave.application.build import run_build_on_record
    from svweave.snapshot.capture import load_record
    from svweave.storage.lock import build_lock

    base = ws.build()
    rec = load_record(ws.root / ".svweave/snapshots" / str(base.snapshot_id))
    with build_lock(ws.root / ".svweave"):
        e = err(run_build_on_record, ws.opts, "p", rec, "r", publish=False)
    assert e.code is ErrorCode.RESOURCE_LIMIT and e.details["reason"] == "build_in_progress"


def test_request_text_fields_fit_the_gateway_cap(ws: EditWs) -> None:
    from svweave.editing.models import MAX_TEXT_BYTES, PrepareRequest
    from svweave.gateway import schemas

    assert MAX_TEXT_BYTES == 60000
    items = schemas.INPUT["svw_edit"]["properties"]["edits"]["items"]["oneOf"]
    assert items[0]["properties"]["new_body"]["maxLength"] == MAX_TEXT_BYTES
    assert items[1]["properties"]["replacement"]["maxLength"] == MAX_TEXT_BYTES
    base = {"base_build_id": "a" * 64, "snapshot_id": "b" * 64, "request_key": "key-12345678"}
    PrepareRequest.model_validate({**base, "edits": [
        {"kind": "symbol_replacement", "declaration_id": "x", "new_body": "y" * 60000}]})
    with pytest.raises(ValueError):
        PrepareRequest.model_validate({**base, "edits": [
            {"kind": "symbol_replacement", "declaration_id": "x", "new_body": "y" * 60001}]})


def test_f6_abort_takes_the_workspace_lock(ws: EditWs) -> None:
    from svweave.storage.lock import workspace_lock

    base = ws.build()
    e = prepared(ws, base, [ws.range_edit("rtl/pkg.sv", *TASK)], serena=False)
    tid = e.data["transaction_id"]
    with workspace_lock(ws.root / ".svweave", exclusive=True):  # "a commit holds the lock"
        x = err(E.abort, ws.opts, "r", transaction_id=tid)
    assert x.code is ErrorCode.EDIT_CONFLICT and x.details["reason"] == "workspace_busy"
    assert st(ws, tid) == "CANDIDATE_READY" and (ws.root / ".svweave/candidates" / tid).exists()
    assert E.abort(ws.opts, "r", transaction_id=tid).data["state"] == "ABORTED"
