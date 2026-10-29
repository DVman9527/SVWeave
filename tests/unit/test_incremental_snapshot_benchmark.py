"""Pure benchmark contract: equivalence/counters, never a wall-time performance gate."""
from __future__ import annotations

import importlib.util
import os
import sys
from pathlib import Path

import pytest


@pytest.fixture
def benchmark(monkeypatch: pytest.MonkeyPatch):
    root = Path(__file__).resolve().parents[2]
    monkeypatch.syspath_prepend(str(root / "scripts"))
    spec = importlib.util.spec_from_file_location("snapshot_benchmark", root / "scripts/benchmark_incremental_snapshot.py")
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_pure_snapshot_benchmark_reports_actual_modes_and_equivalence(tmp_path: Path, benchmark) -> None:
    before_xdg = os.environ.get("XDG_CONFIG_HOME")
    loaded = set(sys.modules)
    report = benchmark.run_benchmark(tmp_path / "paired", files=8, trials=1)
    assert report["correctness"] == "PASS"
    assert report["native_backends"] == "NOT_RUN"
    assert report["corpus"]["top"] == "bench_top"
    assert len(report["pairs"]) == 3 and len(report["samples"]) == 6
    assert all(p["identity_and_exact_tree_equivalence"] == "PASS" for p in report["pairs"])
    noop = next(s for s in report["samples"] if s["mode"] == "incremental" and s["case"] == "no_op")
    assert noop["capture_stats"]["noop_snapshot_reused"]
    assert noop["capture_stats"]["bytes_copied"] == 0
    assert "ratio_incremental_over_full" in report["pairs"][0]
    assert os.environ.get("XDG_CONFIG_HOME") == before_xdg
    assert "pyslang" not in set(sys.modules) - loaded


def test_benchmark_refuses_existing_workspace_and_unbounded_size(tmp_path: Path, benchmark) -> None:
    with pytest.raises(RuntimeError, match="workspace already exists"):
        benchmark.run_benchmark(tmp_path, files=8, trials=1)
    with pytest.raises(RuntimeError, match="bounded"):
        benchmark.run_benchmark(tmp_path / "large", files=2001, trials=1)
