"""SEM/SEC: external static inventory, not real tool execution or UVM acceptance."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from svweave.adapters.verilator import importer
from svweave.domain.errors import ErrorCode, SvwError

FIXTURES = Path(__file__).parents[2] / "fixtures" / "verilator"


def fixture(name: str) -> dict:
    return importer.parse_tree(
        (FIXTURES / f"{name}.out").read_bytes(),
        (FIXTURES / f"{name}.synthetic.tree.meta.json").read_bytes(),
        "5.052",
    )


def test_SEM_verilator_official_fixtures_unchanged() -> None:
    hashes = json.loads((FIXTURES / "SHA256SUMS.json").read_text())
    for name, expected in hashes.items():
        assert hashlib.sha256((FIXTURES / name).read_bytes()).hexdigest() == expected


@pytest.mark.parametrize(
    "name,count",
    [
        ("t_constraint_json_only", 29),
        ("t_json_only_tag", 34),
        ("t_json_only_first", 54),
        ("t_var_port_json_only", 42),
    ],
)
def test_SEM_verilator_official_ast_inventory(name: str, count: int) -> None:
    result = fixture(name)
    assert len(result["items"]) == count
    assert result["completeness"] == "PARTIAL"
    assert result["trust_class"] == "EXTERNAL_DECLARED"
    assert result["runtime_resolution"] == "not_computed"
    assert "references" not in result


def test_SEM_verilator_class_method_and_type_metadata() -> None:
    result = fixture("t_constraint_json_only")
    items = result["items"]
    cls = next(i for i in items if i["node_type"] == "CLASS" and i["name"] == "Packet")
    member = next(i for i in items if i["name"] == "header")
    assert member["parent_node_id"] == cls["node_id"]
    assert member["attributes"]["varType"] == "MEMBER"
    assert any(i["node_type"] == "FUNC" and i["name"] == "strings_equal" for i in items)
    ref = next(i for i in items if i["node_type"] == "CLASSREFDTYPE")
    assert ref["attributes"]["classp"] == cls["node_id"]
    assert ref["source_span"]["start_line"] == 69


def test_SEM_verilator_does_not_reconstruct_erased_width_reference() -> None:
    items = fixture("t_json_only_first")["items"]
    var = next(i for i in items if i["node_type"] == "VAR" and i["name"] == "d")
    dtype = next(i for i in items if i["node_id"] == var["attributes"]["dtypep"])
    assert dtype["attributes"]["range"] == "3:0"
    assert "WIDTH" not in json.dumps(dtype)
    assert "resolved_target" not in var


def sample() -> tuple[dict, dict]:
    tree = {
        "type": "NETLIST",
        "name": "$root",
        "addr": "(A)",
        "loc": "a,0:0,0:0",
        "modulesp": [
            {"type": "CLASS", "name": "C", "addr": "(B)", "loc": "d,2:1,2:6", "stmtsp": []}
        ],
    }
    meta = {"files": {"a": {"filename": "<built-in>"}, "d": {"filename": "c.sv"}}}
    return tree, meta


def parse(tree: dict, meta: dict) -> dict:
    return importer.parse_tree(json.dumps(tree).encode(), json.dumps(meta).encode(), "5.052")


@pytest.mark.parametrize("location", ["wrong", "z,2:1,2:3", "d,3:1,2:3", "d,-1:1,2:3"])
def test_SEC_verilator_bad_locations_fail_closed(location: str) -> None:
    tree, meta = sample()
    tree["modulesp"][0]["loc"] = location
    with pytest.raises(SvwError, match="INVALID_CONFIG"):
        parse(tree, meta)


@pytest.mark.parametrize("data", [b"null", b"[]", b"{", b'"bad"', b'{"x":NaN}', b"\xff"])
def test_SEC_verilator_malformed_json(data: bytes) -> None:
    with pytest.raises(SvwError):
        importer.parse_tree(data, b"{}", "5.052")


def test_SEC_verilator_conflicting_duplicate_keys_rejected() -> None:
    with pytest.raises(SvwError, match="conflicting duplicate"):
        importer.parse_tree(b'{"type":"NETLIST","type":"CLASS"}', b"{}", "5.052")


def test_SEC_verilator_duplicate_node_ids_rejected() -> None:
    tree, meta = sample()
    tree["modulesp"].append(tree["modulesp"][0])
    with pytest.raises(SvwError, match="duplicate AST node"):
        parse(tree, meta)


def test_SEC_verilator_metadata_paths_are_opaque(monkeypatch: pytest.MonkeyPatch) -> None:
    tree, meta = sample()
    meta["files"]["d"]["filename"] = "../../outside/not-a-source.sv"

    def forbidden(*args: object, **kwargs: object) -> None:
        raise AssertionError("parser must never open producer-provided paths")

    monkeypatch.setattr("builtins.open", forbidden)
    result = parse(tree, meta)
    assert result["items"][0]["source_file"] == "../../outside/not-a-source.sv"


@pytest.mark.parametrize("version", ["5.050", "5.054", "", "Verilator 5.052", "5.052\x00"])
def test_SEC_verilator_format_version_fail_closed(version: str) -> None:
    with pytest.raises(SvwError) as exc:
        importer.parse_tree(b"{}", b"{}", version)
    assert exc.value.code == ErrorCode.UNSUPPORTED_OPTION


@pytest.mark.parametrize(
    "limit", ["MAX_TREE_BYTES", "MAX_META_BYTES", "MAX_NODES", "MAX_ITEMS", "MAX_DEPTH"]
)
def test_SEC_verilator_limits_fail_closed(monkeypatch: pytest.MonkeyPatch, limit: str) -> None:
    tree, meta = sample()
    monkeypatch.setattr(importer, limit, 0)
    with pytest.raises(SvwError) as exc:
        parse(tree, meta)
    assert exc.value.code == ErrorCode.RESOURCE_LIMIT


def test_SEC_verilator_bad_child_and_attribute() -> None:
    tree, meta = sample()
    tree["modulesp"][0]["dtypep"] = 17
    with pytest.raises(SvwError, match="scalar AST attribute"):
        parse(tree, meta)
    del tree["modulesp"][0]["dtypep"]
    tree["modulesp"].append("not a node")
    with pytest.raises(SvwError, match="child lists"):
        parse(tree, meta)


@pytest.mark.parametrize("artifact", ["tree", "meta"])
def test_SEC_verilator_numeric_overflow_rejected(artifact: str) -> None:
    tree, meta = sample()
    raw_tree = json.dumps(tree).encode()
    raw_meta = json.dumps(meta).encode()
    if artifact == "tree":
        raw_tree = b'{"ignored":1e999,' + raw_tree[1:]
    else:
        raw_meta = b'{"ignored":1e999,' + raw_meta[1:]
    with pytest.raises(SvwError, match="non-finite"):
        importer.parse_tree(raw_tree, raw_meta, "5.052")


def test_SEC_verilator_metadata_nesting_is_bounded() -> None:
    tree, meta = sample()
    nested = {}
    meta["ignored"] = nested
    for _ in range(importer.MAX_DEPTH + 1):
        child = {}
        nested["child"] = child
        nested = child
    with pytest.raises(SvwError, match="depth/value limit"):
        parse(tree, meta)
