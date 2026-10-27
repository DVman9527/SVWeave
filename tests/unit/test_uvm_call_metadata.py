"""UVM-02/03 pure metadata tests; these do not parse HDL or replace integration."""

from __future__ import annotations

import pytest

from svweave.adapters.slang.uvm_calls import call_metadata, classify_uvm_call, text_is_truncated


@pytest.mark.parametrize(
    "owner,operation,api",
    [
        ("uvm_config_db", "set", "config_db"),
        ("uvm_config_db", "get", "config_db"),
        ("uvm_config_db", "exists", "config_db"),
        ("uvm_config_db", "wait_modified", "config_db"),
        ("uvm_component", "set_inst_override_by_type", "factory"),
        ("uvm_component", "set_type_override", "factory"),
        ("uvm_component", "set_inst_override", "factory"),
        ("uvm_factory", "set_type_override_by_name", "factory"),
        ("uvm_default_factory", "set_type_override_by_type", "factory"),
        ("uvm_object_registry", "create", "factory"),
        ("uvm_component_registry", "set_inst_override", "factory"),
    ],
)
def test_UVM_02_03_resolved_api_allowlist(owner: str, operation: str, api: str) -> None:
    assert classify_uvm_call(f"uvm_pkg::{owner}::{operation}") == (api, operation)


@pytest.mark.parametrize(
    "path",
    [
        "user_pkg::uvm_config_db::set",
        "user_pkg::set",
        "uvm_pkg::other::get",
        "uvm_pkg::uvm_config_db::new",
        "uvm_pkg::uvm_config_db#(int)::set",
        "uvm_pkg::uvm_component::build_phase",
        "uvm_pkg::uvm_component::create",
        "uvm_pkg::uvm_component::set_type_override_by_name",
        "uvm_pkg::uvm_component::set_inst_override_by_name",
    ],
)
def test_UVM_03_no_spelling_or_phase_heuristics(path: str) -> None:
    assert classify_uvm_call(path) is None


def metadata(written: list[dict], **kw: object) -> dict:
    return call_metadata(
        ("config_db", "set"),
        "uvm_pkg::uvm_config_db::set",
        "uvm_pkg::uvm_config_db#(int)::set",
        ["cntxt", "inst_name", "field_name", "value"],
        written,
        syntax_available=bool(kw.get("syntax_available", True)),
        argument_count=int(kw.get("count", len(written))),
    )


def test_UVM_03_named_reordered_arguments_preserve_source_text() -> None:
    result = metadata(
        [
            {"kind": "named", "name": "value", "expression": "width + 1"},
            {"kind": "named", "name": "inst_name", "expression": '"env.*"'},
            {"kind": "named", "name": "cntxt", "expression": "this"},
        ]
    )
    assert [a["formal"] for a in result["arguments"]] == ["value", "inst_name", "cntxt"]
    assert result["arguments"][0]["expression"] == "width + 1"
    assert result["runtime_resolution"] == "not_computed"
    assert result["evidence"] == "static_call_site"
    assert not result["arguments_truncated"]
    assert "field_name" not in [a["formal"] for a in result["arguments"]]


def test_UVM_03_ordered_empty_and_named_empty_are_not_default_values() -> None:
    result = metadata(
        [
            {"kind": "ordered", "expression": "null"},
            {"kind": "empty", "expression": None},
            {"kind": "named", "name": "value", "expression": None},
        ]
    )
    assert [a["formal"] for a in result["arguments"]] == ["cntxt", "inst_name", "value"]
    assert [a["expression"] for a in result["arguments"]] == ["null", None, None]


def test_UVM_03_invalid_associations_remain_visible() -> None:
    result = metadata(
        [
            {"kind": "named", "name": "value", "expression": "1"},
            {"kind": "named", "name": "value", "expression": "2"},
            {"kind": "named", "name": "bogus", "expression": "3"},
            {"kind": "ordered", "expression": "4"},
            {"kind": "unknown", "expression": None},
        ]
    )
    assert [a["association_issue"] for a in result["arguments"]] == [
        None,
        "duplicate_argument",
        "unknown_named_argument",
        "ordered_after_named",
        "unsupported_argument_syntax",
    ]
    assert all(a["formal"] is None for a in result["arguments"][2:])


def test_UVM_03_capture_bounds_and_missing_syntax_are_explicit() -> None:
    assert metadata([], count=17)["arguments_truncated"]
    unavailable = metadata([], count=4, syntax_available=False)
    assert unavailable["arguments"] == [] and not unavailable["syntax_available"]
    excess = metadata([{"kind": "ordered", "expression": "0"}] * 5)
    assert excess["arguments"][-1]["association_issue"] == "too_many_arguments"


@pytest.mark.parametrize(
    "location,limit,expected",
    [
        ({"start": 10, "end": 20}, 10, False),
        ({"start": 10, "end": 21}, 10, True),
        ({"start": 10, "end": 20, "outside_root": True}, 5, None),
        (None, 10, None),
    ],
)
def test_UVM_03_text_cap_uses_byte_span_not_ellipsis(
    location: dict | None, limit: int, expected: bool | None
) -> None:
    assert text_is_truncated(location, limit) is expected
    result = metadata([{"kind": "ordered", "expression": "source…", "text_truncated": expected}])
    assert result["arguments"][0]["text_truncated"] is expected
    assert result["arguments_truncated"] is False  # list bound is independent
