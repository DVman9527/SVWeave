"""Regression tests for the independent M1 review (C1, C2, M1-M7, N*, surviving mutants)."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

import pytest
from click.testing import CliRunner

from svweave.application import doctor as doctor_mod
from svweave.cli import cli
from svweave.config.context import WorkspaceContext
from svweave.config.models import LocalConfig, Manifest, Policy
from svweave.config.paths import PathPolicy
from svweave.config.policy import check_policy_tightening
from svweave.config.trusted import (
    check_state_dir,
    config_home,
    trusted_dir,
    workspace_key,
)
from svweave.domain.errors import ErrorCode, SvwError
from svweave.domain.ids import compute_build_id, sha256_hex
from svweave.manifest.resolver import resolve_profile
from svweave.snapshot.capture import capture_snapshot, check_freshness, snapshots_dir
from svweave.storage.db import Database, plan_gc, run_gc
from svweave.storage.lock import WorkspaceBusy, workspace_lock
from tests.conftest import POLICY, Ws, manifest_dict, profile

SRC = [{"kind": "file", "path": "rtl/a.sv"}]


def expect(code: ErrorCode, fn: Any, *a: Any, **k: Any) -> SvwError:
    with pytest.raises(SvwError) as ei:
        fn(*a, **k)
    assert ei.value.code == code, ei.value
    return ei.value


def nm_for(ws: Ws, **prof: Any) -> Any:
    ws.config({"p": profile(**prof)})
    return resolve_profile(ws.ctx(), "p", {})


# ============================================================ C1: trusted policy outside repo
def test_C1_policy_default_location_is_xdg_workspace_key(
    ws: Ws, _isolated_config_home: Path
) -> None:
    import hashlib

    key = hashlib.sha256(os.path.realpath(ws.root).encode()).hexdigest()[:16]
    assert workspace_key(ws.root) == key
    want = _isolated_config_home / "svweave" / "workspaces" / key
    assert trusted_dir(ws.root) == want == ws.ctx().trusted_dir
    assert ws.policy_path == want / "policy.json"
    assert config_home() == _isolated_config_home


def test_C1_config_home_falls_back_to_home_dot_config(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.delenv("XDG_CONFIG_HOME")
    monkeypatch.setenv("HOME", str(tmp_path))
    assert config_home() == tmp_path / ".config"
    monkeypatch.setenv("XDG_CONFIG_HOME", "relative/not/absolute")  # relative XDG is ignored
    assert config_home() == tmp_path / ".config"


def test_C1_repo_committed_policy_is_never_adopted(ws: Ws) -> None:
    ws.policy_path.unlink()
    ws.write(
        ".svweave/policy.json",
        json.dumps(
            {
                **POLICY,
                "allowed_roots": [str(ws.root)],
                "editing_enabled": True,
                "writable_paths": ["**"],
            }
        ),
    )
    e = expect(ErrorCode.INVALID_CONFIG, ws.ctx)  # no trusted policy => refuse, not fall back
    assert "svweave init" in e.message
    out = CliRunner().invoke(cli, ["--root", str(ws.root), "--compact", "doctor"])
    data = json.loads(out.stdout)
    assert data["error"]["code"] == "INVALID_CONFIG"
    # with a valid trusted policy, doctor warns that the repo-local file is ignored
    ws.policy()
    data = json.loads(
        CliRunner().invoke(cli, ["--root", str(ws.root), "--compact", "doctor"]).stdout
    )
    assert any("IGNORED" in w for w in data["warnings"])
    assert ws.ctx().policy.editing_enabled is False


def test_C1_policy_override_inside_project_tree_rejected(ws: Ws, tmp_path: Path) -> None:
    inside = ws.root / "sneaky_policy.json"
    inside.write_text(json.dumps({**POLICY, "allowed_roots": [str(ws.root)]}))
    expect(ErrorCode.POLICY_DENIED, WorkspaceContext.load, ws.root, policy_path=inside)
    # also via a symlink living outside the tree but pointing into it
    link = tmp_path / "link_policy.json"
    os.symlink(inside, link)
    expect(ErrorCode.POLICY_DENIED, WorkspaceContext.load, ws.root, policy_path=link)
    # CLI: exit 6
    res = CliRunner().invoke(
        cli, ["--root", str(ws.root), "--compact", "--policy", str(inside), "manifest", "resolve"]
    )
    assert res.exit_code == 6 and json.loads(res.stdout)["error"]["code"] == "POLICY_DENIED"
    # init refuses to write the policy into the tree as well
    res = CliRunner().invoke(
        cli, ["--root", str(ws.root), "--compact", "--policy", str(ws.root / "p2.json"), "init"]
    )
    assert res.exit_code == 6 and not (ws.root / "p2.json").exists()
    # an override outside the tree works
    ok = tmp_path / "outside" / "policy.json"
    ok.parent.mkdir()
    ok.write_text(json.dumps({**POLICY, "allowed_roots": [str(ws.root)]}))
    assert WorkspaceContext.load(ws.root, policy_path=ok).workspace_id == "t"


def test_C1_doctor_and_context_load_the_same_policy_file_N4(ws: Ws) -> None:
    inside = ws.root / "p.json"
    inside.write_text(json.dumps({**POLICY, "allowed_roots": [str(ws.root)]}))
    res = CliRunner().invoke(
        cli, ["--root", str(ws.root), "--compact", "--policy", str(inside), "doctor"]
    )
    data = json.loads(res.stdout)
    pol = next(c for c in data["data"]["checks"] if c["name"] == "policy")
    assert pol["status"] == "FAIL" and pol["code"] == "POLICY_DENIED"  # same verdict as ctx
    assert res.exit_code == 6
    # a broken trusted policy fails doctor and ctx identically
    ws.policy_path.write_text("{broken")
    expect(ErrorCode.INVALID_CONFIG, ws.ctx)
    data = json.loads(
        CliRunner().invoke(cli, ["--root", str(ws.root), "--compact", "doctor"]).stdout
    )
    assert next(c for c in data["data"]["checks"] if c["name"] == "policy")["status"] == "FAIL"


def test_C1_policy_roots_must_be_absolute() -> None:
    from pydantic import ValidationError

    for bad in (".", "src", "~/x"):
        with pytest.raises(ValidationError):
            Policy.model_validate({**POLICY, "allowed_roots": [bad]})


def test_C1_state_dir_symlink_or_foreign_owner_rejected(
    ws: Ws, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    check_state_dir(tmp_path / "does-not-exist")  # absent is fine
    real = tmp_path / "elsewhere"
    real.mkdir()
    (ws.root / ".svweave").mkdir(exist_ok=True)
    (ws.root / ".svweave").rmdir()
    os.symlink(real, ws.root / ".svweave")
    expect(ErrorCode.POLICY_DENIED, ws.ctx)
    res = CliRunner().invoke(cli, ["--root", str(ws.root), "--compact", "init"])
    assert res.exit_code == 6 and not any(real.iterdir())
    os.unlink(ws.root / ".svweave")
    (ws.root / ".svweave").mkdir()
    other_uid = os.geteuid() + 1
    monkeypatch.setattr("svweave.config.trusted.os.geteuid", lambda: other_uid)
    expect(ErrorCode.POLICY_DENIED, check_state_dir, ws.root / ".svweave")


def test_C1_policy_file_owner_must_be_current_user(ws: Ws, monkeypatch: pytest.MonkeyPatch) -> None:
    other_uid = os.geteuid() + 1
    monkeypatch.setattr("svweave.config.loader.os.geteuid", lambda: other_uid)
    expect(ErrorCode.POLICY_DENIED, ws.ctx)


# ============================================================ C2: project.root cannot widen roots
def test_C2_project_root_leaving_manifest_dir_denied(
    ws: Ws, tmp_path_factory: pytest.TempPathFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    # keep the trusted dir outside the (widened) tree so the project.root rule is what fires
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path_factory.mktemp("xdg2")))
    secret = ws.root.parent / "secret.sv"
    secret.write_text("// host secret outside repo\n")
    m = manifest_dict({"p": profile(source_inputs=[{"kind": "file", "path": "secret.sv"}])})
    m["project"]["root"] = ".."
    import yaml

    ws.write("svweave.yaml", yaml.safe_dump(m, sort_keys=False))
    ws.policy()  # allowed_roots == [ws.root] only
    e = expect(ErrorCode.POLICY_DENIED, ws.ctx)
    assert "project.root" in e.message
    # even when the policy lists the parent, only an EXACT listing opens it; a broader or
    # unrelated root does not
    ws.policy(allowed_roots=[str(ws.root.parent.parent)])
    expect(ErrorCode.POLICY_DENIED, ws.ctx)
    ws.policy(allowed_roots=[str(ws.root.parent)])  # explicit, trusted decision
    assert ws.ctx().project_root == ws.root.parent.resolve()


def test_C2_project_root_inside_manifest_dir_is_fine(ws: Ws) -> None:
    ws.write("sub/rtl/a.sv")
    import yaml

    m = manifest_dict({"p": profile(source_inputs=[{"kind": "file", "path": "rtl/a.sv"}])})
    m["project"]["root"] = "sub"
    ws.write("svweave.yaml", yaml.safe_dump(m, sort_keys=False))
    assert ws.ctx().project_root == (ws.root / "sub").resolve()


# ============================================================ M1: re-recorded snapshot is newest
def test_M1_recaptured_snapshot_gets_new_seq_and_survives_gc(ws: Ws, tmp_path: Path) -> None:
    ctx = ws.ctx()
    db = Database(tmp_path / "db.sqlite")
    db.upsert_workspace("t", {}, "h")
    ws.config({"p": profile(source_inputs=SRC)})
    original = (ws.root / "rtl/a.sv").read_text()
    ids = []
    for i in range(5):
        ws.write("rtl/a.sv", original if i == 0 else f"module a; // v{i}\nendmodule\n")
        rec = capture_snapshot(ws.ctx(), resolve_profile(ws.ctx(), "p", {}))
        db.record_snapshot(rec)
        ids.append(rec.snapshot_id)
    ws.write("rtl/a.sv", original)  # workspace is back at snapshot 0
    again = capture_snapshot(ws.ctx(), resolve_profile(ws.ctx(), "p", {}))
    assert again.snapshot_id == ids[0]
    db.record_snapshot(again)
    seqs = dict(db.conn.execute("SELECT snapshot_id, seq FROM snapshots").fetchall())
    assert seqs[ids[0]] == max(seqs.values())
    plan = plan_gc(db, snapshots_dir(ctx))
    assert ids[0] not in plan.delete and "recent_successful" in plan.keep[ids[0]]
    assert plan.delete == [ids[2], ids[1]]


# ============================================================ M2: filelist bytes verified
def test_M2_filelist_changed_between_resolve_and_capture_is_unstable(ws: Ws) -> None:
    nm = nm_for(ws, source_inputs=[{"kind": "filelist", "path": "lists/main.f"}])
    assert set(nm.filelist_hashes) == {"lists/main.f"}
    assert nm.filelist_hashes["lists/main.f"] == sha256_hex((ws.root / "lists/main.f").read_bytes())
    ws.write("rtl/c.sv", "module c; endmodule\n")
    p = ws.root / "lists/main.f"
    p.write_text(p.read_text() + "rtl/c.sv\n")
    e = expect(ErrorCode.WORKSPACE_UNSTABLE, capture_snapshot, ws.ctx(), nm)
    assert e.details["changed"] == ["lists/main.f"]
    assert list(snapshots_dir(ws.ctx()).iterdir()) == []  # nothing published, no tmp leftovers
    # re-resolving heals it
    nm2 = resolve_profile(ws.ctx(), "p", {})
    assert capture_snapshot(ws.ctx(), nm2).config_id == nm2.config_id


def test_M2_generated_input_changed_after_resolve_is_unstable(ws: Ws) -> None:
    ws.write("gen/g.sv", "module g; endmodule\n")
    nm = nm_for(ws, generated_inputs=[{"path": "gen/g.sv"}])
    ws.write("gen/g.sv", "module g2; endmodule\n")
    expect(ErrorCode.WORKSPACE_UNSTABLE, capture_snapshot, ws.ctx(), nm)


def test_M2_filelist_hash_per_nested_file(ws: Ws) -> None:
    ws.write("lists/top.f", "-f lists/sub/nested.f\n")
    nm = nm_for(ws, source_inputs=[{"kind": "filelist", "path": "lists/top.f"}])
    assert set(nm.filelist_hashes) == {"lists/top.f", "lists/sub/nested.f"}


# ============================================================ M3: source-dir lookup
def test_M3_header_next_to_source_is_captured_and_tracked(ws: Ws) -> None:
    ws.write("rtl/defs.svh", "`define X 1\n")
    ws.write("rtl/a.sv", '`include "defs.svh"\nmodule a; endmodule\n')
    nm = nm_for(ws, source_inputs=SRC)
    rec = capture_snapshot(ws.ctx(), nm)
    by = {f.logical_path: f.role for f in rec.files}
    assert by["rtl/defs.svh"] == "include_lookup"
    lk = next(x for x in rec.lookup if x.logical_dir == "rtl")
    assert lk.recursive is False and "defs.svh" in lk.entries
    sdir = snapshots_dir(ws.ctx()) / rec.snapshot_id
    assert (sdir / "files/rtl/defs.svh").read_text() == "`define X 1\n"
    ws.write("rtl/defs.svh", "`define X 2\n")
    res = check_freshness(ws.ctx(), rec)
    assert res.freshness == "STALE" and ("rtl/defs.svh", "modified") in {
        (c.path, c.change) for c in res.changed
    }
    # a NEW header next to the source (shadowing a -I one) invalidates too
    ws.write("rtl/shadow.svh", "`define S 1\n")
    assert ("rtl/shadow.svh", "lookup_added") in {
        (c.path, c.change) for c in check_freshness(ws.ctx(), rec).changed
    }


def test_M3_source_dir_is_searched_before_include_dirs(ws: Ws) -> None:
    nm = nm_for(ws, source_inputs=SRC, include_dirs=["inc_b"])
    rec = capture_snapshot(ws.ctx(), nm)
    assert [(x.logical_dir, x.recursive) for x in rec.lookup] == [("rtl", False), ("inc_b", True)]
    # source directories are not recursive: nested dirs of rtl/ are not swept in
    assert not any(f.logical_path.startswith("rtl/with space/") for f in rec.files)


# ============================================================ M4: symlink target obeys exclude
def test_M4_symlink_to_excluded_target_rejected(ws: Ws) -> None:
    ws.write("out/secret.sv", "module s; endmodule\n")  # '**/out/**' is excluded
    os.symlink("../out/secret.sv", ws.root / "rtl/link.sv")
    ws.config({"p": profile(source_inputs=[{"kind": "file", "path": "rtl/link.sv"}])})
    ctx = ws.ctx()
    expect(ErrorCode.PATH_NOT_ALLOWED, ctx.resolve, "out/secret.sv", ctx.project_root)  # direct
    e = expect(ErrorCode.PATH_NOT_ALLOWED, ctx.resolve, "rtl/link.sv", ctx.project_root)
    assert "excluded" in e.details["reason"] or "excluded" in e.message
    expect(ErrorCode.PATH_NOT_ALLOWED, resolve_profile, ctx, "p", {})


def test_M4_symlink_target_outside_include_rules_rejected(ws: Ws) -> None:
    ws.config(
        {"p": profile(source_inputs=SRC)},
        source_roots=[{"id": "project", "path": ".", "include": ["rtl/**", "lists/**"]}],
    )
    os.symlink("../rel.sv", ws.root / "rtl/inc_link.sv")  # rel.sv is not in include rules
    ctx = ws.ctx()
    expect(ErrorCode.PATH_NOT_ALLOWED, ctx.resolve, "rtl/inc_link.sv", ctx.project_root)
    # rules=False (cwd resolution) is unaffected
    assert ctx.resolve("rtl/inc_link.sv", ctx.project_root, rules=False).symlink_target == "rel.sv"


def test_M4_enumerated_symlink_into_excluded_dir_rejected(ws: Ws) -> None:
    ws.write("out/s.svh", "`define S 1\n")
    os.symlink("../../out/s.svh", ws.root / "inc_b/s.svh")
    nm = nm_for(ws, source_inputs=SRC, include_dirs=["inc_b"])
    expect(ErrorCode.PATH_NOT_ALLOWED, capture_snapshot, ws.ctx(), nm)


# ============================================================ M5: only COMPLETE builds publish
def _snap_and_db(ws: Ws, tmp_path: Path) -> tuple[Database, Any, Any]:
    db = Database(tmp_path / "db.sqlite")
    db.upsert_workspace("t", {}, "h")
    nm = nm_for(ws, source_inputs=SRC)
    rec = capture_snapshot(ws.ctx(), nm)
    db.record_snapshot(rec)
    return db, rec, nm


def _bd(rec: Any, nm: Any, n: int, completeness: str) -> dict[str, Any]:
    tc = sha256_hex("tc")
    return {
        "build_id": compute_build_id(rec.snapshot_id, sha256_hex(f"c{n}"), tc),
        "workspace_id": "t",
        "profile": "p",
        "snapshot_id": rec.snapshot_id,
        "config_id": nm.config_id,
        "toolchain_id": tc,
        "semantic_schema_version": "1",
        "completeness": completeness,
        "diagnostic_outcome": "NO_ERRORS" if completeness == "COMPLETE" else "NOT_AVAILABLE",
    }


def test_M5_partial_and_blocked_builds_are_stored_but_never_published(
    ws: Ws, tmp_path: Path
) -> None:
    db, rec, nm = _snap_and_db(ws, tmp_path)
    good = _bd(rec, nm, 1, "COMPLETE")
    assert db.stage_build(good) is True
    for n, comp in ((2, "BLOCKED"), (3, "PARTIAL")):
        bad = _bd(rec, nm, n, comp)
        assert db.stage_build(bad) is False
        stored = db.conn.execute(
            "SELECT completeness FROM builds WHERE build_id=?", (bad["build_id"],)
        ).fetchone()
        assert stored["completeness"] == comp  # addressable by build_id
        assert db.published("t", "p")["build_id"] == good["build_id"]  # type: ignore[index]
    # first-ever build being PARTIAL leaves the pointer empty
    other = _bd(rec, nm, 9, "PARTIAL")
    other["profile"] = "other"
    assert db.stage_build(other) is False and db.published("t", "other") is None
    # defense in depth: the low-level publish refuses too
    with pytest.raises(SvwError) as ei, db.transaction() as c:
        Database._publish(c, "t", "p", _bd(rec, nm, 2, "BLOCKED")["build_id"])
    assert ei.value.code == ErrorCode.PARTIAL_ANALYSIS


# ============================================================ M6: workspace flock
def test_M6_lock_modes(tmp_path: Path) -> None:
    with workspace_lock(tmp_path):
        with workspace_lock(tmp_path):  # shared + shared
            pass
        with pytest.raises(WorkspaceBusy), workspace_lock(tmp_path, exclusive=True, blocking=False):
            pass
    with (
        workspace_lock(tmp_path, exclusive=True, blocking=False),
        pytest.raises(WorkspaceBusy),
        workspace_lock(tmp_path, blocking=False),
    ):
        pass
    assert (tmp_path / "lock").is_file()


def test_M6_gc_during_capture_does_not_delete_live_tmp_dir(ws: Ws, tmp_path: Path) -> None:
    ctx = ws.ctx()
    db = Database(ctx.state_dir / "svweave.db")
    nm = nm_for(ws, source_inputs=SRC)
    sdir = snapshots_dir(ws.ctx())
    seen: list[Any] = []

    def hook(stage: str, attempt: int) -> None:
        plan = run_gc(db, sdir, dry_run=False)
        seen.append(
            (plan.skipped_reason, [p.name for p in sdir.iterdir() if p.name.startswith(".tmp-")])
        )

    rec = capture_snapshot(ws.ctx(), nm, _hook=hook)
    assert rec.snapshot_id and seen[0][0] == "workspace_busy" and len(seen[0][1]) == 1
    # with no live holder GC works normally and reports no skip
    (sdir / ".tmp-stale").mkdir()
    plan = run_gc(db, sdir, dry_run=False)
    assert plan.skipped_reason is None and not (sdir / ".tmp-stale").exists()


def test_M6_busy_gc_keeps_snapshots_too(ws: Ws, tmp_path: Path) -> None:
    ctx = ws.ctx()
    db = Database(ctx.state_dir / "svweave.db")
    db.upsert_workspace("t", {}, "h")
    ws.config({"p": profile(source_inputs=SRC)})
    ids = []
    for i in range(5):
        ws.write("rtl/a.sv", f"module a; // {i}\nendmodule\n")
        r = capture_snapshot(ws.ctx(), resolve_profile(ws.ctx(), "p", {}))
        db.record_snapshot(r)
        ids.append(r.snapshot_id)
    sdir = snapshots_dir(ws.ctx())
    with workspace_lock(ctx.state_dir):
        plan = run_gc(db, sdir, dry_run=False)
    assert plan.skipped_reason == "workspace_busy" and plan.deleted == []
    assert all((sdir / i).is_dir() for i in ids)
    assert plan.to_dict()["skipped_reason"] == "workspace_busy"


# ============================================================ M7: retention_refs and evidence by build
def test_M7_gc_honours_retention_refs_and_build_linked_evidence(ws: Ws, tmp_path: Path) -> None:
    ctx = ws.ctx()
    db = Database(tmp_path / "db.sqlite")
    db.upsert_workspace("t", {}, "h")
    ws.config({"p": profile(source_inputs=SRC)})
    ids = []
    for i in range(6):
        ws.write("rtl/a.sv", f"module a; // g{i}\nendmodule\n")
        r = capture_snapshot(ws.ctx(), resolve_profile(ws.ctx(), "p", {}))
        db.record_snapshot(r)
        ids.append(r.snapshot_id)
    nm = resolve_profile(ws.ctx(), "p", {})
    old_rec = type("R", (), {"snapshot_id": ids[0]})
    b = _bd(old_rec, nm, 1, "COMPLETE")
    db.stage_build(b, publish=False)
    with db.transaction() as c:
        c.execute(
            "INSERT INTO evidence(evidence_id, kind, producer, build_id) "
            "VALUES ('e1','EXTERNAL_DECLARED','x',?)",
            (b["build_id"],),
        )
    db.add_retention_ref(ids[1], "audit-42")
    db.add_retention_ref(ids[1], "audit-42")  # idempotent
    assert json.loads(
        db.conn.execute(
            "SELECT retention_refs FROM snapshots WHERE snapshot_id=?", (ids[1],)
        ).fetchone()[0]
    ) == ["audit-42"]
    plan = plan_gc(db, snapshots_dir(ctx))
    assert plan.keep[ids[0]] == ["evidence"] and plan.keep[ids[1]] == ["retention_ref"]
    assert plan.delete == [ids[2]]
    with db.transaction() as c:  # unparseable refs fail closed
        c.execute("UPDATE snapshots SET retention_refs='not json' WHERE snapshot_id=?", (ids[2],))
    assert ids[2] not in plan_gc(db, snapshots_dir(ctx)).delete
    expect(ErrorCode.MISSING_INPUT, db.add_retention_ref, "0" * 64, "x")


# ============================================================ N2 / N3 / N7 / exit codes / libext
def test_N2_build_id_validates_inputs_and_is_unambiguous() -> None:
    s, c, t = sha256_hex("s"), sha256_hex("c"), sha256_hex("t")
    for bad in ("", "abc", "G" * 64, s.upper(), s + "0"):
        expect(ErrorCode.INVALID_CONFIG, compute_build_id, bad, c, t)
        expect(ErrorCode.INVALID_CONFIG, compute_build_id, s, bad, t)
        expect(ErrorCode.INVALID_CONFIG, compute_build_id, s, c, bad)
    expect(ErrorCode.INVALID_CONFIG, compute_build_id, s, c, t, "1:2")
    expect(ErrorCode.INVALID_CONFIG, compute_build_id, s, c, t, "")
    assert compute_build_id(s, c, t) == sha256_hex(f"{s}:{c}:{t}:1")  # explicit separator


def test_N3_unexpected_exception_becomes_internal_envelope_exit_4(
    ws: Ws, monkeypatch: pytest.MonkeyPatch
) -> None:
    def boom(*a: Any, **k: Any) -> None:
        raise RuntimeError("secret detail /etc/shadow")

    monkeypatch.setattr("svweave.application.use_cases.resolve_profile", boom)
    res = CliRunner().invoke(cli, ["--root", str(ws.root), "--compact", "manifest", "resolve"])
    assert res.exit_code == 4
    env = json.loads(res.stdout)  # stdout stays a valid envelope
    assert env["outcome"] == "ERROR" and env["error"]["code"] == "INTERNAL"
    assert "Traceback" not in res.stdout and "shadow" not in res.stdout
    assert "RuntimeError" in env["error"]["message"]
    assert "Traceback" in res.stderr and "shadow" in res.stderr  # kept on stderr only


def test_WORKSPACE_UNSTABLE_maps_to_exit_5() -> None:
    from svweave.domain.errors import exit_code_for

    assert exit_code_for(ErrorCode.WORKSPACE_UNSTABLE) == 5
    assert exit_code_for(ErrorCode.INTERNAL) == 4


def test_N7_internal_invariants_raise_svwerror_not_assert(ws: Ws, tmp_path: Path) -> None:
    ws.config(
        {"p": profile()},
        source_roots=[{"id": "project", "path": ".", "exclude": ["u/**"]}, {"id": "uvm"}],
    )
    ctx = ws.ctx()
    uvm = next(r for r in ctx.roots if r.id == "uvm")
    from svweave.config.context import _abs

    expect(ErrorCode.MISSING_INPUT, _abs, uvm)  # was `assert root.abs is not None`
    expect(ErrorCode.MISSING_INPUT, ctx._logical, ctx.project_root, uvm)
    db = Database(tmp_path / "x.db")
    expect(ErrorCode.INVALID_CONFIG, db.stage_build, {}, {"bad name!": []})


def test_libext_and_incdir_empty_tokens_rejected(ws: Ws) -> None:
    for body in ("+libext+\n", "+incdir+\n"):
        ws.write("lists/x.f", "rtl/a.sv\n" + body)
        ws.config({"p": profile(source_inputs=[{"kind": "filelist", "path": "lists/x.f"}])})
        expect(ErrorCode.INVALID_CONFIG, resolve_profile, ws.ctx(), "p", {})


# ============================================================ surviving mutants
def test_X5_include_search_order_alone_changes_snapshot_id(ws: Ws) -> None:
    """Both dirs hold only files that are already captured as explicit sources, so the file
    inventory is identical; only the lookup search-order inventory can tell the snapshots apart."""
    ws.write("d1/x.sv", "module x; endmodule\n")
    ws.write("d2/y.sv", "module y; endmodule\n")
    srcs = [{"kind": "file", "path": "d1/x.sv"}, {"kind": "file", "path": "d2/y.sv"}]
    a = nm_for(ws, source_inputs=srcs, include_dirs=["d1", "d2"])
    ra = capture_snapshot(ws.ctx(), a)
    b = nm_for(ws, source_inputs=srcs, include_dirs=["d2", "d1"])
    rb = capture_snapshot(ws.ctx(), b)
    assert [f.logical_path for f in ra.files] == [f.logical_path for f in rb.files]
    assert [(f.sha256, f.size) for f in ra.files] == [(f.sha256, f.size) for f in rb.files]
    assert ra.snapshot_id != rb.snapshot_id


def test_X5_lookup_listing_alone_changes_snapshot_id_via_directory_membership(ws: Ws) -> None:
    ws.write("d1/x.sv", "module x; endmodule\n")
    nm = nm_for(ws, source_inputs=[{"kind": "file", "path": "d1/x.sv"}], include_dirs=["d1"])
    r1 = capture_snapshot(ws.ctx(), nm)
    r2 = capture_snapshot(ws.ctx(), nm)
    assert r1.snapshot_id == r2.snapshot_id
    ws.write("d1/new.svh", "`define N 1\n")
    assert capture_snapshot(ws.ctx(), nm).snapshot_id != r1.snapshot_id


def test_X9_tightening_rejects_local_mapping_outside_policy_directly(
    ws: Ws, tmp_path: Path
) -> None:
    manifest = Manifest.model_validate(
        manifest_dict(
            {"p": profile()}, source_roots=[{"id": "project", "path": "."}, {"id": "ext"}]
        )
    )
    pol = Policy.model_validate({**POLICY, "allowed_roots": [str(ws.root)]})
    outside = tmp_path / "elsewhere"
    outside.mkdir()
    bad = LocalConfig.model_validate(
        {"schema_version": "1.0", "root_mappings": [{"id": "ext", "path": str(outside)}]}
    )
    e = expect(ErrorCode.PATH_NOT_ALLOWED, check_policy_tightening, manifest, bad, pol, ws.root)
    assert e.details["root"] == "ext"
    good = LocalConfig.model_validate(
        {"schema_version": "1.0", "root_mappings": [{"id": "ext", "path": "sub"}]}
    )
    check_policy_tightening(manifest, good, pol, ws.root)


def test_X10_path_policy_check_rejects_symlink_escape_directly(tmp_path: Path) -> None:
    root = tmp_path / "r"
    root.mkdir()
    outside = tmp_path / "o"
    outside.mkdir()
    (outside / "f.sv").write_text("x")
    os.symlink(outside, root / "dirlink")
    os.symlink(outside / "f.sv", root / "filelink")
    pp = PathPolicy([root])
    for raw in ("dirlink/f.sv", "filelink", "dirlink"):
        expect(ErrorCode.PATH_NOT_ALLOWED, pp.check, raw, root)
    # '..' through an in-tree symlink: lexically inside, physically outside
    os.symlink(outside, root / "hop")
    expect(ErrorCode.PATH_NOT_ALLOWED, pp.check, "hop/../r/dirlink/f.sv", root)


def test_doctor_registry_still_pluggable_after_fixes() -> None:
    assert doctor_mod.CHECKS and workspace_key(Path("/tmp")) != workspace_key(Path("/var"))


# ============================================================ N1 / N5 / N6 / N8 / N9 (round 2)
def test_N1_bare_libext_is_not_silently_dropped(ws: Ws) -> None:
    ws.write("lists/x.f", "rtl/a.sv\n+libext+\n")
    ws.config({"p": profile(source_inputs=[{"kind": "filelist", "path": "lists/x.f"}])})
    e = expect(ErrorCode.INVALID_CONFIG, resolve_profile, ws.ctx(), "p", {})
    assert "empty +libext+" in e.message and e.details["origin"]["line"] == 2


def test_N5_same_library_dir_with_different_libext_in_two_groups(ws: Ws) -> None:
    ws.write("libs/lib_a/only_vh.vh", "module only_vh; endmodule\n")
    grp = lambda gid, exts: {  # noqa: E731
        "id": gid,
        "sources": [{"kind": "file", "path": "rtl/a.sv"}],
        "library_dirs": ["libs/lib_a"],
        "library_extensions": exts,
    }
    nm = nm_for(
        ws,
        source_inputs=[],
        compilation_units={
            "mode": "explicit_groups",
            "groups": [grp("g1", [".sv"]), grp("g2", [".vh"])],
        },
    )
    rec = capture_snapshot(ws.ctx(), nm)
    libs = [(x.logical_dir, x.exts, x.entries) for x in rec.lookup if x.kind == "library"]
    assert libs == [
        ("libs/lib_a", [".sv"], ["libmod.sv"]),
        ("libs/lib_a", [".vh"], ["only_vh.vh"]),
    ]  # second group's extension set is not lost to the first group's
    assert "libs/lib_a/only_vh.vh" in {f.logical_path for f in rec.files}
    ws.write("libs/lib_a/new.vh", "module n; endmodule\n")
    res = check_freshness(ws.ctx(), rec)
    assert ("libs/lib_a/new.vh", "lookup_added") in {(c.path, c.change) for c in res.changed}
    ws.write("libs/lib_a/new.sv", "module n2; endmodule\n")
    res2 = check_freshness(ws.ctx(), rec)
    assert ("libs/lib_a/new.sv", "lookup_added") in {(c.path, c.change) for c in res2.changed}


@pytest.mark.skipif(os.geteuid() == 0, reason="root ignores directory permissions")
def test_N6_unreadable_lookup_dir_raises_instead_of_dropping_subtree(ws: Ws) -> None:
    ws.write("inc_b/secret_sub/h.svh", "`define H 1\n")
    nm = nm_for(ws, source_inputs=SRC, include_dirs=["inc_b"])
    sub = ws.root / "inc_b" / "secret_sub"
    sub.chmod(0)
    try:
        e = expect(ErrorCode.MISSING_INPUT, capture_snapshot, ws.ctx(), nm)
    finally:
        sub.chmod(0o755)
    assert e.details["reason"] == "dir_unreadable" and e.details["path"].endswith("secret_sub")
    assert list(snapshots_dir(ws.ctx()).iterdir()) == []
    assert capture_snapshot(ws.ctx(), nm).snapshot_id  # readable again => works


def test_N6_listdir_failure_in_flat_lookup_dirs_also_raises(
    ws: Ws, monkeypatch: pytest.MonkeyPatch
) -> None:
    nm = nm_for(ws, source_inputs=SRC)
    real = os.listdir

    def flaky(p: Any = ".") -> list[str]:
        if str(p).endswith("/rtl"):
            raise PermissionError(13, "Permission denied")
        return real(p)

    monkeypatch.setattr("svweave.snapshot.capture.os.listdir", flaky)
    e = expect(ErrorCode.MISSING_INPUT, capture_snapshot, ws.ctx(), nm)
    assert e.details["reason"] == "dir_unreadable"


def test_N8_external_writable_root_checked_at_mapped_location(ws: Ws) -> None:
    ws.write("vendor/uvm/pkg.sv")
    roots = [
        {"id": "project", "path": ".", "exclude": ["vendor/**"]},
        {"id": "uvm", "writable": True},
    ]
    ws.local([{"id": "uvm", "path": "vendor/uvm"}])
    ws.config({"p": profile()}, source_roots=roots)
    # policy covers the mapped location 'vendor/uvm', not a path named after the id
    ws.policy(writable_paths=["vendor/**"])
    assert ws.ctx().roots[1].writable
    # a policy that only covers "uvm/**" (the id read as a project path) must NOT pass
    ws.policy(writable_paths=["uvm/**"])
    expect(ErrorCode.POLICY_DENIED, ws.ctx)
    ws.policy(writable_paths=[])
    expect(ErrorCode.POLICY_DENIED, ws.ctx)
    # an unmapped external root exposes nothing, so nothing to deny
    (ws.root / "svweave.local.yaml").unlink()
    ws.policy(writable_paths=[])
    assert ws.ctx().roots[1].abs is None


def test_N8_external_root_outside_project_uses_absolute_location(
    ws: Ws, tmp_path_factory: pytest.TempPathFactory
) -> None:
    ext = tmp_path_factory.mktemp("extroot")
    (ext / "a.sv").write_text("module e; endmodule\n")
    ws.config(
        {"p": profile()},
        source_roots=[{"id": "project", "path": "."}, {"id": "uvm", "writable": True}],
    )
    ws.local([{"id": "uvm", "path": str(ext)}])
    ws.policy(allowed_roots=[str(ws.root.resolve()), str(ext.resolve())], writable_paths=["**"])
    # '**' (relative globs) does match absolute strings, so it covers; a narrower one must not
    assert ws.ctx().roots[1].writable
    ws.policy(allowed_roots=[str(ws.root.resolve()), str(ext.resolve())], writable_paths=["rtl/**"])
    expect(ErrorCode.POLICY_DENIED, ws.ctx)
    ws.policy(
        allowed_roots=[str(ws.root.resolve()), str(ext.resolve())],
        writable_paths=[f"{ext.resolve().as_posix()}/**"],
    )
    assert ws.ctx().roots[1].writable


def test_N9_hardlinked_file_flagged_in_inventory_and_helper(ws: Ws, tmp_path: Path) -> None:
    from svweave.config.paths import is_hardlinked

    os.link(ws.root / "rtl/a.sv", ws.root / "rtl/a_hard.sv")
    os.symlink(ws.root / "rtl/a.sv", ws.root / "rtl/a_sym.sv")
    nm = nm_for(
        ws,
        source_inputs=[
            {"kind": "file", "path": "rtl/a.sv"},
            {"kind": "file", "path": "rtl/a_hard.sv"},
            {"kind": "file", "path": "rtl/a_sym.sv"},
            {"kind": "file", "path": "rtl/b.sv"},
        ],
    )
    rec = capture_snapshot(ws.ctx(), nm)
    flags = {f.logical_path: f.hardlinked for f in rec.files}
    assert flags["rtl/a.sv"] and flags["rtl/a_hard.sv"] and flags["rtl/a_sym.sv"]
    assert flags["rtl/b.sv"] is False
    sdir = snapshots_dir(ws.ctx()) / rec.snapshot_id
    assert not is_hardlinked(sdir / "files/rtl/a.sv")  # the snapshot copy is its own inode
    assert is_hardlinked(ws.root / "rtl/a.sv") and is_hardlinked(ws.root / "rtl/a_sym.sv")
    assert not is_hardlinked(ws.root / "rtl/b.sv")
    assert not is_hardlinked(ws.root / "rtl")  # directories never count
    assert not is_hardlinked(ws.root / "missing.sv")
    # the flag survives the JSON record
    from svweave.snapshot.capture import load_record

    assert load_record(sdir).files[0].hardlinked is True
