"""Identity helpers (plan 9.1).

Canonical JSON sorts *object keys* only. Arrays are never reordered: source order,
include order, macro events and compilation groups are semantically significant.
"""

from __future__ import annotations

import hashlib
import json
import re
import uuid
from typing import Any

from svweave.domain.errors import ErrorCode, SvwError

SEMANTIC_SCHEMA_VERSION = "1"

_HEX64 = re.compile(r"^[0-9a-f]{64}$")


def sha256_hex(data: bytes | str) -> str:
    if isinstance(data, str):
        data = data.encode("utf-8")
    return hashlib.sha256(data).hexdigest()


def is_sha256_hex(value: str) -> bool:
    return bool(_HEX64.match(value))


def canonical_json(obj: Any) -> str:
    """Deterministic JSON: sorted object keys, compact, arrays kept in given order."""
    return json.dumps(
        obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
    )


def canonical_hash(obj: Any) -> str:
    return sha256_hex(canonical_json(obj))


def compute_config_id(normalized_semantics: Any) -> str:
    return canonical_hash({"kind": "config", "semantics": normalized_semantics})


def compute_snapshot_id(payload: Any) -> str:
    return canonical_hash({"kind": "snapshot", "payload": payload})


def compute_toolchain_id(parts: Any) -> str:
    """Hash of binary/wheel/asset hashes plus adapter/extractor version."""
    return canonical_hash({"kind": "toolchain", "parts": parts})


def compute_build_id(
    snapshot_id: str,
    config_id: str,
    toolchain_id: str,
    semantic_schema_version: str = SEMANTIC_SCHEMA_VERSION,
    *,
    execution_fingerprint: str | None = None,
) -> str:
    for name, value in (
        ("snapshot_id", snapshot_id),
        ("config_id", config_id),
        ("toolchain_id", toolchain_id),
    ):
        if not is_sha256_hex(value):
            raise SvwError(
                ErrorCode.INVALID_CONFIG,
                f"{name} must be 64 lowercase hex characters",
                {"field": name},
            )
    if not semantic_schema_version or ":" in semantic_schema_version:
        raise SvwError(ErrorCode.INVALID_CONFIG, "invalid semantic_schema_version")
    # Preserve every existing local identity; remote namespaces cannot reuse legacy builds.
    components: tuple[str, ...] = (snapshot_id, config_id, toolchain_id, semantic_schema_version)
    if execution_fingerprint is not None:
        if not is_sha256_hex(execution_fingerprint):
            raise SvwError(ErrorCode.INVALID_CONFIG, "invalid execution_fingerprint")
        components += ("execution-v1", execution_fingerprint)
    return sha256_hex(":".join(components))


def explain_build_change(old: dict[str, str], new: dict[str, str]) -> list[str]:
    """Name the identity dimensions that differ between two build identity dicts."""
    dims = ("snapshot_id", "config_id", "toolchain_id", "semantic_schema_version")
    return [d for d in dims if old.get(d) != new.get(d)]


def new_job_id() -> str:
    """Unique per attempt; deliberately unrelated to the deterministic build_id."""
    return str(uuid.uuid4())
