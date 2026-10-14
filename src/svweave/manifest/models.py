"""Normalized manifest and argument/event ledger models."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from svweave.domain.ids import compute_config_id


class _M(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Origin(_M):
    file: str  # logical path of the filelist / config file
    line: int = 0
    col: int = 0
    field: str | None = None  # for svweave.yaml-sourced entries


class LedgerEntry(_M):
    seq: int
    kind: str
    raw: str
    unit: str | None = None
    name: str | None = None
    value: str | None = None
    resolved: str | None = None  # logical path
    base: str | None = None  # logical dir used as the resolution base
    origin: Origin
    chain: list[str] = Field(default_factory=list)  # filelist include chain, outermost first
    classification: str | None = None


class SourceRef(_M):
    path: str
    raw: str
    origin: Origin
    base: str | None = None
    chain: list[str] = Field(default_factory=list)


class DirRef(_M):
    path: str
    raw: str
    origin: Origin


class DefineRef(_M):
    name: str
    value: str | None = None  # token-exact: quotes kept, None means no '='
    raw: str
    origin: Origin


class LibraryRef(_M):
    path: str
    raw: str
    origin: Origin


class Unit(_M):
    id: str
    group: str | None = None
    sources: list[SourceRef]
    include_dirs: list[DirRef] = Field(default_factory=list)
    defines: list[DefineRef] = Field(default_factory=list)
    library_files: list[LibraryRef] = Field(default_factory=list)
    library_dirs: list[LibraryRef] = Field(default_factory=list)
    library_extensions: list[str] = Field(default_factory=list)


class TopParam(_M):
    top: str
    name: str
    value: str


class GeneratedRef(_M):
    path: str
    generator_command_id: str | None = None
    declared_output_hash: str | None = None
    sha256: str
    provenance_incomplete: bool


class IgnoredOption(_M):
    option: str
    args: list[str] = Field(default_factory=list)
    classification: str
    origin: Origin
    chain: list[str] = Field(default_factory=list)


class NormalizedManifest(_M):
    schema_version: Literal["1.0"] = "1.0"
    dialect: str
    workspace_id: str
    profile: str
    language_mode: str
    cwd: str
    cu_mode: Literal["single", "per_file", "explicit_groups"]
    units: list[Unit]
    top_modules: list[str]
    top_parameters: list[TopParam]
    generated_inputs: list[GeneratedRef]
    ignored_options: list[IgnoredOption]
    filelists: list[str]  # every filelist visited, in visit order (duplicates kept)
    filelist_hashes: dict[str, str] = Field(default_factory=dict)  # logical path -> sha256 read
    diagnostic_policy: str
    language_options: dict[str, str] = Field(default_factory=dict)  # allowlisted slang knobs
    stubs: list[dict[str, str]] = Field(default_factory=list)  # declared stub records (16.5)
    semantic_deviations: list[str] = Field(default_factory=list)
    ledger: list[LedgerEntry]
    config_id: str = ""

    def semantics(self) -> dict[str, Any]:
        """Order-preserving semantic projection (no origins, no ledger, no ignored options)."""
        return {
            "dialect": self.dialect,
            "language_mode": self.language_mode,
            "cu_mode": self.cu_mode,
            "units": [
                {
                    "id": u.id,
                    "group": u.group,
                    "sources": [s.path for s in u.sources],
                    "include_dirs": [d.path for d in u.include_dirs],
                    "defines": [[d.name, d.value] for d in u.defines],
                    "library_files": [x.path for x in u.library_files],
                    "library_dirs": [x.path for x in u.library_dirs],
                    "library_extensions": list(u.library_extensions),
                }
                for u in self.units
            ],
            "top_modules": list(self.top_modules),
            "top_parameters": [[t.top, t.name, t.value] for t in self.top_parameters],
            "generated_inputs": [
                [g.path, g.generator_command_id, g.declared_output_hash]
                for g in self.generated_inputs
            ],
            "diagnostic_policy": self.diagnostic_policy,
            "language_options": dict(sorted(self.language_options.items())),
            "stubs": sorted(self.stubs, key=lambda x: x["module"]),
            "semantic_deviations": list(self.semantic_deviations),
        }

    def compute_config_id(self) -> str:
        return compute_config_id(self.semantics())

    def all_source_paths(self) -> list[str]:
        return [s.path for u in self.units for s in u.sources]
