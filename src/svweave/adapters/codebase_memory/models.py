"""Data carried between the cbm adapter and its callers."""

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from svweave.domain.errors import SvwError
from svweave.domain.ids import canonical_json, sha256_hex
from svweave.security import safeio


@dataclass(frozen=True)
class IndexMapping:
    """snapshot_id <-> upstream project <-> index fingerprint (plan 11.1).

    ``content_fingerprint`` is computed by SVWeave over the immutable snapshot tree; the upstream
    project name is derived by cbm from the path and is never used to infer identity.
    """

    snapshot_id: str
    snapshot_root: str
    project: str
    content_fingerprint: str
    nodes: int
    edges: int
    indexed_at: str | None
    index_mode: str
    cbm_version: str
    cbm_binary_sha256: str
    parse_unusable: tuple[str, ...] = ()
    parse_partial: tuple[tuple[str, str], ...] = ()  # (path, error_ranges)
    created_at: str = ""

    def to_json(self) -> dict[str, Any]:
        d = asdict(self)
        d["parse_unusable"] = list(self.parse_unusable)
        d["parse_partial"] = [list(x) for x in self.parse_partial]
        return d

    @classmethod
    def from_json(cls, d: dict[str, Any]) -> IndexMapping:
        return cls(
            snapshot_id=str(d["snapshot_id"]),
            snapshot_root=str(d["snapshot_root"]),
            project=str(d["project"]),
            content_fingerprint=str(d["content_fingerprint"]),
            nodes=int(d["nodes"]),
            edges=int(d["edges"]),
            indexed_at=d.get("indexed_at"),
            index_mode=str(d.get("index_mode", "")),
            cbm_version=str(d.get("cbm_version", "")),
            cbm_binary_sha256=str(d.get("cbm_binary_sha256", "")),
            parse_unusable=tuple(str(x) for x in d.get("parse_unusable", [])),
            parse_partial=tuple((str(a), str(b)) for a, b in d.get("parse_partial", [])),
            created_at=str(d.get("created_at", "")),
        )


class MappingStore:
    """JSON file under the backend state dir. A storage-table writer can replace it later."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._items: dict[str, IndexMapping] = {}
        self._load()

    def _load(self) -> None:
        try:
            raw = json.loads(safeio.read_regular(self.path))
            self._items = {k: IndexMapping.from_json(v) for k, v in raw.items()}
        except (OSError, ValueError, KeyError, TypeError, SvwError):
            self._items = {}

    def get(self, snapshot_id: str) -> IndexMapping | None:
        return self._items.get(snapshot_id)

    def put(self, m: IndexMapping) -> None:
        self._items[m.snapshot_id] = m
        self._flush()

    def drop(self, snapshot_id: str) -> None:
        if self._items.pop(snapshot_id, None) is not None:
            self._flush()

    def all(self) -> dict[str, IndexMapping]:
        return dict(self._items)

    def _flush(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        data = json.dumps({k: v.to_json() for k, v in self._items.items()}, indent=1, sort_keys=True)
        safeio.write_atomic(self.path, data.encode())


def tree_fingerprint(root: Path) -> str:
    """SHA-256 over {relative path: file sha256 | symlink target} of a directory tree."""
    entries: dict[str, str] = {}
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        dirnames.sort()
        for dn in dirnames:  # os.walk lists symlinks to directories here and never descends
            dp = Path(dirpath) / dn
            if dp.is_symlink():
                entries[dp.relative_to(root).as_posix()] = "symlink:" + os.readlink(dp)
        for fn in sorted(filenames):
            p = Path(dirpath) / fn
            rel = p.relative_to(root).as_posix()
            if p.is_symlink():
                entries[rel] = "symlink:" + os.readlink(p)
                continue
            h = hashlib.sha256()
            with open(p, "rb") as f:
                for chunk in iter(lambda: f.read(1 << 20), b""):
                    h.update(chunk)
            entries[rel] = h.hexdigest()
    return sha256_hex(canonical_json({"kind": "tree", "entries": entries}))


@dataclass
class CbmCandidate:
    """A cbm graph hit normalized to an SVWeave source candidate (evidence SOURCE_CANDIDATE)."""

    snapshot_id: str
    name: str
    upstream_label: str
    qualified_name: str  # project prefix removed
    upstream_qualified_name: str  # as cbm reports it (needed for get_code_snippet)
    file: str  # snapshot-relative POSIX path
    start_line: int | None
    end_line: int | None
    rank: float | None
    coverage: dict[str, Any] = field(default_factory=dict)
    backend: str = "codebase-memory-mcp"
    evidence_kind: str = "SOURCE_CANDIDATE"
    resolution_status: str = "candidate_source"

    def to_json(self) -> dict[str, Any]:
        return asdict(self)
