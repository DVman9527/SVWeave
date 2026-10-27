"""Bounded decoder for the Verilator 5.052 JSON AST interchange subset.

The version is caller-declared, not authenticated. No executable, source file, pointer, or
metadata path is followed. Output is supplemental EXTERNAL_DECLARED inventory, never slang
resolved references. The format pin is checked against upstream v5.052 regression goldens.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from typing import Any

from svweave.domain.errors import ErrorCode, SvwError

SCHEMA_VERSION = "svw-verilator-supplement-1"
SUPPORTED_VERSION = "5.052"
MAX_TREE_BYTES = 16 * 1024 * 1024
MAX_META_BYTES = 2 * 1024 * 1024
MAX_NODES = 100_000
MAX_ITEMS = 10_000
MAX_DEPTH = 128
MAX_STRING = 4096
_LOC = re.compile(r"([^,]+),(\d+):(\d+),(\d+):(\d+)\Z")
_KINDS = frozenset(
    {
        "MODULE",
        "PACKAGE",
        "CLASS",
        "VAR",
        "TYPEDEF",
        "FUNC",
        "TASK",
        "TASKREF",
        "FUNCREF",
        "METHODCALL",
        "VARREF",
        "VARXREF",
        "CELL",
        "PIN",
    }
)
_ATTRIBUTES = frozenset(
    {
        "dtypeName",
        "varType",
        "direction",
        "lifetime",
        "rand",
        "range",
        "declRange",
        "keyword",
        "origName",
        "origParamName",
        "dotted",
        "dtypep",
        "refDTypep",
        "classp",
        "classOrPackagep",
        "taskp",
        "varp",
        "modp",
        "modVarp",
        "isParam",
        "isConst",
        "isVirtual",
        "isStatic",
    }
)
LIMITATIONS = [
    "External JSON and tool version are declared, not authenticated tool execution evidence.",
    "Only selected surviving AST nodes are inventoried; absence is not proof of absence.",
    "Elaboration can erase symbolic dependencies, inactive code and generic bodies.",
    "AST locations and pointers are not verified source tokens or slang resolved references.",
    "No factory/config-db resolution, virtual dispatch, phase execution or simulation is computed.",
]


def _invalid(message: str) -> SvwError:
    return SvwError(ErrorCode.INVALID_CONFIG, message, {"reason": "invalid_verilator_json"})


def _limit(message: str) -> SvwError:
    return SvwError(ErrorCode.RESOURCE_LIMIT, message, {"reason": "verilator_import_limit"})


def _pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result and (type(result[key]) is not type(value) or result[key] != value):
            raise _invalid("conflicting duplicate JSON object key")
        result[key] = value
    return result


def _constant(value: str) -> Any:
    raise _invalid(f"non-finite JSON constant: {value}")


def _float(value: str) -> float:
    number = float(value)
    if not math.isfinite(number):
        raise _invalid("non-finite JSON number")
    return number


def _decode(data: bytes, maximum: int) -> dict[str, Any]:
    if len(data) > maximum:
        raise _limit("Verilator artifact exceeds byte limit")
    try:
        value = json.loads(
            data, object_pairs_hook=_pairs, parse_constant=_constant, parse_float=_float
        )
    except (ValueError, UnicodeError, RecursionError) as exc:
        raise _invalid("invalid or excessively nested JSON") from exc
    if not isinstance(value, dict):
        raise _invalid("Verilator artifact root must be an object")
    stack: list[tuple[Any, int]] = [(value, 0)]
    visited = 0
    while stack:
        item, depth = stack.pop()
        visited += 1
        if depth > MAX_DEPTH or visited > 1_000_000:
            raise _limit("JSON structure depth/value limit exceeded")
        if isinstance(item, dict):
            stack.extend((child, depth + 1) for child in item.values())
        elif isinstance(item, list):
            stack.extend((child, depth + 1) for child in item)
    return value


def _string(value: Any, field: str, *, empty: bool = False) -> str:
    if not isinstance(value, str) or (not empty and not value) or "\x00" in value:
        raise _invalid(f"invalid {field}")
    if len(value) > MAX_STRING:
        raise _limit(f"{field} exceeds string limit")
    return value


def parse_tree(tree_bytes: bytes, meta_bytes: bytes, tool_version: str) -> dict[str, Any]:
    """Decode a complete NETLIST JSON object and its metadata, without filesystem access.

    Filename strings are opaque producer labels (including absolute paths); consumers must
    use an explicit safe source mapping, never open a path from this result. Pointer IDs have
    only artifact-local meaning. Limits fail closed rather than silently dropping inventory.
    """
    if tool_version != SUPPORTED_VERSION:
        raise SvwError(
            ErrorCode.UNSUPPORTED_OPTION,
            "unsupported Verilator JSON version",
            {"supported": SUPPORTED_VERSION, "declared": tool_version},
        )
    tree = _decode(tree_bytes, MAX_TREE_BYTES)
    meta = _decode(meta_bytes, MAX_META_BYTES)
    if tree.get("type") != "NETLIST":
        raise _invalid("expected Verilator NETLIST root")
    raw_files = meta.get("files")
    if not isinstance(raw_files, dict) or not raw_files:
        raise _invalid("metadata requires a nonempty files mapping")
    files: dict[str, str] = {}
    for key, entry in raw_files.items():
        _string(key, "file id")
        if not isinstance(entry, dict):
            raise _invalid("invalid file metadata")
        files[key] = _string(entry.get("filename"), "filename")
    items: list[dict[str, Any]] = []
    stack: list[tuple[dict[str, Any], str | None, int]] = [(tree, None, 0)]
    seen: set[str] = set()
    count = 0
    while stack:
        node, parent_id, depth = stack.pop()
        count += 1
        if count > MAX_NODES or depth > MAX_DEPTH:
            raise _limit("Verilator AST node/depth limit exceeded")
        kind = _string(node.get("type"), "node type")
        node_id = _string(node.get("addr"), "node id")
        if node_id in seen:
            raise _invalid("duplicate AST node id")
        seen.add(node_id)
        name = _string(node.get("name"), "node name", empty=True)
        verilog_name = _string(node.get("verilogName", name), "Verilog name", empty=True)
        loc = _string(node.get("loc"), "location")
        match = _LOC.fullmatch(loc)
        if match is None:
            raise _invalid("invalid Verilator location")
        file_id = match[1]
        if file_id not in files:
            raise _invalid("AST location refers to unknown metadata file id")
        sl, sc, el, ec = (int(match[i]) for i in range(2, 6))
        if (el, ec) < (sl, sc):
            raise _invalid("reversed AST location")
        if (kind in _KINDS or kind.endswith("DTYPE")) and sl > 0 and sc > 0:
            attrs: dict[str, Any] = {}
            for field in sorted(_ATTRIBUTES & node.keys()):
                value = node[field]
                if isinstance(value, list):
                    continue  # some node classes reuse a pointer name for a child list
                if isinstance(value, str):
                    attrs[field] = _string(value, field, empty=True)
                elif isinstance(value, bool):
                    attrs[field] = value
                else:
                    raise _invalid(f"invalid scalar AST attribute {field}")
            items.append(
                {
                    "node_type": kind,
                    "name": name,
                    "verilog_name": verilog_name,
                    "node_id": node_id,
                    "parent_node_id": parent_id,
                    "source_file_id": file_id,
                    "source_file": files[file_id],
                    "source_span": {
                        "start_line": sl,
                        "start_column": sc,
                        "end_line": el,
                        "end_column": ec,
                    },
                    "raw_location": loc,
                    "attributes": attrs,
                }
            )
            if len(items) > MAX_ITEMS:
                raise _limit("Verilator inventory item limit exceeded")
        children: list[dict[str, Any]] = []
        for value in node.values():
            if isinstance(value, list):
                for child in value:
                    if not isinstance(child, dict):
                        raise _invalid("AST child lists must contain node objects")
                    children.append(child)
            elif isinstance(value, dict):
                raise _invalid("unexpected nested object in AST node")
        stack.extend((child, node_id, depth + 1) for child in reversed(children))
    return {
        "schema_version": SCHEMA_VERSION,
        "tool_version": tool_version,
        "tree_sha256": hashlib.sha256(tree_bytes).hexdigest(),
        "meta_sha256": hashlib.sha256(meta_bytes).hexdigest(),
        "files": files,
        "items": items,
        "node_count": count,
        "omitted_node_count": count - len(items),
        "limitations": list(LIMITATIONS),
        "completeness": "PARTIAL",
        "runtime_resolution": "not_computed",
        "trust_class": "EXTERNAL_DECLARED",
    }
