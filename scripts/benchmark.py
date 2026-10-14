#!/usr/bin/env python3
"""PERF-01 benchmark (plan 22.3): fixed synthetic corpus, raw JSON, per-target PASS/FAIL.

What is measured (everything is recorded, nothing is estimated):

* host: CPU model, logical CPUs, RAM (total/available at start), OS, Python, filesystem of the
  corpus, toolchain lock sha256, corpus stats (files, lines, content hash);
* tool startup: wall time of ``svweave --help`` (python import cost, no work), median of 5;
* cold semantic build: ``--cold-runs`` (>=5) runs of the real CLI ``svweave build --no-reuse`` in a
  subprocess, each on a FRESH ``.svweave`` state directory (snapshot, DB, runs all rebuilt);
  wall (total, incl. startup), build wall of the worker, median/max;
* memory: worker peak RSS (``wait4`` rusage as reported by the build) and a sampled peak of the
  SUM of VmRSS over the whole process tree of the CLI (gateway + worker);
* hot queries (in-process application services, ``limit<=100``): symbol, hierarchy, references,
  >=``--hot-runs`` (>=30) timed calls each after 3 warm-up calls: p50/p95/max and the query set;
* hot repository search: literal text fallback (always) and graph (codebase-memory-mcp) if the
  backend can index the snapshot; otherwise ``SKIPPED`` with the reason;
* disk usage per component of ``<corpus>/.svweave`` (snapshots, DB, runs, logs, backends, ...).

The reference HOST (8 logical CPUs, 32 GiB) is recorded as ``reference_host_met``; plan 22.3 asks
to record the actual host, so a different host does not by itself void the compare.

Budget compare (plan 22.3): cold build <= 120 s, worker peak <= 8 GiB, hot query p95 <= 1 s,
hot search p95 <= 2 s. A target is FAIL (with a reason) when over budget. A run at ``--scale < 1``
is NOT the reference corpus: results are flagged ``reference_corpus=false`` and a PASS there is not
a statement about the 100k-line corpus.

OS page cache is not dropped (no privilege): the corpus is warm after generation; recorded as
``page_cache: warm``. The trusted policy is written under ``<bench dir>/xdg`` (never the user's).

Usage::

    .venv/bin/python scripts/benchmark.py --scale 0.1 [--cold-runs 5] [--hot-runs 30]
        [--corpus DIR] [--no-graph] [--evidence-dir evidence/bench]
"""

from __future__ import annotations

import argparse
import contextlib
import datetime as dt
import hashlib
import json
import os
import platform
import shutil
import statistics
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _bench_common import source_identity  # noqa: E402

REPO = Path(__file__).resolve().parents[1]
BENCH = REPO / ".svweave" / "bench"
SVWEAVE = REPO / ".venv" / "bin" / "svweave"
PY = REPO / ".venv" / "bin" / "python"

BUDGET = {
    "cold_build_s": 120.0,
    "worker_peak_gib": 8.0,
    "hot_query_p95_s": 1.0,
    "hot_search_p95_s": 2.0,
}


# --------------------------------------------------------------------------------------- host
def _read(p: str) -> str:
    try:
        return Path(p).read_text()
    except OSError:
        return ""


def host_info(path: Path) -> dict[str, Any]:
    cpu = next((ln.split(":", 1)[1].strip() for ln in _read("/proc/cpuinfo").splitlines()
                if ln.startswith("model name")), "unknown")
    mem: dict[str, int] = {}
    for ln in _read("/proc/meminfo").splitlines():
        k, _, v = ln.partition(":")
        if v.strip().endswith("kB"):
            mem[k] = int(v.split()[0])
    fs = {"mount": None, "fstype": None}
    best = ""
    real = os.path.realpath(path)
    for ln in _read("/proc/mounts").splitlines():
        parts = ln.split()
        if len(parts) >= 3:
            mp = parts[1].replace("\\040", " ")
            if (real == mp or real.startswith(mp.rstrip("/") + "/")) and len(mp) > len(best):
                best, fs = mp, {"mount": mp, "device": parts[0], "fstype": parts[2]}
    return {
        "cpu_model": cpu,
        "logical_cpus": os.cpu_count(),
        "ram_total_gib": round(mem.get("MemTotal", 0) / 1048576, 2),
        "ram_available_gib_at_start": round(mem.get("MemAvailable", 0) / 1048576, 2),
        "swap_total_gib": round(mem.get("SwapTotal", 0) / 1048576, 2),
        "os": platform.platform(),
        "kernel": platform.release(),
        "python": platform.python_version(),
        "filesystem": fs,
        "page_cache": "warm (not dropped: no privilege)",
        "loadavg_at_start": os.getloadavg(),
    }


# ----------------------------------------------------------------------------- process sampling
def _tree_rss_kib(root_pid: int) -> int:
    """Sum of VmRSS (KiB) over root_pid and all its descendants (Linux /proc)."""
    ppid: dict[int, int] = {}
    for d in os.listdir("/proc"):
        if d.isdigit():
            try:
                stat = Path(f"/proc/{d}/stat").read_text()
                ppid[int(d)] = int(stat.rsplit(")", 1)[1].split()[1])
            except (OSError, ValueError, IndexError):
                continue
    todo, seen = [root_pid], set()
    while todo:
        p = todo.pop()
        if p in seen:
            continue
        seen.add(p)
        todo += [c for c, pp in ppid.items() if pp == p]
    total = 0
    for p in seen:
        try:
            for ln in Path(f"/proc/{p}/status").read_text().splitlines():
                if ln.startswith("VmRSS:"):
                    total += int(ln.split()[1])
        except (OSError, ValueError):
            continue
    return total


class Sampler(threading.Thread):
    def __init__(self, pid: int, interval: float = 0.05) -> None:
        super().__init__(daemon=True)
        self.pid, self.interval, self.peak, self.samples = pid, interval, 0, 0
        self._stop_ev = threading.Event()

    def run(self) -> None:
        while not self._stop_ev.is_set():
            self.peak = max(self.peak, _tree_rss_kib(self.pid))
            self.samples += 1
            self._stop_ev.wait(self.interval)

    def stop(self) -> None:
        self._stop_ev.set()
        self.join(timeout=2)


def run_cli(args: list[str], env: dict[str, str], cwd: Path) -> dict[str, Any]:
    """Run the real CLI; return wall time, exit code, stdout and a sampled tree-RSS peak."""
    t0 = time.perf_counter()
    proc = subprocess.Popen([str(SVWEAVE), *args], cwd=cwd, env=env, stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, text=True)
    smp = Sampler(proc.pid)
    smp.start()
    out, err = proc.communicate()
    wall = time.perf_counter() - t0
    smp.stop()
    return {"wall_s": wall, "exit": proc.returncode, "stdout": out, "stderr": err[-2000:],
            "tree_rss_peak_kib_sampled": smp.peak, "samples": smp.samples}


# ----------------------------------------------------------------------------------- statistics
def pct(vals: list[float], q: float) -> float:
    s = sorted(vals)
    if not s:
        return float("nan")
    k = (len(s) - 1) * q
    lo, hi = int(k), min(int(k) + 1, len(s) - 1)
    return s[lo] + (s[hi] - s[lo]) * (k - lo)


def summarize(vals: list[float]) -> dict[str, Any]:
    return {"n": len(vals), "p50_s": pct(vals, 0.5), "p95_s": pct(vals, 0.95), "max_s": max(vals),
            "min_s": min(vals), "raw_s": [round(v, 6) for v in vals]}


def timed(fn: Any, n: int, warm: int = 3) -> list[float]:
    for _ in range(warm):
        fn(0)
    out = []
    for i in range(n):
        t0 = time.perf_counter()
        fn(i)
        out.append(time.perf_counter() - t0)
    return out


# ----------------------------------------------------------------------------------- disk usage
def du(path: Path) -> int:
    total = 0
    if not path.exists():
        return 0
    for dp, _dn, fns in os.walk(path):
        for f in fns:
            with contextlib.suppress(OSError):
                total += os.lstat(os.path.join(dp, f)).st_size
    return total


def disk_usage(corpus: Path) -> dict[str, Any]:
    st = corpus / ".svweave"
    comp: dict[str, int] = {}
    known = {"snapshots", "runs", "manifests", "candidates", "journal", "backends", "bundles",
             "recovery"}
    for c in sorted(known):
        comp[c] = du(st / c)
    comp["semantic_db(svweave.db*)"] = sum(
        (st / n).stat().st_size for n in ("svweave.db", "svweave.db-wal", "svweave.db-shm")
        if (st / n).exists())
    logs = 0
    for p in (st / "runs").glob("*/events.jsonl"):
        logs += p.stat().st_size
    for p in (st / "runs").glob("*/*.log"):
        logs += p.stat().st_size
    comp["logs(events.jsonl,*.log; subset of runs)"] = logs
    accounted = sum(v for k, v in comp.items() if not k.startswith("logs"))
    comp["other"] = max(0, du(st) - accounted)
    return {"bytes": comp, "total_state_bytes": du(st), "source_corpus_bytes": du(corpus) - du(st),
            "note": "candidates/journal are empty here (no edit); graph state lives in backends/"}


# ----------------------------------------------------------------------------- policy / workspace
def write_policy(corpus: Path, xdg: Path) -> Path:
    os.environ["XDG_CONFIG_HOME"] = str(xdg)
    from svweave.config.trusted import policy_path_for

    p = policy_path_for(corpus / "svweave.yaml")
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps({
        "schema_version": "1.0", "allowed_roots": [str(corpus.resolve())], "writable_paths": [],
        "approval_required": True, "network_enabled": False, "editing_enabled": False}))
    return p


def sha256_file(p: Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest()


# --------------------------------------------------------------------------------------- phases
def cold_builds(corpus: Path, env: dict[str, str], runs: int) -> dict[str, Any]:
    results: list[dict[str, Any]] = []
    for i in range(runs):
        shutil.rmtree(corpus / ".svweave", ignore_errors=True)  # fresh workspace state each time
        r = run_cli(["--root", str(corpus), "--compact", "build", "--profile", "bench"], env, corpus)
        row: dict[str, Any] = {"run": i + 1, "exit": r["exit"], "total_wall_s": r["wall_s"],
                               "tree_rss_peak_kib_sampled": r["tree_rss_peak_kib_sampled"]}
        try:
            env_json = json.loads(r["stdout"])
            d = env_json["data"]
            row.update(outcome=env_json["outcome"], completeness=d.get("completeness"),
                       reused=d.get("reused"), counts=d.get("counts"),
                       worker=d.get("resource_usage"), build_id=env_json.get("build_id"),
                       snapshot_id=env_json.get("snapshot_id"))
        except (ValueError, KeyError, TypeError):
            row["error"] = r["stderr"][-500:] or r["stdout"][-500:]
        results.append(row)
    ok = [r for r in results if r.get("exit") == 0 and r.get("completeness") == "COMPLETE"
          and not r.get("reused")]
    walls = [r["total_wall_s"] for r in ok]
    wk = [r["worker"]["wall_s"] for r in ok if r.get("worker")]
    rss = [r["worker"]["peak_rss_kib"] for r in ok if r.get("worker")]
    tree = [r["tree_rss_peak_kib_sampled"] for r in ok]
    return {
        "runs": results, "ok_runs": len(ok),
        "total_wall_s": {"median": statistics.median(walls), "max": max(walls), "min": min(walls)}
        if walls else None,
        "worker_wall_s": {"median": statistics.median(wk), "max": max(wk)} if wk else None,
        "worker_peak_rss_kib": {"median": statistics.median(rss), "max": max(rss)} if rss else None,
        "process_tree_rss_peak_kib_sampled": {"median": statistics.median(tree), "max": max(tree)}
        if tree else None,
        "definition": "total_wall_s = whole `svweave build` process (tool startup included, "
                      "separately measured below); worker_wall_s = slang worker only",
        "fresh_state_each_run": True,
    }


def startup(env: dict[str, str], corpus: Path) -> dict[str, Any]:
    vals = [run_cli(["--help"], env, corpus)["wall_s"] for _ in range(5)]
    return {"command": "svweave --help", "median_s": statistics.median(vals), "max_s": max(vals),
            "raw_s": [round(v, 4) for v in vals]}


def hot_queries(corpus: Path, runs: int) -> dict[str, Any]:
    from svweave.application import queries
    from svweave.application.options import WorkspaceOptions
    from svweave.storage.db import Database

    opts = WorkspaceOptions(root=corpus)
    with Database(corpus / ".svweave" / "svweave.db") as db:
        b = db.conn.execute("SELECT build_id FROM builds ORDER BY created_at DESC LIMIT 1").fetchone()
        bid = b["build_id"]
        classes = [dict(r) for r in db.conn.execute(
            "SELECT declaration_id, qualified_name FROM declarations WHERE build_id=? AND "
            "kind='class' ORDER BY qualified_name LIMIT 12", (bid,))]
        funcs = [dict(r) for r in db.conn.execute(
            "SELECT declaration_id, qualified_name FROM declarations WHERE build_id=? AND "
            "kind='function' ORDER BY qualified_name LIMIT 12", (bid,))]
        popular = [dict(r) for r in db.conn.execute(
            'SELECT d.symbol_id AS symbol_id, COUNT(*) AS n FROM "references" r JOIN declarations d '
            "ON d.build_id=r.build_id AND d.declaration_id=r.target_id WHERE r.build_id=? "
            "AND d.symbol_id IS NOT NULL GROUP BY d.symbol_id ORDER BY n DESC LIMIT 8", (bid,))]
    sym_set = [c["qualified_name"] for c in classes + funcs] or ["bench_top"]
    out: dict[str, Any] = {"build_id": bid, "limit": 100}

    def q_symbol(i: int) -> Any:
        return queries.symbol(opts, bid, "bench", name=sym_set[i % len(sym_set)])

    def q_hier(i: int) -> Any:
        return queries.hierarchy(opts, bid, "bench", top="bench_top", depth=2 + i % 3, limit=100)

    def q_refs(i: int) -> Any:
        return queries.references(opts, bid, popular[i % len(popular)]["symbol_id"], "bench",
                                  limit=100)

    for name, fn, qset in (
        ("symbol", q_symbol, {"names": sym_set}),
        ("hierarchy", q_hier, {"top": "bench_top", "depth": "2..4", "limit": 100}),
        ("references", q_refs, {"symbols_by_reference_count_desc": [p["n"] for p in popular],
                                "limit": 100}),
    ):
        if name == "references" and not popular:
            out[name] = {"status": "SKIPPED", "reason": "no resolved references in the build"}
            continue
        try:
            fn(0)
            out[name] = {**summarize(timed(fn, runs)), "query_set": qset, "limit": 100}
        except Exception as e:  # noqa: BLE001 - recorded, not hidden
            out[name] = {"status": "ERROR", "error": f"{type(e).__name__}: {str(e)[:300]}"}
    out["definition"] = ("in-process application services (what the MCP server calls), SQLite "
                         "warm, 3 warm-up calls not counted")
    return out


TEXT_QUERIES = ["mod_0007", "agent_a", "fn_3", "pkg_001", "stage", "g_stage", "base_agent",
                "bench_top", "ifc_002", "w_ge_nothing_matches_this"]


def search_phase(corpus: Path, runs: int, use_graph: bool) -> dict[str, Any]:
    from svweave.application import services
    from svweave.application.options import WorkspaceOptions
    from svweave.application.search import svw_search
    from svweave.application.session import Session
    from svweave.snapshot.capture import snapshots_dir

    opts = WorkspaceOptions(root=corpus)
    ctx = opts.load()
    sid = sorted((snapshots_dir(ctx)).iterdir())[0].name
    sroot = snapshots_dir(ctx) / sid / "files"
    from svweave.application.keys import cursor_key

    key = cursor_key(ctx)

    def text_q(i: int) -> Any:
        return svw_search(snapshot_id=sid, snapshot_root=sroot, query=TEXT_QUERIES[i % len(TEXT_QUERIES)],
                          limit=100, registry=None, cursor_key=key)

    res: dict[str, Any] = {"snapshot_id": sid}
    probe = text_q(0)
    res["text"] = {**summarize(timed(text_q, runs)), "query_type": "literal text (text_fallback)",
                   "graph_state": "not used", "backend_used": probe.data.get("backend_used"),
                   "queries": TEXT_QUERIES, "limit": 100}
    if not use_graph:
        res["graph"] = {"status": "SKIPPED", "reason": "--no-graph given"}
        return res
    sess = Session(opts)
    try:
        t0 = time.perf_counter()
        first = services.search(sess, "bench-g0", snapshot_id=sid, query="mod_0007", limit=100)
        index_first_s = time.perf_counter() - t0
        used = first.data.get("backend_used")
        if used != "codebase-memory-mcp":
            res["graph"] = {"status": "SKIPPED", "backend_used": used,
                            "reason": "graph backend not usable for this corpus/host: "
                                      + "; ".join(first.warnings)[:600],
                            "first_call_s": index_first_s}
            return res
        reg = sess.registry
        cbm_pid = getattr(reg.cbm(sroot), "pid", None) if reg is not None else None
        gq = ["mod_0007", "agent_a", "bench_top", "mod_0123", "ifc_002", "pkg_001"]

        def graph_q(i: int) -> Any:
            e = services.search(sess, "bench-g", snapshot_id=sid, query=gq[i % len(gq)], limit=100)
            if e.data.get("backend_used") != "codebase-memory-mcp":
                raise RuntimeError("graph backend degraded during timing: " + str(e.warnings)[:200])
            return e

        peak = _tree_rss_kib(cbm_pid) if cbm_pid else None
        res["graph"] = {**summarize(timed(graph_q, runs)), "query_type": "graph (cbm search_graph)",
                        "graph_state": (reg.health("codebase-memory-mcp").status.value if reg is not None else None),
                        "graph_state_reasons": (reg.health("codebase-memory-mcp").reasons[:3]
                                                if reg is not None else None),
                        "first_call_incl_index_s": index_first_s, "queries": gq, "limit": 100,
                        "cbm_rss_kib_after_queries": peak,
                        "items_found_first": len(first.data.get("items", []))}
    except Exception as e:  # noqa: BLE001
        res["graph"] = {"status": "SKIPPED", "reason": f"{type(e).__name__}: {str(e)[:500]}"}
    finally:
        sess.close()
    return res


def compare(cold: dict[str, Any], hot: dict[str, Any], srch: dict[str, Any],
            reference: bool) -> dict[str, Any]:
    def row(name: str, value: float | None, budget: float, unit: str, why_missing: str = "") -> dict[str, Any]:
        if value is None:
            return {"target": name, "budget": f"<= {budget} {unit}", "status": "FAIL",
                    "measured": None, "reason": why_missing or "no measurement"}
        st = "PASS" if value <= budget else "FAIL"
        d: dict[str, Any] = {"target": name, "budget": f"<= {budget} {unit}",
                             "measured": round(value, 4), "unit": unit, "status": st}
        if st == "FAIL":
            d["reason"] = f"{value:.3f} {unit} exceeds {budget} {unit}"
        return d

    rows = [
        row("cold semantic build (median of runs)",
            (cold.get("total_wall_s") or {}).get("median"), BUDGET["cold_build_s"], "s",
            "no COMPLETE cold build"),
        row("cold semantic build (max of runs)",
            (cold.get("total_wall_s") or {}).get("max"), BUDGET["cold_build_s"], "s",
            "no COMPLETE cold build"),
    ]
    w = (cold.get("worker_peak_rss_kib") or {}).get("max")
    rows.append(row("HDL worker peak RSS (max)", None if w is None else w / 1048576,
                    BUDGET["worker_peak_gib"], "GiB", "no worker rusage"))
    for k in ("symbol", "hierarchy", "references"):
        v = hot.get(k, {})
        rows.append(row(f"hot {k} query p95", v.get("p95_s"), BUDGET["hot_query_p95_s"], "s",
                        v.get("reason") or v.get("error") or "not measured"))
    rows.append(row("hot repository search p95 (text fallback)", srch.get("text", {}).get("p95_s"),
                    BUDGET["hot_search_p95_s"], "s", "not measured"))
    g = srch.get("graph", {})
    if g.get("status") == "SKIPPED":
        rows.append({"target": "hot repository search p95 (graph)", "status": "SKIPPED",
                     "reason": g.get("reason")})
    else:
        rows.append(row("hot repository search p95 (graph)", g.get("p95_s"),
                        BUDGET["hot_search_p95_s"], "s", "not measured"))
    failed = [r for r in rows if r["status"] == "FAIL"]
    return {
        "rows": rows,
        "all_measured_targets_pass": not failed,
        "reference_corpus": reference,
        "caveat": None if reference else (
            "NOT the plan 22.3 reference corpus (reduced scale): these PASS/FAIL values say "
            "nothing about the 100,000-line / 500-file target."),
    }


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    ap.add_argument("--scale", type=float, default=1.0)
    ap.add_argument("--seed", type=int, default=1)
    ap.add_argument("--corpus", type=Path, default=None)
    ap.add_argument("--cold-runs", type=int, default=5)
    ap.add_argument("--hot-runs", type=int, default=30)
    ap.add_argument("--no-graph", action="store_true")
    ap.add_argument("--out-dir", type=Path, default=BENCH)
    ap.add_argument("--evidence-dir", type=Path, default=None)
    a = ap.parse_args(argv)
    if a.cold_runs < 5 or a.hot_runs < 30:
        raise SystemExit("plan 22.3 requires --cold-runs >= 5 and --hot-runs >= 30")

    import gen_corpus

    corpus = (a.corpus or a.out_dir / f"corpus-s{a.scale}").resolve()
    stats_f = corpus / "corpus_stats.json"
    if not stats_f.exists():
        gen_corpus.generate(corpus, a.scale, a.seed, force=True)
    stats = json.loads(stats_f.read_text())
    xdg = (a.out_dir / "xdg").resolve()
    write_policy(corpus, xdg)
    env = {k: v for k, v in os.environ.items() if k in ("PATH", "HOME", "LANG", "LC_ALL")}
    env["XDG_CONFIG_HOME"] = str(xdg)

    started = dt.datetime.now(dt.UTC)
    result: dict[str, Any] = {
        "benchmark": "PERF-01",
        "started_utc": started.isoformat(timespec="seconds"),
        "host": host_info(corpus),
        "toolchain_lock_sha256": sha256_file(REPO / "toolchain.lock.json"),
        "source_identity": source_identity(),
        "svweave_version": "0.1.0",
        "corpus": {k: stats[k] for k in ("scale", "seed", "files", "lines", "bytes", "content_sha256",
                                         "packages", "interfaces", "modules")},
        "corpus_dir": "(local path omitted)",
        "uvm_in_loc": False,
        "targets_plan_22_3": BUDGET,
    }
    result["reference_host_met"] = (result["host"]["logical_cpus"] or 0) >= 8 and \
        result["host"]["ram_total_gib"] >= 32
    print(f"[bench] corpus {stats['files']} files / {stats['lines']} lines (scale {a.scale})",
          flush=True)
    result["tool_startup"] = startup(env, corpus)
    print("[bench] cold builds ...", flush=True)
    result["cold_build"] = cold_builds(corpus, env, a.cold_runs)
    if result["cold_build"]["ok_runs"] == 0:
        result["error"] = "no COMPLETE cold build; see cold_build.runs"
        hot, srch = {}, {}
    else:
        os.environ["XDG_CONFIG_HOME"] = str(xdg)
        print("[bench] hot queries ...", flush=True)
        hot = hot_queries(corpus, a.hot_runs)
        print("[bench] search ...", flush=True)
        srch = search_phase(corpus, a.hot_runs, not a.no_graph)
    result["hot_queries"] = hot
    result["hot_search"] = srch
    result["disk_usage"] = disk_usage(corpus)
    result["compare_plan_22_3"] = compare(
        result["cold_build"], hot, srch,
        reference=a.scale >= 1.0)
    leaked = subprocess.run(["pgrep", "-fa", "codebase-memory-mcp|verible-verilog-ls|serena"],
                            capture_output=True, text=True).stdout.strip().splitlines()
    result["leaked_backend_processes_after_run"] = [ln for ln in leaked if "pgrep" not in ln]
    result["finished_utc"] = dt.datetime.now(dt.UTC).isoformat(timespec="seconds")
    a.out_dir.mkdir(parents=True, exist_ok=True)
    out = a.out_dir / (started.strftime("%Y%m%dT%H%M%SZ") + f"-s{a.scale}.json")
    out.write_text(json.dumps(result, indent=2, default=str) + "\n")
    print(f"[bench] wrote {out}")
    if a.evidence_dir:
        a.evidence_dir.mkdir(parents=True, exist_ok=True)
        shutil.copy(out, a.evidence_dir / out.name)
        print(f"[bench] copied to {a.evidence_dir / out.name}")
    for r in result["compare_plan_22_3"]["rows"]:
        print(f"  {r['status']:8} {r['target']}: {r.get('measured')} {r.get('unit', '')} "
              f"({r.get('budget', '')}) {r.get('reason', '')}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
