"""JSON Schemas of the MCP tools (plan 14.2, 14.3): the single source for ``tools/list``,
runtime input validation and ``schemas/mcp/<tool>.{input,output}.json``.

Inputs forbid unknown fields, use enums, 64-hex ID patterns, string length caps and ``limit<=500``.
Outputs are the common envelope (plan 14.5) with a tool-specific ``data`` object.
"""

from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any

from svweave.domain.envelope import Envelope

TOOL_NAMES = (
    "svw_status",
    "svw_build",
    "svw_job",
    "svw_search",
    "svw_symbol",
    "svw_references",
    "svw_hierarchy",
    "svw_connections",
    "svw_impact",
    "svw_diff_impact",
    "svw_assertions",
    "svw_evidence",
    "svw_edit",
)

SCHEMA_DIALECT = "https://json-schema.org/draft/2020-12/schema"
#: control characters (incl. newline, which `$` would otherwise tolerate) never occur in any text
_CTRL = "\\u0000-\\u001f\\u007f"
HEX64 = {"type": "string", "minLength": 64, "maxLength": 64, "pattern": "^[0-9a-f]{64}$"}
JOB_ID = {
    "type": "string",
    "pattern": "^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$",
}
PROFILE = {"type": "string", "pattern": "^[A-Za-z0-9_.-]{1,64}$"}
LIMIT = {"type": "integer", "minimum": 1, "maximum": 500, "default": 100}
#: a git revision as a plain token (no option, no whitespace, no control character, no ".." range)
GIT_REF = {
    "type": "string",
    "minLength": 1,
    "maxLength": 200,
    "pattern": r"^(?!.*\.\.)[A-Za-z0-9_][A-Za-z0-9._/@^~+:-]*$",
}
CURSOR = {"type": "string", "minLength": 1, "maxLength": 1024, "pattern": "^[A-Za-z0-9_.-]+$"}
REL_PATH = {
    "type": "string",
    "minLength": 1,
    "maxLength": 256,
    "pattern": f"^(?!/)(?!(.*/)?\\.\\.(/|$))[^{_CTRL}]+$",
}
EVIDENCE_TYPES = ["static", "eda_compile", "simulation", "formal", "coverage"]


def text(n: int) -> dict[str, Any]:
    return {"type": "string", "minLength": 1, "maxLength": n, "pattern": f"^[^{_CTRL}]+$"}


def _obj(props: dict[str, Any], required: list[str], **extra: Any) -> dict[str, Any]:
    s: dict[str, Any] = {
        "$schema": SCHEMA_DIALECT,
        "type": "object",
        "properties": props,
        "additionalProperties": False,
    }
    if required:
        s["required"] = required
    s.update(extra)
    return s


def _one_of(*groups: list[str]) -> list[dict[str, Any]]:
    return [{"required": g} for g in groups]


_PAGING = {"limit": LIMIT, "cursor": CURSOR}

INPUT: dict[str, dict[str, Any]] = {
    "svw_status": _obj({"profile": PROFILE, "probe": {"type": "boolean", "default": False}}, []),
    "svw_build": _obj(
        {
            "profile": PROFILE,
            "expected_snapshot_id": HEX64,
            "reuse": {"type": "boolean", "default": True},
        },
        ["profile"],
    ),
    "svw_job": _obj(
        {"job_id": JOB_ID, "action": {"enum": ["status", "cancel"], "default": "status"}},
        ["job_id"],
    ),
    "svw_search": _obj(
        {"snapshot_id": HEX64, "query": text(512), "scope": REL_PATH, **_PAGING},
        ["snapshot_id", "query"],
    ),
    "svw_symbol": _obj(
        {
            "build_id": HEX64,
            "symbol_id": HEX64,
            "location": _obj(
                {
                    "file": REL_PATH | {"maxLength": 1024},
                    "byte": {"type": "integer", "minimum": 0, "maximum": 2**40},
                },
                ["file", "byte"],
            )
            | {"$schema": None},
            "name": text(512),
            **_PAGING,
        },
        ["build_id"],
        oneOf=_one_of(["symbol_id"], ["location"], ["name"]),
    ),
    "svw_references": _obj(
        {"build_id": HEX64, "symbol_id": HEX64, "scope": REL_PATH, **_PAGING},
        ["build_id", "symbol_id"],
    ),
    "svw_hierarchy": _obj(
        {
            "build_id": HEX64,
            "instance_id": HEX64,
            "instance_path": text(1024),
            "top": text(256),
            "depth": {"type": "integer", "minimum": 0, "maximum": 1000},
            **_PAGING,
        },
        ["build_id"],
        allOf=[
            {"not": {"required": ["instance_id", "instance_path"]}},
            {"not": {"required": ["instance_id", "top"]}},
            {"not": {"required": ["instance_path", "top"]}},
        ],
    ),
    "svw_connections": _obj(
        {
            "build_id": HEX64,
            "instance_id": HEX64,
            "instance_path": text(1024),
            "port": text(256),
            **_PAGING,
        },
        ["build_id"],
        oneOf=_one_of(["instance_id"], ["instance_path"]),
    ),
    "svw_impact": _obj(
        {
            "build_id": HEX64,
            "symbol_id": HEX64,
            "location": _obj(
                {
                    "file": REL_PATH | {"maxLength": 1024},
                    "byte": {"type": "integer", "minimum": 0, "maximum": 2**40},
                },
                ["file", "byte"],
            )
            | {"$schema": None},
            "name": text(512),
            "instance_id": HEX64,
            "instance_path": text(1024),
            "depth": {"type": "integer", "minimum": 1, "maximum": 20, "default": 3},
            "limit": {"type": "integer", "minimum": 1, "maximum": 500, "default": 50},
        },
        ["build_id"],
        oneOf=_one_of(
            ["symbol_id"], ["location"], ["name"], ["instance_id"], ["instance_path"]
        ),
    ),
    "svw_diff_impact": _obj(
        {
            "build_id": HEX64,
            "base": GIT_REF,
            "head": GIT_REF,
            "depth": {"type": "integer", "minimum": 1, "maximum": 20, "default": 3},
            "limit": {"type": "integer", "minimum": 1, "maximum": 500, "default": 50},
        },
        ["build_id"],
    ),
    "svw_assertions": _obj(
        {
            "build_id": HEX64,
            "scope": text(1024),
            "kind": {"enum": ["source", "bind", "elaborated"]},
            **_PAGING,
        },
        ["build_id"],
    ),
    "svw_evidence": _obj(
        {
            "build_id": HEX64,
            "evidence_id": HEX64,
            "type": {"enum": EVIDENCE_TYPES},
            **_PAGING,
        },
        [],
        oneOf=_one_of(["build_id"], ["evidence_id"]),
    ),
}
TXN_ID = {"type": "string", "pattern": "^txn-[0-9a-f]{20}$"}
REQUEST_KEY = {"type": "string", "pattern": "^[A-Za-z0-9._:-]{8,128}$"}
_EDIT_ITEM = {
    "oneOf": [
        _obj(
            {
                "kind": {"const": "symbol_replacement"},
                "declaration_id": {"type": "string", "minLength": 1, "maxLength": 256},
                "new_body": {"type": "string", "maxLength": 60000},
            },
            ["kind", "declaration_id", "new_body"],
        ),
        _obj(
            {
                "kind": {"const": "exact_range"},
                "logical_path": {"type": "string", "minLength": 1, "maxLength": 1024},
                "start_byte": {"type": "integer", "minimum": 0, "maximum": 2**40},
                "end_byte": {"type": "integer", "minimum": 0, "maximum": 2**40},
                "expected_sha256": HEX64,
                "replacement": {"type": "string", "maxLength": 60000},
            },
            ["kind", "logical_path", "start_byte", "end_byte", "expected_sha256", "replacement"],
        ),
        _obj(
            {
                "kind": {"const": "slang_symbol"},
                "qualified_name": {"type": "string", "minLength": 1, "maxLength": 512},
                "symbol_id": HEX64,
                "replacement": {"type": "string", "maxLength": 60000},
                "expected_sha256": HEX64,
            },
            ["kind", "replacement"],
            oneOf=_one_of(["qualified_name"], ["symbol_id"]),
        ),
    ]
}
for _e in _EDIT_ITEM["oneOf"]:
    del _e["$schema"]
_EDIT_PROPS: dict[str, Any] = {
    "action": {"enum": ["prepare", "validate", "commit", "abort"]},
    "base_build_id": HEX64,
    "snapshot_id": HEX64,
    "edits": {"type": "array", "minItems": 1, "maxItems": 50, "items": _EDIT_ITEM},
    "validate_profiles": {"type": "array", "maxItems": 20, "items": PROFILE},
    "request_key": REQUEST_KEY,
    "transaction_id": TXN_ID,
    "expected_patch_hash": HEX64,
}


def _edit_branch(action: str, props: list[str]) -> dict[str, Any]:
    required = ["base_build_id", "snapshot_id", "edits", "request_key"] if action == "prepare" else props
    b = _obj(
        {"action": {"const": action}, **{k: _EDIT_PROPS[k] for k in props}},
        ["action", *required],
    )
    del b["$schema"]
    return b


# svw_edit: a discriminated union on `action` (plan 14.4). There is NO approval field anywhere:
# approval exists only in the trusted local store, created by the human CLI.
INPUT["svw_edit"] = _obj(
    dict(_EDIT_PROPS),
    ["action"],
    oneOf=[
        _edit_branch(
            "prepare",
            ["base_build_id", "snapshot_id", "edits", "validate_profiles", "request_key"],
        ),
        _edit_branch("validate", ["transaction_id"]),
        _edit_branch("commit", ["transaction_id", "expected_patch_hash", "request_key"]),
        _edit_branch("abort", ["transaction_id"]),
    ],
)
for _s in INPUT.values():  # nested "$schema": None placeholders are removed
    for _p in _s["properties"].values():
        if isinstance(_p, dict) and _p.get("$schema", 1) is None:
            del _p["$schema"]

_ITEMS = {"type": "array", "items": {"type": "object"}}
_PAGED = {"items": _ITEMS, "total_matches": {"type": "integer", "minimum": 0}}
_JOB = {
    "job_id": {"type": "string"},
    "kind": {"type": "string"},
    "profile": {"type": ["string", "null"]},
    "lifecycle": {"enum": ["QUEUED", "RUNNING", "SUCCEEDED", "FAILED", "CANCELLED", "TIMED_OUT"]},
    "build_id": {"type": ["string", "null"]},
    "snapshot_id": {"type": ["string", "null"]},
    "created_at": {"type": ["string", "null"]},
    "started_at": {"type": ["string", "null"]},
    "finished_at": {"type": ["string", "null"]},
    "exit_code": {"type": ["integer", "null"]},
    "error": {"type": ["string", "null"]},
    "cancel_requested": {"type": "boolean"},
    "owned_by_this_session": {"type": "boolean"},
    "already_running": {"type": "boolean"},
    "cancel_accepted": {"type": "boolean"},
    "cancel_note": {"type": "string"},
    "progress": {"type": "object"},
    "result": {"type": "object"},
}

DATA: dict[str, dict[str, Any]] = {
    "svw_status": {
        "type": "object",
        "properties": {
            "workspace_id": {"type": "string"},
            "isolation": {"type": "object"},
            "client_boundary": {"type": "object"},
            "backends": {"type": "object"},
            "capabilities": {"type": "object"},
            "profiles": _ITEMS,
            "pending_edits": {"type": "integer", "minimum": 0},
            "active_jobs": _ITEMS,
            "unlinked_evidence": {"type": "integer", "minimum": 0},
            "probe_requested": {"type": "boolean"},
        },
    },
    "svw_build": {"type": "object", "properties": _JOB},
    "svw_job": {"type": "object", "properties": _JOB},
    "svw_search": {
        "type": "object",
        "properties": {
            "items": _ITEMS,
            "backend_used": {"type": "string"},
            "degraded": {"type": "boolean"},
            "query": {"type": "string"},
        },
    },
    "svw_symbol": {"type": "object", "properties": {"candidates": _ITEMS}},
    "svw_references": {"type": "object", "properties": _PAGED},
    "svw_hierarchy": {"type": "object", "properties": {**_PAGED, "roots": {"type": "array"}}},
    "svw_connections": {"type": "object", "properties": _PAGED},
    "svw_diff_impact": {
        "type": "object",
        "properties": {
            "base": {"type": "string"},
            "head": {"type": "string"},
            "summary": {"type": "object"},
            "truncated_lists": {"type": "array"},
            "note": {"type": "string"},
        },
    },
    "svw_impact": {
        "type": "object",
        "properties": {
            "target": {"type": "object"},
            "summary": {"type": "object"},
            "truncated_lists": {"type": "array"},
            "note": {"type": "string"},
        },
    },
    "svw_assertions": {"type": "object", "properties": _PAGED},
    "svw_edit": {
        "type": "object",
        "properties": {
            "transaction_id": {"type": "string"},
            "state": {"type": "string"},
            "patch_hash": {"type": ["string", "null"]},
            "base_build_id": {"type": ["string", "null"]},
            "base_snapshot_id": {"type": ["string", "null"]},
            "candidate_snapshot_id": {"type": ["string", "null"]},
            "profiles": {"type": "array"},
            "files": {"type": "array"},
            "edits": _ITEMS,
            "edit_paths": {"type": "array"},
            "expires_at": {"type": ["string", "null"]},
            "auto_committable": {"type": ["boolean", "null"]},
            "not_run": {"type": "object"},
            "validation": {"type": "object"},
            "diff": {"type": "string"},
            "diagnostics_delta": {"type": "object"},
            "files_changed": {"type": "array"},
            "approval_command": {"type": "string"},
            "post_apply": {"type": "object"},
            "idempotent_replay": {"type": "boolean"},
        },
    },
    "svw_evidence": {
        "type": "object",
        "properties": {
            **_PAGED,
            "states": {"type": "object"},
            "unlinked_records_in_store": {"type": "integer", "minimum": 0},
        },
    },
}


def output_schema(name: str) -> dict[str, Any]:
    s = copy.deepcopy(Envelope.model_json_schema(mode="serialization"))
    s["$schema"] = SCHEMA_DIALECT
    s["title"] = f"{name} result envelope"
    s["properties"]["data"] = DATA[name]
    return s


def input_schema(name: str) -> dict[str, Any]:
    return copy.deepcopy(INPUT[name])


def _dump(obj: Any) -> str:
    return json.dumps(obj, indent=2, sort_keys=True, ensure_ascii=False) + "\n"


def mcp_schema_files(names: tuple[str, ...] = TOOL_NAMES) -> dict[str, str]:
    out: dict[str, str] = {}
    for n in names:
        out[f"{n}.input.json"] = _dump(input_schema(n))
        out[f"{n}.output.json"] = _dump(output_schema(n))
    return out


def emit_mcp_schemas(out_dir: Path) -> list[Path]:
    out_dir.mkdir(parents=True, exist_ok=True)
    written = []
    for name, text_ in mcp_schema_files().items():
        p = out_dir / name
        p.write_text(text_, encoding="utf-8")
        written.append(p)
    return written


def check_mcp_schemas(out_dir: Path) -> list[str]:
    bad = []
    for name, text_ in mcp_schema_files().items():
        p = out_dir / name
        if not p.is_file() or p.read_text(encoding="utf-8") != text_:
            bad.append(f"mcp/{name}")
    return bad
