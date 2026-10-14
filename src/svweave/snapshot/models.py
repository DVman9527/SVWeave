"""Snapshot spec (what to capture) and record (what was captured)."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class _M(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ExplicitEntry(_M):
    logical: str
    role: Literal["filelist", "source", "library_file", "generated"]


class LookupSpec(_M):
    kind: Literal["include", "library"]
    logical_dir: str
    exts: list[str] = Field(default_factory=list)  # library lookups only
    recursive: bool = True  # False: only the directory itself (source-file directories)


class SnapshotSpec(_M):
    """Derived from a normalized manifest; stored in the record so freshness can be recomputed
    without re-resolving the manifest."""

    explicit: list[ExplicitEntry]
    lookups: list[LookupSpec]
    root_ids: list[str]


class SnapFile(_M):
    logical_path: str
    original_path: str
    snapshot_path: str  # relative to the snapshot directory
    sha256: str
    size: int
    role: str
    symlink_target: str | None = None
    hardlinked: bool = False  # st_nlink > 1: another name may alias these bytes (SEC-01)


class LookupInventory(_M):
    kind: Literal["include", "library"]
    logical_dir: str
    recursive: bool = True
    exts: list[str] = Field(default_factory=list)
    original_dir: str
    entries: list[str]  # candidate file names (relative POSIX), sorted; this is a set listing


class SnapshotRecord(_M):
    snapshot_id: str
    workspace_id: str
    profile: str
    config_id: str
    created_at: str
    attempts: int
    spec: SnapshotSpec
    files: list[SnapFile]
    lookup: list[LookupInventory]


class Change(_M):
    path: str
    change: Literal["modified", "removed", "added", "lookup_added", "lookup_removed"]


class FreshnessResult(_M):
    freshness: Literal["CURRENT", "STALE"]
    snapshot_id: str
    current_snapshot_id: str
    changed: list[Change]
