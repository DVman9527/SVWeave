# Known limitations

Status: SVWeave v0.1.0-dev is a **developer preview** and is **UNQUALIFIED** unless a
qualification report (`.svweave/qualification/report.json`, produced by `scripts/qualify.py`) says
otherwise. This file collects every limitation recorded by the implementers (ADRs, spike report,
`docs/supported_features.md`, `docs/editing.md`). It is a collection, not a promise that the list
is complete (plan 26.3: limitations, unsupported features and semantic deviations must stay
visible). Plan references are to `SVWeave_plan.md`.

What static means here: every result is from static parsing and elaboration of the build you
selected. Nothing below, and nothing SVWeave reports, is a simulation, formal, coverage, CDC/RDC,
timing or signoff result (plan 4.3).

## 1. Scope and platform

| Limitation | Detail |
|---|---|
| Linux only | Qualified host class: Linux x86_64 with unprivileged user namespaces and `bwrap`. macOS and Windows are not qualified (plan 4.2, P1). The pinned codebase-memory-mcp is the `linux-amd64-portable` static build. |
| No EDA runner | There is no simulator, formal or EDA-compile invocation. `eda_compile`, `simulation`, `formal` and `coverage` are `NOT_RUN` unless the user imports external evidence (`svweave evidence import`), which is stored as `EXTERNAL_DECLARED` and is never verified by SVWeave. |
| No incremental build | Every build is a full parse and elaboration (ADR-006). An edit triggers a full rebuild. There is no claim that changing one file is cheaper. |
| Not a language-server replacement | No LSP, no semantic rename, no refactoring beyond the controlled edit flow. |
| Not implemented (plan 4.3) | Bit-level connectivity, clock/reset domain analysis, CDC/RDC/timing/power, encrypted source recovery, UVM factory/`config_db` resolution, SVA proof, coverage closure, VHDL/AMS/mixed-language. |
| One workspace per server | `svweave serve` is bound to one workspace; there is no HTTP endpoint (ADR-009). |
| UVM | The pinned Accellera `uvm-core` 2020.3.1 is an external input mapped through `svweave.local.yaml`. UVM runtime topology, phase scheduling, factory overrides and `config_db` hits are not computed; call sites are listed as written. |

## 2. Semantic core (pyslang extraction, ADR-003, `docs/supported_features.md`)

* **No "find all references" in pyslang.** References are the occurrences found by visiting
  elaborated code. Not covered: uninstantiated generic class bodies, definitions never elaborated,
  inactive generate and `` `ifdef `` branches, library modules never instantiated, and class
  specializations reached only as the static scope of a call (for example the body behind
  `uvm_config_db#(int)::set`). A missing reference is therefore not proof of absence; every
  reference result states its searched scope.
* **Unsupported reference kinds** (not extracted, counted in `unsupported_nodes`): packed and
  unpacked dimension expressions, subroutine return-type names, class names used only as scope
  prefixes (`Cls::member`), enum/struct member type names, `export` and `import` of DPI beyond the
  declaration, clocking-block signals, bind target instance lists
  (`bind mod : inst_list`), checker instances, primitives, config blocks.
* **Virtual dispatch.** A call through a handle is `dynamic_unresolved`; the static target is the
  declared method and the overriding methods are listed as candidates. None is chosen.
* **Macros.** Locations in macro bodies are shared by every expansion; a location query there
  returns all scoped candidates and `AMBIGUOUS_SYMBOL`. Editing such declarations is refused.
* **`explicit_groups` compilation units use slang `-C` files.** The library limitation: a group
  may not declare library inputs (`CAPABILITY_MISMATCH`). Top parameter overrides use slang 12's
  top-scoped form `-G <top>.NAME=VALUE` (verified: it overrides only that top; an unknown
  parameter is a slang error), so different values per top are expressible.
* **Timescale and language version have no free knob.** A design that mixes files with and
  without `` `timescale `` gets slang's `MissingTimeScale` error and the build is `PARTIAL`
  (`HAS_ERRORS`). The only supported knobs are the allowlisted profile field `language_options`
  (`timescale: <1|10|100><unit>/<1|10|100><unit>` -> `--timescale`, and `std:
  1364-2005|1800-2017|1800-2023|latest` -> `--std`); they are part of `config_id` and listed in
  the build's `semantic_deviations`. No other slang flag can be passed through.
* **The ignorable filelist option allowlist is fixed** (not configurable). Any other unknown
  option blocks the build (`UNSUPPORTED_OPTION`); ignorable ones are recorded in the ledger and
  are not part of `config_id`.
* **Encrypted / protected sources and implicit blackboxes are never COMPLETE.** A `` `pragma
  protect `` envelope is skipped by slang and an empty-bodied module that has ports is an implicit
  blackbox: both add a blocking coverage gap (`protected_envelope`, `empty_module_body`) and the
  build is `PARTIAL` with absence-of-dependency claims disabled. A stub model supplied by the
  owner is declared in `svweave.yaml` under `analysis.stubs` (module, original IP, model sha256,
  replaced scope; plan 16.5): the gap is then a recorded `stub_boundary`, still `PARTIAL`.
* **`__FILE__` shows the snapshot path.** The analysed bytes live under the snapshot directory
  (plan 9.4), so `` `__FILE__ `` expands to that path, not the original path.
* **Interfaces and virtual interfaces.** Associations name the interface instance and modport.
  Virtual interface handles are typed candidates and are never bound to an instance.
* **Port connections** cover direct instance associations only, not netlist dataflow.
* **SVA.** Declarations, bind directives and elaborated lanes are inventoried; execution and proof
  status are always `NOT_RUN`.
* **`ifdef`-disabled regions.** Declarations in the disabled side are recovered by re-parsing the
  token list; a fragment that is not a complete declaration is listed as a region without
  declarations.
* **PARTIAL builds.** Errors, unresolved or unsupported references, explicit stub boundaries or
  missing port associations make a build `PARTIAL` (exit code 3). Only COMPLETE builds move the
  published pointer.
* **`svw_edit` validate and commit run their builds synchronously** inside the tool call (they
  hold the call open for the duration of the build); only `svw_build` is an asynchronous job with
  `svw_job` polling. A client timeout shorter than the build time must use `svw_build` first.
* **Snapshot completeness for nested includes is directive-driven.** Relative includes with a
  directory part (`` `include "headers/defs.svh" ``, `"../common/x.svh"`) are followed
  transitively by scanning the HDL files of each source directory (bounded; only directories that
  pass the root rules). An include path assembled by macros is not discovered: the file is then
  not in the snapshot and the build is stopped by the dependency check (BLOCKED / error), never
  silently completed.
* **Toolchain identity.** `toolchain_id` also covers the sha256 of the extractor package sources
  (`adapters/slang/*.py`) and the verified content of the installed pyslang (every file hashed
  against its dist-info `RECORD`; the `RECORD` sha256 and any unlisted file in the package are
  checked against the lock). A mismatch is `CAPABILITY_MISMATCH`. The lock is found only via
  `SVWEAVE_TOOLCHAIN_ROOT` or the installed tool, never from the cwd or the analysed repository.
* **Cancellation** works inside the process that started the build (`cancel_job`); the MCP
  `svw_job cancel` kills the worker process group.
* **Read-path freshness uses a stat shortcut (plan 9.6).** To keep hot queries under the
  budget, `freshness` is the cached result of the last full hash check, reused while a stat
  fingerprint (path, size, mtime_ns, inode, ctime_ns of every snapshot-inventory file, the lookup
  directories incl. sub-directories, the loaded manifest/local/policy, the allowlisted env values
  and the toolchain identity) is unchanged; any difference triggers the full check again. An edit
  that preserves size, mtime, inode **and ctime** of a file is invisible to this shortcut on the
  read path only (ctime cannot be set from user space; it needs root or a clock rollback). Build
  and edit commit/publish paths always capture and byte-compare, never use the shortcut. The
  cache is per process (a new CLI invocation pays one full check).
* **Macro definition text edits are refused in v1** (reason `macro_definition_edit_unsupported`;
  they are outside the qualified subset). It covers the `define directive and its continuation
  lines; edits to macro invocation arguments stay allowed.

## 3. Graph backend: codebase-memory-mcp v0.11.0 (plan 11, `docs/m0_spike_report.md` 5.2)

The graph is a **candidate finder**, never a source of semantic truth (`SOURCE_CANDIDATE`
evidence). Measured gaps on the fixtures:

* The UVM package file `svw_demo_uvm_pkg.sv` is reported `parse_unusable` for the whole file.
  Only part of its content (package, `svw_env`, `svw_smoke_test`, one `run_phase`, one
  `build_phase`) is extracted; `svw_driver`, `svw_monitor`, the driver's `run_phase` and per-class
  method scoping are missing. `svw_uvm_top.sv` and `demo_bind.sv` are `parse_partial`.
* SystemVerilog `module` and `interface` declarations are labelled **`Class`** (a file is labelled
  `Module`).
* Not extracted anywhere: **ports, generate blocks and instances, properties/assertions, bind
  relations, includes.**
* `index_repository` modes `fast` and `full` produced identical graphs on the fixtures.
* The backend health is therefore **DEGRADED** (not READY) even when it works; the recorded gaps
  appear in the health reasons and in search limitations. When the graph is unavailable the search
  falls back to a **marked literal text search** (`backend_used: text_fallback`, outcome
  `PARTIAL`); a fallback never turns a failed mandatory backend into a success.
* Defaults of the upstream binary are unsafe (HTTP UI on port 9749, watchers); SVWeave turns them
  off in a dedicated state directory, runs the process in a bwrap sandbox without network, and does
  not expose `delete_project`, `manage_adr`, `ingest_traces` or cross-repo modes.
* The non-portable cbm build needs glibc 2.38 and fails on older hosts (EL8 has glibc 2.28); the
  pinned portable (static) build is used. See `docs/install.md`.
* The upstream warns `daemon.private_dir_group_writable_ancestor` when ancestors of the state
  directory are group-writable. The state directory itself is mode 0700.
* Sharing one index between the default-profile indexer and an analysis-profile query process was
  not tested.

## 4. Edit backend: Serena v1.7.0 + Verible (plan 12, ADR-012, `docs/editing.md`)

* **Verible does not expose `task` declarations or `function new` constructors**
  (`documentSymbol` omits them). Serena cannot locate them. Edits to tasks, constructors and any
  unqualified symbol kind are planned as a gateway **`exact_range`** patch
  (`edit_path: "exact_range"`, `counted_as_serena_semantic_edit: false`) and are never reported
  as Serena edits. The plan's original target `svw_driver::run_phase` (a task) is therefore
  BLOCKED for the Serena path; the same-name FUNCTION methods `svw_env::build_phase` and
  `svw_smoke_test::build_phase` are the verified substitute.
* **Tasks and constructors are now editable by name through slang** (ADR-013, edit kind
  `slang_symbol`, `edit_path: "slang_symbol"`, `counted_as_serena_semantic_edit: false`): the
  semantic database locates the declaration, the edit then runs the same exact-range CAS /
  approval path. This is a deviation from plan 12.2, which names Serena as the semantic edit path.
  Serena itself stays function-only and DEGRADED, so the G0 waiver is still needed; the
  `slang_symbol` edit is not a Serena edit and does not close G0. It needs a COMPLETE build, and its
  containment is lexical (the replacement is one declaration with no preprocessor directive, pasting
  or unknown macro, or macro whose body closes a construct or changes compiler state) plus a slang check after the candidate build (target span
  equals the replacement; declarations outside it unchanged). Macro invocations of existing macros
  are opaque, and what a body does is covered only by diagnostics, the diff and human review.
* The qualified Serena edit set is derived from recorded probe checks; today it is `function`
  only. A bare name such as `build_phase` is refused with `AMBIGUOUS_SYMBOL` (also for
  `slang_symbol`).
* Serena's symbol span may include qualifiers such as `virtual` that slang's declaration span
  excludes; those bytes are preserved. A Serena edit and an exact-range edit in the same file are
  refused (split the transaction).
* Cross-file references in Serena need a generated `verible.filelist` in the candidate root; the
  adapter writes it and the edit diff excludes it. Verible does not resolve UVM, so
  `get_diagnostics_for_file` is not validation; validation is always slang.
* Serena's own configuration lives in a **writable per-workspace state directory**
  (`SERENA_HOME` under the backend state dir), because Serena needs to write caches and
  `project.yml` somewhere. The adapter writes `serena_config.yml` and the restricted context file
  there before every launch (`trusted_project_path_patterns: []`, shell and file-creation tools
  excluded), and rejects a candidate containing `.serena/`. A process that can write that state
  directory as the same user can change those files; the controls hold for operations that go
  through the gateway.
* Serena's symbol cache can lag behind exact-range edits made outside Serena. The adapter detects
  a stale view and raises `EDIT_CONFLICT` (`serena_stale_view`).
* Not supported: renames, deletes, new files, binary files, symlinks, hardlinked targets, edits to
  filelists and generated inputs.
* Snapshots of non-base validate profiles are not pinned against `cache gc`.
* Several file replacements are not one atomic transaction. Per-file atomic replace, a write-ahead
  journal and a cooperative lock are provided; other editors can observe an intermediate state.
  Recovery never overwrites bytes it did not write (`docs/editing.md`).
* Static slang validation only. A passing validation says nothing about simulation behaviour.

## 5. Approval and client boundary (plan 23.2, `docs/security.md`)

* **Approval can be forged by a same-UID agent with unrestricted host access.** The approval record
  and its HMAC key are ordinary files in the user's trusted directory
  (`$XDG_CONFIG_HOME/svweave/workspaces/<key>/`). An agent that runs as the same user with a shell
  can read the key and write a valid approval, or simply edit the workspace. SVWeave does not and
  cannot prevent that. `svweave doctor` therefore reports **"client boundary not qualified"**, and
  editing is disabled by default.
* The `SVWEAVE_TEST_CONFIRM_TOKEN` environment variable (test harness only) supplies the typed
  confirmation without a TTY. Such an approval is recorded as `method: test_token` and is **not
  qualified**; do not use it in a controlled deployment.
* MCP tool allowlists and annotations, Serena's read-only mode and prompts are hints, not
  isolation (plan 23.2).
* Local indexing is not "end-to-end offline": snippets returned to a cloud-model MCP client leave
  the machine.

## 6. Isolation (ADR-011)

* `bwrap` 0.4.0 provides filesystem, PID, IPC, UTS and network namespaces. It applies **no
  resource limits and no seccomp filter**; memory limits come from `RLIMIT_AS` on the HDL worker
  only. When user namespaces are unavailable, backends run only in an explicitly degraded mode
  that is flagged in health and envelopes.
* Every path a sandboxed backend needs must be bound explicitly; "file not found" from a sandboxed
  backend is a configuration error, not missing source.

## 7. Qualification status (plan 22.2, 26.2)

* No licensed EDA environment was used: EDA compile, simulation and formal are `NOT_RUN`.
* Legal review of third-party licenses is `NOT_DONE`; transitive dependency licenses of the Serena
  venv and of the gateway venv, and code statically linked into the slang, pyslang and Verible
  binaries, are not inventoried (`license_manifest.json`).
* Performance numbers exist only for the corpus and host recorded in `evidence/bench/`. They are
  not a statement about other hosts or real projects, and no token-saving claim is made (plan 22.4
  has not been executed).
* SVWeave's own license is not decided (plan 3.2): **TBD by owner**.
