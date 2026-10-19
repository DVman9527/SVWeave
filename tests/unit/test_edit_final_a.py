"""Final fix round A (security + editing): A1 terminal-escape approval spoofing, A3 recovery
roll-forward gates, A4 policy re-check at commit, A5 GC retention, A6 generated inputs.
Each test fails without its fix."""

from __future__ import annotations

import io
import sys
from pathlib import Path
from typing import Any

import pytest

from svweave.application import edit as E
from svweave.domain.errors import ErrorCode, SvwError
from svweave.editing.confirm import tty_confirm
from svweave.editing.diffing import has_unsafe_control, visible
from tests.unit._edit_fx import EditWs, make_ws, prepared

TASK = ('"task hello"', '"task hello 2"')


@pytest.fixture
def ws(tmp_path: Path) -> EditWs:
    return make_ws(tmp_path)


def err(fn: Any, *a: Any, **kw: Any) -> SvwError:
    with pytest.raises(SvwError) as ei:
        fn(*a, **kw)
    return ei.value


# ------------------------------------------------------------------------------------ A1
BAD_TEXTS = [
    "\x1b[2J fake approval prompt",
    "x\ry",  # lone CR rewrites the terminal line
    "bell\x07",
    "del\x7f",
    "bidi ‮ reversed",
    "iso ⁦ isolate ⁩",
    "c1 \x9b31m",
]


@pytest.mark.parametrize("text", BAD_TEXTS)
def test_a1_unsafe_control_detector(text: str) -> None:
    assert has_unsafe_control(text)


def test_a1_tab_lf_and_crlf_are_fine() -> None:
    assert not has_unsafe_control("a\tb\nc\r\nd\n")
    assert has_unsafe_control("a\r")  # CR not followed by LF


@pytest.mark.parametrize("text", BAD_TEXTS)
def test_a1_range_replacement_with_control_chars_is_refused_at_prepare(
    ws: EditWs, text: str
) -> None:
    base = ws.build()
    e = err(
        prepared, ws, base,
        [ws.range_edit("rtl/pkg.sv", TASK[0], '"task ' + text + '"')], serena=False,
    )
    assert e.code is ErrorCode.POLICY_DENIED
    assert e.details["reason"] == "control_character_in_edit"


def test_a1_symbol_replacement_with_escape_is_refused(ws: EditWs) -> None:
    base = ws.build()
    d = ws.decl(base.build_id or "", "p::c::g", "function")
    edit = {"kind": "symbol_replacement", "declaration_id": d["declaration_id"],
            "new_body": "function int g(int x);\x1b[2J return 1; endfunction"}
    e = err(prepared, ws, base, [edit], serena=False)
    assert e.details["reason"] == "control_character_in_edit"


def test_a1_candidate_tampered_with_escape_fails_validation(ws: EditWs) -> None:
    base = ws.build()
    tid = prepared(ws, base, [ws.range_edit("rtl/pkg.sv", *TASK)], serena=False).data["transaction_id"]
    p = ws.root / ".svweave" / "candidates" / tid / "rtl/pkg.sv"
    p.write_bytes(p.read_bytes().replace(b"task hello 2", b"task \x1b[2Jhello"))
    v = E.validate(ws.opts, "r", transaction_id=tid)
    assert v.error is not None
    assert "control_character_in_edit" in {f["reason"] for f in v.error.details["failures"]}
    assert v.error.code == "POLICY_DENIED"


def test_a1_crlf_file_edit_still_allowed(tmp_path: Path) -> None:
    w = make_ws(tmp_path)
    (w.root / "rtl/other.sv").write_bytes(b"// other\r\nmodule other; endmodule\r\n")
    base = w.build()
    e = prepared(w, base, [w.range_edit("rtl/other.sv", "module other", "module o2")], serena=False)
    assert E.validate(w.opts, "r", transaction_id=e.data["transaction_id"]).error is None


def test_a1_visible_escapes_everything_nonprintable() -> None:
    out = visible("a\x1b[2Jb\rc‮d\x7f\tz\n")
    assert "\x1b" not in out and "\r" not in out and "‮" not in out and "\x7f" not in out
    assert "\\x1b" in out and "\\r" in out and "\\u202e" in out and "\\x7f" in out
    assert "\n" in out and "\t" in out


def test_a1_tty_confirm_never_writes_raw_controls(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("os.isatty", lambda fd: True)
    monkeypatch.setattr(sys, "stdin", io.StringIO("abc\n"))
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True, raising=False)
    sink = io.StringIO()
    monkeypatch.setattr(sys, "stderr", sink)
    tty_confirm({
        "transaction_id": "t\x1b[1m", "profiles": ["p\x1b[2J"], "edit_paths": ["e\x07"],
        "patch_hash": "h" * 64,
        "diff": "+Approve? \x1b[2K\x1b[1A\x1b[2Kall good\r\n+bidi ‮ x\n",
    })
    shown = sink.getvalue()
    assert "\x1b" not in shown and "\x07" not in shown and "‮" not in shown
    assert "\r" not in shown
    assert "\\x1b[2K" in shown


# ------------------------------------------------------------------------------------ A6
def _declare_generated(ws: EditWs, path: str) -> None:
    import yaml

    p = ws.root / "svweave.yaml"
    m = yaml.safe_load(p.read_text())
    m["profiles"]["p"]["generated_inputs"] = [{"path": path}]
    p.write_text(yaml.safe_dump(m, sort_keys=False))


def test_a6_generated_input_that_is_also_a_source_cannot_be_edited(ws: EditWs) -> None:
    _declare_generated(ws, "rtl/other.sv")  # listed as a compilation source AND generated
    base = ws.build()
    assert base.build_id, base.error
    e = err(prepared, ws, base,
            [ws.range_edit("rtl/other.sv", "module other", "module o2")], serena=False)
    assert e.code is ErrorCode.POLICY_DENIED
    assert e.details["reason"] == "role_not_editable"
    assert ws.text("rtl/other.sv").startswith("// other file")


# ------------------------------------------------------------------------------------ A4
def _edit_manifest(ws: EditWs, fn: Any) -> None:
    import yaml

    p = ws.root / "svweave.yaml"
    m = yaml.safe_load(p.read_text())
    fn(m)
    p.write_text(yaml.safe_dump(m, sort_keys=False))


def _commit(ws: EditWs, tid: str, h: str) -> Any:
    return E.commit(ws.opts, "r", transaction_id=tid, expected_patch_hash=h,
                    request_key="commit-key-a4")


def test_a4_newly_mandatory_validate_profile_refuses_the_commit(tmp_path: Path) -> None:
    from tests.conftest import profile
    from tests.unit._edit_fx import to_approval

    q = profile(source_inputs=[{"kind": "file", "path": "rtl/pkg.sv"},
                               {"kind": "file", "path": "rtl/top.sv"}],
                top_modules=["top"], include_dirs=["rtl"])
    ws = make_ws(tmp_path, extra_profiles={"q": q})
    base = ws.build()
    before = ws.read("rtl/pkg.sv")
    tid, h = to_approval(ws, base, [ws.range_edit("rtl/pkg.sv", *TASK)], serena=False)
    _edit_manifest(ws, lambda m: m["editing"].__setitem__("validate_profiles", ["p", "q"]))
    e = err(_commit, ws, tid, h)
    assert e.code in (ErrorCode.EDIT_CONFLICT, ErrorCode.POLICY_DENIED)
    assert e.details["reason"] == "validate_profiles_changed" and e.details["missing"] == ["q"]
    assert ws.read("rtl/pkg.sv") == before


def test_a4_source_root_made_read_only_after_approval_refuses_the_commit(ws: EditWs) -> None:
    from tests.unit._edit_fx import to_approval

    base = ws.build()
    before = ws.read("rtl/pkg.sv")
    tid, h = to_approval(ws, base, [ws.range_edit("rtl/pkg.sv", *TASK)], serena=False)

    def ro(m: dict[str, Any]) -> None:
        for r in m["source_roots"]:
            if r["id"] == "rtl":
                r["writable"] = False

    _edit_manifest(ws, ro)
    e = err(_commit, ws, tid, h)
    assert e.code is ErrorCode.POLICY_DENIED and e.details["reason"] == "root_not_writable"
    assert ws.read("rtl/pkg.sv") == before


def test_a4_policy_inputs_are_bound_into_the_approval(ws: EditWs) -> None:
    from svweave.application.edit import _profiles_binding
    from svweave.editing.store import TxnStore
    from tests.unit._edit_fx import to_approval

    base = ws.build()
    tid, _ = to_approval(ws, base, [ws.range_edit("rtl/pkg.sv", *TASK)], serena=False)

    def binding() -> dict[str, Any]:
        with ws.db() as db:
            return _profiles_binding(TxnStore(db).get(tid), ws.w.ctx()).as_dict()

    b0 = binding()
    assert "edit_policy_hash" in b0

    def ro(m: dict[str, Any]) -> None:
        for r in m["source_roots"]:
            if r["id"] == "rtl":
                r["writable"] = False

    _edit_manifest(ws, ro)
    assert binding()["edit_policy_hash"] != b0["edit_policy_hash"]


# ------------------------------------------------------------------------------------ A5
def _gc_world(tmp_path: Path, n: int) -> Any:
    import shutil

    from svweave.snapshot.capture import snapshots_dir
    from svweave.storage.db import Database
    from tests.conftest import FIXTURES, Ws
    from tests.unit.test_storage import make_snaps

    shutil.copytree(FIXTURES / "filelists" / "proj", tmp_path / "gcws")
    ws = Ws(tmp_path / "gcws")
    ws.policy()
    snaps = make_snaps(ws, n)
    db = Database(tmp_path / "state" / "svweave.db")
    db.upsert_workspace("t", {"project": "project"}, "0" * 64)
    for s in snaps:
        db.record_snapshot(s)
    return db, snapshots_dir(ws.ctx()), [s.snapshot_id for s in snaps]


def test_a5_snapshots_in_profile_builds_of_open_transactions_are_retained(tmp_path: Path) -> None:
    import json

    from svweave.storage.db import plan_gc

    db, sdir, ids = _gc_world(tmp_path, 6)
    rec = {"profile_builds": {"q": {"baseline_snapshot_id": ids[0], "candidate_snapshot_id": ids[1],
                                    "baseline_build_id": "x", "candidate_build_id": "y"}}}
    with db.transaction() as c:
        c.execute(
            "INSERT INTO edit_transactions(transaction_id, base_snapshot_id, state, record) "
            "VALUES ('t1', ?, 'AWAITING_APPROVAL', ?)", (ids[5], json.dumps(rec)))
        c.execute(
            "INSERT INTO edit_transactions(transaction_id, base_snapshot_id, state, record) "
            "VALUES ('t2', ?, 'ABORTED', ?)", (ids[2], json.dumps(
                {"profile_builds": {"q": {"baseline_snapshot_id": ids[2]}}})))
    plan = plan_gc(db, sdir, keep_last=1)
    assert ids[0] not in plan.delete and ids[1] not in plan.delete
    assert "pending_edit" in plan.keep[ids[0]] and "pending_edit" in plan.keep[ids[1]]
    assert ids[2] in plan.delete  # a terminal transaction protects nothing


def test_a5_the_plan_is_recomputed_under_the_exclusive_lock(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import contextlib

    from svweave.storage import db as dbmod

    db, sdir, ids = _gc_world(tmp_path, 5)
    victim = dbmod.plan_gc(db, sdir, keep_last=1).delete[0]
    real = dbmod.workspace_lock

    @contextlib.contextmanager
    def racing(*a: Any, **kw: Any) -> Any:  # a prepare lands after the first plan, before the lock
        with real(*a, **kw):
            pass
        with db.transaction() as c:
            c.execute(
                "INSERT INTO edit_transactions(transaction_id, base_snapshot_id, state) "
                "VALUES ('late', ?, 'CANDIDATE_READY')", (victim,))
        with real(*a, **kw) as lk:
            yield lk

    monkeypatch.setattr(dbmod, "workspace_lock", racing)
    plan = dbmod.run_gc(db, sdir, dry_run=False, keep_last=1)
    assert victim not in plan.deleted
    assert (sdir / victim / "snapshot.json").is_file()
