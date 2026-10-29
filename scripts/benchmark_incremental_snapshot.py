#!/usr/bin/env python3
"""Paired snapshot-only benchmark: no-op, one-file and broader edits on ONE profile/top.

No native backend is imported or executed. Timings are observations, not a speed gate or
PERF-01 qualification. Git/stat hints never replace content verification. Source generation,
baseline setup, and independent output checks are outside the timed capture interval.

Example:
  .venv/bin/python scripts/benchmark_incremental_snapshot.py --files 1000 --trials 5 \
      --out .svweave/ci/incremental-snapshot.json

Separate full/incremental workspaces have byte-identical inputs. Mode order alternates, and
changed targets have unique snapshot IDs so later trials cannot become accidental no-ops.
Actual incremental logical I/O counters include copy fallback and verification, NOT physical
I/O. Legacy copy/hash volumes are derived from its successful, single-attempt algorithm and
are labeled as such. Nested stage timings must not be summed. Output correctness, not elapsed
time, determines exit status. Reflink availability is observed from real captures, never mocked.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import resource
import statistics
import subprocess
import sys
import tempfile
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from _bench_common import source_identity
from gen_corpus import generate

from svweave.config.context import WorkspaceContext
from svweave.config.trusted import policy_path_for
from svweave.manifest.resolver import resolve_profile
from svweave.snapshot import capture as cap
from svweave.snapshot import incremental as inc
from svweave.snapshot.models import SnapshotRecord

REPO = Path(__file__).resolve().parents[1]
CODE_FILES = (
    "src/svweave/snapshot/capture.py", "src/svweave/snapshot/incremental.py",
    "src/svweave/manifest/resolver.py", "scripts/gen_corpus.py",
    "scripts/benchmark_incremental_snapshot.py",
)


def _code_hashes() -> dict[str, str]:
    return {name: hashlib.sha256((REPO / name).read_bytes()).hexdigest() for name in CODE_FILES}


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise RuntimeError(message)


@contextmanager
def _stage_timers():
    """Temporary wrappers, restored even on failure; records inclusive/nested durations."""
    metrics: dict[str, dict[str, int | float]] = {}
    originals = []
    for module, names in (
        (cap, ("build_spec", "_nested_include_dirs", "_enumerate", "_inventory",
               "_content_matches", "_freeze", "remove_snapshot_dir", "_hash_file")),
        (inc, ("_inventory", "verify_snapshot_tree", "_copy_source", "_clone_verified", "_hash_fd")),
    ):
        for name in names:
            original = getattr(module, name)
            key = module.__name__.split(".")[-1] + "." + name

            def timed(*args, _key=key, _fn=original, **kwargs):
                start = time.perf_counter()
                try:
                    return _fn(*args, **kwargs)
                finally:
                    entry = metrics.setdefault(_key, {"calls": 0, "wall_s": 0.0})
                    entry["calls"] += 1
                    entry["wall_s"] += time.perf_counter() - start

            originals.append((module, name, original))
            setattr(module, name, timed)
    try:
        yield metrics
    finally:
        for module, name, original in originals:
            setattr(module, name, original)


def _projection(record: SnapshotRecord) -> dict[str, Any]:
    return {
        "snapshot_id": record.snapshot_id, "config_id": record.config_id,
        "spec": record.spec.model_dump(),
        "files": [{k: v for k, v in item.model_dump().items() if k != "original_path"}
                  for item in record.files],
        "lookup": [{k: v for k, v in item.model_dump().items() if k != "original_dir"}
                   for item in record.lookup],
    }


def _check_tree(ctx: WorkspaceContext, record: SnapshotRecord, baseline: SnapshotRecord) -> str:
    """Independent byte/path/inode checks; does not use the incremental verifier as oracle."""
    root = cap.snapshots_dir(ctx) / record.snapshot_id
    donor = cap.snapshots_dir(ctx) / baseline.snapshot_id
    expected = {item.snapshot_path for item in record.files}
    actual = {p.relative_to(root).as_posix() for p in (root / "files").rglob("*") if p.is_file()}
    _require(actual == expected, "unexpected or missing output files")
    for path in root.rglob("*"):
        _require(not path.is_symlink(), "snapshot contains a symlink")
        _require(not path.stat().st_mode & 0o222, "snapshot has a writable member")
    tree = {}
    for item in record.files:
        path = root / item.snapshot_path
        st = path.lstat()
        original = Path(item.original_path).stat()
        _require(path.is_file() and st.st_nlink == 1, "snapshot member is not independent regular file")
        _require((st.st_dev, st.st_ino) != (original.st_dev, original.st_ino), "source inode reused")
        previous = donor / item.snapshot_path
        if record.snapshot_id != baseline.snapshot_id and previous.exists():
            old = previous.stat()
            _require((st.st_dev, st.st_ino) != (old.st_dev, old.st_ino), "donor inode reused")
        data = path.read_bytes()
        digest = hashlib.sha256(data).hexdigest()
        _require(len(data) == item.size and digest == item.sha256, "snapshot byte mismatch")
        tree[item.snapshot_path] = digest
    return hashlib.sha256(json.dumps(tree, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def _setup(root: Path, files: int) -> tuple[WorkspaceContext, SnapshotRecord, dict[str, Any]]:
    corpus = generate(root, files / 500.0, 1, False)
    policy = policy_path_for(root / "svweave.yaml")
    policy.parent.mkdir(parents=True, exist_ok=True)
    policy.write_text(json.dumps({
        "schema_version": "1.0", "allowed_roots": [str(root.resolve())],
        "writable_paths": [], "approval_required": True, "network_enabled": False,
        "editing_enabled": False,
    }))
    ctx = WorkspaceContext.load(root)
    baseline = cap.capture_snapshot(ctx, resolve_profile(ctx, "bench"))
    return ctx, baseline, corpus


def _sample(mode: str, ctx: WorkspaceContext, baseline: SnapshotRecord,
            case: str, trial: int, changed: int) -> tuple[SnapshotRecord, dict[str, Any]]:
    start = time.perf_counter()
    manifest = resolve_profile(ctx, "bench")
    manifest_s = time.perf_counter() - start
    with _stage_timers() as stages:
        start = time.perf_counter()
        if mode == "full":
            record = cap.capture_snapshot(ctx, manifest)
            stats = None
        else:
            record, counts = inc.capture_snapshot_incremental(ctx, manifest, baseline=baseline)
            stats = counts.to_dict()
        elapsed = time.perf_counter() - start
    _require(record.attempts == 1, "retry invalidates this stable-workspace timing trial")
    _require((record.snapshot_id == baseline.snapshot_id) == (changed == 0), "unexpected snapshot reuse")
    size = sum(item.size for item in record.files)
    legacy_io = None
    if mode == "full":
        legacy_io = {
            "basis": "derived from legacy single-attempt algorithm and verified returned inventory; not syscall or physical I/O counters",
            "files_copied": len(record.files), "bytes_copied": size,
            "files_hashed": 2 * len(record.files), "bytes_hashed": 2 * size,
            "snapshot_files_verified": len(record.files) if changed == 0 else 0,
            "snapshot_bytes_verified": size if changed == 0 else 0,
            "files_cloned": 0, "bytes_cloned": 0,
        }
    else:
        _require(stats is not None, "missing actual incremental counters")
        if changed == 0:
            _require(stats["noop_snapshot_reused"] and stats["files_copied"] == 0
                     and stats["files_cloned"] == 0, "no-op unexpectedly materialized files")
        else:
            _require(not stats["noop_snapshot_reused"], "edited target was cached")
            _require(stats["files_copied"] + stats["files_cloned"] == len(record.files),
                     "unexpected materialization count")
        _require(stats["bytes_hashed"] >= 2 * size, "missing mandatory source content checks")
    return record, {
        "case": case, "trial": trial, "mode": mode, "changed_files": changed,
        "capture_wall_s": elapsed, "manifest_resolution_s": manifest_s,
        "snapshot_id": record.snapshot_id, "config_id": record.config_id,
        "inventory_files": len(record.files), "inventory_bytes": size,
        "capture_stats": stats, "legacy_logical_io": legacy_io, "inclusive_stages": stages,
        "tree_sha256": _check_tree(ctx, record, baseline),
        "integrity": "PASS: exact files/bytes; readonly; nlink=1; source/donor-independent inode",
    }


def run_benchmark(work: Path, *, files: int = 1000, trials: int = 5,
                  progress_path: Path | None = None) -> dict[str, Any]:
    """Caller supplies a fresh directory. Writes generated inputs/state only below it."""
    _require(8 <= files <= 2000 and 1 <= trials <= 9, "bounded files/trials required")
    _require(not work.exists(), "workspace already exists; refusing to replace it")
    work.mkdir(parents=True)
    old_xdg = os.environ.get("XDG_CONFIG_HOME")
    os.environ["XDG_CONFIG_HOME"] = str(work / "xdg")
    before = _code_hashes()
    if progress_path is not None:
        progress_path.write_text(json.dumps({"source_hashes": before, "workspace": str(work)}) + "\n")
    samples: list[dict[str, Any]] = []
    pairs = []
    try:
        contexts, baselines, corpora, originals = {}, {}, {}, {}
        for mode in ("full", "incremental"):
            ctx, baseline, corpus = _setup(work / mode, files)
            contexts[mode], baselines[mode], corpora[mode] = ctx, baseline, corpus
            paths = sorted((ctx.project_root / "rtl/mod").glob("*.sv"))[-100:]
            originals[mode] = {p.relative_to(ctx.project_root).as_posix(): p.read_bytes() for p in paths}
        _require(corpora["full"] == corpora["incremental"], "corpus mismatch")
        _require(baselines["full"].snapshot_id == baselines["incremental"].snapshot_id,
                 "baseline identity mismatch")
        broad = len(originals["full"])
        for case, changed in (("no_op", 0), ("one_file_edit", 1), ("broader_edit", broad)):
            for trial in range(1, trials + 1):
                results = {}
                order = ["full", "incremental"] if trial % 2 else ["incremental", "full"]
                for mode in order:
                    ctx = contexts[mode]
                    for rel, data in originals[mode].items():
                        (ctx.project_root / rel).write_bytes(data)
                    edits = list(originals[mode].items())[-changed:] if changed else []
                    for rel, data in edits:
                        new = data.replace(b"// synthetic module ", f"// syntheti{trial} module ".encode(), 1)
                        _require(new != data and len(new) == len(data), "edit is not one same-size comment change")
                        (ctx.project_root / rel).write_bytes(new)
                    record, sample = _sample(mode, ctx, baselines[mode], case, trial, changed)
                    sample["order"] = order
                    samples.append(sample)
                    if progress_path is not None:
                        with progress_path.open("a") as stream:
                            stream.write(json.dumps(sample) + "\n")
                    results[mode] = (record, sample)
                _require(_projection(results["full"][0]) == _projection(results["incremental"][0]),
                         "ordered snapshot identity/inventory/lookup mismatch")
                left, right = results["full"][1], results["incremental"][1]
                _require(left["tree_sha256"] == right["tree_sha256"], "exact content tree mismatch")
                pairs.append({"case": case, "trial": trial, "full_s": left["capture_wall_s"],
                              "incremental_s": right["capture_wall_s"],
                              "ratio_incremental_over_full": right["capture_wall_s"] / left["capture_wall_s"],
                              "identity_and_exact_tree_equivalence": "PASS"})
        summary = {}
        for case in ("no_op", "one_file_edit", "broader_edit"):
            entry = {}
            for mode in ("full", "incremental"):
                values = [s["capture_wall_s"] for s in samples if s["case"] == case and s["mode"] == mode]
                entry[mode] = {"n": len(values), "median_s": statistics.median(values),
                               "min_s": min(values), "max_s": max(values), "raw_s": values}
            entry["median_paired_ratio_incremental_over_full"] = statistics.median(
                p["ratio_incremental_over_full"] for p in pairs if p["case"] == case)
            summary[case] = entry
        after = _code_hashes()
        _require(before == after, "benchmark source changed during measurement")
        filesystem = subprocess.run(["df", "-T", str(work)], capture_output=True, text=True, timeout=10)
        return {
            "benchmark": "incremental-snapshot-paired", "correctness": "PASS",
            "source_identity": source_identity(), "source_hashes": before,
            "host": {"python": sys.version, "platform": platform.platform(),
                     "logical_cpus": os.cpu_count(), "filesystem": filesystem.stdout.strip(),
                     "max_rss_kib": resource.getrusage(resource.RUSAGE_SELF).ru_maxrss,
                     "page_cache": "warm; not dropped; setup outside timing"},
            "corpus": corpora["full"], "workspace": str(work), "trials": trials,
            "method": "separate states; alternating mode order; unique edited target IDs; same-size comment edits; post-timing independent integrity checks",
            "limitations": ["Not a semantic/graph/Serena benchmark or PERF-01 qualification.",
                            "No fixed speed threshold. A slower fallback is reported, not hidden.",
                            "Incremental counters are actual logical I/O, not physical disk writes.",
                            "Legacy logical volumes are algorithm-derived and explicitly labeled.",
                            "Stage timers are nested/inclusive and add wrapper overhead; do not sum.",
                            "Same-size comment edits test capture, not semantic incremental compilation."],
            "native_backends": "NOT_RUN", "summary": summary, "pairs": pairs, "samples": samples,
        }
    finally:
        if old_xdg is None:
            os.environ.pop("XDG_CONFIG_HOME", None)
        else:
            os.environ["XDG_CONFIG_HOME"] = old_xdg


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--files", type=int, default=1000)
    parser.add_argument("--trials", type=int, default=5)
    parser.add_argument("--out", type=Path, default=REPO / ".svweave/ci/incremental-snapshot.json")
    parser.add_argument("--work-root", type=Path, help="parent for temporary paired workspaces; defaults beside output")
    parser.add_argument("--keep-workspace", action="store_true")
    args = parser.parse_args()
    if not 8 <= args.files <= 2000 or not 1 <= args.trials <= 9:
        parser.error("--files must be 8..2000 and --trials must be 1..9")
    args.out.parent.mkdir(parents=True, exist_ok=True)
    parent = args.work_root or args.out.parent
    parent.mkdir(parents=True, exist_ok=True)
    holder = Path(tempfile.mkdtemp(prefix="snapshot-paired-", dir=parent))
    try:
        report = run_benchmark(holder / "work", files=args.files, trials=args.trials,
                               progress_path=args.out.with_suffix(".samples.jsonl"))
        args.out.write_text(json.dumps(report, indent=2) + "\n")
        print(json.dumps({"correctness": report["correctness"], "summary": report["summary"],
                          "evidence": str(args.out)}, indent=2))
        return 0
    except Exception as exc:
        args.out.write_text(json.dumps({"benchmark": "incremental-snapshot-paired", "correctness": "FAIL",
                                       "error": f"{type(exc).__name__}: {exc}",
                                       "raw_samples": str(args.out.with_suffix(".samples.jsonl")),
                                       "native_backends": "NOT_RUN"}, indent=2) + "\n")
        raise
    finally:
        if not args.keep_workspace:
            cap.remove_snapshot_dir(holder)


if __name__ == "__main__":
    raise SystemExit(main())
