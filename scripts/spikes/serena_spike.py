#!/usr/bin/env python3
"""M0 Serena spike (SVW-002): real Serena v1.7.0 MCP server (stdio) + pinned verible-verilog-ls.

Steps:
  1. fresh candidate COPY of fixtures (.svweave/spike/candidate/fixtures)
  2. write dedicated SERENA_HOME config (dashboard/GUI off, untrusted projects, project data
     folder OUTSIDE the candidate) and project.yml (language_servers: [systemverilog])
  3. launch `serena start-mcp-server` with a restricted custom context (fixed_tools)
  4. initialize + tools/list (schemas saved)
  5. get_symbols_overview / find_symbol / find_referencing_symbols on the UVM fixture
  6. replace_symbol_body of svw_driver/run_phase: "driver fixture" -> "driver fixture edited"
  7. diff candidate vs original; check only svw_driver::run_phase bytes changed; original untouched
  8. re-run the pyslang UVM build on the candidate (scripts/spikes/pyslang_spike.py --only uvm)

Usage:
    .venv/bin/python scripts/spikes/serena_spike.py [--wrap JSON_ARGV_PREFIX] [--out DIR]
"""

from __future__ import annotations

import argparse
import difflib
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
    list_all_tools,
    make_copy,
    mcp_session,
    proc_tree_hwm,
    serena_env,
    tools_digest,
    tree_manifest,
    write_verible_filelist,
    write_json,
)

UVM_REL = "uvm/svw_demo_uvm_pkg.sv"
OLD_TEXT = '"driver fixture"'
NEW_TEXT = '"driver fixture edited"'

CONTEXT_YML = """\
description: SVWeave M0 spike context - symbol read + symbol body edit only; no shell, no file create, no project switching
prompt: null
excluded_tools: []
included_optional_tools: []
fixed_tools:
  - get_symbols_overview
  - find_symbol
  - find_referencing_symbols
  - find_declaration
  - replace_symbol_body
  - get_diagnostics_for_file
tool_description_overrides: {}
single_project: true
structured_tool_output: null
"""


def serena_config_yml(projects_dir: Path) -> str:
    return f"""\
language_backend: LSP
line_ending: lf
gui_log_window: False
web_dashboard: False
web_dashboard_open_on_launch: False
web_dashboard_interface:
web_dashboard_listen_address: 127.0.0.1
web_dashboard_trusted_hosts:
  - 127.0.0.1
jetbrains_plugin_server_address: 127.0.0.1
jetbrains_launch_command:
log_level: 20
trace_lsp_communication: False
ls_specific_settings: {{}}
ls_priorities:
ignored_paths: []
read_only_memory_patterns: []
ignored_memory_patterns: []
tool_timeout: 120
excluded_tools: [execute_shell_command, create_text_file, activate_project, remove_project, open_dashboard]
included_optional_tools: []
fixed_tools: []
base_modes: []
default_modes:
default_max_tool_answer_chars: 150000
token_count_estimator: CHAR_COUNT
symbol_info_budget: 10
record_tool_usage: False
project_serena_folder_location: "{projects_dir}/$projectFolderName/.serena"
trusted_project_path_patterns: []
projects: []
"""


PROJECT_YML = """\
project_name: "svw_candidate"
language_servers: ["systemverilog"]
encoding: "utf-8"
activation_command:
line_ending: lf
language_backend:
ignore_all_files_in_gitignore: false
ls_specific_settings: {}
ls_workspace_folders: ["."]
ls_additional_workspace_folders: []
ignored_paths: []
read_only: false
excluded_tools: []
included_optional_tools: []
fixed_tools: []
default_modes:
added_modes:
initial_prompt: ""
symbol_info_budget:
read_only_memory_patterns: []
ignored_memory_patterns: []
"""


def prepare(state: Path, candidate_root: Path) -> dict:
    from _sandbox import probe_spec  # load local package path before the shared config helper
    from svweave.adapters.serena.config import SerenaPaths, check_candidate

    candidate_root = check_candidate(candidate_root)
    paths = SerenaPaths(state)
    # Validate writable scope before writing config (standalone CLI paths are untrusted).
    probe_spec(sources=(candidate_root,), writable=(paths.work,))
    paths.prepare(candidate_root)
    return {"serena_home": str(paths.serena_home), "context_file": str(paths.context_file),
            "project_yml": str(paths.projects_dir / candidate_root.name / ".serena/project.yml")}


def changed_line_ranges(a: str, b: str) -> list[dict]:
    sm = difflib.SequenceMatcher(a=a, b=b, autojunk=False)
    out = []
    for tag, i1, i2, j1, j2 in sm.get_opcodes():
        if tag != "equal":
            out.append({"tag": tag, "orig_byte_range": [len(a[:i1].encode()), len(a[:i2].encode())],
                        "orig_text": a[i1:i2], "new_text": b[j1:j2]})
    return out


async def run(a) -> dict:
    from _sandbox import checked_text, serena_spec
    if a.wrap:
        raise ValueError("arbitrary --wrap is no longer supported; isolation is mandatory")
    cand_parent = Path(a.candidate).resolve()
    manifest_orig = tree_manifest(REPO / "fixtures")
    cand_manifest = make_copy([REPO / "fixtures/demo", REPO / "fixtures/uvm"], cand_parent / "fixtures")
    project = cand_parent / "fixtures"
    verible_filelist = write_verible_filelist(project)
    cand_manifest = tree_manifest(project)
    state = Path(a.state).resolve()
    cfg = prepare(state, project)
    env = serena_env(state)
    exe = str(TOOLCHAIN / "serena-venv/bin/serena")
    args = ["start-mcp-server", "--project", str(project), "--context", cfg["context_file"],
            "--transport", "stdio", "--enable-web-dashboard", "false", "--open-web-dashboard", "false",
            "--enable-gui-log-window", "false", "--log-level", "INFO", "--tool-timeout", "120"]
    ev: dict = {"backend": "serena", "serena_exe": exe, "args": args, "env": env, "config": cfg,
                "candidate": str(project), "candidate_manifest_before": cand_manifest,
                "verible_filelist": verible_filelist}
    command = exe
    # This historical edit spike may write only its disposable candidate copy. The
    # qualification probe instead binds its fixture copy read-only.
    spec = serena_spec(TOOLCHAIN, state, project, cfg["context_file"], env, edit_candidate=True)
    ev["verible_ls_version"] = checked_text(
        spec, [str(TOOLCHAIN / "verible/bin/verible-verilog-ls"), "--version"], timeout=10)

    orig_text = (project / UVM_REL).read_text()
    async with mcp_session(command, args, env, str(project), Path(a.out) / "serena_stderr.log",
                           read_timeout=a.timeout, sandbox=spec) as (s, init, timings):
        ev["initialize"] = init
        ev["timings"] = timings
        tools = await list_all_tools(s)
        ev["tools"] = tools
        ev["tools_digest"] = tools_digest(tools)
        names = {t["name"] for t in tools}
        calls = []
        ev["calls"] = calls
        calls.append(await call(s, "get_symbols_overview", {"relative_path": UVM_REL, "depth": 2},
                                timeout=a.timeout))
        calls.append(await call(s, "find_symbol", {"name_path_pattern": "svw_driver/run_phase",
                                                   "relative_path": UVM_REL, "include_body": True},
                                timeout=a.timeout))
        calls.append(await call(s, "find_symbol", {"name_path_pattern": "run_phase", "relative_path": UVM_REL,
                                                   "include_body": False}, timeout=a.timeout))
        calls.append(await call(s, "find_symbol", {"name_path_pattern": "demo_lane", "include_body": False},
                                timeout=a.timeout))
        if "find_referencing_symbols" in names:
            calls.append(await call(s, "find_referencing_symbols", {"name_path": "svw_driver",
                                                                    "relative_path": UVM_REL},
                                    timeout=a.timeout))
            calls.append(await call(s, "find_referencing_symbols", {"name_path": "demo_lane",
                                                                    "relative_path": "demo/rtl/demo_lane.sv"},
                                    timeout=a.timeout))
        # EDIT 1 (plan §21.5 target): svw_driver/run_phase  "driver fixture" -> "driver fixture edited"
        ev["edits"] = []
        ev["edits"].append(await attempt_edit(s, project, calls, "svw_driver/run_phase", OLD_TEXT, NEW_TEXT,
                                              a.timeout, "svw_demo_uvm_pkg::svw_driver::run_phase",
                                              "svw_demo_uvm_pkg::svw_monitor::run_phase"))
        # EDIT 2 (fallback evidence, separate record): same-name FUNCTION method in two classes.
        # svw_env/build_phase vs svw_smoke_test/build_phase; Verible reports functions as symbols.
        ev["edits"].append(await attempt_edit(s, project, calls, "svw_env/build_phase", '"drv"', '"drv_edited"',
                                              a.timeout, "svw_demo_uvm_pkg::svw_env::build_phase",
                                              "svw_demo_uvm_pkg::svw_smoke_test::build_phase"))
        ev["backend_process_tree_peak_rss"] = proc_tree_hwm()
        if "get_diagnostics_for_file" in names:
            calls.append(await call(s, "get_diagnostics_for_file", {"relative_path": UVM_REL}, timeout=a.timeout))

    ev["candidate_manifest_after"] = tree_manifest(project)
    ev["changed_files_in_candidate"] = sorted(k for k in set(cand_manifest) | set(ev["candidate_manifest_after"])
                                              if cand_manifest.get(k) != ev["candidate_manifest_after"].get(k))
    ev["original_fixtures_unchanged"] = tree_manifest(REPO / "fixtures") == manifest_orig
    ev["unified_diff_total"] = "".join(difflib.unified_diff(
        orig_text.splitlines(True), (project / UVM_REL).read_text().splitlines(True),
        f"a/{UVM_REL}", f"b/{UVM_REL}"))
    ev["peak_rss_kib_children"] = resource.getrusage(resource.RUSAGE_CHILDREN).ru_maxrss
    return ev


def _body_from_find_symbol(j):
    if isinstance(j, list) and j and isinstance(j[0], dict):
        return j[0].get("body")
    if isinstance(j, dict):
        for v in j.values():
            if isinstance(v, list) and v and isinstance(v[0], dict) and "body" in v[0]:
                return v[0]["body"]
    return None


_PYSLANG_RANGES: dict | None = None


def pyslang_ranges() -> dict:
    """Byte ranges of the same-name methods in the ORIGINAL fixture, from the real pyslang build."""
    global _PYSLANG_RANGES
    if _PYSLANG_RANGES is None:
        outp = REPO / ".svweave/spike/serena/pyslang_original_uvm.json"
        from pyslang_spike import run_probe
        r = run_probe(["uvm"], REPO, REPO / "fixtures", TOOLCHAIN / "uvm-core/src")
        outp.parent.mkdir(parents=True, exist_ok=True)
        outp.write_text(json.dumps(r, indent=2) + "\n")
        _PYSLANG_RANGES = {x["qualified"]: [x["decl_range"]["start"]["file_offset"], x["decl_range"]["end_offset"]]
                           for x in r["builds"]["uvm"]["result"]["same_name_methods"]}
    return _PYSLANG_RANGES


async def attempt_edit(s, project, calls, name_path, old, new, timeout, target_q, sibling_q) -> dict:
    rec: dict = {"name_path": name_path, "relative_path": UVM_REL, "old_literal": old, "new_literal": new,
                 "target_qualified": target_q, "sibling_qualified": sibling_q}
    before = (project / UVM_REL).read_text()
    fs = await call(s, "find_symbol", {"name_path_pattern": name_path, "relative_path": UVM_REL,
                                       "include_body": True}, timeout=timeout)
    calls.append(fs)
    rec["find_symbol_result"] = fs.get("text") if fs.get("ok") else fs
    body = _body_from_find_symbol(fs.get("json"))
    rec["body_from_serena"] = body
    if not body or old not in body:
        rec["attempted"] = False
        rec["status"] = "BLOCKED"
        rec["reason"] = (f"find_symbol(name_path_pattern={name_path!r}, include_body=True) returned "
                         f"{fs.get('text')!r}; no symbol body available for replace_symbol_body")
        return rec
    new_body = body.replace(old, new)
    r = await call(s, "replace_symbol_body", {"name_path": name_path, "relative_path": UVM_REL, "body": new_body},
                   timeout=timeout)
    calls.append(r)
    rec["attempted"] = True
    rec["replace_result"] = r.get("text") if r.get("ok") else r
    after = (project / UVM_REL).read_text()
    rec["unified_diff"] = "".join(difflib.unified_diff(before.splitlines(True), after.splitlines(True),
                                                       f"a/{UVM_REL}", f"b/{UVM_REL}"))
    changes = changed_line_ranges(before, after)
    rec["byte_changes"] = changes
    rng = pyslang_ranges()
    t, sib = rng[target_q], rng[sibling_q]
    # offsets: 'before' equals the original fixture for edit 1; for edit 2 it differs from the original
    # only by edit 1's length delta located BEFORE/AFTER; compute shift for ranges after previous edits.
    orig = (REPO / "fixtures" / UVM_REL).read_text()
    shift_changes = changed_line_ranges(orig, before)

    def shifted(off):
        d = 0
        for c in shift_changes:
            if c["orig_byte_range"][1] <= off:
                d += len(c["new_text"].encode()) - len(c["orig_text"].encode())
        return off + d
    t2, sib2 = [shifted(t[0]), shifted(t[1])], [shifted(sib[0]), shifted(sib[1])]
    inside = bool(changes) and all(t2[0] <= c["orig_byte_range"][0] and c["orig_byte_range"][1] <= t2[1]
                                   for c in changes)
    touches_sib = any(not (c["orig_byte_range"][1] <= sib2[0] or c["orig_byte_range"][0] >= sib2[1])
                      for c in changes)
    rec["scope_check"] = {"target_byte_range_pyslang": t2, "sibling_byte_range_pyslang": sib2,
                          "n_changes": len(changes), "all_changes_inside_target": inside,
                          "sibling_touched": touches_sib, "literal_now_present": new in after}
    rec["status"] = "PASS" if (r.get("ok") and inside and not touches_sib and new in after) else "FAIL"
    return rec


def validate_candidate(cand_parent: Path, out: Path) -> dict:
    from pyslang_spike import run_probe
    report = run_probe(["uvm"], cand_parent, cand_parent / "fixtures", TOOLCHAIN / "uvm-core/src")
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, indent=2) + "\n")
    r = report["builds"]["uvm"]
    res = {"returncode": r["returncode"]}
    try:
        rr = r["result"]
        res.update({"wall_s": r["wall_s"], "peak_rss_kib": r["peak_rss_kib_children_max"],
                    "diagnostics_count": rr["diagnostics_count"], "errors": rr["diagnostics_errors"],
                    "run_phase": [{"q": x["qualified"], "text": x["decl_range"]["text"]}
                                  for x in rr["same_name_methods"]]})
    except Exception as e:
        res["parse_error"] = repr(e)
    return res


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--candidate", default=str(REPO / ".svweave/spike/candidate"))
    ap.add_argument("--state", default=str(STATE / "serena"))
    ap.add_argument("--out", default=str(REPO / ".svweave/spike/serena"))
    ap.add_argument("--evidence", default=str(REPO / "evidence/m0/serena_spike.json"))
    ap.add_argument("--wrap", default=None)
    ap.add_argument("--timeout", type=float, default=180)
    a = ap.parse_args()
    t0 = time.monotonic()
    try:
        ev = anyio.run(run, a)
        ev["status"] = "completed"
    except BaseException as e:
        ev = {"status": "exception", "exception": f"{type(e).__name__}: {e!r}"}
    ev["wall_s"] = round(time.monotonic() - t0, 3)
    if ev.get("status") == "completed":
        ev["pyslang_validation_on_candidate"] = validate_candidate(
            Path(a.candidate).resolve(), Path(a.out) / "pyslang_candidate_uvm.json")
    write_json(Path(a.out) / "result.json", ev)
    if a.evidence:
        write_json(Path(a.evidence), ev)
    summary = {k: ev.get(k) for k in ("status", "exception", "changed_files_in_candidate",
                                      "original_fixtures_unchanged", "wall_s", "peak_rss_kib_children")}
    summary["edits"] = [{k: e.get(k) for k in ("name_path", "status", "reason", "scope_check", "unified_diff")}
                        for e in ev.get("edits", [])]
    pv = ev.get("pyslang_validation_on_candidate") or {}
    summary["candidate_pyslang_errors"] = pv.get("errors")
    print(json.dumps(summary, indent=2))
    return 0 if ev.get("status") == "completed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
