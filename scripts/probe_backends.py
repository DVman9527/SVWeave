#!/usr/bin/env python3
"""Re-runnable backend contract probe (plan §7.4, SVW-002).

For each backend: launch the REAL pinned backend, record initialization / negotiated protocol /
tools-list / per-tool schema SHA-256 (MCP backends), LSP + Verible version (Serena), binding/CLI
version (slang), run one real fixture query, and classify READY / DEGRADED / UNAVAILABLE.
A backend is never READY merely because its process started: READY requires the expected tools
AND correct answers to the fixture queries listed under `checks`.

Writes .svweave/probe/capability_report.json (and prints a summary).

Usage: .venv/bin/python scripts/probe_backends.py [--out PATH] [--only pyslang,slang_cli,cbm,serena]
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import subprocess
import sys
import time
from pathlib import Path

import anyio

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "scripts"))
from _required_backends import required_backend_errors  # noqa: E402

sys.path.insert(0, str(REPO / "scripts/spikes"))
from _mcp_common import (  # noqa: E402
    STATE,
    TOOLCHAIN,
    call,
    canonical_json,
    cbm_env,
    list_all_tools,
    make_copy,
    mcp_session,
    proc_tree_hwm,
    serena_env,
    sha256_bytes,
    sha256_file,
    tools_digest,
    tree_manifest,
    write_verible_filelist,
)

PROBE = REPO / ".svweave/probe"
CBM_REQUIRED = ["index_repository", "index_status", "search_graph", "get_graph_schema", "get_code_snippet"]
SERENA_REQUIRED = ["find_symbol", "find_referencing_symbols", "get_symbols_overview", "replace_symbol_body"]
SERENA_FORBIDDEN = ["execute_shell_command", "create_text_file", "activate_project", "remove_project",
                    "open_dashboard", "replace_content", "write_memory"]
CBM_NOT_EXPOSED_BY_GATEWAY = ["delete_project", "manage_adr", "ingest_traces", "index_repository:cross-repo"]


def classify(checks: list[dict], alive: bool) -> str:
    if not alive:
        return "UNAVAILABLE"
    if any(c["required_for_ready"] and not c["pass"] for c in checks):
        # a required capability failed but the backend answered: degraded, unless nothing works
        return "DEGRADED" if any(c["pass"] for c in checks) else "UNAVAILABLE"
    return "READY"


def chk(name: str, ok: bool, detail, required: bool = True) -> dict:
    return {"check": name, "pass": bool(ok), "required_for_ready": required, "detail": detail}


# ---------------------------------------------------------------- pyslang
def probe_pyslang() -> dict:
    t0 = time.monotonic()
    rep: dict = {"backend": "pyslang", "kind": "in-process binding run in a child worker process"}
    try:
        v = subprocess.run([sys.executable, "-I", "-c", "import pyslang;print(pyslang.__version__)"],
                           capture_output=True, text=True, timeout=60)
        rep["version"] = v.stdout.strip()
        out = PROBE / "pyslang_result.json"
        p = subprocess.run([sys.executable, "-I", str(REPO / "scripts/spikes/pyslang_spike.py"),
                            "--only", "demo_L2,demo_L4,uvm", "--out", str(out), "--evidence", ""],
                           capture_output=True, text=True, timeout=600)
        r = json.loads(out.read_text())
        b = r["builds"]
        s = r["summary"]
        checks = [
            chk("demo LANES=2: 2 demo_lane instances, 0 diagnostics",
                s.get("demo_L2_lane_count") == 2 and s.get("demo_L2_diag_count") == 0,
                {"count": s.get("demo_L2_lane_count"), "diags": s.get("demo_L2_diag_count")}),
            chk("demo LANES=4: 4 demo_lane instances with g_lane[i].u_lane paths",
                s.get("demo_L4_lane_count") == 4 and "demo_top.g_lane[3].u_lane" in (s.get("demo_L4_lane_paths") or []),
                s.get("demo_L4_lane_paths")),
            chk("port actual expression keeps slice data_i[lane]", s.get("g_lane0_data_i_actual_text") == "data_i[lane]",
                s.get("g_lane0_data_i_actual_text")),
            chk("bind: one stream_hold_sva per lane", len(s.get("bound_checker_paths_L2") or []) == 2,
                s.get("bound_checker_paths_L2")),
            chk("UVM profile: 0 errors, two distinct run_phase scopes",
                s.get("uvm_error_count") == 0 and len({x["hier"] for x in s.get("run_phase") or []}) == 2,
                s.get("run_phase")),
        ]
        rep["checks"] = checks
        rep["measurements"] = {k: {"wall_s": v.get("wall_s"), "peak_rss_kib": v.get("peak_rss_kib_children_max")}
                               for k, v in b.items()}
        rep["status"] = classify(checks, alive=p.returncode == 0)
    except Exception as e:
        rep["status"] = "UNAVAILABLE"
        rep["error"] = f"{type(e).__name__}: {e}"
    rep["probe_wall_s"] = round(time.monotonic() - t0, 3)
    return rep


# ---------------------------------------------------------------- slang CLI
def probe_slang_cli() -> dict:
    exe = TOOLCHAIN / "slang/slang"
    rep: dict = {"backend": "slang_cli", "kind": "reproduction/diagnostic CLI (ADR-003)", "binary": str(exe)}
    if not exe.exists():
        rep["status"] = "UNAVAILABLE"
        rep["error"] = "binary missing"
        return rep
    rep["binary_sha256"] = sha256_file(exe)
    rep["version"] = subprocess.run([str(exe), "--version"], capture_output=True, text=True, timeout=10).stdout.strip()
    dbg = PROBE / "slang_cli"
    dbg.mkdir(parents=True, exist_ok=True)
    argv = [str(exe), "-f", "fixtures/demo/filelists/rtl.f", "-I", "fixtures/demo/include", "-DSVW_DEMO=1",
            "--single-unit", "--top", "demo_top", "--ast-json", str(dbg / "design.ast.json"),
            "--ast-json-source-info", "--all-deps", str(dbg / "dependencies.txt")]
    p = subprocess.run(argv, capture_output=True, text=True, timeout=60, cwd=REPO)
    deps = (dbg / "dependencies.txt").read_text().split() if (dbg / "dependencies.txt").exists() else []
    checks = [chk("plan §10.6 command: build succeeded with 0 errors", p.returncode == 0 and "0 errors" in p.stdout,
                  p.stdout[-400:]),
              chk("--all-deps lists the include header", "fixtures/demo/include/demo_defs.svh" in deps, deps)]
    rep.update({"argv": argv, "checks": checks, "status": classify(checks, alive=True),
                "paired_with_pyslang": "slang 12.0.0+545f88b2e / pyslang 12.0.0 (same release tag v12.0)"})
    return rep


# ---------------------------------------------------------------- cbm
async def _probe_cbm() -> dict:
    sys.path.insert(0, str(REPO / "scripts/spikes"))
    from cbm_spike import configure_state
    binary = TOOLCHAIN / "cbm/codebase-memory-mcp"
    state = STATE / "cbm_probe"
    env = cbm_env(state)
    rep: dict = {"backend": "codebase-memory-mcp", "binary": str(binary), "binary_sha256": sha256_file(binary)}
    rep["state_config"] = configure_state(binary, env, state)["config_list"]
    snap = PROBE / "cbm_snapshot"
    manifest = make_copy([REPO / "fixtures/demo", REPO / "fixtures/uvm"], snap / "fixtures")
    rep["snapshot_manifest_sha256"] = sha256_bytes(canonical_json(manifest))
    async with mcp_session(str(binary), [], env, None, PROBE / "cbm_stderr.log", read_timeout=180) as (s, init, tm):
        rep["alive"] = True
        rep["initialize"] = init
        rep["protocol_version"] = init.get("protocolVersion")
        rep["server_info"] = init.get("serverInfo")
        tools = await list_all_tools(s)
        rep["tools"] = [{"name": t["name"], "inputSchema": t.get("inputSchema"),
                         "outputSchema": t.get("outputSchema"), "annotations": t.get("annotations")} for t in tools]
        rep["schema_digest"] = tools_digest(tools)
        names = {t["name"] for t in tools}
        idx = await call(s, "index_repository", {"repo_path": str(snap / "fixtures"), "mode": "fast"}, timeout=180)
        j = idx.get("json") or {}
        project = j.get("project")
        st = await call(s, "index_status", {"project": project, "format": "json", "diagnostics": "summary"}) if project else {}
        stj = st.get("json") or {}
        q1 = await call(s, "search_graph", {"project": project, "query": "demo_lane", "format": "json"})
        q2 = await call(s, "search_graph", {"project": project, "name_pattern": "svw_driver", "format": "json"})
        q3 = await call(s, "search_graph", {"project": project, "name_pattern": "run_phase", "format": "json"})
        rep["backend_process_tree_peak_rss"] = proc_tree_hwm()
        rows = lambda c: ((c.get("json") or {}).get("rows") or  # noqa: E731
                          [r for g in (c.get("json") or {}).get("groups", []) for r in g.get("rows", [])])
        unusable = [f["path"] for f in (stj.get("parse_unusable") or {}).get("files", [])]
        partial = [f["path"] for f in (stj.get("parse_partial") or {}).get("files", [])]
        rep["index"] = {"project": project, "status": stj.get("status"), "nodes": stj.get("nodes"),
                        "edges": stj.get("edges"), "parse_unusable": unusable, "parse_partial": partial}
        checks = [
            chk("required tools present", all(n in names for n in CBM_REQUIRED),
                {"missing": [n for n in CBM_REQUIRED if n not in names]}),
            chk("index_status == ready", stj.get("status") == "ready", stj.get("status")),
            chk("search_graph finds demo_lane", any("demo_lane" in json.dumps(r) for r in rows(q1)), rows(q1)),
            chk("search_graph finds class svw_driver", bool(rows(q2)), rows(q2)),
            chk("two run_phase methods extracted", len(rows(q3)) >= 2, rows(q3)),
            chk("no fixture file parse_unusable", not unusable, unusable),
        ]
        rep["checks"] = checks
        rep["gateway_must_not_expose"] = [n for n in CBM_NOT_EXPOSED_BY_GATEWAY if n.split(":")[0] in names]
    rep["status"] = classify(rep["checks"], alive=True)
    return rep


def probe_cbm() -> dict:
    t0 = time.monotonic()
    try:
        rep = anyio.run(_probe_cbm)
    except BaseException as e:
        rep = {"backend": "codebase-memory-mcp", "status": "UNAVAILABLE", "error": f"{type(e).__name__}: {e!r}"}
    rep["probe_wall_s"] = round(time.monotonic() - t0, 3)
    return rep


# ---------------------------------------------------------------- serena
async def _probe_serena() -> dict:
    from serena_spike import prepare
    state = STATE / "serena_probe"
    cand = PROBE / "serena_candidate"
    manifest = make_copy([REPO / "fixtures/demo", REPO / "fixtures/uvm"], cand / "fixtures")
    project = cand / "fixtures"
    rep_filelist = write_verible_filelist(project)
    manifest = tree_manifest(project)
    cfg = prepare(state, project)
    env = serena_env(state)
    exe = TOOLCHAIN / "serena-venv/bin/serena"
    vls = TOOLCHAIN / "verible/bin/verible-verilog-ls"
    rep: dict = {"backend": "serena", "exe": str(exe), "config": cfg, "verible_filelist": rep_filelist,
                 "verible_ls": str(vls), "verible_ls_sha256": sha256_file(vls),
                 "verible_ls_version": subprocess.run([str(vls), "--version"], capture_output=True, text=True,
                                                      timeout=10).stdout.strip(),
                 "serena_version": subprocess.run([str(TOOLCHAIN / "serena-venv/bin/python"), "-I", "-c",
                                                   "import importlib.metadata as m;print(m.version('serena-agent'))"],
                                                  capture_output=True, text=True, timeout=30).stdout.strip(),
                 "which_verible_in_serena_env": subprocess.run(["/usr/bin/which", "verible-verilog-ls"], env=env,
                                                               capture_output=True, text=True).stdout.strip()}
    args = ["start-mcp-server", "--project", str(project), "--context", cfg["context_file"], "--transport", "stdio",
            "--enable-web-dashboard", "false", "--open-web-dashboard", "false", "--enable-gui-log-window", "false",
            "--log-level", "INFO", "--tool-timeout", "120"]
    rep["args"] = args
    async with mcp_session(str(exe), args, env, str(project), PROBE / "serena_stderr.log", read_timeout=180) as (s, init, tm):
        rep["alive"] = True
        rep["initialize"] = init
        rep["protocol_version"] = init.get("protocolVersion")
        rep["server_info"] = init.get("serverInfo")
        tools = await list_all_tools(s)
        rep["tools"] = [{"name": t["name"], "inputSchema": t.get("inputSchema"),
                         "outputSchema": t.get("outputSchema"), "annotations": t.get("annotations")} for t in tools]
        rep["schema_digest"] = tools_digest(tools)
        names = {t["name"] for t in tools}
        ov = await call(s, "get_symbols_overview", {"relative_path": "uvm/svw_demo_uvm_pkg.sv", "depth": 2})
        f1 = await call(s, "find_symbol", {"name_path_pattern": "demo_lane"})
        f2 = await call(s, "find_symbol", {"name_path_pattern": "svw_driver/run_phase",
                                           "relative_path": "uvm/svw_demo_uvm_pkg.sv"})
        f3 = await call(s, "find_symbol", {"name_path_pattern": "svw_env/build_phase",
                                           "relative_path": "uvm/svw_demo_uvm_pkg.sv"})
        rf = await call(s, "find_referencing_symbols", {"name_path": "demo_lane", "relative_path": "demo/rtl/demo_lane.sv"})
        rep["backend_process_tree_peak_rss"] = proc_tree_hwm()
        rep["queries"] = {"overview": ov.get("text"), "find_demo_lane": f1.get("text"),
                          "find_svw_driver_run_phase": f2.get("text"), "find_svw_env_build_phase": f3.get("text"),
                          "refs_demo_lane": rf.get("text")}
        checks = [
            chk("required tools present", all(n in names for n in SERENA_REQUIRED),
                {"missing": [n for n in SERENA_REQUIRED if n not in names]}),
            chk("forbidden tools absent (shell/file-create/project-switch/dashboard/regex-replace/memory)",
                not (names & set(SERENA_FORBIDDEN)), sorted(names & set(SERENA_FORBIDDEN))),
            chk("pinned verible-verilog-ls first on PATH", rep["which_verible_in_serena_env"] == str(vls),
                rep["which_verible_in_serena_env"]),
            chk("find_symbol demo_lane", "demo_lane" in (f1.get("text") or ""), f1.get("text")),
            chk("find_referencing_symbols demo_lane -> demo_top/g_lane", "g_lane" in (rf.get("text") or ""),
                rf.get("text")),
            chk("find_symbol svw_env/build_phase (function method)", "build_phase" in (f3.get("text") or ""),
                f3.get("text")),
            chk("find_symbol svw_driver/run_phase (task method)", "run_phase" in (f2.get("text") or ""),
                f2.get("text")),
        ]
        rep["checks"] = checks
    rep["candidate_unchanged_by_probe"] = tree_manifest(project) == manifest
    rep["status"] = classify(rep["checks"], alive=True)
    return rep


def probe_serena() -> dict:
    t0 = time.monotonic()
    try:
        rep = anyio.run(_probe_serena)
    except BaseException as e:
        rep = {"backend": "serena", "status": "UNAVAILABLE", "error": f"{type(e).__name__}: {e!r}"}
    rep["probe_wall_s"] = round(time.monotonic() - t0, 3)
    return rep


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=str(PROBE / "capability_report.json"))
    ap.add_argument("--only", default="pyslang,slang_cli,cbm,serena")
    a = ap.parse_args()
    PROBE.mkdir(parents=True, exist_ok=True)
    fns = {"pyslang": probe_pyslang, "slang_cli": probe_slang_cli, "cbm": probe_cbm, "serena": probe_serena}
    report: dict = {"schema_version": "1.0", "generated_at": dt.datetime.now(dt.UTC).isoformat(),
                    "generator": "scripts/probe_backends.py", "platform": "linux-x86_64",
                    "qualification_state": "UNQUALIFIED",
                    "mcp_client_sdk": subprocess.run([sys.executable, "-I", "-c",
                                                      "import importlib.metadata as m;print(m.version('mcp'))"],
                                                     capture_output=True, text=True).stdout.strip(),
                    "backends": {}}
    lock = REPO / "toolchain.lock.json"
    if lock.exists():
        # hash only the components block (the lock itself records this report's hash -> avoid a cycle)
        report["toolchain_lock_components_sha256"] = sha256_bytes(
            canonical_json(json.loads(lock.read_text())["components"]))
    for name in a.only.split(","):
        report["backends"][name] = fns[name]()  # sequential: one heavy process at a time
    report["summary"] = {k: v.get("status") for k, v in report["backends"].items()}
    report["required_backend_errors"] = required_backend_errors(report["summary"])
    report["limitations"] = [
        f"{b}: {c['check']}" for b, v in report["backends"].items() for c in v.get("checks", []) if not c["pass"]]
    Path(a.out).parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(report, indent=2, default=str, ensure_ascii=False) + "\n"
    # no private absolute paths in committed evidence (plan 26.3): repo-relative placeholders
    text = text.replace(str(REPO), "${REPO}").replace(str(Path.home()), "${HOME}")
    Path(a.out).write_text(text)
    print(json.dumps({"summary": report["summary"], "limitations": report["limitations"]}, indent=2))
    return int(os.environ.get("SVWEAVE_REQUIRE_BACKENDS") == "1"
               and bool(report["required_backend_errors"]))


if __name__ == "__main__":
    raise SystemExit(main())
