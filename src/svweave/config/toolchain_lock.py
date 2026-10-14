"""Model of ``toolchain.lock.json`` (plan 17): the source of ``schemas/toolchain.schema.json``.

The lock pins every external component by hash. Unknown fields are rejected so a typo cannot
silently drop a pin; a new pin is added here first, then to the lock.
"""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, StringConstraints

Sha256 = Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{64}$")]
Commit = Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{40}$")]
Text = Annotated[str, StringConstraints(min_length=1)]


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class SlangPin(_Strict):
    commit: Commit
    artifact_sha256: Sha256
    tag: Text
    version_string: Text
    source_url: Text
    install_dir: Text
    archive_strip_components: int
    binary: Text
    binary_sha256: Sha256
    role: Text
    paired_with: Text


class PyslangPin(_Strict):
    version: Text
    wheel_sha256: Sha256
    #: sha256 of the installed dist-info RECORD; the installed files are verified against the
    #: hashes inside that RECORD at every build identity computation (CAPABILITY_MISMATCH)
    record_sha256: Sha256
    wheel_filename: Text
    source_url: Text
    install_target: Text
    upstream_commit: Commit


class SerenaPin(_Strict):
    commit: Commit
    dependency_lock_sha256: Sha256
    tag: Text
    package: Text
    source_url: Text
    src_dir: Text
    venv_dir: Text
    install_method: Text
    license_file_sha256: Sha256
    pinned_mcp_python_sdk_inside_serena: Text


class VeriblePin(_Strict):
    version: Text
    artifact_sha256: Sha256
    commit: Commit
    source_url: Text
    install_dir: Text
    archive_strip_components: int
    binary: Text
    binary_sha256: Sha256


class CbmPin(_Strict):
    commit: Commit
    artifact_sha256: Sha256
    tag: Text
    variant: Text
    source_url: Text
    upstream_checksums_url: Text
    upstream_checksums_sha256: Sha256
    install_dir: Text
    archive_strip_components: int
    binary: Text
    binary_sha256: Sha256


class McpSdkPin(_Strict):
    version: Text
    wheel_sha256: Sha256
    wheel_filename: Text
    source_url: Text
    install_target: Text
    note: Text


class UvmPin(_Strict):
    tag: Text
    commit: Commit
    artifact_sha256: Sha256
    source_url: Text
    install_dir: Text
    archive_strip_components: int
    role: Text


class Components(_Strict):
    slang: SlangPin
    pyslang: PyslangPin
    serena: SerenaPin
    verible: VeriblePin
    codebase_memory_mcp: CbmPin
    mcp_sdk: McpSdkPin
    uvm_core: UvmPin


class Wheel(_Strict):
    filename: Text
    sha256: Sha256
    source_url: Text


class GatewayWheelhouse(_Strict):
    """Hash-pinned offline wheelhouse of the gateway environment (scripts/gateway-requirements)."""

    requirements_in: Text
    requirements_file: Text
    requirements_sha256: Sha256
    wheelhouse_dir: Text
    target: Text
    install: Text
    wheels: list[Wheel]


class ToolchainLock(_Strict):
    schema_version: Literal["1.0"]
    qualification_state: Text
    platform: Text
    python_minor: Text
    created_at: Text
    created_by: Text
    components: Components
    capability_report_sha256: Sha256 | None = None
    license_manifest_sha256: Sha256 | None = None
    gateway_wheelhouse: GatewayWheelhouse | None = None
