#!/usr/bin/env python3
"""M0 codebase-memory-mcp spike (SVW-002): real binary over MCP stdio, official mcp SDK client.

Steps: fresh snapshot copy of fixtures -> initialize -> tools/list (schemas saved) ->
index_repository on the COPY -> poll index_status -> search_graph / get_code_snippet /
get_graph_schema / get_file_outline / check_index_coverage -> evidence JSON.

Usage:
    .venv/bin/python scripts/spikes/cbm_spike.py [--mode fast|moderate|full] [--tool-profile analysis]
        [--snapshot DIR] [--out DIR] [--no-copy]
Environment isolation: HOME / XDG_* / CBM_CACHE_DIR -> .toolchain/state/cbm, CBM_RUNTIME_DIR -> a short private temp dir
"""

from __future__ import annotations

import argparse
import json
import resource
import sys
import time
from pathlib import Path

import anyio

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _mcp_common import (  # noqa: E402
    REPO,
    STATE,
    TOOLCHAIN,
    call,
    cbm_env,
    list_all_tools,
    make_copy,
    mcp_session,
    proc_tree_hwm,
    sha256_file,
    tools_digest,
    tree_manifest,
    write_json,
)

QUERIES = ["demo_lane", "demo_top", "stream_hold_sva", "svw_driver", "svw_monitor", "run_phase",
           "p_stall_holds", "svw_demo_uvm_pkg"]


async def run(a) -> dict:
    from _sandbox import probe_spec
    if a.wrap:
        raise ValueError("arbitrary --wrap is no longer supported; isolation is mandatory")
    snap = Path(a.snapshot).resolve()
    if not a.no_copy:
        manifest = make_copy([REPO / "fixtures/demo", REPO / "fixtures/uvm"], snap / "fixtures")
    else:
        manifest = tree_manifest(snap / "fixtures")
    repo_path = str(snap / "fixtures")
    out_dir = Path(a.out)
    binary = Path(a.binary)
    state = Path(a.state).resolve()
    env = cbm_env(state)
    ev_cfg = configure_state(binary, env, state)
    ev: dict = {"backend": "codebase-memory-mcp", "binary": str(binary), "binary_sha256": sha256_file(binary),
                "repo_path": repo_path, "snapshot_manifest": manifest, "index_mode": a.mode,
                "server_args": [], "env": env, "state_config": ev_cfg}
    args = []
    if a.tool_profile:
        args.append(f"--tool-profile={a.tool_profile}")
    ev["server_args"] = args
    command = str(binary)
    spec = probe_spec(sources=(snap / "fixtures",), tools=(binary,), writable=(state,),
                      env=env, tmp_dirs=("/tmp/cbm-rt",))

    async with mcp_session(command, args, env, None, out_dir / "cbm_stderr.log",
                           read_timeout=a.timeout, sandbox=spec) as (s, init, timings):
        ev["initialize"] = init
        ev["timings"] = timings
        tools = await list_all_tools(s)
        ev["tools"] = tools
        ev["tools_digest"] = tools_digest(tools)
        names = {t["name"] for t in tools}
        calls = []
        ev["calls"] = calls

        t0 = time.monotonic()
        r = await call(s, "index_repository", {"repo_path": repo_path, "mode": a.mode}, timeout=a.timeout)
        calls.append(r)
        project = None
        if r.get("json") and isinstance(r["json"], dict):
            project = r["json"].get("project") or r["json"].get("name") or r["json"].get("project_name")
        ev["index_call_s"] = round(time.monotonic() - t0, 3)

        if project is None:
            lp = await call(s, "list_projects", {"format": "json"})
            calls.append(lp)
            ev["project_resolution"] = "list_projects"
            j = lp.get("json")
            if isinstance(j, dict):
                for p in j.get("projects", []):
                    if p.get("root_path") == repo_path or p.get("path") == repo_path or repo_path in json.dumps(p):
                        project = p.get("name") or p.get("project")
        ev["project"] = project

        # poll index_status until a terminal state
        status_hist = []
        deadline = time.monotonic() + a.timeout
        while project and time.monotonic() < deadline:
            st = await call(s, "index_status", {"project": project, "format": "json", "diagnostics": "summary"})
            status_hist.append(st)
            j = st.get("json") or {}
            state = (j.get("status") or j.get("state") or "") if isinstance(j, dict) else ""
            if str(state).lower() in ("ready", "indexed", "complete", "completed", "done", "idle", "error",
                                      "failed"):
                break
            await anyio.sleep(1.0)
        ev["index_status_history"] = status_hist
        ev["index_total_s"] = round(time.monotonic() - t0, 3)

        if project:
            if "get_graph_schema" in names:
                calls.append(await call(s, "get_graph_schema", {"project": project, "format": "json",
                                                                "diagnostics": "full"}))
            for q in QUERIES:
                calls.append(await call(s, "search_graph", {"project": project, "query": q, "limit": 20,
                                                            "format": "json"}))
                calls.append(await call(s, "search_graph", {"project": project, "name_pattern": q,
                                                            "limit": 20, "format": "json"}))
            for f in ["demo/rtl/demo_top.sv", "demo/rtl/demo_lane.sv", "demo/sva/stream_hold_sva.sv",
                      "demo/sva/demo_bind.sv", "uvm/svw_demo_uvm_pkg.sv", "demo/include/demo_defs.svh"]:
                calls.append(await call(s, "get_file_outline", {"project": project, "file_path": f,
                                                                "format": "json"}))
            if "check_index_coverage" in names:
                calls.append(await call(s, "check_index_coverage", {"project": project, "format": "json",
                                                                    "diagnostics": "full",
                                                                    "paths": sorted(manifest)}))
            # get_code_snippet for a qualified name returned by search_graph (first hit for demo_lane)
            qn = _first_qn(calls, "demo_lane") or "demo_lane"
            calls.append(await call(s, "get_code_snippet", {"project": project, "qualified_name": qn,
                                                            "source_mode": "full"}))
            qn2 = _first_qn(calls, "run_phase")
            if qn2:
                calls.append(await call(s, "get_code_snippet", {"project": project, "qualified_name": qn2,
                                                                "source_mode": "full"}))
            calls.append(await call(s, "query_graph", {"project": project, "query":
                                                       "MATCH (n) RETURN labels(n) AS label, count(*) AS c",
                                                       "format": "json"}))
            calls.append(await call(s, "query_graph", {"project": project, "query":
                                                       "MATCH (n) RETURN n.name, labels(n), n.file_path LIMIT 200",
                                                       "format": "json"}))
            calls.append(await call(s, "search_code", {"project": project, "pattern": "run_phase",
                                                       "mode": "compact"}))
            ev["backend_process_tree_peak_rss"] = proc_tree_hwm()
            if a.cleanup and "delete_project" in names:
                calls.append(await call(s, "delete_project", {"project": project}))
    ev["peak_rss_kib_children"] = resource.getrusage(resource.RUSAGE_CHILDREN).ru_maxrss
    ev["snapshot_manifest_after"] = tree_manifest(snap / "fixtures")
    ev["snapshot_unchanged"] = ev["snapshot_manifest_after"] == manifest
    return ev


def configure_state(binary: Path, env: dict, state: Path) -> dict:
    """Idempotently persist safe settings in the DEDICATED state: no HTTP UI, no watchers, no auto-index."""
    from _sandbox import probe_spec, run
    from svweave.security import safeio
    spec = probe_spec(tools=(binary,), writable=(state,), env=env, tmp_dirs=("/tmp/cbm-rt",))
    state.mkdir(parents=True, exist_ok=True)
    for sub in ("home", "cache"):
        safeio.ensure_private_dir(state, sub)
    state.chmod(0o700)
    out = {}
    for k, v in (("ui_enabled", "false"), ("auto_watch", "false"), ("watcher_enabled", "false"),
                 ("auto_index", "false")):
        p = run(spec, [str(binary), "config", "set", k, v], timeout=30)
        if p.returncode:
            raise RuntimeError(f"cbm safe config {k} failed: {p.stderr[-1000:]}")
        out[k] = {"rc": p.returncode, "stdout": p.stdout.strip()}
    p = run(spec, [str(binary), "config", "list"], timeout=30)
    if p.returncode:
        raise RuntimeError(f"cbm config list failed: {p.stderr[-1000:]}")
    out["config_list"] = p.stdout
    return out


def _first_qn(calls, needle):
    for c in calls:
        if c.get("tool") == "search_graph" and needle in json.dumps(c.get("arguments")):
            j = c.get("json")
            if isinstance(j, dict):
                cols = j.get("cols") or []
                if "qn" in cols:
                    i = cols.index("qn")
                    for row in j.get("rows", []):
                        if needle in str(row[i]):
                            return row[i]
                for key in ("results", "nodes", "matches"):
                    for n in j.get(key, []) or []:
                        qn = n.get("qualified_name") or n.get("qn")
                        if qn and needle in qn:
                            return qn
    return None


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--binary", default=str(TOOLCHAIN / "cbm/codebase-memory-mcp"))
    ap.add_argument("--snapshot", default=str(REPO / ".svweave/spike/snapshot"))
    ap.add_argument("--out", default=str(REPO / ".svweave/spike/cbm"))
    ap.add_argument("--evidence", default=str(REPO / "evidence/m0/cbm_spike.json"))
    ap.add_argument("--mode", default="fast")
    ap.add_argument("--tool-profile", default=None)
    ap.add_argument("--no-copy", action="store_true")
    ap.add_argument("--cleanup", action="store_true")
    ap.add_argument("--wrap", default=None, help="JSON argv prefix, e.g. a bwrap command")
    ap.add_argument("--state", default=str(STATE / "cbm"), help="dedicated cbm HOME/cache/runtime root")
    ap.add_argument("--timeout", type=float, default=180)
    a = ap.parse_args()
    t0 = time.monotonic()
    try:
        ev = anyio.run(run, a)
        ev["status"] = "completed"
    except BaseException as e:  # record raw failure
        ev = {"status": "exception", "exception": f"{type(e).__name__}: {e!r}"}
    ev["wall_s"] = round(time.monotonic() - t0, 3)
    write_json(Path(a.out) / "result.json", ev)
    if a.evidence:
        write_json(Path(a.evidence), ev)
    print(json.dumps({k: ev.get(k) for k in ("status", "project", "index_total_s", "wall_s",
                                             "peak_rss_kib_children", "snapshot_unchanged", "exception")},
                     indent=2))
    return 0 if ev.get("status") == "completed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
