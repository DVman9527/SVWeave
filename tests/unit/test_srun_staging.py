"""SRUN-06 immutable publication unit tests; no Slurm or backend process."""
from __future__ import annotations

import hashlib
import json
import os
import stat
from pathlib import Path

import pytest

from svweave.execution.staging import publish_inputs


def inputs(tmp_path: Path) -> tuple[Path, Path, dict[str, str]]:
    source = tmp_path / "snapshot"
    source.mkdir()
    (source / "rtl").mkdir()
    path = source / "rtl/a.sv"
    path.write_bytes(b"module a; endmodule\n")
    path.chmod(0o444)
    root = tmp_path / "shared"
    root.mkdir(mode=0o700)
    return source, root, {"rtl/a.sv": hashlib.sha256(path.read_bytes()).hexdigest()}


def test_SRUN_06_publish_final_paths_hashes_and_no_replacement(tmp_path: Path) -> None:
    source, root, hashes = inputs(tmp_path)
    payload = json.dumps({"snapshot_root": str(root / "attempt" / "rtl")}).encode()
    staged = publish_inputs(source, root, "attempt", hashes, generated={"request.json": payload})
    assert (staged.path / "request.json").read_bytes() == payload
    assert staged.hashes["request.json"] == hashlib.sha256(payload).hexdigest()
    assert staged.total_bytes == sum(p.stat().st_size for p in staged.path.rglob("*") if p.is_file())
    assert not stat.S_IMODE(staged.path.stat().st_mode) & 0o222
    assert not stat.S_IMODE((staged.path / "rtl/a.sv").stat().st_mode) & 0o222
    with pytest.raises(FileExistsError):
        publish_inputs(source, root, "attempt", hashes)
    assert (staged.path / "request.json").read_bytes() == payload
    assert [p.name for p in root.iterdir()] == ["attempt"]


@pytest.mark.parametrize("bad", ["../escape", "/absolute", "a/../b", "a//b", "a\nb", "./a", "."])
def test_SRUN_03_staging_rejects_path_injection(tmp_path: Path, bad: str) -> None:
    source, root, hashes = inputs(tmp_path)
    with pytest.raises(ValueError):
        publish_inputs(source, root, "attempt", hashes, generated={bad: b"data"})
    assert not list(root.iterdir())


@pytest.mark.parametrize("failure", ["hash", "writable", "symlink", "fifo", "quota"])
def test_SRUN_06_failed_publication_keeps_original_and_cleans_only_temp(tmp_path: Path, failure: str) -> None:
    source, root, hashes = inputs(tmp_path)
    unrelated = root / "unrelated"
    unrelated.mkdir()
    (unrelated / "keep").write_text("keep")
    path = source / "rtl/a.sv"
    quota = 1024
    if failure == "hash":
        hashes["rtl/a.sv"] = "0" * 64
    elif failure == "writable":
        path.chmod(0o644)
    elif failure == "symlink":
        path.unlink()
        path.symlink_to(unrelated / "keep")
    elif failure == "fifo":
        path.unlink()
        os.mkfifo(path, 0o400)
    else:
        quota = 1
    with pytest.raises((ValueError, OSError)):
        publish_inputs(source, root, "attempt", hashes, max_total_bytes=quota)
    assert list(root.iterdir()) == [unrelated]
    assert (unrelated / "keep").read_text() == "keep"


def test_SRUN_06_publication_refuses_symlink_parent(tmp_path: Path) -> None:
    source, root, hashes = inputs(tmp_path)
    link = tmp_path / "link"
    link.symlink_to(root, target_is_directory=True)
    with pytest.raises(OSError):
        publish_inputs(source, link, "attempt", hashes)
    assert not list(root.iterdir())


@pytest.mark.parametrize("quota", [0, -1, float("inf"), float("nan"), True])
def test_SRUN_03_staging_requires_finite_integer_quota(tmp_path: Path, quota: object) -> None:
    source, root, hashes = inputs(tmp_path)
    with pytest.raises(ValueError):
        publish_inputs(source, root, "attempt", hashes, max_total_bytes=quota)  # type: ignore[arg-type]


def test_SRUN_06_group_request_uses_final_shared_paths_and_never_copies_db(tmp_path: Path, ws: object) -> None:
    from svweave.execution.staging import prepare_slang_staging
    from svweave.manifest.resolver import resolve_profile
    from svweave.snapshot.capture import capture_snapshot, snapshots_dir
    from tests.conftest import Ws

    assert isinstance(ws, Ws)
    ws.config({"p": {"compilation_units": {"mode": "explicit_groups", "groups": [
        {"id": "g", "sources": [{"kind": "file", "path": "rtl/a.sv"}]}]}, "top_modules": ["a"]}})
    ctx = ws.ctx()
    manifest = resolve_profile(ctx, "p")
    snapshot = capture_snapshot(ctx, manifest)
    source = snapshots_dir(ctx) / snapshot.snapshot_id
    shared = tmp_path / "shared"
    shared.mkdir(mode=0o700)
    (ctx.state_dir / "svweave.db").write_bytes(b"private local database")
    staged = prepare_slang_staging(manifest, snapshot, source, shared, "attempt")
    request = staged.request
    assert request.snapshot_root == str(shared / "attempt/files")
    assert request.group_files
    for group in request.group_files:
        assert Path(group.path).parent == shared / "attempt/groups"
        content = Path(group.path).read_text()
        assert str(shared / "attempt/files/rtl/a.sv") in content
        assert str(source) not in content
        assert hashlib.sha256(Path(group.path).read_bytes()).hexdigest() == group.sha256
    assert not (staged.inputs.path / "svweave.db").exists()
    assert (ctx.state_dir / "svweave.db").read_bytes() == b"private local database"
