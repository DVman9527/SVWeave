"""Incremental performance observations never bypass mandatory correctness/sandbox gates."""

from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]


def test_incremental_snapshot_benchmark_is_manual_bounded_and_retains_raw_samples() -> None:
    job = yaml.safe_load((ROOT / ".github/workflows/ci.yml").read_text())["jobs"]["fast"]
    steps = job["steps"]
    step = next(s for s in steps if s.get("name", "").startswith("Paired snapshot I/O benchmark"))
    assert "workflow_dispatch" in step["if"] and "inputs.real_backends" in step["if"]
    assert "!inputs.cleanroom_only" in step["if"]
    assert step["timeout-minutes"] == 10
    assert "--files 1000 --trials 5" in step["run"]
    assert "continue-on-error" not in step and "|| true" not in step["run"]
    upload = next(s for s in steps if s.get("name") == "Upload snapshot I/O benchmark evidence")
    assert "always()" in upload["if"]
    assert "incremental-snapshot.samples.jsonl" in upload["with"]["path"]


def test_native_incremental_benchmark_follows_real_correctness_gates() -> None:
    job = yaml.safe_load((ROOT / ".github/workflows/ci.yml").read_text())["jobs"]["real-backends"]
    steps = job["steps"]
    names = [s.get("name") for s in steps]
    index = names.index("Paired full-build and snapshot-update benchmark (whole-profile semantics)")
    for prerequisite in (
        "Install and verify the mandatory worker sandbox",
        "Required native probe isolation regressions (fail closed)",
        "Integration, fault injection and acceptance (skips are failures)",
        "Critical mutation suite (plan 20.5)",
    ):
        assert names.index(prerequisite) < index
    step = steps[index]
    assert step["if"] == "${{ !inputs.cleanroom_only }}"
    assert step["timeout-minutes"] == 15
    assert "--scale 1.0 --pairs 2" in step["run"]
    assert "continue-on-error" not in step and "|| true" not in step["run"]
    assert job["env"]["SVWEAVE_REQUIRE_BACKENDS"] == "1"
