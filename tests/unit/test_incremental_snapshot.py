"""SNP-INC: snapshot I/O optimization, not a replacement for native semantic acceptance."""

from __future__ import annotations

import errno
import fcntl
import os
import shutil
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

import pytest

from svweave.domain.errors import ErrorCode, SvwError
from svweave.manifest.resolver import resolve_profile
from svweave.snapshot import capture as cap
from svweave.snapshot import incremental as inc
from svweave.snapshot.models import SnapshotRecord
from tests.conftest import Ws, profile


def setup(ws: Ws) -> tuple[Any, SnapshotRecord]:
    ws.config({"p": profile(source_inputs=[{"kind": "file", "path": "rtl/a.sv"}],
                             include_dirs=["inc_a", "inc_b"])})
    nm = resolve_profile(ws.ctx(), "p", {})
    return nm, cap.capture_snapshot(ws.ctx(), nm)


def directory(ws: Ws, rec: SnapshotRecord) -> Path:
    return cap.snapshots_dir(ws.ctx()) / rec.snapshot_id


def make_writable(path: Path) -> None:
    for root, _dirs, files in os.walk(path):
        Path(root).chmod(0o755)
        for name in files:
            p = Path(root) / name
            if not p.is_symlink():
                p.chmod(0o644)


def fake_clone(src: int, dst: int) -> None:
    """Unit-only successful ioctl stand-in; never performance or integration evidence."""
    while chunk := os.read(src, 4096):
        os.write(dst, chunk)
    os.lseek(dst, 0, os.SEEK_SET)


def no_clone(src: int, dst: int) -> None:
    raise OSError(errno.EOPNOTSUPP, "unit fault: reflink unavailable")


def test_SNP_INC_01_noop_reuses_verified_tree_without_copy(ws: Ws) -> None:
    nm, old = setup(ws)
    new, stats = inc.capture_snapshot_incremental(ws.ctx(), nm, baseline=old)
    assert new.snapshot_id == old.snapshot_id
    assert new.config_id == nm.config_id
    assert stats.noop_snapshot_reused and stats.mode == "reused_existing"
    assert stats.files_copied == stats.bytes_copied == stats.files_cloned == 0
    assert stats.files_hashed == 2 * len(new.files)
    assert stats.snapshot_files_verified == len(new.files)
    assert stats.bytes_hashed == 2 * sum(f.size for f in new.files)
    assert not list(cap.snapshots_dir(ws.ctx()).glob(".tmp-*"))
    assert inc.verify_snapshot_tree(directory(ws, new), new)


def test_SNP_INC_02_cold_preserves_two_source_passes_and_legacy_identity(ws: Ws) -> None:
    nm, old = setup(ws)
    cap.remove_snapshot_dir(directory(ws, old))
    rec, stats = inc.capture_snapshot_incremental(ws.ctx(), nm)
    assert rec.snapshot_id == old.snapshot_id
    assert stats.mode == "full_copy" and stats.files_copied == len(rec.files)
    assert stats.files_hashed == 2 * len(rec.files)
    assert stats.bytes_copied == sum(f.size for f in rec.files)
    assert stats.fallback_reasons == ["no_baseline"]


@pytest.mark.parametrize("clone", [fake_clone, no_clone], ids=["clone-unit-double", "copy-fallback"])
def test_SNP_INC_03_changed_snapshot_matches_full_capture_and_keeps_old_bytes(
    ws: Ws, monkeypatch: pytest.MonkeyPatch, clone: Any,
) -> None:
    nm, old = setup(ws)
    before = {f.logical_path: (directory(ws, old) / f.snapshot_path).read_bytes() for f in old.files}
    ws.write("rtl/a.sv", "module a; wire changed; endmodule\n")
    monkeypatch.setattr(inc, "_clone_file", clone)
    new, stats = inc.capture_snapshot_incremental(ws.ctx(), nm, baseline=old)
    full = cap.capture_snapshot(ws.ctx(), nm)
    assert new.snapshot_id == full.snapshot_id != old.snapshot_id
    assert new.files == full.files and new.lookup == full.lookup
    assert inc.verify_snapshot_tree(directory(ws, new), new)
    for f in new.files:
        target = directory(ws, new) / f.snapshot_path
        original = Path(f.original_path)
        assert target.stat().st_nlink == 1 and target.stat().st_ino != original.stat().st_ino
        if f.logical_path in before:
            previous = directory(ws, old) / f.snapshot_path
            assert previous.read_bytes() == before[f.logical_path]
            assert target.stat().st_ino != previous.stat().st_ino
    assert stats.mode == "delta" and not stats.noop_snapshot_reused
    if clone is no_clone:
        assert stats.files_cloned == 0 and stats.files_copied == len(new.files)
        assert stats.reflink_attempts == 1 and stats.reflink_skipped > 0
        assert stats.reflink_supported is False
        assert "reflink_unavailable_errno_95" in stats.fallback_reasons
    else:
        assert stats.files_copied == 1 and stats.files_cloned == len(new.files) - 1


@pytest.mark.parametrize("mutation", ["removed", "renamed", "lookup_added"])
def test_SNP_INC_04_inventory_membership_matches_legacy(
    ws: Ws, monkeypatch: pytest.MonkeyPatch, mutation: str,
) -> None:
    nm, old = setup(ws)
    if mutation == "lookup_added":
        ws.write("inc_a/common.svh", "`define NEW_PRIORITY 1\n")
    elif mutation == "removed":
        (ws.root / "inc_b/common.svh").unlink()
    else:
        (ws.root / "inc_b/common.svh").rename(ws.root / "inc_b/renamed.svh")
    monkeypatch.setattr(inc, "_clone_file", no_clone)
    new, _ = inc.capture_snapshot_incremental(ws.ctx(), nm, baseline=old)
    assert new.snapshot_id == cap.capture_snapshot(ws.ctx(), nm).snapshot_id != old.snapshot_id
    assert inc.verify_snapshot_tree(directory(ws, new), new)
    if mutation in ("removed", "renamed"):
        assert not (directory(ws, new) / "files/inc_b/common.svh").exists()
        assert (directory(ws, old) / "files/inc_b/common.svh").exists()


@pytest.mark.parametrize("corruption", ["bytes", "extra_file", "extra_dir", "parent_link", "hardlink"])
def test_SNP_INC_05_corrupt_cached_snapshot_is_not_reused(
    ws: Ws, monkeypatch: pytest.MonkeyPatch, corruption: str,
) -> None:
    nm, old = setup(ws)
    root = directory(ws, old)
    make_writable(root)
    target = root / "files/inc_b/common.svh"
    original = target.read_bytes()
    if corruption == "bytes":
        target.write_bytes(b"x" * len(original))
    elif corruption == "extra_file":
        (root / "files/inc_b/planted.svh").write_text("`define PLANTED 1\n")
    elif corruption == "extra_dir":
        (root / "files/empty_extra").mkdir()
    elif corruption == "parent_link":
        shutil.rmtree(root / "files/inc_b")
        (root / "files/inc_b").symlink_to(ws.root / "inc_b", target_is_directory=True)
    else:
        target.unlink()
        os.link(ws.root / "inc_b/common.svh", target)
    assert not inc.verify_snapshot_tree(root, old)
    monkeypatch.setattr(inc, "_clone_file", fake_clone)
    new, stats = inc.capture_snapshot_incremental(ws.ctx(), nm, baseline=old)
    assert new.snapshot_id == old.snapshot_id and not stats.noop_snapshot_reused
    assert "snapshot_cache_invalid_rebuilt" in stats.fallback_reasons
    assert inc.verify_snapshot_tree(root, new)
    assert target.read_bytes() == original
    assert (ws.root / "inc_b/common.svh").read_bytes() == original


def test_SNP_INC_06_donor_hash_mismatch_cannot_contaminate_new_snapshot(
    ws: Ws, monkeypatch: pytest.MonkeyPatch,
) -> None:
    nm, old = setup(ws)
    make_writable(directory(ws, old))
    target = directory(ws, old) / "files/inc_b/common.svh"
    target.write_bytes(b"x" * target.stat().st_size)
    ws.write("rtl/a.sv", "module a; wire new_version; endmodule\n")
    monkeypatch.setattr(inc, "_clone_file", fake_clone)
    new, stats = inc.capture_snapshot_incremental(ws.ctx(), nm, baseline=old)
    assert "donor_content_mismatch" in stats.fallback_reasons
    assert new.snapshot_id == cap.capture_snapshot(ws.ctx(), nm).snapshot_id
    assert (directory(ws, new) / "files/inc_b/common.svh").read_bytes() == (
        ws.root / "inc_b/common.svh"
    ).read_bytes()


def test_SNP_INC_07_noop_race_retries_and_continuous_changes_are_bounded(ws: Ws) -> None:
    nm, old = setup(ws)

    def once(event: str, attempt: int) -> None:
        if event == "after_copy" and attempt == 1:
            ws.write("inc_a/late.svh", "`define LATE 1\n")

    new, stats = inc.capture_snapshot_incremental(ws.ctx(), nm, baseline=old, _hook=once)
    assert new.attempts == stats.attempts == 2
    assert "inc_a/late.svh" in {f.logical_path for f in new.files}

    def always(event: str, attempt: int) -> None:
        if event == "after_copy":
            ws.write("inc_a/late.svh", f"`define LATE {attempt + 10}\n")

    with pytest.raises(SvwError) as exc:
        inc.capture_snapshot_incremental(ws.ctx(), nm, baseline=new, _hook=always, max_retries=3)
    assert exc.value.code is ErrorCode.WORKSPACE_UNSTABLE
    assert not list(cap.snapshots_dir(ws.ctx()).glob(".tmp-*"))
    assert inc.verify_snapshot_tree(directory(ws, new), new)


def test_SNP_INC_08_first_hash_has_in_progress_total_byte_cap(
    ws: Ws, monkeypatch: pytest.MonkeyPatch,
) -> None:
    nm, old = setup(ws)
    ws.write("rtl/a.sv", "// pad\n" * 20_000)
    monkeypatch.setattr(cap, "MAX_TOTAL_BYTES", 4096)
    monkeypatch.setattr(cap, "_CHUNK", 1024)
    actual_hash = inc._hash_fd
    read_bytes: list[int] = []

    def counted_hash(
        fd: int, budget: int, stats: inc.CaptureStats, *, source: bool,
    ) -> tuple[str, int]:
        try:
            return actual_hash(fd, budget, stats, source=source)
        finally:
            if source:
                read_bytes.append(stats.bytes_hashed)

    monkeypatch.setattr(inc, "_hash_fd", counted_hash)
    with pytest.raises(SvwError) as exc:
        inc.capture_snapshot_incremental(ws.ctx(), nm, baseline=old)
    assert exc.value.code is ErrorCode.RESOURCE_LIMIT
    assert read_bytes and max(read_bytes) <= 4097
    assert not list(cap.snapshots_dir(ws.ctx()).glob(".tmp-*"))


def test_SNP_INC_09_loader_refuses_unsafe_or_invalid_metadata(ws: Ws) -> None:
    _, old = setup(ws)
    assert inc.load_baseline_record(ws.ctx(), old.snapshot_id) == old
    assert inc.load_baseline_record(ws.ctx(), "../outside") is None
    root = directory(ws, old)
    make_writable(root)
    metadata = root / "snapshot.json"
    metadata.unlink()
    metadata.symlink_to(ws.root / "svweave.yaml")
    assert inc.load_baseline_record(ws.ctx(), old.snapshot_id) is None


def test_SNP_INC_10_stat_preserving_dirty_edit_and_untracked_input_are_hashed(ws: Ws) -> None:
    nm, old = setup(ws)
    header = ws.root / "inc_b/common.svh"
    st = header.stat()
    original = header.read_bytes()
    header.write_bytes(original.replace(b"1", b"2") if b"1" in original else b"x" * len(original))
    os.utime(header, ns=(st.st_atime_ns, st.st_mtime_ns))
    ws.write("inc_a/untracked.svh", "`define UNTRACKED 1\n")
    new, _ = inc.capture_snapshot_incremental(ws.ctx(), nm, baseline=old)
    assert new.snapshot_id != old.snapshot_id
    assert new.snapshot_id == cap.capture_snapshot(ws.ctx(), nm).snapshot_id


def test_SNP_INC_11_changed_capture_enumerates_only_twice(
    ws: Ws, monkeypatch: pytest.MonkeyPatch,
) -> None:
    nm, old = setup(ws)
    ws.write("rtl/a.sv", "module a; wire next; endmodule\n")
    original = cap._enumerate
    calls = 0

    def counted(*args: Any, **kwargs: Any) -> Any:
        nonlocal calls
        calls += 1
        return original(*args, **kwargs)

    monkeypatch.setattr(cap, "_enumerate", counted)
    inc.capture_snapshot_incremental(ws.ctx(), nm, baseline=old)
    assert calls == 2


def test_SNP_INC_12_tampering_after_hash_does_not_pass_second_tree_check(
    ws: Ws, monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, old = setup(ws)
    root = directory(ws, old)
    original = inc._hash_fd
    tampered = False

    def tamper(fd: int, budget: int, stats: inc.CaptureStats, *, source: bool) -> tuple[str, int]:
        nonlocal tampered
        result = original(fd, budget, stats, source=source)
        if not source and not tampered:
            target = root / old.files[0].snapshot_path
            target.chmod(0o644)
            target.write_bytes(b"x" * target.stat().st_size)
            target.chmod(0o444)
            tampered = True
        return result

    monkeypatch.setattr(inc, "_hash_fd", tamper)
    assert not inc.verify_snapshot_tree(root, old)
    assert tampered


@pytest.mark.parametrize("legacy_peer", [False, True])
def test_SNP_INC_13_concurrent_same_identity_publication_has_one_valid_winner(
    ws: Ws, legacy_peer: bool,
) -> None:
    nm, old = setup(ws)
    ws.write("rtl/a.sv", "module a; wire concurrent; endmodule\n")
    barrier = threading.Barrier(2)

    def meet(event: str, attempt: int) -> None:
        if event == "after_copy":
            barrier.wait(timeout=10)

    def capture(legacy: bool) -> SnapshotRecord:
        if legacy:
            return cap.capture_snapshot(ws.ctx(), nm, _hook=meet)
        return inc.capture_snapshot_incremental(ws.ctx(), nm, baseline=old, _hook=meet)[0]

    with ThreadPoolExecutor(max_workers=2) as pool:
        one = pool.submit(capture, False)
        two = pool.submit(capture, legacy_peer)
        a, b = one.result(timeout=20), two.result(timeout=20)
    assert a.snapshot_id == b.snapshot_id != old.snapshot_id
    assert inc.verify_snapshot_tree(directory(ws, a), a)
    assert inc.verify_snapshot_tree(directory(ws, old), old)
    assert not list(cap.snapshots_dir(ws.ctx()).glob(".tmp-*"))


def test_SNP_INC_14_corrupt_cache_repair_holds_publication_lock(
    ws: Ws, monkeypatch: pytest.MonkeyPatch,
) -> None:
    nm, old = setup(ws)
    root = directory(ws, old)
    make_writable(root)
    (root / "files/planted.svh").write_text("planted\n")
    actual_remove = cap.remove_snapshot_dir
    checked = False

    def check_locked(path: Path) -> None:
        nonlocal checked
        if path == root:
            lock = os.open(ws.ctx().state_dir / "snapshot.publish.lock", os.O_RDWR)
            try:
                with pytest.raises(BlockingIOError):
                    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                checked = True
            finally:
                os.close(lock)
        actual_remove(path)

    monkeypatch.setattr(cap, "remove_snapshot_dir", check_locked)
    new, _ = inc.capture_snapshot_incremental(ws.ctx(), nm, baseline=old)
    assert checked and inc.verify_snapshot_tree(root, new)


def test_SNP_INC_15_source_change_between_inventory_and_copy_retries(ws: Ws) -> None:
    nm, old = setup(ws)
    ws.write("rtl/a.sv", "module a; wire initial; endmodule\n")

    def change(event: str, attempt: int) -> None:
        if event == "after_inventory" and attempt == 1:
            ws.write("rtl/a.sv", "module a; wire newer; endmodule\n")

    new, stats = inc.capture_snapshot_incremental(ws.ctx(), nm, baseline=old, _hook=change)
    assert stats.attempts == 2
    assert new.snapshot_id == cap.capture_snapshot(ws.ctx(), nm).snapshot_id


def test_SNP_INC_16_same_byte_symlink_retarget_keeps_identity_and_current_metadata(ws: Ws) -> None:
    ws.write("rtl/one.svh", "module alias; endmodule\n")
    ws.write("rtl/two.svh", "module alias; endmodule\n")
    alias = ws.root / "rtl/alias.sv"
    alias.symlink_to(ws.root / "rtl/one.svh")
    ws.config({"p": profile(source_inputs=[{"kind": "file", "path": "rtl/alias.sv"}])})
    nm = resolve_profile(ws.ctx(), "p", {})
    old = cap.capture_snapshot(ws.ctx(), nm)
    alias.unlink()
    alias.symlink_to(ws.root / "rtl/two.svh")
    new, stats = inc.capture_snapshot_incremental(ws.ctx(), nm, baseline=old)
    assert new.snapshot_id == old.snapshot_id and stats.noop_snapshot_reused
    assert next(f for f in new.files if f.logical_path == "rtl/alias.sv").symlink_target == "rtl/two.svh"
    assert next(f for f in old.files if f.logical_path == "rtl/alias.sv").symlink_target == "rtl/one.svh"


def test_SNP_INC_17_symlink_retarget_outside_root_blocked_before_directive_read(
    ws: Ws, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    alias = ws.root / "rtl/alias.sv"
    alias.symlink_to(ws.root / "rtl/a.sv")
    nm, old = setup(ws)
    outside = tmp_path / "outside.sv"
    outside.write_text("module not_allowed; endmodule\n")
    alias.unlink()
    alias.symlink_to(outside)
    real_read = Path.read_text

    def guarded(path: Path, *args: Any, **kwargs: Any) -> str:
        if path == alias:
            pytest.fail("directive scanner read the disallowed symlink target")
        return real_read(path, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", guarded)
    with pytest.raises(SvwError) as exc:
        inc.capture_snapshot_incremental(ws.ctx(), nm, baseline=old)
    assert exc.value.code is ErrorCode.PATH_NOT_ALLOWED
    assert inc.verify_snapshot_tree(directory(ws, old), old)


@pytest.mark.parametrize("fault", ["wrong_size", "wrong_hash"])
def test_SNP_INC_18_faulty_clone_destination_is_replaced_by_verified_source_copy(
    ws: Ws, monkeypatch: pytest.MonkeyPatch, fault: str,
) -> None:
    nm, old = setup(ws)
    ws.write("rtl/a.sv", "module a; wire changed; endmodule\n")

    def corrupt_clone(src: int, dst: int) -> None:
        size = os.fstat(src).st_size
        os.write(dst, b"x" * (size + 1 if fault == "wrong_size" else size))
        os.lseek(dst, 0, os.SEEK_SET)

    monkeypatch.setattr(inc, "_clone_file", corrupt_clone)
    new, stats = inc.capture_snapshot_incremental(ws.ctx(), nm, baseline=old)
    assert "donor_content_mismatch" in stats.fallback_reasons
    # A zero-byte file has no different same-size byte sequence; that clone is genuinely valid.
    assert stats.files_copied == sum(fault == "wrong_size" or f.size > 0 for f in new.files)
    assert new.snapshot_id == cap.capture_snapshot(ws.ctx(), nm).snapshot_id


def test_SNP_INC_19_scan_retarget_after_resolution_never_reads_outside_bytes(
    ws: Ws, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    nm, old = setup(ws)
    outside = tmp_path / "outside.sv"
    outside.write_text("module outside; endmodule\n")
    target = ws.root / "rtl/a.sv"
    actual_read = cap._read_scan_file
    retargeted = False

    def retarget(directory_fd: int, name: str) -> bytes:
        nonlocal retargeted
        if name == target.name and not retargeted:
            target.unlink()
            target.symlink_to(outside)
            retargeted = True
        return actual_read(directory_fd, name)

    monkeypatch.setattr(cap, "_read_scan_file", retarget)
    with pytest.raises(SvwError) as exc:
        inc.capture_snapshot_incremental(ws.ctx(), nm, baseline=old)
    assert exc.value.code is ErrorCode.POLICY_DENIED
    assert retargeted and inc.verify_snapshot_tree(directory(ws, old), old)


def test_SNP_INC_20_directive_scan_read_is_bounded_not_just_stat_checked(
    ws: Ws, monkeypatch: pytest.MonkeyPatch,
) -> None:
    setup(ws)
    ws.write("rtl/a.sv", "// growth\n" * 10_000)
    monkeypatch.setattr(cap, "MAX_INCLUDE_SCAN_BYTES", 8)
    actual = cap._read_scan_file
    counts: list[int] = []

    def bounded(directory_fd: int, name: str) -> bytes:
        data = actual(directory_fd, name)
        counts.append(len(data))
        return data

    monkeypatch.setattr(cap, "_read_scan_file", bounded)
    assert cap._nested_include_dirs(ws.ctx(), ["rtl/a.sv"]) == []
    assert counts and all(size <= 9 for size in counts)


def test_SNP_INC_21_directory_alias_does_not_bypass_target_file_exclusion(
    ws: Ws, monkeypatch: pytest.MonkeyPatch,
) -> None:
    ws.write("secret/not_allowed.sv", "module secret; endmodule\n")
    (ws.root / "alias").symlink_to(ws.root / "secret", target_is_directory=True)
    ws.config({"p": profile()}, source_roots=[{
        "id": "project", "path": ".", "include": ["**"], "exclude": ["secret/*.sv"],
    }])

    def no_read(directory_fd: int, name: str) -> bytes:
        pytest.fail("directory alias bypassed excluded target-file rules")

    monkeypatch.setattr(cap, "_read_scan_file", no_read)
    assert list(cap._scan_directory_texts(ws.ctx(), "alias", 10)) == []
