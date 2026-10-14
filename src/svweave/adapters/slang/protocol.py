"""Request/response contract between the gateway and the HDL worker process.

Only plain JSON crosses the process boundary; native pyslang objects never do (plan 10.2).
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field

EXTRACTOR_VERSION = "svweave-slang-extractor/1"
DTO_SCHEMA = "svw-extract-1"


class _M(BaseModel):
    model_config = ConfigDict(extra="forbid")


class GroupFile(_M):
    """A per-compilation-group command file written by the adapter (explicit_groups)."""

    path: str  # absolute path (outside the snapshot, in the run directory)
    sha256: str


class ExtractOptions(_M):
    include_inactive_regions: bool = True
    max_text_bytes: int = 4096  # cap for any single captured source text (expressions, types)


class WorkerRequest(_M):
    schema_version: str = DTO_SCHEMA
    snapshot_root: str  # absolute path of <snapshot>/files; also the worker's cwd
    args: list[str]  # slang argument array (no executable name)
    group_files: list[GroupFile] = Field(default_factory=list)
    tops: list[str] = Field(default_factory=list)
    options: ExtractOptions = Field(default_factory=ExtractOptions)
    test_fault: str | None = None  # set only by tests through the runner keyword; never config


class ExtractResult(_M):
    """Loose host-side view of the worker output; sections are validated by the ingester."""

    schema_version: str
    extractor_version: str
    pyslang_version: str
    status: dict[str, Any]
    driver_messages: list[str] = Field(default_factory=list)
    diagnostics: list[dict[str, Any]] = Field(default_factory=list)
    dependencies: dict[str, Any] = Field(default_factory=dict)
    declarations: list[dict[str, Any]] = Field(default_factory=list)
    references: list[dict[str, Any]] = Field(default_factory=list)
    instances: list[dict[str, Any]] = Field(default_factory=list)
    port_bindings: list[dict[str, Any]] = Field(default_factory=list)
    assertions: list[dict[str, Any]] = Field(default_factory=list)
    specializations: list[dict[str, Any]] = Field(default_factory=list)
    inactive: list[dict[str, Any]] = Field(default_factory=list)
    gaps: list[dict[str, Any]] = Field(default_factory=list)
    unsupported_nodes: list[dict[str, Any]] = Field(default_factory=list)
    dropped_refs: list[dict[str, Any]] = Field(default_factory=list)
    stats: dict[str, Any] = Field(default_factory=dict)
