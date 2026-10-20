"""Tool contract: schema files in sync, strictness, honest annotations (plan 14.2, 14.3)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from jsonschema import Draft202012Validator

from svweave.config.schemas import check_schemas
from svweave.gateway import schemas
from svweave.gateway.tools import TOOLS

REPO = Path(__file__).resolve().parents[2]
READ_ONLY = {
    "svw_symbol",
    "svw_references",
    "svw_hierarchy",
    "svw_connections",
    "svw_impact",
    "svw_diff_impact",
    "svw_rename_preview",
    "svw_assertions",
    "svw_evidence",
}


def test_exactly_the_planned_tools() -> None:
    assert set(TOOLS) == set(schemas.TOOL_NAMES)
    assert len(TOOLS) == 14 and "svw_edit" in TOOLS
    assert not any("approv" in t for t in TOOLS)  # approval is never an MCP tool


def test_schema_files_are_in_sync_with_the_code() -> None:
    assert schemas.check_mcp_schemas(REPO / "schemas" / "mcp") == []
    assert check_schemas(REPO / "schemas") == []  # includes evidence.schema.json
    assert (REPO / "schemas" / "evidence.schema.json").is_file()


@pytest.mark.parametrize("name", schemas.TOOL_NAMES)
def test_schemas_are_valid_strict_and_bounded(name: str) -> None:
    inp = json.loads((REPO / "schemas" / "mcp" / f"{name}.input.json").read_text())
    out = json.loads((REPO / "schemas" / "mcp" / f"{name}.output.json").read_text())
    Draft202012Validator.check_schema(inp)
    Draft202012Validator.check_schema(out)
    assert inp["type"] == "object" and inp["additionalProperties"] is False
    props: dict[str, Any] = inp["properties"]
    if "limit" in props:
        assert props["limit"]["maximum"] == 500 and props["limit"]["minimum"] == 1
    for pname, p in props.items():
        if pname.endswith("_id") and pname not in ("job_id", "instance_path", "transaction_id"):
            assert p["pattern"] == "^[0-9a-f]{64}$", pname
        if p.get("type") == "string":
            assert "maxLength" in p or "pattern" in p or "enum" in p, f"{name}.{pname} unbounded"
    assert out["properties"]["data"]["type"] == "object"
    assert out["additionalProperties"] is False  # envelope keys are closed
    assert "outcome" in out["properties"] and "error" in out["properties"]


def test_semantic_tools_require_build_id_and_search_requires_snapshot_id() -> None:
    for n in ("svw_symbol", "svw_references", "svw_hierarchy", "svw_connections", "svw_assertions"):
        assert "build_id" in schemas.INPUT[n]["required"]
    assert "snapshot_id" in schemas.INPUT["svw_search"]["required"]
    assert "build_id" not in schemas.INPUT["svw_search"]["properties"]


def test_annotations_are_honest() -> None:
    for name, t in TOOLS.items():
        a = t.annotations
        assert a["destructiveHint"] is (name == "svw_edit")
        assert a["openWorldHint"] is False
        assert (a["readOnlyHint"] is True) == (name in READ_ONLY), name
    assert TOOLS["svw_build"].annotations["readOnlyHint"] is False
    assert TOOLS["svw_job"].annotations["readOnlyHint"] is False  # cancel affects workers
    assert TOOLS["svw_status"].annotations["readOnlyHint"] is False  # probe starts backends
    assert TOOLS["svw_search"].annotations["readOnlyHint"] is False  # graph indexing cache


def test_oneof_rules_in_input_schemas() -> None:
    v = Draft202012Validator(schemas.INPUT["svw_symbol"])
    h = "a" * 64
    assert v.is_valid({"build_id": h, "symbol_id": h})
    assert v.is_valid({"build_id": h, "location": {"file": "a.sv", "byte": 3}})
    assert not v.is_valid({"build_id": h})
    assert not v.is_valid({"build_id": h, "symbol_id": h, "name": "x"})
    assert not v.is_valid({"build_id": h, "location": {"file": "a.sv", "byte": 3, "x": 1}})
    e = Draft202012Validator(schemas.INPUT["svw_evidence"])
    assert e.is_valid({"build_id": h}) and e.is_valid({"evidence_id": h})
    assert not e.is_valid({}) and not e.is_valid({"build_id": h, "evidence_id": h})
    hv = Draft202012Validator(schemas.INPUT["svw_hierarchy"])
    assert hv.is_valid({"build_id": h, "top": "t"})
    assert not hv.is_valid({"build_id": h, "top": "t", "instance_id": h})


def test_scope_pattern_rejects_traversal_and_absolute() -> None:
    v = Draft202012Validator(schemas.INPUT["svw_search"])
    h = "a" * 64
    ok = {"snapshot_id": h, "query": "x"}
    assert v.is_valid({**ok, "scope": "rtl/**/*.sv"})
    for bad in ("/etc/passwd", "../x", "a/../b", "a/.."):
        assert not v.is_valid({**ok, "scope": bad}), bad
