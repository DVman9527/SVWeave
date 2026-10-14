"""C2: G5 accepts a benchmark only when complete (every plan 22.3 target exactly once, explicit PASS),
schema-valid, and tied to the current source tree and toolchain lock."""

from __future__ import annotations

import copy
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))
import _bench_common as bc  # noqa: E402

CUR = {"git_head": "NONE", "src_tree_sha256": "a" * 64}
LOCK = "b" * 64


def good() -> dict[str, Any]:
    return {
        "benchmark": "PERF-01", "host": {}, "toolchain_lock_sha256": LOCK, "source_identity": dict(CUR),
        "corpus": {"scale": 1.0, "files": 500, "lines": 100374},
        "cold_build": {"ok_runs": 5}, "hot_queries": {}, "hot_search": {}, "disk_usage": {},
        "compare_plan_22_3": {"reference_corpus": True, "rows": [
            {"target": t, "status": "PASS", "measured": 1.0} for t in bc.MANDATORY_TARGETS]},
    }


def check(bj: Any) -> list[str]:
    return bc.validate_benchmark(bj, current=CUR, current_lock_sha256=LOCK)


def test_complete_benchmark_is_accepted() -> None:
    assert check(good()) == []


def test_missing_target_is_rejected() -> None:
    b = good()
    b["compare_plan_22_3"]["rows"].pop()
    assert any("appears 0 times" in w for w in check(b))


def test_duplicate_target_is_rejected() -> None:
    b = good()
    b["compare_plan_22_3"]["rows"].append(copy.deepcopy(b["compare_plan_22_3"]["rows"][0]))
    assert any("appears 2 times" in w for w in check(b))


def test_only_explicit_pass_is_accepted() -> None:
    for st in ("SKIPPED", "BLOCKED", "NOT_RUN", "FAIL"):
        b = good()
        b["compare_plan_22_3"]["rows"][-1]["status"] = st
        assert any("only an explicit PASS" in w for w in check(b)), st
    b = good()
    del b["compare_plan_22_3"]["rows"][0]["status"]
    assert any(w.startswith("schema") for w in check(b))


def test_schema_violations_and_identity_mismatch_are_rejected() -> None:
    b = good()
    del b["source_identity"]
    assert any("source_identity" in w for w in check(b))
    b = good()
    b["source_identity"]["src_tree_sha256"] = "c" * 64
    assert any("source identity" in w for w in check(b))
    b = good()
    b["toolchain_lock_sha256"] = "d" * 64
    assert any("lock sha256" in w for w in check(b))
    assert check([]) == ["benchmark JSON is not an object"]


def test_reduced_scale_and_too_few_cold_runs_are_rejected() -> None:
    b = good()
    b["corpus"]["scale"] = 0.1
    b["cold_build"]["ok_runs"] = 3
    w = check(b)
    assert any("reference corpus" in x for x in w) and any("ok_runs" in x for x in w)
