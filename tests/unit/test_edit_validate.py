"""Validate (plan 12.4): EDT-06, EDT-09, EDT-10 (candidate level), diagnostics delta, limits."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import pytest

from svweave.application import edit as E
from svweave.domain.errors import ErrorCode, SvwError
from svweave.domain.ids import canonical_hash
from tests.unit._edit_fx import BAD_TOP, EditWs, make_ws, prepared, tamper_candidate

TASK = ('"task hello"', '"task hello 2"')


@pytest.fixture
def ws(tmp_path: Path) -> EditWs:
    return make_ws(tmp_path)


def go(ws: EditWs, edits: list[dict[str, Any]], **kw: Any) -> tuple[str, Any]:
    base = ws.build()
    e = prepared(ws, base, edits, serena=False, **kw)
    tid = e.data["transaction_id"]
    return tid, base


def validate(ws: EditWs, tid: str) -> Any:
    return E.validate(ws.opts, "r-val", transaction_id=tid)


def state(ws: EditWs, tid: str) -> str:
    return next(r["state"] for r in ws.txn_rows() if r["transaction_id"] == tid)


def test_valid_candidate_reaches_awaiting_approval_with_full_evidence(ws: EditWs) -> None:
    base = ws.build()
    tid, _ = go(ws, [ws.range_edit("rtl/pkg.sv", *TASK)])
    v = validate(ws, tid)
    assert v.error is None and v.data["state"] == "AWAITING_APPROVAL"
    assert v.data["validation"]["ok"] and v.data["validation"]["auto_committable"]
    [f] = v.data["files_changed"]
    assert f["logical_path"] == "rtl/pkg.sv" and f["added_lines"] == 1 and f["removed_lines"] == 1
    assert len(f["before_sha256"]) == 64 and f["before_sha256"] != f["after_sha256"]
    assert f["before_sha256"] == __import__("hashlib").sha256(ws.read("rtl/pkg.sv")).hexdigest()
    assert v.data["diff"].startswith("--- a/rtl/pkg.sv")
    d = v.data["diagnostics_delta"]["p"]
    assert d["errors"] == [0, 0] and d["new_warnings"] == []
    assert v.data["not_run"] == {"eda_compile": "NOT_RUN", "simulation": "NOT_RUN", "formal": "NOT_RUN"}
    assert any("NOT_RUN" in w for w in v.warnings) and "approve" in v.data["approval_command"]
    # patch hash = sha256(canonical patch record)
    row = next(r for r in ws.txn_rows() if r["transaction_id"] == tid)
    assert row["patch_hash"] == v.data["patch_hash"] and len(row["patch_hash"]) == 64
    assert row["candidate_snapshot_id"] and row["candidate_snapshot_id"] != base.snapshot_id
    # both builds exist; the candidate build did NOT move the published pointer
    with ws.db() as db:
        n = db.conn.execute("SELECT COUNT(*) FROM builds").fetchone()[0]
        pub = db.conn.execute("SELECT build_id FROM published_pointer").fetchone()[0]
    assert n == 2 and pub == base.build_id
    # the original is still untouched
    assert b'"task hello"' in ws.read("rtl/pkg.sv")


def test_patch_hash_is_deterministic_and_covers_the_bytes(ws: EditWs) -> None:
    base = ws.build()
    hashes = []
    for i, new in enumerate(('"task hello 2"', '"task hello 2"', '"task hello 3"')):
        e = prepared(ws, base, [ws.range_edit("rtl/pkg.sv", '"task hello"', new)], serena=False,
                     key=f"det-key-{i:04d}")
        hashes.append(validate(ws, e.data["transaction_id"]).data["patch_hash"])
    assert hashes[0] == hashes[1] != hashes[2]
    assert hashes[0] != canonical_hash("x")


def test_validate_is_replayable_and_state_gated(ws: EditWs) -> None:
    tid, _ = go(ws, [ws.range_edit("rtl/pkg.sv", *TASK)])
    v1 = validate(ws, tid)
    v2 = validate(ws, tid)
    assert v2.data["idempotent_replay"] and v2.data["patch_hash"] == v1.data["patch_hash"]


# ------------------------------------------------------------------------------ EDT-06 / EDT-10
def reasons(v: Any) -> set[str]:
    return {f["reason"] for f in v.data["validation"]["failures"]}


def test_edt06_extra_file_changed_in_candidate_is_rejected_and_diff_kept(ws: EditWs) -> None:
    tid, _ = go(ws, [ws.range_edit("rtl/pkg.sv", *TASK)])
    tamper_candidate(ws, tid, "rtl/other.sv", lambda b: b + b"// surprise\n")
    v = validate(ws, tid)
    assert v.error and v.error.code == ErrorCode.POLICY_DENIED.value
    assert state(ws, tid) == "VALIDATION_FAILED"
    assert {"undeclared_file_changed", "candidate_modified_after_prepare"} <= reasons(v)
    assert v.data["diff"], "the diff stays reviewable (EDT-09)"
    assert v.data["validation"]["auto_committable"] is False
    assert ws.read("rtl/other.sv").startswith(b"// other file")


@pytest.mark.parametrize("what", ["new_file", "delete", "rename", "symlink", "binary", "hardlink"])
def test_edt10_candidate_with_new_deleted_renamed_binary_or_linked_files(
    ws: EditWs, what: str, tmp_path: Path
) -> None:
    tid, _ = go(ws, [ws.range_edit("rtl/pkg.sv", *TASK)])
    cand = ws.root / ".svweave/candidates" / tid / "rtl"
    if what == "new_file":
        (cand / "new.sv").write_text("module n; endmodule\n")
        want = "file_added"
    elif what == "delete":
        (cand / "other.sv").unlink()
        want = "file_removed"
    elif what == "rename":
        os.rename(cand / "other.sv", cand / "renamed.sv")
        want = "file_removed"
    elif what == "symlink":
        (cand / "other.sv").unlink()
        os.symlink("pkg.sv", cand / "other.sv")
        want = "symlink_or_special_file"
    elif what == "binary":
        (cand / "top.sv").write_bytes(b"\x00\x01\x02binary")
        want = "binary_file"
    else:
        os.link(cand / "top.sv", tmp_path / "alias.sv")
        (cand / "top.sv").write_text(ws.text("rtl/top.sv") + "// x\n")  # keeps the link? no: rewrite
        os.unlink(cand / "top.sv")
        os.link(tmp_path / "alias.sv", cand / "top.sv")
        with open(cand / "top.sv", "ab") as f:
            f.write(b"// hardlinked change\n")
        want = "hardlinked_file"
    v = validate(ws, tid)
    assert v.error is not None and state(ws, tid) == "VALIDATION_FAILED"
    assert want in reasons(v), reasons(v)
    # nothing in the original workspace changed
    assert b'"task hello"' in ws.read("rtl/pkg.sv") and ws.read("rtl/other.sv").startswith(b"// other")
    assert not (ws.root / "rtl" / "new.sv").exists() and not (ws.root / "rtl" / "renamed.sv").exists()


def test_candidate_edit_outside_allowed_paths_is_rejected(tmp_path: Path) -> None:
    ws2 = make_ws(tmp_path, editing={"allowed_paths": ["rtl/pkg.sv"]})
    tid, _ = go(ws2, [ws2.range_edit("rtl/pkg.sv", *TASK)])
    tamper_candidate(ws2, tid, "rtl/top.sv", lambda b: b + b"// x\n")
    v = validate(ws2, tid)
    assert "path_not_allowed" in reasons(v) or "undeclared_file_changed" in reasons(v)
    assert state(ws2, tid) == "VALIDATION_FAILED"


def test_limits_on_files_and_changed_lines(tmp_path: Path) -> None:
    ws2 = make_ws(tmp_path, editing={"max_changed_lines": 3, "max_files_per_patch": 1})
    base = ws2.build()
    big = ws2.range_edit("rtl/pkg.sv", '"task hello"',
                         '"a"); $display("1");\n$display("2");\n$display("3");\n$display("4"')
    with pytest.raises(SvwError) as ei:  # refused already at prepare
        prepared(ws2, base, [big], serena=False)
    assert ei.value.code is ErrorCode.POLICY_DENIED
    assert "too_many_changed_lines" in str(ei.value.details["failures"])
    two = [ws2.range_edit("rtl/pkg.sv", *TASK), ws2.range_edit("rtl/other.sv", "module other", "module other2")]
    with pytest.raises(SvwError) as ei2:
        prepared(ws2, base, two, serena=False, key="two-files-0001")
    assert "too_many_files" in str(ei2.value.details["failures"])
    # ... and again at validate time if the candidate grows afterwards
    e = prepared(ws2, base, [ws2.range_edit("rtl/pkg.sv", *TASK)], serena=False, key="grow-key-0001")
    tid = e.data["transaction_id"]
    tamper_candidate(ws2, tid, "rtl/pkg.sv", lambda b: b + b"// 1\n// 2\n// 3\n// 4\n")
    v = validate(ws2, tid)
    assert "too_many_changed_lines" in reasons(v)
    assert v.error.code == ErrorCode.POLICY_DENIED.value  # type: ignore[union-attr]


def test_empty_patch_is_rejected_at_prepare(ws: EditWs) -> None:
    base = ws.build()
    with pytest.raises(SvwError) as ei:
        prepared(ws, base, [ws.range_edit("rtl/pkg.sv", '"task hello"', '"task hello"')], serena=False)
    assert "empty_patch" in str(ei.value.details["failures"])


# ------------------------------------------------------------------------------ EDT-09
def test_edt09_candidate_introducing_an_error_fails_but_keeps_the_diff(ws: EditWs) -> None:
    tid, _ = go(ws, [ws.range_edit("rtl/top.sv", "q <= q + 4'd1;", "q <= q + nosuchsig;")])
    v = validate(ws, tid)
    assert state(ws, tid) == "VALIDATION_FAILED"
    assert v.error and v.error.code == ErrorCode.PARTIAL_ANALYSIS.value
    assert "candidate_not_clean" in reasons(v) and "nosuchsig" in v.data["diff"]
    assert v.data["diagnostics_delta"]["p"]["errors"][0] == 0
    assert v.data["diagnostics_delta"]["p"]["errors"][1] >= 1


def test_edt09_new_non_waived_warning_fails(ws: EditWs) -> None:
    tid, _ = go(ws, [ws.range_edit("rtl/top.sv", "q <= q + 4'd1;", "begin if (clk); q <= q + 4'd1; end")])
    v = validate(ws, tid)
    assert "new_warnings" in reasons(v) and state(ws, tid) == "VALIDATION_FAILED"
    assert v.data["diagnostics_delta"]["p"]["new_warnings"][0]["code"] == "EmptyBody"


def test_edt09_partial_baseline_profile_is_not_auto_committable_but_diff_returned(
    tmp_path: Path,
) -> None:
    from tests.conftest import profile

    broken = profile(
        source_inputs=[{"kind": "file", "path": "rtl/pkg.sv"}, {"kind": "file", "path": "rtl/top.sv"},
                       {"kind": "file", "path": "rtl/missing_dep.sv"}],
        top_modules=["top"], include_dirs=["rtl"],
    )
    ws2 = make_ws(tmp_path, extra_profiles={"broken": broken},
                  editing={"validate_profiles": ["p", "broken"]})
    (ws2.root / "rtl" / "missing_dep.sv").write_text("module dep(; endmodule\n")  # syntax error
    base = ws2.build("p")
    e = prepared(ws2, base, [ws2.range_edit("rtl/pkg.sv", *TASK)], serena=False,
                 profiles=["p", "broken"])
    v = validate(ws2, e.data["transaction_id"])
    assert v.error is not None and state(ws2, e.data["transaction_id"]) == "VALIDATION_FAILED"
    assert "baseline_not_clean" in reasons(v)
    assert v.data["validation"]["auto_committable"] is False and v.data["validation"]["baseline_partial"]
    assert v.data["diff"].startswith("--- a/rtl/pkg.sv")
    # approval is impossible for it
    with pytest.raises(Exception) as ei:
        E.approve(ws2.opts, "r", transaction_id=e.data["transaction_id"],
                  patch_hash=v.data["patch_hash"], confirm=lambda i: E.Confirmation("x", "t"))
    assert getattr(ei.value, "code", None) is ErrorCode.APPROVAL_REQUIRED


def test_baseline_partial_single_profile_cannot_even_prepare(tmp_path: Path) -> None:
    bad = make_ws(tmp_path, top=BAD_TOP)
    base = bad.build()
    with pytest.raises(Exception) as ei:
        prepared(bad, base, [bad.range_edit("rtl/pkg.sv", *TASK)], serena=False)
    assert ei.value.code is ErrorCode.PARTIAL_ANALYSIS  # type: ignore[attr-defined]


def test_edit_not_covered_by_a_validate_profile_fails_closed(tmp_path: Path) -> None:
    from tests.conftest import profile

    small = profile(source_inputs=[{"kind": "file", "path": "ro/lib.sv"}], top_modules=["lib"])
    ws2 = make_ws(tmp_path, extra_profiles={"small": small},
                  editing={"validate_profiles": ["p", "small"]})
    base = ws2.build()
    e = prepared(ws2, base, [ws2.range_edit("rtl/pkg.sv", *TASK)], serena=False,
                 profiles=["p", "small"])
    v = validate(ws2, e.data["transaction_id"])
    assert "profile_does_not_cover_edit" in reasons(v)


def test_stale_workspace_at_validate_time_sets_conflict(ws: EditWs) -> None:
    tid, _ = go(ws, [ws.range_edit("rtl/pkg.sv", *TASK)])
    (ws.root / "rtl" / "top.sv").write_text(ws.text("rtl/top.sv") + "// human\n")
    with pytest.raises(Exception) as ei:
        validate(ws, tid)
    assert ei.value.code is ErrorCode.STALE_SNAPSHOT  # type: ignore[attr-defined]
    assert state(ws, tid) == "CONFLICT"
