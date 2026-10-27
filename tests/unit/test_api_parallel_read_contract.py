"""API-PAR / HDL-R01: canonicalization must not hide coverage or freshness regressions.

Synthetic envelopes exercise only the comparison helper; real backend/rebuild coverage remains
in tests/integration/test_api_parallel_reads.py (plan 9.6, 10.4 and 14.5).
"""

from __future__ import annotations

from copy import deepcopy
from typing import Any

import pytest

from tests.integration.test_api_parallel_reads import norm


def _envelope(freshness: str = "CURRENT", *, search: bool = False) -> dict[str, Any]:
    warnings = ["persistent coverage warning"]
    refs = {
        "status": "PARTIAL",
        "features": {"dimension_expressions": "UNSUPPORTED"},
        "scopes": {"inactive_branches": "NOT_SEARCHED"},
        "observations": {"unresolved_references": 0},
        "blockers": ["known_reference_omissions", "unsearched_source_scopes"],
    }
    if freshness != "CURRENT":
        warnings.append(
            "the workspace changed since this snapshot; results describe the snapshot"
            if search else
            f"freshness is {freshness}: the workspace changed since this build's snapshot; "
            "results describe the snapshot, not the current files"
        )
        refs["blockers"].append(f"freshness_{freshness.lower()}")
    return {
        "request_id": "one", "build_id": None if search else "build", "snapshot_id": "snapshot",
        "freshness": freshness, "warnings": warnings,
        "analysis_coverage": {"completeness": "COMPLETE", "reference_coverage": None if search else refs},
        "data": {"items": ["unchanged"]}, "pagination": {"next_cursor": "signed-cursor"},
    }


@pytest.mark.parametrize("freshness,search", [
    ("CURRENT", False), ("STALE", False), ("HISTORICAL", False),
    ("CURRENT", True), ("STALE", True),
])
def test_API_PAR_freshness_canonicalization_validates_but_preserves_the_response(
    freshness: str, search: bool,
) -> None:
    env = _envelope(freshness, search=search)
    before = deepcopy(env)
    baseline = _envelope(search=search)
    assert norm(env, freshness_moves=True) == norm(baseline, freshness_moves=True)
    assert env == before  # the original still carries its exact warning and blocker
    if freshness != "CURRENT":
        assert norm(env) != norm(baseline)  # default comparisons never hide freshness


@pytest.mark.parametrize("freshness,blockers", [
    ("STALE", []),
    ("STALE", ["freshness_historical"]),
    ("STALE", ["freshness_stale", "freshness_stale"]),
    ("STALE", ["freshness_stale", "freshness_unknown"]),
    ("HISTORICAL", ["freshness_stale"]),
    ("CURRENT", ["freshness_stale"]),
])
def test_API_PAR_missing_duplicate_or_contradictory_freshness_blocker_fails(
    freshness: str, blockers: list[str],
) -> None:
    env = _envelope(freshness)
    env["analysis_coverage"]["reference_coverage"]["blockers"] = ["known_reference_omissions", *blockers]
    with pytest.raises(AssertionError):
        norm(env, freshness_moves=True)


@pytest.mark.parametrize("search", [False, True])
@pytest.mark.parametrize("change", ["missing", "duplicate", "contradictory", "changed_text"])
def test_API_PAR_freshness_warning_must_be_present_exactly_once_and_truthful(
    search: bool, change: str,
) -> None:
    env = _envelope("STALE", search=search)
    warning = env["warnings"].pop()
    if change == "duplicate":
        env["warnings"].extend([warning, warning])
    elif change == "contradictory":
        env["warnings"].append("freshness is CURRENT: incorrect warning")
    elif change == "changed_text":
        env["warnings"].append(warning + " changed")
    with pytest.raises(AssertionError):
        norm(env, freshness_moves=True)


@pytest.mark.parametrize("path,value", [
    (("data", "items"), ["changed"]),
    (("pagination", "next_cursor"), "different-cursor"),
    (("analysis_coverage", "completeness"), "PARTIAL"),
    (("analysis_coverage", "reference_coverage", "status"), "UNKNOWN"),
    (("analysis_coverage", "reference_coverage", "features"), {}),
    (("analysis_coverage", "reference_coverage", "scopes"), {}),
    (("analysis_coverage", "reference_coverage", "observations"), {"unresolved_references": 1}),
    (("analysis_coverage", "reference_coverage", "blockers"), ["unexpected", "freshness_stale"]),
])
def test_API_PAR_other_data_and_coverage_changes_still_fail_equality(
    path: tuple[str, ...], value: Any,
) -> None:
    baseline = _envelope()
    env = _envelope("STALE")
    target = env
    for part in path[:-1]:
        target = target[part]
    target[path[-1]] = value
    assert norm(env, freshness_moves=True) != norm(baseline, freshness_moves=True)


def test_API_PAR_other_warnings_remain_compared_and_only_request_id_is_ignored() -> None:
    baseline = _envelope()
    env = deepcopy(baseline)
    env["request_id"] = "two"
    assert norm(env) == norm(baseline)
    env = _envelope("STALE")
    env["warnings"].insert(0, "an unrelated warning mentioning workspace changed since this snapshot")
    assert norm(env, freshness_moves=True) != norm(baseline, freshness_moves=True)


def test_API_PAR_build_reference_coverage_cannot_disappear() -> None:
    env = _envelope("STALE")
    env["analysis_coverage"]["reference_coverage"] = None
    with pytest.raises(AssertionError):
        norm(env, freshness_moves=True)
