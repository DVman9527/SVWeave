"""Pure formatting of resolved UVM call sites; never evaluates HDL or runtime matches.

Recognition uses the resolved declaration's lexical path, not source spelling. This is
an allowlist for the pinned UVM API, not authentication of a library named ``uvm_pkg``.
"""

from __future__ import annotations

from typing import Any

_CONFIG_OPS = frozenset({"set", "get", "exists", "wait_modified"})
_OVERRIDE_OPS = frozenset(
    {
        "set_type_override_by_type",
        "set_type_override_by_name",
        "set_inst_override_by_type",
        "set_inst_override_by_name",
    }
)


def classify_uvm_call(lexical_path: str) -> tuple[str, str] | None:
    owner, _, operation = lexical_path.rpartition("::")
    if owner == "uvm_pkg::uvm_config_db" and operation in _CONFIG_OPS:
        return "config_db", operation
    if (
        owner in {"uvm_pkg::uvm_factory", "uvm_pkg::uvm_default_factory"}
        and operation in _OVERRIDE_OPS
    ):
        return "factory", operation
    if owner == "uvm_pkg::uvm_component" and operation in {
        "set_type_override_by_type",
        "set_inst_override_by_type",
        "set_type_override",
        "set_inst_override",
    }:
        return "factory", operation
    if owner in {
        "uvm_pkg::uvm_object_registry",
        "uvm_pkg::uvm_component_registry",
    } and operation in {"create", "set_type_override", "set_inst_override"}:
        return "factory", operation
    return None


def call_metadata(
    classification: tuple[str, str],
    lexical_path: str,
    specialization_path: str,
    formal_names: list[str],
    written: list[dict[str, Any]],
    *,
    syntax_available: bool,
    argument_count: int,
) -> dict[str, Any]:
    """Associate written arguments, without treating omitted/default text as a value.

    ``written`` comes from slang argument syntax: kind (ordered/named/empty/unknown),
    name (named only), and expression text. Keep source order and
    duplicate/unknown associations visible in erroneous PARTIAL compilations.
    """
    bindings = []
    seen: set[str] = set()
    named_seen = False
    for position, arg in enumerate(written):
        kind = arg["kind"]
        formal = None
        issue = None
        if kind == "named":
            named_seen = True
            if arg.get("name") in formal_names:
                formal = arg["name"]
            else:
                issue = "unknown_named_argument"
        elif kind in {"ordered", "empty"}:
            if named_seen:
                issue = "ordered_after_named"
            elif position < len(formal_names):
                formal = formal_names[position]
            else:
                issue = "too_many_arguments"
        else:
            issue = "unsupported_argument_syntax"
        if formal is not None:
            if formal in seen:
                issue = "duplicate_argument"
            seen.add(formal)
        bindings.append({**arg, "formal": formal, "association_issue": issue})
    return {
        "api": classification[0],
        "operation": classification[1],
        "declaration": lexical_path,
        "specialization_path": specialization_path,
        "evidence": "static_call_site",
        "runtime_resolution": "not_computed",
        "syntax_available": syntax_available,
        "argument_count": argument_count,
        "arguments_truncated": argument_count > len(written),
        "arguments": bindings,
    }


def text_is_truncated(location: dict[str, Any] | None, max_bytes: int) -> bool | None:
    """Mirror Locator's byte cap without guessing from an expression's suffix."""
    if location is None or location.get("outside_root"):
        return None
    return bool(location["end"] - location["start"] > max_bytes)
