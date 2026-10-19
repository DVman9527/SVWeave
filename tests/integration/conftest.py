"""Shared fixtures for the real-backend integration tests (cbm, Serena, bwrap).

Host has little free RAM: backends are started one at a time (module scoped) and always torn down.
"""

from __future__ import annotations

import os
import shutil
from collections.abc import Iterator
from pathlib import Path

import pytest

from svweave.adapters.codebase_memory import CbmAdapter
from svweave.orchestration.toolchain import Toolchain
from svweave.security.sandbox import SandboxRunner, probe_host

REPO = Path(__file__).resolve().parents[2]
SID_A = "a" * 64  # demo + uvm fixtures
SID_B = "b" * 64  # demo only (a DIFFERENT snapshot)


def skip_or_fail(reason: str) -> None:
    """Missing backends skip locally, but FAIL when SVWEAVE_REQUIRE_BACKENDS=1 (CI / release)."""
    if os.environ.get("SVWEAVE_REQUIRE_BACKENDS") == "1":
        pytest.fail(f"required backend missing: {reason}", pytrace=False)
    pytest.skip(reason)


def pytest_configure(config: pytest.Config) -> None:
    config.addinivalue_line("markers", "integration: uses the real pinned backends / bwrap")


@pytest.fixture(scope="session")
def toolchain() -> Toolchain:
    tc = Toolchain.load(REPO)
    return tc


@pytest.fixture(scope="session")
def sandbox() -> SandboxRunner:
    if not probe_host().available:
        skip_or_fail("bwrap is not usable on this host")
    return SandboxRunner(allow_degraded=False)


def copy_fixture_dirs(dest: Path, names: tuple[str, ...] = ("demo", "uvm")) -> Path:
    if dest.exists():
        shutil.rmtree(dest)
    for n in names:
        shutil.copytree(REPO / "fixtures" / n, dest / n, symlinks=True)
    return dest


@pytest.fixture(scope="module")
def snapshots_root(tmp_path_factory: pytest.TempPathFactory) -> Path:
    root = tmp_path_factory.mktemp("snapshots")
    copy_fixture_dirs(root / SID_A, ("demo", "uvm"))
    copy_fixture_dirs(root / SID_B, ("demo",))
    return root


@pytest.fixture(scope="module")
def cbm(
    toolchain: Toolchain,
    sandbox: SandboxRunner,
    snapshots_root: Path,
    tmp_path_factory: pytest.TempPathFactory,
) -> Iterator[CbmAdapter]:
    if not toolchain.cbm_binary.is_file():
        skip_or_fail("cbm binary not installed")
    ad = CbmAdapter(toolchain, tmp_path_factory.mktemp("cbm-state"), snapshots_root, sandbox)
    ad.start(snapshots_root / SID_A)
    try:
        yield ad
    finally:
        ad.stop()
