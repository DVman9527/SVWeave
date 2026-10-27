"""Pydantic models for svweave.yaml, svweave.local.yaml and policy.json.

All models forbid unknown fields (plan 8.1): a typo is a schema error, never ignored.
"""

from __future__ import annotations

import re
from typing import Annotated, Final, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator

from svweave.config.execution import ExecutionPolicy, ExecutionSelection

NonEmptyStr = Annotated[str, StringConstraints(min_length=1)]
_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_ID = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_.-]*$")

DIALECT: Final = "svweave-filelist-v1"


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


def _check_name(value: str, what: str) -> str:
    if not _NAME.match(value):
        raise ValueError(f"{what} {value!r} is not a valid identifier")
    return value


def _check_id(value: str, what: str) -> str:
    if not _ID.match(value):
        raise ValueError(f"{what} {value!r} must match {_ID.pattern}")
    return value


def _check_path(value: str) -> str:
    if "\x00" in value:
        raise ValueError("path contains NUL")
    return value


class Project(_Strict):
    id: NonEmptyStr
    root: NonEmptyStr = "."
    default_profile: NonEmptyStr

    @model_validator(mode="after")
    def _v(self) -> Project:
        _check_id(self.id, "project.id")
        _check_path(self.root)
        return self


class SourceRoot(_Strict):
    """A trusted source root. `path` omitted means external: resolved through
    svweave.local.yaml root_mappings under the same id."""

    id: NonEmptyStr
    path: NonEmptyStr | None = None
    include: list[NonEmptyStr] = Field(default_factory=lambda: ["**"])
    exclude: list[NonEmptyStr] = Field(default_factory=list)
    writable: bool = False

    @model_validator(mode="after")
    def _v(self) -> SourceRoot:
        _check_id(self.id, "source_roots[].id")
        if self.path is not None:
            _check_path(self.path)
        return self


class SourceInput(_Strict):
    kind: Literal["filelist", "file"]
    path: NonEmptyStr
    dialect: Literal["svweave-filelist-v1"] = DIALECT
    path_mode: Literal["cwd", "filelist"] = "cwd"

    @model_validator(mode="after")
    def _v(self) -> SourceInput:
        _check_path(self.path)
        return self


class Define(_Strict):
    name: NonEmptyStr
    value: str | None = None

    @model_validator(mode="after")
    def _v(self) -> Define:
        _check_name(self.name, "define name")
        return self


class TopParameter(_Strict):
    top: NonEmptyStr
    name: NonEmptyStr
    value: str

    @model_validator(mode="after")
    def _v(self) -> TopParameter:
        _check_name(self.top, "top")
        _check_name(self.name, "parameter name")
        return self


class GeneratedInput(_Strict):
    path: NonEmptyStr
    generator_command_id: NonEmptyStr | None = None
    input_hashes: dict[str, str] = Field(default_factory=dict)
    output_hash: str | None = None


class CompilationGroup(_Strict):
    id: NonEmptyStr
    sources: list[SourceInput]
    include_dirs: list[NonEmptyStr] = Field(default_factory=list)
    defines: list[Define] = Field(default_factory=list)
    library_dirs: list[NonEmptyStr] = Field(default_factory=list)
    library_files: list[NonEmptyStr] = Field(default_factory=list)
    library_extensions: list[NonEmptyStr] = Field(default_factory=lambda: [".v", ".sv"])

    @model_validator(mode="after")
    def _v(self) -> CompilationGroup:
        _check_id(self.id, "group id")
        if not self.sources:
            raise ValueError("group must list at least one source")
        return self


class CompilationUnits(_Strict):
    mode: Literal["single", "per_file", "explicit_groups"]
    groups: list[CompilationGroup] = Field(default_factory=list)

    @model_validator(mode="after")
    def _v(self) -> CompilationUnits:
        if self.mode == "explicit_groups":
            if not self.groups:
                raise ValueError("explicit_groups requires an ordered, non-empty groups list")
            ids = [g.id for g in self.groups]
            if len(set(ids)) != len(ids):
                raise ValueError("duplicate group ids")
        elif self.groups:
            raise ValueError(f"groups are only allowed with mode explicit_groups, not {self.mode}")
        return self


_TIME_UNIT = r"(?:1|10|100)(?:s|ms|us|ns|ps|fs)"


class LanguageOptions(_Strict):
    """Allowlisted slang language knobs (plan 8.3 'unsupported options block'): ``timescale`` ->
    ``--timescale <base>/<precision>``, ``std`` -> ``--std <version>``, ``compat`` ->
    ``--compat <vendor>`` (slang's vendor-leniency bundle; only ``vcs`` is accepted). Recorded in
    config_id and in the build's semantic_deviations. Nothing else can be passed through to slang."""

    timescale: Annotated[str, StringConstraints(pattern=rf"^{_TIME_UNIT}/{_TIME_UNIT}$")] | None = (
        None
    )
    std: Literal["1364-2005", "1800-2017", "1800-2023", "latest"] | None = None
    compat: Literal["vcs"] | None = None


#: Options the filelist resolver interprets itself (or already ignores): a profile cannot redeclare them.
_RESERVED_OPTIONS = frozenset({"-f", "-F", "-v", "-y", "-l", "-logfile", "-nologo", "-quiet", "-q"})
#: ``-G`` is slang's parameter override and every ``--long`` option is a slang option: both change
#: what is elaborated, so they are never declarable.
_RESERVED_OPTION_PREFIXES = ("+incdir+", "+define+", "+libext+", "-D", "-I", "-G", "--")


class IgnoredOptionDecl(_Strict):
    """A simulator option the profile explicitly declares irrelevant to static analysis.

    Filelists written for a simulator carry run-time options (``+notimingcheck``, ``+nospecify``) that
    SVWeave does not model; by default any unknown option BLOCKS the build. Declaring it here is an
    explicit, reviewable decision: the option is still recorded in the ``ignored_options`` ledger
    (classification ``profile_declared``) and is part of the build's config identity. Only options
    that take NO argument can be declared (an argument-taking option would swallow the next token,
    which may be a source file). Options the resolver interprets (include dirs, defines, libraries,
    nested filelists) and slang's own options (``--...``, ``-G``) cannot be declared.
    """

    option: Annotated[str, StringConstraints(pattern=r"^[-+][A-Za-z0-9_.:=+-]+$")]
    reason: NonEmptyStr

    @model_validator(mode="after")
    def _v(self) -> IgnoredOptionDecl:
        if self.option in _RESERVED_OPTIONS or self.option.startswith(_RESERVED_OPTION_PREFIXES):
            raise ValueError(
                f"option {self.option!r} changes what is analysed and cannot be declared ignorable"
            )
        return self


class Profile(_Strict):
    cwd: NonEmptyStr = "."
    language_mode: Literal["systemverilog"] = "systemverilog"
    source_inputs: list[SourceInput] = Field(default_factory=list)
    compilation_units: CompilationUnits
    top_modules: list[NonEmptyStr] = Field(default_factory=list)
    defines: list[Define] = Field(default_factory=list)
    include_dirs: list[NonEmptyStr] = Field(default_factory=list)
    top_parameters: list[TopParameter] = Field(default_factory=list)
    library_dirs: list[NonEmptyStr] = Field(default_factory=list)
    library_files: list[NonEmptyStr] = Field(default_factory=list)
    library_extensions: list[NonEmptyStr] = Field(default_factory=lambda: [".v", ".sv"])
    environment_allowlist: list[NonEmptyStr] = Field(default_factory=list)
    generated_inputs: list[GeneratedInput] = Field(default_factory=list)
    diagnostic_policy: Literal["no_errors"] = "no_errors"
    language_options: LanguageOptions = Field(default_factory=lambda: LanguageOptions())
    ignored_options: list[IgnoredOptionDecl] = Field(default_factory=list)

    @model_validator(mode="after")
    def _v(self) -> Profile:
        _check_path(self.cwd)
        names = [d.option for d in self.ignored_options]
        if len(names) != len(set(names)):
            raise ValueError("ignored_options lists the same option twice")
        for t in self.top_modules:
            _check_name(t, "top module")
        for e in self.environment_allowlist:
            _check_name(e, "environment variable")
        for ext in self.library_extensions:
            if not ext.startswith("."):
                raise ValueError(f"library extension {ext!r} must start with '.'")
        if self.compilation_units.mode == "explicit_groups":
            leftovers = [
                n
                for n in (
                    "source_inputs",
                    "defines",
                    "include_dirs",
                    "library_dirs",
                    "library_files",
                )
                if getattr(self, n)
            ]
            if leftovers:
                raise ValueError(
                    "explicit_groups profiles must declare sources/include dirs/defines/library "
                    f"inside groups only (no inheritance); found profile-level: {leftovers}"
                )
        elif not self.source_inputs:
            raise ValueError("source_inputs must not be empty")
        return self


class StubRecord(_Strict):
    """Declared stub / interface model replacing a blackbox or encrypted IP (plan 16.5). The build
    stays PARTIAL; the record only names what was replaced."""

    module: NonEmptyStr
    original_ip: NonEmptyStr
    model_sha256: Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{64}$")]
    replaced_scope: NonEmptyStr

    @model_validator(mode="after")
    def _v(self) -> StubRecord:
        _check_name(self.module, "stub module")
        return self


class Analysis(_Strict):
    missing_input_policy: Literal["block"] = "block"
    unsupported_option_policy: Literal["block"] = "block"
    stub_policy: Literal["explicit_only"] = "explicit_only"
    stubs: list[StubRecord] = Field(default_factory=list)
    max_build_seconds: int = Field(default=120, ge=1)
    max_worker_memory_mib: int = Field(default=8192, ge=64)


class Editing(_Strict):
    enabled: bool = False
    allowed_paths: list[NonEmptyStr] = Field(default_factory=list)
    max_files_per_patch: int = Field(default=5, ge=1)
    max_changed_lines: int = Field(default=200, ge=1)
    require_human_approval: bool = True
    validate_profiles: list[NonEmptyStr] = Field(default_factory=list)


class Manifest(_Strict):
    """svweave.yaml"""

    schema_version: Literal["1.0"]
    project: Project
    source_roots: list[SourceRoot]
    profiles: dict[str, Profile]
    analysis: Analysis = Field(default_factory=Analysis)
    editing: Editing = Field(default_factory=Editing)

    @model_validator(mode="after")
    def _v(self) -> Manifest:
        ids = [r.id for r in self.source_roots]
        if len(set(ids)) != len(ids):
            raise ValueError("duplicate source_roots ids")
        if not self.source_roots:
            raise ValueError("source_roots must not be empty")
        if not self.profiles:
            raise ValueError("profiles must not be empty")
        for name in self.profiles:
            _check_id(name, "profile name")
        if self.project.default_profile not in self.profiles:
            raise ValueError(f"default_profile {self.project.default_profile!r} is not a profile")
        for p in self.editing.validate_profiles:
            if p not in self.profiles:
                raise ValueError(f"editing.validate_profiles references unknown profile {p!r}")
        return self


class RootMapping(_Strict):
    id: NonEmptyStr
    path: NonEmptyStr

    @model_validator(mode="after")
    def _v(self) -> RootMapping:
        _check_id(self.id, "root mapping id")
        _check_path(self.path)
        return self


class LocalConfig(_Strict):
    """svweave.local.yaml: host-specific external root mappings."""

    schema_version: Literal["1.0"]
    root_mappings: list[RootMapping] = Field(default_factory=list)
    execution: ExecutionSelection = Field(default_factory=ExecutionSelection)

    @model_validator(mode="after")
    def _v(self) -> LocalConfig:
        ids = [m.id for m in self.root_mappings]
        if len(set(ids)) != len(ids):
            raise ValueError("duplicate root mapping ids")
        return self


class Policy(_Strict):
    """policy.json: trusted, created by the CLI; a repository profile can only tighten it."""

    schema_version: Literal["1.0"]
    allowed_roots: list[NonEmptyStr]
    writable_paths: list[str] = Field(default_factory=list)
    approval_required: bool = True
    network_enabled: bool = False
    editing_enabled: bool = False
    #: Accept approvals made through the non-qualified test-token path (never a human at a TTY).
    #: Only a human editing the trusted policy.json can set it; default off.
    allow_test_approvals: bool = False
    execution: ExecutionPolicy = Field(default_factory=ExecutionPolicy)

    @model_validator(mode="after")
    def _v(self) -> Policy:
        if not self.allowed_roots:
            raise ValueError("allowed_roots must not be empty")
        for r in self.allowed_roots:
            _check_path(r)
            if not r.startswith("/"):
                raise ValueError(f"allowed_roots entries must be absolute paths, got {r!r}")
        return self
