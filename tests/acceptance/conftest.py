"""Acceptance tests: the plan 1.3 demo flow against the REAL pinned backends (no mocks).

The ``acceptance`` marker is registered here (pyproject.toml is not edited). Missing backends skip
locally and FAIL when ``SVWEAVE_REQUIRE_BACKENDS=1`` (the release setting).
"""

from __future__ import annotations

import pytest


def pytest_configure(config: pytest.Config) -> None:
    config.addinivalue_line(
        "markers", "acceptance: end-to-end flow of the plan 1.3 demo against the real backends"
    )
    config.addinivalue_line("markers", "integration: uses the real pinned backends / bwrap")
