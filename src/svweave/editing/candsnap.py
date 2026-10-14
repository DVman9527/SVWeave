"""Snapshot of the CANDIDATE (plan 12.4 validate): the base snapshot record with the edited files'
hashes replaced. Logical paths are identical to the base, so slang's results line up with the
baseline; the snapshot id is derived from the candidate content only.
"""

from __future__ import annotations

import os
import shutil
import stat
import uuid
from datetime import UTC, datetime
from pathlib import Path

from svweave.config.context import WorkspaceContext, snapshot_rel
from svweave.domain.ids import sha256_hex
from svweave.snapshot.capture import (
    load_record,
    remove_snapshot_dir,
    snapshot_id_for,
    snapshots_dir,
)
from svweave.snapshot.models import SnapFile, SnapshotRecord

_CHUNK = 1 << 20


def _freeze(root: Path) -> None:
    for dirpath, dirnames, filenames in os.walk(root, topdown=False):
        for fn in filenames:
            os.chmod(os.path.join(dirpath, fn), stat.S_IRUSR | stat.S_IRGRP | stat.S_IROTH)
        for dn in dirnames:
            os.chmod(os.path.join(dirpath, dn), 0o555)
    os.chmod(root, 0o555)


def derive_candidate_snapshot(
    ctx: WorkspaceContext, base: SnapshotRecord, changed: dict[str, bytes]
) -> SnapshotRecord | None:
    """Immutable snapshot of ``base`` with ``changed`` (logical path -> new bytes) applied.

    Returns ``None`` when no changed file belongs to this snapshot (the profile does not cover
    the edit, so it cannot validate it).
    """
    inv = {f.logical_path for f in base.files}
    touched = sorted(p for p in changed if p in inv)
    if not touched:
        return None
    files: list[SnapFile] = []
    for f in base.files:
        if f.logical_path in changed:
            data = changed[f.logical_path]
            files.append(f.model_copy(update={"sha256": sha256_hex(data), "size": len(data)}))
        else:
            files.append(f)
    sid = snapshot_id_for(base.spec, files, base.lookup)
    root = snapshots_dir(ctx)
    final = root / sid
    if final.exists():
        return load_record(final)
    rec = SnapshotRecord(
        snapshot_id=sid,
        workspace_id=base.workspace_id,
        profile=base.profile,
        config_id=base.config_id,
        created_at=datetime.now(UTC).isoformat(timespec="seconds"),
        attempts=1,
        spec=base.spec,
        files=files,
        lookup=base.lookup,
    )
    tmp = root / f".tmp-{uuid.uuid4().hex}"
    src_files = root / base.snapshot_id / "files"
    try:
        (tmp / "files").mkdir(parents=True)
        for f in files:  # real byte copies, never links
            dest = tmp / "files" / snapshot_rel(f.logical_path)
            dest.parent.mkdir(parents=True, exist_ok=True)
            if f.logical_path in changed:
                dest.write_bytes(changed[f.logical_path])
            else:
                with open(src_files / snapshot_rel(f.logical_path), "rb") as fin, open(
                    dest, "xb"
                ) as fout:
                    shutil.copyfileobj(fin, fout, _CHUNK)
        (tmp / "snapshot.json").write_text(rec.model_dump_json(indent=2), encoding="utf-8")
        _freeze(tmp)
        os.rename(tmp, final)
    except BaseException:
        remove_snapshot_dir(tmp)
        raise
    return rec
