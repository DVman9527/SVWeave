#!/usr/bin/env python3
"""CI-only, bounded real full-build/update timing on one synthetic profile/top.

Two paired one-file semantic edits at scale 1.0 (500 files) by default. Two baseline builds
are outside measured pairs. Each update is followed by an untimed --no-reuse semantic
comparison: eight sequential workers total. Mandatory bwrap and existing resource limits
remain unchanged. No graph/Serena starts, no fixed speed threshold, no qualification claim.

  SVWEAVE_REQUIRE_BACKENDS=1 .venv/bin/python scripts/benchmark_incremental_build.py \
      --scale 1.0 --pairs 2 --out .svweave/ci/incremental-build.json

Run only in the CI environment authorized for native workers. The parent process samples
RSS; an instrumented CLI child records capture wall time without replacing worker execution.
Every raw CLI result, stderr, timing and relevant run artifact is preserved beside the report,
including failures. Semantic equality compares complete table digests, retaining semantic IDs.
"""
from __future__ import annotations

import argparse
import contextlib
import hashlib
import json
import os
import selectors
import signal
import sqlite3
import statistics
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

from _bench_common import source_identity
from benchmark import Sampler, host_info
from gen_corpus import generate

REPO = Path(__file__).resolve().parents[1]
CLI_TIMEOUT_S = 180.0
TOTAL_TIMEOUT_S = 840.0
MAX_OUTPUT_BYTES = 2 << 20
_DEADLINE = float("inf")
TABLES = (
    "declarations", "references", "instances", "port_bindings", "assertions", "diagnostics",
    "specializations", "inactive_regions", "coverage_gaps", "unsupported_nodes", "build_reports",
)


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise RuntimeError(message)


def _invoke(metrics_path: Path, cli_args: list[str]) -> None:
    """Real CLI with capture-only timing hooks; worker and publication code stay unchanged."""
    from svweave.snapshot import capture, incremental

    timings: dict[str, Any] = {"capture_calls": [], "capture_total_s": 0.0}
    for module, name in ((capture, "capture_snapshot"), (incremental, "capture_snapshot_incremental")):
        original = getattr(module, name)

        def measured(*args, _fn=original, _name=name, **kwargs):
            start = time.perf_counter()
            try:
                return _fn(*args, **kwargs)
            finally:
                elapsed = time.perf_counter() - start
                timings["capture_calls"].append({"function": _name, "wall_s": elapsed})
                timings["capture_total_s"] += elapsed

        setattr(module, name, measured)
    try:
        from svweave.cli import main

        sys.argv = ["svweave", *cli_args]
        main()
    finally:
        metrics_path.write_text(json.dumps(timings, indent=2) + "\n")


def semantic_digests(root: Path, build_id: str) -> dict[str, Any]:
    """Read-only full-row digest. Only execution time/job and diagnostic autoincrement vary."""
    result = {}
    uri = (root / ".svweave/svweave.db").resolve().as_uri() + "?mode=ro"
    with sqlite3.connect(uri, uri=True) as db:
        db.row_factory = sqlite3.Row
        for table in TABLES:
            rows = [dict(row) for row in db.execute(f'SELECT * FROM "{table}" WHERE build_id=?', (build_id,))]
            normalized = []
            for row in rows:
                if table in {"diagnostics", "build_reports"}:
                    row.pop("job_id", None)
                if table == "diagnostics":
                    row.pop("diagnostic_id", None)
                if table == "build_reports":
                    stats = json.loads(row["stats"])
                    _require(isinstance(stats.get("worker_wall_s"), (int, float)), "worker timing absent")
                    stats.pop("worker_wall_s")
                    row["stats"] = json.dumps(stats, sort_keys=True)
                normalized.append(json.dumps(row, sort_keys=True, separators=(",", ":")))
            result[table] = {"rows": len(rows), "sha256": hashlib.sha256("\n".join(sorted(normalized)).encode()).hexdigest()}
    _require(result["declarations"]["rows"] > 0 and result["instances"]["rows"] > 0,
             "empty semantic result is not an equivalence proof")
    return result


def snapshot_digest(root: Path, snapshot_id: str) -> str:
    """Compare actual generated source bytes, not only a claimed snapshot ID."""
    captured = root / ".svweave/snapshots" / snapshot_id / "files"
    expected = {"corpus.f", *(root / "corpus.f").read_text().splitlines()}
    paths = list(captured.rglob("*"))
    _require(all(not path.is_symlink() for path in paths), "snapshot contains symlink")
    actual = {path.relative_to(captured).as_posix() for path in paths if path.is_file()}
    _require(actual == expected, "snapshot tree has unexpected or missing members")
    rows = []
    for rel in sorted(expected):
        path = captured / rel
        info, original = path.stat(), (root / rel).stat()
        _require(not info.st_mode & 0o222 and info.st_nlink == 1, "snapshot file is writable/shared")
        _require((info.st_dev, info.st_ino) != (original.st_dev, original.st_ino), "source inode reused")
        data = path.read_bytes()
        _require(data == (root / rel).read_bytes(), "snapshot differs from final source bytes")
        rows.append([rel, hashlib.sha256(data).hexdigest()])
    return hashlib.sha256(json.dumps(rows, separators=(",", ":")).encode()).hexdigest()


def _validate_result(raw: dict[str, Any], *, update: bool, rerun: bool = False) -> dict[str, Any]:
    env = json.loads(raw["stdout"])
    data = env["data"]
    _require(raw["exit"] == 0 and env["outcome"] == "OK" and env.get("error") is None,
             "real CLI build failed; inspect preserved raw result")
    _require(data["completeness"] == "COMPLETE" and data["diagnostic_outcome"] == "NO_ERRORS",
             "worker output is not COMPLETE/NO_ERRORS")
    _require(data["worker_isolation"] == "bwrap", "mandatory worker isolation not demonstrated")
    _require(data["reused"] is False and bool(data["job_id"]), "timed edit did not execute a worker")
    _require(data["resource_usage"]["isolation"] == "bwrap", "worker resource evidence lacks bwrap")
    if update:
        _require(data["update"]["semantic_action"] == "full_profile_analysis", "update did not perform whole-profile analysis")
    if rerun:
        _require(data.get("verified_rerun") is True and bool(data.get("semantic_result_sha256")),
                 "untimed full rerun did not verify every stored semantic table")
    return env


def _whole_profile(root: Path, env: dict[str, Any], evidence: Path) -> None:
    run = root / env["data"]["run_dir"]
    limits = {"request.json": 512 << 10, "dependency_report.json": 512 << 10,
              "resource_usage.json": 32 << 10, "events.jsonl": 64 << 10, "result.json": 0}
    artifacts = {}
    for name, limit in limits.items():
        digest, size, kept = hashlib.sha256(), 0, bytearray()
        with (run / name).open("rb") as stream:
            while chunk := stream.read(64 << 10):
                digest.update(chunk)
                size += len(chunk)
                if len(kept) + len(chunk) <= limit:
                    kept.extend(chunk)
        artifacts[name] = {"sha256": digest.hexdigest(), "bytes": size, "copied": size <= limit}
        if size <= limit:
            (evidence / name).write_bytes(kept)
    (evidence / "artifact-digests.json").write_text(json.dumps(artifacts, indent=2) + "\n")
    _require(artifacts["request.json"]["copied"] and artifacts["dependency_report.json"]["copied"],
             "request/dependency evidence exceeds bounded benchmark artifact allowance")
    request = json.loads((run / "request.json").read_text())["worker_request"]
    source_paths = (root / "corpus.f").read_text().splitlines()
    expected = {str(Path(request["snapshot_root"]) / rel) for rel in source_paths}
    _require(request["tops"] == ["bench_top"] and expected <= set(request["args"]),
             "worker request did not include the entire one-profile source set")
    deps = json.loads((run / "dependency_report.json").read_text())
    _require(deps["ok"] and not deps["violations"] and set(source_paths) <= set(deps["files_read"]),
             "whole-profile dependency evidence incomplete")


def _environment(root: Path) -> dict[str, str]:
    """No inherited secrets, Git filters, Python startup injection or degraded-worker flags."""
    home = root.parent / "controller-home"
    home.mkdir(exist_ok=True)
    env = {
        "PATH": str(Path(sys.executable).parent) + ":/usr/local/bin:/usr/bin:/bin",
        "HOME": str(home), "XDG_CONFIG_HOME": os.environ["XDG_CONFIG_HOME"],
        "XDG_CACHE_HOME": str(home / "cache"), "XDG_DATA_HOME": str(home / "data"),
        "LANG": "C.UTF-8", "LC_ALL": "C.UTF-8", "CI": "true",
        "PYTHONPATH": str(REPO / "src"), "PYTHONDONTWRITEBYTECODE": "1",
        "PYTHONUNBUFFERED": "1", "SVWEAVE_REQUIRE_BACKENDS": "1",
    }
    if "SVWEAVE_TOOLCHAIN_ROOT" in os.environ:
        env["SVWEAVE_TOOLCHAIN_ROOT"] = os.environ["SVWEAVE_TOOLCHAIN_ROOT"]
    return env


def _stop(proc: subprocess.Popen[bytes]) -> None:
    # CLI owns a new session. Mandatory bwrap also uses --die-with-parent for its worker.
    with contextlib.suppress(ProcessLookupError):
        os.killpg(proc.pid, signal.SIGKILL)
    with contextlib.suppress(subprocess.TimeoutExpired):
        proc.wait(timeout=2)


def _bounded_output(proc: subprocess.Popen[bytes], timeout: float) -> tuple[str, str, str | None]:
    """Drain both pipes under a cumulative memory/output and wall-time bound."""
    deadline = time.monotonic() + timeout
    buffers = {"stdout": bytearray(), "stderr": bytearray()}
    reason = None
    assert proc.stdout is not None and proc.stderr is not None
    try:
        with selectors.DefaultSelector() as selector:
            for name, pipe in (("stdout", proc.stdout), ("stderr", proc.stderr)):
                os.set_blocking(pipe.fileno(), False)
                selector.register(pipe, selectors.EVENT_READ, name)
            while selector.get_map():
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    reason = "cli_timeout"
                    break
                for key, _ in selector.select(timeout=min(remaining, 0.25)):
                    chunk = os.read(key.fd, 64 << 10)
                    if not chunk:
                        selector.unregister(key.fileobj)
                        continue
                    available = MAX_OUTPUT_BYTES - sum(map(len, buffers.values()))
                    buffers[key.data].extend(chunk[:available])
                    if len(chunk) > available:
                        reason = "cli_output_limit"
                        break
                if reason:
                    break
            if reason is None:
                try:
                    proc.wait(timeout=max(0.01, deadline - time.monotonic()))
                except subprocess.TimeoutExpired:
                    reason = "cli_timeout"
    finally:
        _stop(proc)  # kills unexpected stragglers in the CLI group even after normal exit
        proc.stdout.close()
        proc.stderr.close()
    return (*(buffers[name].decode("utf-8", "replace") for name in ("stdout", "stderr")), reason)


def _run_cli(root: Path, evidence: Path, args: list[str], *, update: bool = False,
             rerun: bool = False) -> dict[str, Any]:
    evidence.mkdir(parents=True, exist_ok=False)
    metrics = evidence / "capture-timing.json"
    command = [sys.executable, str(Path(__file__).resolve()), "--_invoke", str(metrics),
               "--root", str(root), "--compact", *args]
    timeout = min(CLI_TIMEOUT_S, _DEADLINE - time.monotonic())
    _require(timeout > 0, "total benchmark deadline reached")
    start = time.perf_counter()
    proc = subprocess.Popen(command, cwd=REPO, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, env=_environment(root), start_new_session=True)
    sampler = Sampler(proc.pid)
    sampler.start()
    try:
        stdout, stderr, failure = _bounded_output(proc, timeout)
    finally:
        sampler.stop()
    raw = {"command": command, "wall_s": time.perf_counter() - start, "exit": proc.returncode,
           "stdout": stdout, "stderr": stderr, "tree_rss_peak_kib_sampled": sampler.peak,
           "timeout_s": timeout, "output_limit_bytes": MAX_OUTPUT_BYTES, "controller_failure": failure}
    (evidence / "cli.json").write_text(json.dumps(raw, indent=2) + "\n")
    _require(failure is None, f"bounded CLI failed: {failure}; raw evidence preserved")
    env = _validate_result(raw, update=update, rerun=rerun)
    _whole_profile(root, env, evidence)
    measured = json.loads(metrics.read_text())
    _require(len(measured["capture_calls"]) == 1 and measured["capture_total_s"] >= 0,
             "capture timing hook was not exercised exactly once")
    return {"end_to_end_s": raw["wall_s"], "capture_s": measured["capture_total_s"],
            "worker": env["data"]["resource_usage"], "snapshot_id": env["snapshot_id"],
            "build_id": env["build_id"], "tree_rss_peak_kib_sampled": sampler.peak,
            "materialization": env["data"].get("update", {}).get("materialization"),
            "semantic_action": env["data"].get("update", {}).get("semantic_action", "full_profile_analysis"),
            "verified_rerun": env["data"].get("verified_rerun", False),
            "whole_profile_request_and_dependencies": "PASS", "evidence": str(evidence)}


def _cleanup_workspace(work: Path, report: dict[str, Any]) -> None:
    from svweave.snapshot.capture import remove_snapshot_dir

    try:
        remove_snapshot_dir(work)
        report["runtime_workspace_removed"] = True
    except OSError as exc:
        report["runtime_workspace_removed"] = False
        report["status"] = "FAIL"
        report["cleanup_error"] = f"{type(exc).__name__}: {exc}"
        raise


def main() -> int:
    global _DEADLINE
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--scale", type=float, default=1.0)
    parser.add_argument("--pairs", type=int, default=2)
    parser.add_argument("--out", type=Path, default=REPO / ".svweave/ci/incremental-build.json")
    args = parser.parse_args()
    if os.environ.get("CI") != "true" or os.environ.get("SVWEAVE_REQUIRE_BACKENDS") != "1":
        parser.error("CI-only native benchmark requires CI=true and SVWEAVE_REQUIRE_BACKENDS=1")
    if not 0 < args.scale <= 1.0 or not 1 <= args.pairs <= 2:
        parser.error("bounded --scale must be <=1.0; --pairs must be 1 or 2")
    args.out.parent.mkdir(parents=True, exist_ok=True)
    raw_root = args.out.with_suffix("")
    raw_root.mkdir(parents=True, exist_ok=False)
    work = Path(tempfile.mkdtemp(prefix="incremental-build-", dir=os.environ.get("RUNNER_TEMP")))
    if work.resolve().is_relative_to(args.out.parent.resolve()):
        work.rmdir()
        parser.error("RUNNER_TEMP must be outside the artifact directory")
    _DEADLINE = time.monotonic() + TOTAL_TIMEOUT_S
    previous_xdg = os.environ.get("XDG_CONFIG_HOME")
    os.environ["XDG_CONFIG_HOME"] = str(work / "xdg")
    report: dict[str, Any] = {
        "benchmark": "incremental-build-paired", "status": "RUNNING", "source_identity": source_identity(),
        "toolchain_lock_sha256": hashlib.sha256((REPO / "toolchain.lock.json").read_bytes()).hexdigest(),
        "benchmark_script_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        "host": host_info(work), "workspace": str(work), "baselines": {}, "pairs": [],
        "controller_bounds": {"total_timeout_s": TOTAL_TIMEOUT_S, "per_cli_timeout_s": CLI_TIMEOUT_S,
                              "per_cli_combined_output_bytes": MAX_OUTPUT_BYTES},
        "limitations": ["CI-only sequential real workers, mandatory bwrap; no graph or Serena.",
                        "Two pairs are bounded observations, not a stable performance estimate or PERF-01 qualification.",
                        "No fixed speed threshold; slower fallbacks remain visible.",
                        "Instrumented CLI end-to-end includes Python startup and capture timing-hook imports.",
                        "Capture and worker wall times are separate; their remainder is NOT attributed to SQLite or any one stage."],
    }

    def persist() -> None:
        args.out.write_text(json.dumps(report, indent=2) + "\n")

    persist()
    try:
        roots, originals = {}, {}
        from svweave.config.trusted import policy_path_for

        for mode in ("full", "update"):
            root = work / mode
            report["corpus"] = generate(root, args.scale, 1, False)
            policy = policy_path_for(root / "svweave.yaml")
            policy.parent.mkdir(parents=True, exist_ok=True)
            policy.write_text(json.dumps({"schema_version": "1.0", "allowed_roots": [str(root.resolve())],
                                          "writable_paths": [], "approval_required": True,
                                          "network_enabled": False, "editing_enabled": False}))
            roots[mode] = root
            leaf = sorted((root / "rtl/mod").glob("*.sv"))[-1]
            originals[mode] = (leaf, leaf.read_bytes())
            report["baselines"][mode] = _run_cli(root, raw_root / f"baseline-{mode}", ["build", "--profile", "bench"])
            persist()
        for trial in range(1, args.pairs + 1):
            pair: dict[str, Any] = {"trial": trial, "timed": {}}
            report["pairs"].append(pair)
            for mode in (["full", "update"] if trial % 2 else ["update", "full"]):
                leaf, original = originals[mode]
                changed = original.replace(b"parameter int unsigned N = 3", f"parameter int unsigned N = {3 + trial}".encode(), 1)
                _require(changed != original and len(changed) == len(original), "semantic edit anchor missing")
                leaf.write_bytes(changed)
                command = ["build", "--profile", "bench"] if mode == "full" else ["update", "--profile", "bench", "--no-git"]
                pair["timed"][mode] = _run_cli(roots[mode], raw_root / f"pair-{trial}-{mode}", command, update=mode == "update")
                persist()
            left, right = pair["timed"]["full"], pair["timed"]["update"]
            _require(left["build_id"] == right["build_id"] and left["snapshot_id"] == right["snapshot_id"],
                     "full/update build identity mismatch")
            full_snapshot = snapshot_digest(roots["full"], left["snapshot_id"])
            _require(full_snapshot == snapshot_digest(roots["update"], right["snapshot_id"]),
                     "full/update actual snapshot bytes differ")
            pair["snapshot_tree_sha256"] = full_snapshot
            full = semantic_digests(roots["full"], left["build_id"])
            updated = semantic_digests(roots["update"], right["build_id"])
            _require(full == updated, "complete normalized semantic rows differ")
            pair["semantic_tables"] = full
            pair["semantic_equivalence"] = "PASS"
            pair["untimed_verification"] = _run_cli(roots["update"], raw_root / f"pair-{trial}-verified-rerun",
                                                    ["build", "--profile", "bench", "--no-reuse"], rerun=True)
            _require(semantic_digests(roots["update"], right["build_id"]) == updated,
                     "verified rerun modified immutable semantic rows")
            pair["ratio_update_over_full"] = right["end_to_end_s"] / left["end_to_end_s"]
            persist()
        _require(report["source_identity"] == source_identity(), "source changed during benchmark")
        report["summary"] = {
            mode: {"end_to_end_median_s": statistics.median(p["timed"][mode]["end_to_end_s"] for p in report["pairs"]),
                   "capture_median_s": statistics.median(p["timed"][mode]["capture_s"] for p in report["pairs"]),
                   "worker_median_s": statistics.median(p["timed"][mode]["worker"]["wall_s"] for p in report["pairs"])}
            for mode in ("full", "update")
        }
        report["status"] = "PASS"
        persist()
        print(json.dumps({"status": "PASS", "summary": report["summary"], "evidence": str(args.out)}, indent=2))
        return 0
    except Exception as exc:
        report["status"] = "FAIL"
        report["error"] = f"{type(exc).__name__}: {exc}"
        persist()
        raise
    finally:
        try:
            _cleanup_workspace(work, report)
        finally:
            try:
                persist()
            finally:
                if previous_xdg is None:
                    os.environ.pop("XDG_CONFIG_HOME", None)
                else:
                    os.environ["XDG_CONFIG_HOME"] = previous_xdg


if __name__ == "__main__":
    if len(sys.argv) >= 4 and sys.argv[1] == "--_invoke":
        _invoke(Path(sys.argv[2]), sys.argv[3:])
    else:
        raise SystemExit(main())
