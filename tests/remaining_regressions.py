"""Mode-only HDL-CI-01 guard: DEP-01 is BLOCKED, never silently absent or waived.

Loaded explicitly by the remaining-regressions workflow, not by normal/full pytest runs.
Collection must remove exactly the known DEP-01 node before any test body executes.
"""
from __future__ import annotations

import json
import os
from collections.abc import Generator
from pathlib import Path

import pytest

BLOCKED_NODE = (
    "tests/integration/test_dep01_cleanroom.py::"
    "test_DEP_01_offline_bootstrap_into_empty_dir_then_probe_all_three_backends"
)
LEDGER = Path(".svweave/ci/remaining-regressions-scope.json")
SUITES = ["tests/integration", "tests/fault_injection", "tests/acceptance"]


@pytest.hookimpl(wrapper=True, tryfirst=True)
def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> Generator[None, None, None]:
    if (os.environ.get("SVWEAVE_REQUIRE_BACKENDS") != "1" or config.args != SUITES
            or config.getoption("deselect") != [BLOCKED_NODE]):
        raise pytest.UsageError("remaining-regressions requires the exact suites, required mode and DEP-01 exclusion")
    try:
        ledger = json.loads(LEDGER.read_text())
        identity = json.loads((LEDGER.parent / "identity.json").read_text())
        source = identity["source_identity"]
        blocked = ledger["excluded_tests"]
        valid = (
            ledger["scope"] == "remaining_regressions"
            and ledger["full_qualification_status"] == "BLOCKED"
            and ledger["qualify_py_executed"] is False and ledger["release_qualified"] is False
            and len(blocked) == 1 and blocked[0]["nodeid"] == BLOCKED_NODE
            and blocked[0]["status"] == "BLOCKED" and blocked[0]["waived"] is False
            and blocked[0]["acceptance_unchanged"] is True
            and ledger["identity"] == identity and identity["remaining_regressions_only"] == "true"
            and all(isinstance(value, str) and value.strip() for value in (
                source["git_head"], source["src_tree_sha256"], identity["lock_sha256"],
                identity["workflow_sha256"],
            ))
        )
    except (OSError, ValueError, KeyError, TypeError, IndexError) as error:
        raise pytest.UsageError(f"remaining-regressions BLOCKED ledger unavailable: {error}") from error
    if not valid:
        raise pytest.UsageError("remaining-regressions requires an identity-linked unwaived BLOCKED ledger")
    before = [item.nodeid for item in items]
    yield
    selected = [item.nodeid for item in items]
    removed = [node for node in before if node not in set(selected)]
    valid = (before.count(BLOCKED_NODE) == 1 and removed == [BLOCKED_NODE]
             and len(before) == len(set(before)) and len(selected) == len(set(selected))
             and set(selected) <= set(before) and bool(selected))
    ledger["collection"] = {
        "status": "VALIDATED" if valid else "FAIL",
        "collected_count": len(before), "selected_count": len(selected),
        "blocked_count": len(removed), "blocked_nodeids": removed,
    }
    LEDGER.write_text(json.dumps(ledger, indent=2) + "\n")
    if not valid:
        raise pytest.UsageError("remaining-regressions must collect and exclude exactly the known DEP-01 node")
