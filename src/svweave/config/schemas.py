"""JSON Schema emission from the models (schemas/*.schema.json)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from pydantic import BaseModel

from svweave.application.evidence import EvidenceManifest
from svweave.config.models import LocalConfig, Manifest, Policy
from svweave.config.toolchain_lock import ToolchainLock
from svweave.editing.models import PrepareRequest

SCHEMA_MODELS: dict[str, type[BaseModel]] = {
    "manifest.schema.json": Manifest,
    "policy.schema.json": Policy,
    "local.schema.json": LocalConfig,
    "evidence.schema.json": EvidenceManifest,
    "edit.schema.json": PrepareRequest,
    "toolchain.schema.json": ToolchainLock,
}


def schema_text(model: type[BaseModel]) -> str:
    schema: dict[str, Any] = model.model_json_schema(mode="validation")
    return json.dumps(schema, indent=2, sort_keys=True, ensure_ascii=False) + "\n"


def emit_schemas(out_dir: Path) -> list[Path]:
    out_dir.mkdir(parents=True, exist_ok=True)
    written = []
    for name, model in SCHEMA_MODELS.items():
        p = out_dir / name
        p.write_text(schema_text(model), encoding="utf-8")
        written.append(p)
    return written


def check_schemas(out_dir: Path) -> list[str]:
    """Return names of schema files that are missing or out of sync with the models."""
    bad = []
    for name, model in SCHEMA_MODELS.items():
        p = out_dir / name
        if not p.is_file() or p.read_text(encoding="utf-8") != schema_text(model):
            bad.append(name)
    return bad
