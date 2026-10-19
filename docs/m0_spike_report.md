# M0 spike report: SVW-001, SVW-002, SVW-003 (partial)

Date: 2026-10-13. Host: Linux 4.18 (EL8) x86_64, glibc 2.28, 16 CPUs, about 1 GiB free RAM when
work started. All backends are the real pinned binaries. No mocks, no regex substitutes.
`qualification_state` stays `UNQUALIFIED`.

## 1. Pinned toolchain (from `toolchain.lock.json`)

| Component | Version / commit | Artifact SHA-256 | Probe status |
|---|---|---|---|
| pyslang | 12.0.0 (upstream 545f88b2ed3a) | wheel 3c24dfadf0f318928433eedd7558e128f7a05f8a4d514f81aab08f2cba3db47d | READY |
| slang CLI | v12.0, 12.0.0+545f88b2e | tar d1f680e2c90b95daab3b812a6431055e3d8828fde281dbf89b72ee3eceb90dd5; bin c6e06db3… | READY |
| codebase-memory-mcp | v0.11.0, 8972ea69c6ad (linux-amd64-portable) | tar 1f9e8293eb2bc5c05cfa27a7e8fc033da6d729ffad525ccfcdaa3fd606306683; bin ce11c141… | DEGRADED |
| Serena (serena-agent) | v1.7.0, 949a27ef1e5f | uv.lock 48f88af6b9a7c942820b65d0321176b33b49f0012a8a0cd036ee1f5140cfe167 | DEGRADED |
| Verible | v0.0-4296-g0f262651 (0f26265133bd) | tar 8569defb891d2316067613ea00442af28a7a09d405d95b54c0c91f9942d26635; ls bin d401eaa3… | (used by Serena) |
| MCP Python SDK (gateway) | mcp 2.3.0 | wheel dd0c44c089d16453e8ae31a3877a0054d7a2314caaa81f5e0541b9b1734b2377 | used by all spikes |
| Accellera uvm-core | 2020.3.1 (78c06547a2a0) | tar f55bdbc02cc500d4a2f41b31bad127653289eb2073f806ce156fb82662b362b8 | used by UVM profile |
| bubblewrap | 0.4.0 (host) | n/a (OS package) | works unprivileged |

Reproduction: `.venv/bin/python scripts/bootstrap_locked.py` (install/repair) and `--verify-only`
(no network). A full re-install into an empty scratch directory re-downloaded everything, verified
all hashes, rebuilt the Serena venv from `uv.lock`, and returned `ok: true` in 17.9 s
(`evidence/m0/bootstrap_repro_scratch.json`). Verify-only on the live tree: `evidence/m0/bootstrap_verify_only.json`.

## 2. G0 gate checklist (plan §18.1)

| G0 item | Result | Evidence |
|---|---|---|
| Lock tool candidates; required version fields non-null | PASS | `toolchain.lock.json` (all §7.2 fields filled; capability/license SHA-256 filled) |
| License inventory | PASS (inventory) / legal review NOT_DONE | `license_manifest.json`, `THIRD_PARTY_NOTICES.md`, `evidence/m0/licenses/` |
| Probe MCP schemas / Verible | PASS | `capability_report.json` (tools + per-tool schema SHA-256, protocol, LSP version) |
| pyslang: generate hierarchy | PASS | `evidence/m0/pyslang_spike.json` (LANES=2 → 2, LANES=4 → 4, `demo_top.g_lane[i].u_lane`) |
| pyslang: two same-name class methods | PASS | `svw_demo_uvm_pkg::svw_driver::run_phase` @312 and `…svw_monitor::run_phase` @637, distinct scopes |
| pyslang: one bind | PASS | `u_stream_hold_sva` under each lane; directive range in `demo_bind.sv`; `p_stall_holds`, `ap_stall_holds` |
| pyslang: port actual keeps slice | PASS | `data_i` actual `data_i[lane]`, bytes 780–792 of demo_top.sv |
| Graph extracts minimal SV fixture | PARTIAL | `evidence/m0/cbm_spike.json`: modules found (labelled `Class`); UVM package file `parse_unusable`; `svw_driver`, `svw_monitor`, 1 of 2 `run_phase`, `p_stall_holds`, and bind not extracted |
| Serena reads candidate | PASS | `find_symbol`, `get_symbols_overview`, `find_referencing_symbols` (cross-file needs `verible.filelist`) |
| Serena modifies candidate: `svw_driver::run_phase` (plan §21.5 target) | **BLOCKED** | Verible LS does not report `task` symbols → `find_symbol('svw_driver/run_phase')` = `[]`; `evidence/m0/verible_lsp_probe.json` |
| Serena modifies candidate: a real SV method via `replace_symbol_body` | PASS (substitute target) | `svw_env/build_phase` edited; only that method changed; `svw_smoke_test/build_phase` untouched; original fixtures unchanged; pyslang on candidate 0 errors |
| Isolation of Serena writes / graph state | PASS | `evidence/m0/isolation_probe.json`, `*_bwrap.json`; ADR-011 |
| Dedicated state; offline assets | PASS | HOME/XDG/SERENA_HOME/CBM_CACHE_DIR under `.toolchain/state/*`; pinned Verible first on PATH (no download); both spikes passed under `--unshare-net` |
| "Three backends real operations succeed" | **PARTIAL** | pyslang full; cbm indexing/query works but SV coverage is partial; Serena edit works for functions, not tasks |

**G0 overall: NOT PASSED as written.** The plan's required Serena edit target, the task
`svw_driver::run_phase`, is BLOCKED by a Verible capability gap, and graph extraction of the UVM
fixture is partial. The plan rule applies: report blockers, do not build large abstractions yet.
Decisions needed: (a) accept the exact-range patch path (plan §12.2) for task bodies, plus a Serena
symbol edit for functions, as the v1 claim; or (b) change the fixture or target; or (c) look for a
Verible/LS version that emits task symbols. (c) is untested.

## 3. Measurements

| Run | Wall | Peak RSS |
|---|---|---|
| pyslang demo LANES=2 (child process) | 0.144 s | 34 528 KiB |
| pyslang demo LANES=4 | 0.123 s | ≤34 528 KiB (ru_maxrss running max) |
| pyslang UVM 2020.3.1 + fixture | 0.768 s | 129 196 KiB |
| pyslang UVM on Serena candidate | 0.588 s | 131 256 KiB |
| slang CLI demo (§10.6 command) | 0.06–0.08 s | 10.7–11.0 MiB |
| slang CLI UVM | 0.75 s | 114 MiB |
| cbm: initialize | 8.2 s | cbm process VmHWM 7 544 KiB |
| cbm: index_repository (7 files, fast and full identical) | 3.0–3.3 s | — |
| cbm spike end-to-end | 14–29 s (shutdown dominates) | — |
| Serena: initialize | 3.2 s | serena python 109 160 KiB + verible-verilog-ls 5 136 KiB |
| Serena spike end-to-end (incl. edits) | 7.8–8.4 s | — |

## 4. Reproduce

All commands run from the repo root with `.venv/bin/python`. Each writes `.svweave/spike/<name>/result.json`
and, unless `--evidence ""`, `evidence/m0/<name>.json`.

```bash
.venv/bin/python scripts/bootstrap_locked.py --verify-only
.venv/bin/python scripts/spikes/pyslang_spike.py                 # builds demo_L2, demo_L4, uvm
.venv/bin/python scripts/spikes/cbm_spike.py --mode fast         # snapshot copy -> .svweave/spike/snapshot
.venv/bin/python scripts/spikes/serena_spike.py                  # candidate copy -> .svweave/spike/candidate
.venv/bin/python scripts/spikes/verible_lsp_probe.py             # raw LSP documentSymbol
.venv/bin/python scripts/spikes/isolation_probe.py               # bwrap write test + cbm/serena under bwrap
.venv/bin/python scripts/probe_backends.py                       # -> .svweave/probe/capability_report.json
.toolchain/slang/slang -f fixtures/demo/filelists/rtl.f -I fixtures/demo/include -DSVW_DEMO=1 \
  --single-unit --top demo_top --ast-json .svweave/debug/design.ast.json --ast-json-source-info \
  --all-deps .svweave/debug/dependencies.txt
```

Fixtures were created verbatim from plan §21.1–21.5. slang reported 0 errors and 0 warnings for the
demo and 0 errors for UVM, so no fixture edits were needed. The single UVM warning comes from the
UVM library itself (`uvm_config_db_implementation.svh:375`, unknown escape `\.`).

## 5. Facts for implementers

### 5.1 pyslang
See `docs/adr/ADR-003-pyslang-extraction.md` for exact classes, methods and snippets.

### 5.2 codebase-memory-mcp v0.11.0
- Launch: `codebase-memory-mcp` with no args gives an MCP stdio server. Negotiated protocol
  `2025-11-25`; serverInfo `{name: codebase-memory-mcp, version: 0.11.0}`. The client SDK offers newer
  versions, and the server negotiated down.
- Isolation env: `HOME`, `XDG_CONFIG_HOME`, `XDG_CACHE_HOME`, `CBM_CACHE_DIR`, `CBM_RUNTIME_DIR`, all
  pointing into `.toolchain/state/cbm*/` with mode 700. The global `~/.cache/codebase-memory-mcp` was
  never created.
- **Defaults are unsafe for us:** `ui_enabled=true` (HTTP UI on port 9749), `auto_watch=true`,
  `watcher_enabled=true`. Before launch, run `codebase-memory-mcp config set ui_enabled false`
  (likewise `auto_watch`, `watcher_enabled`, `auto_index`) with the isolated env. Settings persist in
  `<CBM_CACHE_DIR>/_config.db`. `cbm_spike.configure_state()` does this.
- Tools (17): `index_repository{repo_path*, mode: full|moderate|fast|cross-repo-intelligence, target_projects, name, persistence}`,
  `search_graph{project*, query | name_pattern | qn_pattern | label | file_pattern | relationship, limit, offset, format: tree|json, …}`,
  `query_graph{query* (Cypher), project*, graph: code|missed, max_rows, cursor, format}`,
  `trace_path{function_name*, project*, direction, depth, mode: calls|data_flow|cross_service, …}`,
  `get_code_snippet{qualified_name*, project*, source_mode: auto|full|outline, include_neighbors, …}`,
  `get_file_outline{project*, file_path*, labels, format}`, `get_graph_schema{project*, format, diagnostics}`,
  `index_status{project*, diagnostics: none|summary|full, format}`, `check_index_coverage{project*, paths|scopes (one required), …}`,
  `search_code{pattern*, project*, regex, mode, …}`, `list_projects`, `compare_graphs`, `get_architecture`,
  `detect_changes`, `delete_project`, `manage_adr`, `ingest_traces` (`*` = required). Full schemas
  and SHA-256 are in `capability_report.json`. The gateway must not expose `delete_project`,
  `manage_adr`, `ingest_traces`, or cross-repo `index_repository` modes.
- `--tool-profile=analysis` exposes 13 read tools but **not** `index_repository`. `scout` exposes 8
  (`evidence/m0/cbm_tool_profiles.json`). Indexing therefore needs the default profile or
  `codebase-memory-mcp cli index_repository …`. Sharing an index between a default-profile indexer
  and an analysis-profile query process was not tested.
- `index_repository` was **synchronous** on this fixture: the response already contained
  `status: indexed`, node/edge counts and parse coverage, and `index_status` was `ready` on the first
  poll. Use `format: "json"`; the default `tree` output is a compact text table.
- The project name is derived from the absolute path with `/` replaced by `-`, e.g.
  `home-tim-projects-SVWeave-.svweave-spike-snapshot-fixtures`. Qualified names are
  `<project>.<dir>.<file-stem>.<symbol>`.
- SV extraction facts on the fixture. Graph labels: Folder, File, Module (= the file), Class (= a SV
  `module`), Function, Project, Branch. Edges: DEFINES, USAGE, CONTAINS_FILE, ….
  - `demo_lane`, `demo_top` and `stream_hold_sva` are extracted as `Class` nodes with correct line
    ranges. `get_code_snippet` returns the module source.
  - `demo_bind.sv`: no entities (parse_partial at line 7). `demo_defs.svh`: no entities.
  - `svw_demo_uvm_pkg.sv`: `parse_unusable` across the whole file. Extracted: package as `Class`
    (lines 1–2), `svw_env`, `svw_smoke_test`, one `run_phase` (line 20, svw_monitor's, not scoped to
    its class), and one `build_phase`. **Missing:** `svw_driver`, `svw_monitor`, driver `run_phase`,
    and per-class method scoping.
  - Missing everywhere: generate blocks and instances, ports, property/assertion (`p_stall_holds`),
    bind relations, includes.
  - `mode: full` and `mode: fast` produced identical graphs (27 nodes / 36 edges).
- Annotation oddity: `get_file_outline` is annotated `readOnlyHint=false, destructiveHint=true`.
- stderr repeatedly logs `daemon.private_dir_group_writable_ancestor mode=0775` (group-writable
  ancestors of the state dir).

### 5.3 Serena v1.7.0
- Launch (argv, cwd = candidate):
  `serena start-mcp-server --project <candidate> --context <svweave_context.yml> --transport stdio --enable-web-dashboard false --open-web-dashboard false --enable-gui-log-window false --log-level INFO --tool-timeout 120`.
  Negotiated protocol `2025-11-25`. **serverInfo.version is `1.28.1`**, which is the version of
  Serena's embedded mcp SDK, not of Serena (1.7.0). Read the Serena version from package metadata.
- Env: `HOME`, `SERENA_HOME=<state>/home/.serena`, `XDG_*` under the state dir, and
  `PATH=<.toolchain/verible/bin>:<serena-venv/bin>:/usr/bin:/bin`. SolidLSP checks `PATH` before
  downloading Verible (default download version v0.0-4051 from github.com), so the pinned binary
  must come first on PATH. The probe confirms `which verible-verilog-ls` resolves to the pinned binary.
- `serena_config.yml` (in SERENA_HOME) keys that matter: `web_dashboard: False`,
  `web_dashboard_open_on_launch: False`, `gui_log_window: False`, `record_tool_usage: False`,
  `project_serena_folder_location: "<state>/projects/$projectFolderName/.serena"` (keeps project.yml
  and caches **outside** the candidate), `trusted_project_path_patterns: []`, and
  `excluded_tools: [execute_shell_command, create_text_file, activate_project, remove_project, open_dashboard]`.
  **If the key is omitted the dataclass default is `["**"]`, which trusts every project.** An
  untrusted project's `activation_command` (a shell command from project.yml) and project-level
  `ls_specific_settings` are ignored.
- **Fallback risk:** if `<project>/.serena/` exists inside the candidate, Serena uses it in preference
  to the configured location when the configured one does not exist. That makes the folder
  repository-controlled configuration, so the gateway must reject candidates that contain `.serena/`.
- project.yml schema key is `language_servers: ["systemverilog"]`. `languages` and `language` are
  accepted as legacy aliases.
- Custom context YAML: `fixed_tools: [get_symbols_overview, find_symbol, find_referencing_symbols, find_declaration, replace_symbol_body, get_diagnostics_for_file]`
  and `single_project: true`. `tools/list` then returns exactly those 6 tools: no shell, no file
  create, no `activate_project`, no memory tools, no regex `replace_content`.
- Tool shapes: `find_symbol{name_path_pattern*, relative_path, depth, include_body, include_info, include_kinds, exclude_kinds, substring_matching, max_matches, max_answer_chars}`,
  `find_referencing_symbols{name_path*, relative_path*, include_kinds, exclude_kinds, max_answer_chars}`,
  `get_symbols_overview{relative_path*, depth, max_answer_chars}`, `replace_symbol_body{name_path*, relative_path*, body*}`,
  `find_declaration{relative_path*, regex*, containing_symbol_name_path, include_body, include_info}`,
  `get_diagnostics_for_file{relative_path*, start_line, end_line, min_severity, max_answer_chars}`.
  Results are JSON text. Name paths are `pkg/class/method`; a relative pattern like
  `svw_env/build_phase` matches by suffix. `relative_path` is relative to the project root.
- `replace_symbol_body` expects the **full definition** (for SV, from `virtual function …` through
  `endfunction`). Take it from `find_symbol(..., include_body=true)` and modify it.
- Verible symbol model (raw LSP evidence in `evidence/m0/verible_lsp_probe.json`): documentSymbol
  reports package, class, module and `function` declarations, but **not `task` declarations** and not
  `function new` constructors. SV `module` is reported with LSP kind Method (6). Verible LS
  capabilities: definition, references, rename, documentSymbol, documentHighlight, formatting,
  codeAction, diagnostics. hoverProvider is false.
- **Cross-file references need `verible.filelist`** in the project root, listing project-relative
  source files. Without it `find_referencing_symbols(demo_lane)` returned `{}` whenever
  `demo_top.sv` had not been opened in that LS session (deterministic after Serena's symbol cache
  existed). With the file, it returned `demo_top/g_lane`. The spikes and probe now generate it in the
  candidate before launch.
- `get_diagnostics_for_file` on the UVM fixture returned `{}`. Verible does not resolve UVM, so do
  not treat this as validation. Validation stays with pyslang.
- On shutdown Serena logs `ERROR … Failed to write to stdin: [Errno 32] Broken pipe` after verible
  has already exited (exit code 0). This is benign.

### 5.4 Isolation (bwrap)
Command template and results: `docs/adr/ADR-011-isolation-bwrap.md`. Both MCP backends ran
unmodified under `--unshare-net` with a read-only original source, a read-only toolchain, and RW
limited to candidate + state.

## 6. Blockers and quirks (verbatim)
1. BLOCKED: `find_symbol(name_path_pattern='svw_driver/run_phase', include_body=True) returned '[]'; no symbol body available for replace_symbol_body`.
   Root cause: verible-verilog-ls v0.0-4296-g0f262651 documentSymbol for
   `class c1; function void f1(); task t1(); virtual task run_phase…` returns only `p, c1, f1, m, mf`.
2. cbm `index_repository` result: `"parse_unusable":{"files":[{"path":"uvm/svw_demo_uvm_pkg.sv","whole_file":true,"range_end":50}]...}`,
   `"parse_partial":{"files":[{"path":"demo/sva/demo_bind.sv","error_ranges":"7-7"},{"path":"uvm/svw_uvm_top.sv","error_ranges":"4-4"}]}`.
3. The non-portable cbm build fails on this host: `/lib64/libc.so.6: version 'GLIBC_2.38' not found (required by cbm/codebase-memory-mcp)`.
   The portable (static) build is pinned. In upstream `checksums.txt` the `-ui-` and non-ui tarballs
   have identical SHA-256, so they are the same binary and the UI is controlled by config.
4. pyslang `Compilation.getAllDiagnostics()` includes `DiagnosticSeverity.Ignored` entries (UVM: 1096 total, 1 effective).
5. pyslang output-port actuals are `AssignmentExpression` with `syntax == None`; use `.left`.
6. pyslang creates one BufferID per `include (UVM: 18 572 buffers / 169 files).
7. The plan's Serena license statement matches Serena main, not the pinned v1.7.0 (MIT). See `license_manifest.json`.
8. Serena `serverInfo.version` reports the embedded MCP SDK version (`1.28.1`).
9. Serena trusts every project if `trusted_project_path_patterns` is omitted (dataclass default `["**"]`).
10. Serena falls back to an in-candidate `.serena/` folder if one exists.
11. cbm defaults `ui_enabled=true` (port 9749), and watchers are on.
12. Verible needs `verible.filelist` for cross-file references.
13. Not done in M0: transitive-dependency license audit, legal review, slang/Verible bundled-library
    inventory, resource limits (prlimit/cgroups) and seccomp in the sandbox, shared-index behaviour
    across cbm tool profiles, Windows/WSL.
