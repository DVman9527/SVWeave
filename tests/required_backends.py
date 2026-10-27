"""HDL-CI-02: required qualification cannot turn any skipped case into a green run.

This covers skipif markers (including missing pinned UVM), fixture skips, xfails,
and collection-time skips. Local, non-required development keeps pytest's usual
skip behavior. No missing dependency is replaced or bypassed.
"""

from __future__ import annotations

import os
from collections.abc import Generator

import pytest


def _require_executed(report: pytest.TestReport | pytest.CollectReport) -> None:
    if os.environ.get("SVWEAVE_REQUIRE_BACKENDS") != "1" or not report.skipped:
        return
    report.outcome = "failed"
    report.longrepr = (
        f"{report.nodeid}: required qualification test did not execute "
        f"(SVWEAVE_REQUIRE_BACKENDS=1); skip is not PASS:\n{report.longrepr}"
    )
    if hasattr(report, "wasxfail"):
        delattr(report, "wasxfail")


@pytest.hookimpl(wrapper=True, tryfirst=True)
def pytest_runtest_makereport() -> Generator[None, pytest.TestReport, pytest.TestReport]:
    report = yield
    _require_executed(report)
    return report


@pytest.hookimpl(wrapper=True, tryfirst=True)
def pytest_make_collect_report() -> Generator[None, pytest.CollectReport, pytest.CollectReport]:
    report = yield
    _require_executed(report)
    return report
