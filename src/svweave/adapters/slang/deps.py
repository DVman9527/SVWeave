"""Dependency leak check (plan 8.4 step 9, 9.3).

The set of files slang ACTUALLY read must be a subset of the snapshot inventory (plus the
adapter-written group command files declared in the request). A file read from outside the
snapshot, or inside the snapshot but never inventoried, means the build is not reproducible from
its snapshot: analysis is BLOCKED and must never be published.
"""

from __future__ import annotations

from typing import Any

from svweave.adapters.slang.ingest import snapshot_rel_of_logical
from svweave.snapshot.models import SnapshotRecord


def check_dependencies(
    deps: dict[str, Any], snap: SnapshotRecord, adapter_files: list[str]
) -> dict[str, Any]:
    inventory = {snapshot_rel_of_logical(f.logical_path) for f in snap.files}
    adapter = set(adapter_files)
    read: list[str] = []
    violations: list[dict[str, str]] = []
    for f in deps.get("files", []):
        path = f["path"]
        if f.get("outside_root"):
            if path in adapter:
                continue
            violations.append({"path": path, "reason": "read_outside_snapshot"})
            continue
        read.append(path)
        if path not in inventory:
            violations.append({"path": path, "reason": "not_in_snapshot_inventory"})
    read_set = set(read)
    unread = sorted(inventory - read_set)
    lib_missing: list[str] = []
    for lk in snap.lookup:
        if lk.kind != "library":
            continue
        base = lk.logical_dir.rstrip("/")
        for name in lk.entries:
            rel = snapshot_rel_of_logical(f"{base}/{name}" if base else name)
            if rel not in read_set:
                lib_missing.append(rel)
    return {
        "files_read": sorted(read_set),
        "violations": violations,
        "ok": not violations,
        "inventory_files": len(inventory),
        "inventory_not_read": unread,
        "library_members_not_loaded": sorted(lib_missing),
        "total_buffers": deps.get("total_buffers"),
        "include_directives": deps.get("include_directives", []),
    }
