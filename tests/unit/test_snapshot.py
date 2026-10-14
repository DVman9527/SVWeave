from __future__ import annotations

import os
import shutil
import stat
import subprocess
from pathlib import Path
from typing import Any

import pytest

from svweave.domain.errors import ErrorCode, SvwError
from svweave.domain.ids import is_sha256_hex
from svweave.manifest.resolver import resolve_profile
from svweave.snapshot import capture as cap
from svweave.snapshot.capture import (
    capture_snapshot,
    check_freshness,
    load_record,
    remove_snapshot_dir,
    snapshots_dir,
)
from tests.conftest import FIXTURES, Ws, profile

SRC = [{"kind": "file", "path": "rtl/a.sv"}]


def nm_for(ws: Ws, **prof: Any) -> Any:
    ws.config({"p": profile(**prof)})
    return resolve_profile(ws.ctx(), "p", {})


def snap(ws: Ws, nm: Any, **kw: Any) -> Any:
    return capture_snapshot(ws.ctx(), nm, **kw)


def lk(rec: Any, d: str) -> Any:
    return next(x for x in rec.lookup if x.logical_dir == d)


def changes(res: Any) -> set[tuple[str, str]]:
    return {(c.path, c.change) for c in res.changed}


def test_snapshot_layout_immutability_and_no_hardlinks(ws: Ws) -> None:
    nm = nm_for(
        ws, source_inputs=[{"kind": "filelist", "path": "lists/main.f"}], include_dirs=["inc_b"]
    )
    rec = snap(ws, nm)
    assert is_sha256_hex(rec.snapshot_id)
    sdir = snapshots_dir(ws.ctx()) / rec.snapshot_id
    assert (sdir / "snapshot.json").is_file()
    assert load_record(sdir) == rec
    by = {f.logical_path: f for f in rec.files}
    f = by["rtl/a.sv"]
    copy = sdir / f.snapshot_path
    orig = ws.root / "rtl" / "a.sv"
    assert f.snapshot_path == "files/rtl/a.sv" and f.original_path == str(orig)
    assert copy.read_bytes() == orig.read_bytes()
    st_c, st_o = copy.stat(), orig.stat()
    assert st_c.st_ino != st_o.st_ino and st_c.st_nlink == 1  # copied, not hardlinked
    assert stat.S_IMODE(st_c.st_mode) == 0o444
    assert stat.S_IMODE(sdir.stat().st_mode) == 0o555
    if os.geteuid() != 0:
        with pytest.raises(PermissionError):
            copy.write_text("x")
        with pytest.raises(PermissionError):
            (sdir / "files" / "new.sv").write_text("x")
    # later edits to the original never reach the snapshot
    orig.write_text("module changed; endmodule\n")
    assert copy.read_text() != orig.read_text()
    # filelist, sources and the include lookup files are all in the inventory
    assert by["lists/main.f"].role == "filelist"
    assert by["rtl/with space/sp.sv"].role == "source"
    assert by["inc_b/common.svh"].role == "include_lookup"
    assert "out/ignored.sv" not in by
    assert rec.attempts == 1 and rec.workspace_id == "t" and rec.config_id == nm.config_id
    # removing a frozen snapshot works
    remove_snapshot_dir(sdir)
    assert not sdir.exists()
    remove_snapshot_dir(sdir)  # idempotent


def test_capture_is_idempotent_and_relocatable(ws: Ws, tmp_path: Path) -> None:
    nm = nm_for(ws, source_inputs=SRC, include_dirs=["inc_b"])
    r1 = snap(ws, nm)
    r2 = snap(ws, nm)
    assert r1.snapshot_id == r2.snapshot_id
    dirs = [p.name for p in snapshots_dir(ws.ctx()).iterdir()]
    assert dirs == [r1.snapshot_id]  # no temp leftovers, no duplicates
    # same content at another location -> same ids (host paths are not part of identity)
    other = Ws(tmp_path / "moved")
    shutil.copytree(ws.root, other.root, ignore=shutil.ignore_patterns(".svweave"))
    other.policy()
    nm2 = resolve_profile(other.ctx(), "p", {})
    assert nm2.config_id == nm.config_id
    assert snap(other, nm2).snapshot_id == r1.snapshot_id


def test_SNP_01_modified_included_header_makes_snapshot_stale(ws: Ws) -> None:
    nm = nm_for(ws, source_inputs=SRC, include_dirs=["inc_b"])
    rec = snap(ws, nm)
    ctx = ws.ctx()
    res = check_freshness(ctx, rec)
    assert (
        res.freshness == "CURRENT"
        and res.changed == []
        and res.current_snapshot_id == rec.snapshot_id
    )
    ws.write("inc_b/common.svh", "`define COMMON_FROM_B 3\n")
    res = check_freshness(ctx, rec)
    assert res.freshness == "STALE"
    assert changes(res) == {("inc_b/common.svh", "modified")}
    assert res.current_snapshot_id != rec.snapshot_id
    assert snap(ws, nm).snapshot_id != rec.snapshot_id
    # an edit that keeps the size identical is still detected (content hash, not mtime/size)
    ws.write("inc_b/common.svh", "`define COMMON_FROM_B 4\n")
    ws.write("inc_b/common.svh", "`define COMMON_FROM_B 5\n")
    assert check_freshness(ctx, rec).freshness == "STALE"


def test_SNP_02_new_higher_priority_header_invalidates_even_if_original_unchanged(ws: Ws) -> None:
    nm = nm_for(ws, source_inputs=SRC, include_dirs=["inc_a", "inc_b"])
    rec = snap(ws, nm)
    before_b = (ws.root / "inc_b" / "common.svh").read_bytes()
    ws.write("inc_a/common.svh", "`define COMMON_FROM_A 1\n")
    assert (ws.root / "inc_b" / "common.svh").read_bytes() == before_b  # untouched
    res = check_freshness(ws.ctx(), rec)
    assert res.freshness == "STALE"
    assert ("inc_a/common.svh", "lookup_added") in changes(res)
    assert ("inc_a/common.svh", "added") in changes(res)
    assert not any(p == "inc_b/common.svh" for p, _ in changes(res))
    new = snap(ws, nm)
    assert new.snapshot_id != rec.snapshot_id
    lk = {x.logical_dir: x.entries for x in new.lookup}
    assert "common.svh" in lk["inc_a"] and "common.svh" in lk["inc_b"]
    # deleting the candidate again is also a change (negative lookup restored)
    (ws.root / "inc_a" / "common.svh").unlink()
    res2 = check_freshness(ws.ctx(), new)
    assert ("inc_a/common.svh", "lookup_removed") in changes(res2)
    assert ("inc_a/common.svh", "removed") in changes(res2)
    assert check_freshness(ws.ctx(), rec).freshness == "CURRENT"


def test_SNP_02_lookup_inventory_covers_search_space_even_when_empty_dir(ws: Ws) -> None:
    nm = nm_for(ws, source_inputs=SRC, include_dirs=["inc_a", "inc_b"])
    rec = snap(ws, nm)
    assert [(x.kind, x.logical_dir) for x in rec.lookup] == [
        ("include", "rtl"),  # directory of the source file: searched before the -I dirs
        ("include", "inc_a"),
        ("include", "inc_b"),
    ]
    assert lk(rec, "inc_a").entries == [".gitkeep"]


def test_SNP_02_nested_include_subdir_and_excluded_dirs(ws: Ws) -> None:
    ws.write("inc_b/sub/deep.svh", "`define D 1\n")
    ws.write("inc_b/out/skip.svh", "`define S 1\n")  # excluded by **/out/**
    nm = nm_for(ws, source_inputs=SRC, include_dirs=["inc_b"])
    rec = snap(ws, nm)
    assert lk(rec, "inc_b").entries == ["common.svh", "sub/deep.svh"]
    assert "inc_b/out/skip.svh" not in {f.logical_path for f in rec.files}
    ws.write("inc_b/out/skip.svh", "`define S 2\n")  # excluded content does not matter
    assert check_freshness(ws.ctx(), rec).freshness == "CURRENT"


def _git(root: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@example.com", *args],
        cwd=root,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


@pytest.mark.skipif(shutil.which("git") is None, reason="git not installed")
def test_SNP_03_uncommitted_and_untracked_bytes_used_not_git_sha(ws: Ws) -> None:
    _git(ws.root, "init", "-q")
    _git(ws.root, "add", "rtl", "lists")
    _git(ws.root, "commit", "-q", "-m", "base")
    head = _git(ws.root, "rev-parse", "HEAD")
    ws.write("lists/g1.f", "rtl/a.sv\n")
    nm1 = nm_for(ws, source_inputs=[{"kind": "filelist", "path": "lists/g1.f"}])
    s1 = snap(ws, nm1)
    # uncommitted edit of a tracked .sv, plus an untracked file referenced by the filelist
    (ws.root / "rtl" / "a.sv").write_text("module a; wire edited; endmodule\n")
    ws.write("rtl/untracked.sv", "module untracked; endmodule\n")
    ws.write("lists/g1.f", "rtl/a.sv\nrtl/untracked.sv\n")
    assert _git(ws.root, "rev-parse", "HEAD") == head  # Git SHA unchanged
    assert "rtl/untracked.sv" in _git(ws.root, "status", "--porcelain")
    nm2 = resolve_profile(ws.ctx(), "p", {})
    s2 = snap(ws, nm2)
    assert s2.snapshot_id != s1.snapshot_id
    sdir = snapshots_dir(ws.ctx()) / s2.snapshot_id
    assert (sdir / "files/rtl/a.sv").read_text() == "module a; wire edited; endmodule\n"
    assert (sdir / "files/rtl/untracked.sv").is_file()
    assert [f.role for f in s2.files if f.logical_path == "rtl/untracked.sv"] == ["source"]


def test_SNP_03_modified_sv_without_git(ws: Ws) -> None:
    nm = nm_for(ws, source_inputs=SRC)
    s1 = snap(ws, nm)
    (ws.root / "rtl" / "a.sv").write_text("module a; wire x; endmodule\n")
    res = check_freshness(ws.ctx(), s1)
    assert res.freshness == "STALE" and changes(res) == {("rtl/a.sv", "modified")}
    assert snap(ws, nm).snapshot_id != s1.snapshot_id


def test_freshness_removed_file_and_missing_dir(ws: Ws) -> None:
    nm = nm_for(ws, source_inputs=SRC, include_dirs=["inc_b"])
    rec = snap(ws, nm)
    (ws.root / "rtl" / "a.sv").unlink()
    shutil.rmtree(ws.root / "inc_b")
    res = check_freshness(ws.ctx(), rec)
    assert res.freshness == "STALE"
    assert ("rtl/a.sv", "removed") in changes(res)
    assert ("inc_b/common.svh", "removed") in changes(res)
    assert ("inc_b/common.svh", "lookup_removed") in changes(res)


# --------------------------------------------------------------------------- SNP-05
def test_SNP_05_continuous_modification_gives_workspace_unstable(ws: Ws) -> None:
    nm = nm_for(ws, source_inputs=SRC)
    calls: list[int] = []

    def mutate(event: str, attempt: int) -> None:
        calls.append(attempt)
        p = ws.root / "rtl" / "a.sv"
        p.write_text(f"module a; // writer {attempt}\nendmodule\n")

    with pytest.raises(SvwError) as ei:
        snap(ws, nm, max_retries=3, _hook=mutate)
    assert ei.value.code == ErrorCode.WORKSPACE_UNSTABLE
    assert ei.value.details["attempts"] == 3 and calls == [1, 2, 3]  # bounded
    assert ei.value.exit_code == 5  # stale/unstable family
    assert list(snapshots_dir(ws.ctx()).iterdir()) == []  # nothing mixed was published


def test_SNP_05_transient_modification_retries_then_consistent(ws: Ws) -> None:
    nm = nm_for(ws, source_inputs=SRC)

    def once(event: str, attempt: int) -> None:
        if attempt == 1:
            (ws.root / "rtl" / "a.sv").write_text("module a; // v2\nendmodule\n")

    rec = snap(ws, nm, _hook=once)
    assert rec.attempts == 2
    sdir = snapshots_dir(ws.ctx()) / rec.snapshot_id
    assert (sdir / "files/rtl/a.sv").read_text() == "module a; // v2\nendmodule\n"
    assert check_freshness(ws.ctx(), rec).freshness == "CURRENT"


def test_SNP_05_new_file_appearing_in_lookup_dir_during_capture(ws: Ws) -> None:
    nm = nm_for(ws, source_inputs=SRC, include_dirs=["inc_a"])

    def add(event: str, attempt: int) -> None:
        if attempt == 1:
            ws.write("inc_a/late.svh", "`define L 1\n")

    rec = snap(ws, nm, _hook=add)
    assert rec.attempts == 2 and "late.svh" in lk(rec, "inc_a").entries


def test_SNP_05_other_failure_cleans_temp_dir(ws: Ws, monkeypatch: pytest.MonkeyPatch) -> None:
    nm = nm_for(ws, source_inputs=SRC)
    monkeypatch.setattr(cap, "MAX_TOTAL_BYTES", 5)
    with pytest.raises(SvwError) as ei:
        snap(ws, nm)
    assert ei.value.code == ErrorCode.RESOURCE_LIMIT
    assert list(snapshots_dir(ws.ctx()).iterdir()) == []
    monkeypatch.setattr(cap, "MAX_TOTAL_BYTES", 1 << 30)
    monkeypatch.setattr(cap, "MAX_FILES", 0)
    with pytest.raises(SvwError) as ei2:
        snap(ws, nm)
    assert ei2.value.code == ErrorCode.RESOURCE_LIMIT


# --------------------------------------------------------------------------- symlinks / roots
def test_symlink_inside_roots_records_target_and_copies_bytes(ws: Ws) -> None:
    os.symlink(ws.root / "rtl" / "a.sv", ws.root / "rtl" / "alias.sv")
    nm = nm_for(ws, source_inputs=[{"kind": "file", "path": "rtl/alias.sv"}])
    rec = snap(ws, nm)
    f = rec.files[0]
    assert f.logical_path == "rtl/alias.sv" and f.symlink_target == "rtl/a.sv"
    sdir = snapshots_dir(ws.ctx()) / rec.snapshot_id
    assert not (sdir / f.snapshot_path).is_symlink()


def test_symlink_escape_in_include_dir_rejected(ws: Ws, tmp_path: Path) -> None:
    secret = tmp_path / "secret.svh"
    secret.write_text("`define SECRET 1\n")
    os.symlink(secret, ws.root / "inc_b" / "leak.svh")
    nm = nm_for(ws, source_inputs=SRC, include_dirs=["inc_b"])
    with pytest.raises(SvwError) as ei:
        snap(ws, nm)
    assert ei.value.code == ErrorCode.PATH_NOT_ALLOWED
    assert not list(snapshots_dir(ws.ctx()).glob("*/files/inc_b/leak.svh"))


def test_symlinked_dir_loop_and_escape_inside_include(ws: Ws, tmp_path: Path) -> None:
    os.symlink(ws.root / "inc_b", ws.root / "inc_b" / "loop")  # loop back to itself
    os.symlink(ws.root / "inc_a", ws.root / "inc_b" / "to_a")  # allowed (inside roots)
    nm = nm_for(ws, source_inputs=SRC, include_dirs=["inc_b"])
    rec = snap(ws, nm)
    assert "common.svh" in lk(rec, "inc_b").entries
    os.symlink(tmp_path, ws.root / "inc_b" / "outside")
    with pytest.raises(SvwError) as ei:
        snap(ws, nm)
    assert ei.value.code == ErrorCode.PATH_NOT_ALLOWED


def test_external_root_files_are_mounted_under_ext(ws: Ws) -> None:
    ext = ws.root / "uvm_src"
    ext.mkdir()
    (ext / "uvm_pkg.sv").write_text("package uvm_pkg; endpackage\n")
    (ext / "inc").mkdir()
    (ext / "inc" / "macros.svh").write_text("`define M 1\n")
    ws.config(
        {
            "p": profile(
                source_inputs=[{"kind": "file", "path": "@uvm/uvm_pkg.sv"}],
                include_dirs=["@uvm/inc"],
            )
        },
        source_roots=[{"id": "project", "path": ".", "exclude": ["uvm_src/**"]}, {"id": "uvm"}],
    )
    ws.local([{"id": "uvm", "path": "uvm_src"}])
    ctx = ws.ctx()
    nm = resolve_profile(ctx, "p", {})
    rec = capture_snapshot(ctx, nm)
    by = {f.logical_path: f for f in rec.files}
    assert by["@uvm/uvm_pkg.sv"].snapshot_path == "files/_ext/uvm/uvm_pkg.sv"
    assert by["@uvm/inc/macros.svh"].role == "include_lookup"
    sdir = snapshots_dir(ctx) / rec.snapshot_id
    assert (sdir / "files/_ext/uvm/inc/macros.svh").is_file()
    assert check_freshness(ctx, rec).freshness == "CURRENT"
    (ext / "inc" / "macros.svh").write_text("`define M 2\n")
    assert changes(check_freshness(ctx, rec)) == {("@uvm/inc/macros.svh", "modified")}
    assert rec.spec.root_ids == ["project", "uvm"]


def test_fixture_tree_exists() -> None:
    assert (FIXTURES / "filelists" / "proj" / "lists" / "main.f").is_file()
