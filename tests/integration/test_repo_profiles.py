"""The shipped svweave.yaml profiles (rtl_demo, rtl_demo_l4, uvm_smoke) build with the real
toolchain from a clean copy of the fixtures (plan 1.3, 21)."""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from svweave.application import queries as Q
from tests.conftest import FIXTURES, Ws
from tests.integration.helpers import UVM_SRC, build_profile, opts

REPO = Path(__file__).resolve().parents[2]
pytestmark = pytest.mark.integration


def repo_copy(tmp: Path) -> Ws:
    root = tmp / "repo"
    root.mkdir()
    shutil.copy(REPO / "svweave.yaml", root / "svweave.yaml")
    shutil.copytree(FIXTURES / "demo", root / "fixtures" / "demo")
    shutil.copytree(FIXTURES / "uvm", root / "fixtures" / "uvm")
    w = Ws(root)
    w.policy(allowed_roots=[str(root.resolve()), str(UVM_SRC.resolve())])
    w.local([{"id": "uvm", "path": str(UVM_SRC)}])
    return w


def test_shipped_demo_profiles_give_2_and_4_lanes(tmp_path: Path) -> None:
    w = repo_copy(tmp_path)
    e2 = build_profile(w, "rtl_demo")
    e4 = build_profile(w, "rtl_demo_l4")
    for env, lanes in ((e2, 2), (e4, 4)):
        assert (
            env.analysis_coverage is not None and env.analysis_coverage.completeness == "COMPLETE"
        )
        h = Q.hierarchy(opts(w), env.build_id, "r", limit=500)  # type: ignore[arg-type]
        assert sum(1 for i in h.data["items"] if i["definition"] == "demo_lane") == lanes
    assert e2.build_id != e4.build_id and e2.snapshot_id == e4.snapshot_id
    assert e2.profile == "rtl_demo" and e4.profile == "rtl_demo_l4"


@pytest.mark.skipif(not (UVM_SRC / "uvm_pkg.sv").is_file(), reason="pinned UVM library missing")
def test_shipped_uvm_smoke_profile_builds_against_the_pinned_library(tmp_path: Path) -> None:
    w = repo_copy(tmp_path)
    env = build_profile(w, "uvm_smoke")
    assert env.analysis_coverage is not None and env.analysis_coverage.completeness == "COMPLETE"
    assert env.data["counts"]["errors"] == 0 and env.data["published"] is True
