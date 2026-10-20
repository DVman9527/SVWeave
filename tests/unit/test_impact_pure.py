"""IMP-U: the pure parts of the impact use case."""

from __future__ import annotations

from typing import Any

from svweave.application.impact import _closure, _fit, _twins


def test_IMP_U01_closure_is_breadth_first_shortest_level_and_loop_safe() -> None:
    up = {"leaf": {"mid": 2, "top": 1}, "mid": {"top": 4}, "top": {"leaf": 9}}  # top->leaf is a cycle
    got = _closure(up, "leaf", 5)
    assert got == [(1, "mid", 2), (1, "top", 1)]  # top is level 1 (shortest), not again via mid
    assert _closure(up, "mid", 1) == [(1, "top", 4)]
    assert _closure(up, "mid", 3) == [(1, "top", 4), (2, "leaf", 9)]  # start itself never listed
    assert _closure({}, "x", 3) == []


class Row(dict[str, Any]):
    """sqlite3.Row stand-in: item access only."""


def _row(kind: str, did: str, **over: Any) -> Row:
    base = {"name": "p", "file": "a.sv", "span_start": 1, "span_end": 2, "scope": "m", "kind": kind,
            "declaration_id": did}
    return Row({**base, **over})


def test_IMP_U02_port_and_implicit_net_are_one_entity_anything_else_is_ambiguous() -> None:
    net, port = _row("net", "n"), _row("port", "p")
    got = _twins([net, port])  # type: ignore[arg-type]
    assert got is not None and [r["kind"] for r in got] == ["port", "net"]  # port first
    assert _twins([net]) is None  # a single row is not a pair
    other = _row("net", "x", span_start=9)
    assert _twins([net, other]) is None  # different span: genuinely ambiguous
    assert _twins([net, _row("net", "y", scope="other")]) is None  # different scope


def test_IMP_U03_fit_cuts_the_largest_list_and_reports_it() -> None:
    data: dict[str, Any] = {
        "target": {"x": 1},
        "instances": [{"p": "a" * 200} for _ in range(100)],
        "dependents": [{"q": "b"} for _ in range(5)],
        "instances_total": 100,
    }
    cut = _fit(data, 4000)
    assert cut == ["instances"] and 1 <= len(data["instances"]) < 100
    assert len(data["dependents"]) == 5 and data["instances_total"] == 100  # totals stay complete
    assert _fit({"instances": [1]}, 10) == []  # nothing left to cut: no infinite loop
