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
| No product EDA runner | Product runtime does not invoke a simulator, formal or EDA compiler. A separate explicitly authorized native static-lint/JSON validation harness is test infrastructure, not EDA signoff or a query backend. `eda_compile`, `simulation`, `formal` and `coverage` remain separate; imported external evidence (`svweave evidence import`) is `EXTERNAL_DECLARED` and not verified by SVWeave. |
| No incremental build | Every build is a full parse and elaboration (ADR-006). An edit triggers a full rebuild. There is no claim that changing one file is cheaper. |
| Not a language-server replacement | No LSP, no semantic rename, no refactoring beyond the controlled edit flow. |
| Not implemented (plan 4.3) | Bit-level connectivity, clock/reset domain analysis, CDC/RDC/timing/power, encrypted source recovery, UVM factory/`config_db` resolution, SVA proof, coverage closure, VHDL/AMS/mixed-language. |
| One workspace per server | `svweave serve` is bound to one workspace; there is no HTTP endpoint (ADR-009); the only UI is the static HTML graph export (ADR-014). |
| UVM | The pinned Accellera `uvm-core` 2020.3.1 is an external input mapped through `svweave.local.yaml`. UVM runtime topology, phase scheduling, factory overrides and `config_db` hits are not computed; call sites are listed as written. |

## 2. Semantic core (pyslang extraction, ADR-003, `docs/supported_features.md`)

* **No "find all references" in pyslang.** References are the occurrences found by visiting
  elaborated code. Not covered: uninstantiated generic class bodies, definitions never elaborated,
  inactive generate and `` `ifdef `` branches, library modules never instantiated, and class
  specializations reached only as the static scope of a call (for example the body behind
  `uvm_config_db#(int)::set`). A missing reference is therefore not proof of absence; every
  reference result states its searched scope.
* **Build COMPLETE is not complete reference coverage (HDL-R01).** Output envelope **1.1**
  adds `analysis_coverage.reference_coverage`: explicit feature/scope statuses, observed
  project/external unsupported/drop counts, query scope/depth/truncation and blocker codes.
  The current contract always has `absence_proof_supported=false`; empty references or impact
  do not prove unused/safe-to-delete status or a complete rename. Positive indexed occurrences
  remain usable; this does not make all builds PARTIAL. Old/malformed coverage metadata is
  `UNKNOWN`, not complete; zero observed gaps is not evidence of exhaustive traversal. Non-current
  freshness, partial/blocked builds, truncation and unsearched scopes never relax this boundary.
  Strict 1.0 output clients must upgrade schemas; the new reader's ability to read old 1.0
  payloads does not make new 1.1 payloads valid under old strict schemas. See the
  [contract and test mapping](supported_features.md#machine-readable-reference-coverage-hdl-r01-output-envelope-11).
* **Known reference omissions** (not exhaustively counted in `unsupported_nodes`): packed and
  unpacked dimension expressions, subroutine return-type names, class names used only as scope
  prefixes (`Cls::member`), enum/struct member type names, `export` and `import` of DPI beyond the
  declaration, clocking-block signals, bind target instance lists
  (`bind mod : inst_list`), checker instances, primitives, config blocks. Only encountered
  unsupported node kinds/dropped occurrences are counted; an unvisited construct can be missing
  without contributing a count. No new references are invented by the coverage contract.
* **UVM call metadata is a bounded static inventory.** `context.uvm` recognizes an explicit
  resolved `uvm_pkg` API allowlist, not arbitrary wrappers or library authenticity. Written
  named/ordered/empty arguments are associated with formals, without evaluating values or
  filling defaults. Only 16 arguments are captured, with count/truncation and missing-syntax
  flags; expression text retains the existing size cap. `runtime_resolution=not_computed`
  applies even to factory `create` and config-db `get`. This does not extend traversal to
  previously unvisited generic bodies, or infer phase execution. See the UVM static call
  metadata section in `docs/supported_features.md`.
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
* **Ignored filelist options are restricted.** Built-in output-only options are allowlisted;
  profiles can also explicitly declare zero-argument runtime options in `ignored_options`.
  Declared names enter config identity and all ignored occurrences enter the ledger. Other
  unknown options block with `UNSUPPORTED_OPTION`; this is not arbitrary flag passthrough.
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
* No general automatic semantic rename is supported; the separate read-only rename preview
  below can prepare a candidate request, not prove exhaustive references. Deletes, new files,
  binary files, symlinks, hardlinked targets, and edits to filelists/generated inputs are unsupported.
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
* Earlier bounded evidence: [run 40474142649](../evidence/verilator-native-run40474142649/README.md)
  at `305e8c30747ba2eeb1b53a66eaa29e8255d8e245` passed five native Verilator static
  checks and 21 real slang semantic/UVM tests, each with zero failures/errors/skips. The
  separate supplement/gate suite has 195 parent testcase nodes plus 75 pytest 9.1.1 subtest
  outcomes (270 producer-reported aggregate), not 270 independently named tests. At that
  source, the strict qualification parser rejects the count mismatch; raw XML is preserved.
  This is not a current full three-backend qualification or a waiver/license signoff.
* Latest selected-scope evidence: [run 40617140613](../evidence/remaining-regressions-run40617140613/README.md)
  at `4b6454417ca1f92fae5063116f9458cbfbca443d` passed fast CI and **234 selected regressions**
  (203 integration + 19 fault-injection + 12 acceptance), with zero failures/errors/skips.
  The identity-bound ledger validates **235 collected = 234 selected + 1 DEP-01 BLOCKED**;
  DEP-01 is unwaived and its original acceptance is unchanged. A separate **40-case boundary
  suite PASS** includes four native cases also present in the 234, so do not add these as
  independent test totals. Backend probe results remain slang READY / graph and Serena DEGRADED.
  Fresh **26/26 mutations were killed**, with no survivors/errors. `qualify.py` was not run;
  the ledger explicitly retains `full_qualification_status=BLOCKED` and `release_qualified=false`.
* The same run's PERF-01 result is only a **0.05-scale smoke measurement: 4,844 lines / 25 files**,
  on four logical CPUs / 15.61 GiB RAM with warm page cache. It records `reference_host_met=false`
  and `reference_corpus=false`; graph search is **SKIPPED** (`--no-graph`). Passing measured
  targets does not establish plan 22.3 reference-scale or graph performance. The October 13
  mutation report also present in the artifact is historical carryover, not part of this run's
  26 mutations. Native Verilator was not rerun; simulation/formal/RTL coverage remain NOT_RUN.
* Original DEP-01 limitation: [run 40490935466 evidence](../evidence/dep01-nested-denial-run40490935466/README.md)
  at `4a7a360b455a4dc4db5cffa2b711beb1baf5e266` has fast CI success and **40 passing
  probe-isolation checks** (36 pure + 4 native), but the focused cleanroom test is **1 FAIL**,
  zero errors/skips. Nested preflight at **10:46:27.646820–10:46:27.717638 UTC** recorded
  `ready=false`; the bounded audit event at **10:46:27.708 UTC** denied `bwrap` capability
  **21 / sys_admin** under `unpriv_bwrap`. The official profile deliberately restricts child
  capabilities. This blocks the required inner sandbox inside the outer network-isolated
  controller; it is not permission to relax AppArmor or remove isolation.
* Historical broad-suite evidence remains [run 40485644533](https://github.com/DVman9527/SVWeave/actions/runs/40485644533)
  at `2d26630ab278c22079c0750832cacfcb8d5f00db`: **235 cases, 229 PASS, 6 FAIL, 0 errors/skips**.
  Its 34 boundary checks and backend contract probe succeeded, but review subsequently found
  other direct-native/degraded test paths. Do not call the whole broad suite isolated merely
  because boundary tests passed. Later containment/contract fixes were reviewed and **195
  local pure checks passed**. The five non-DEP failures now pass in the 234 selected regressions
  above, but DEP-01 remains blocked; this does not rewrite the historical six failures or pass
  the entire original suite. In the earlier diagnostic-only run, full probes/broad suites/mutation/benchmark were excluded by explicit
  conditions—**NOT_RUN**, not PASS. A dated mutation file in its artifact is historical carryover.
* **Latest user decision, 2026-10-28 03:17:21 UTC: option 2 explicitly selected**, superseding
  option 1 for future acceptance. [DEP-01-R2-host-controller / ADR-016](adr/ADR-016-dep01-trusted-host-controller.md)
  retains network-isolated offline install/verify and every backend's mandatory read-only,
  no-network, no-inherited-secret sandbox; only the trusted controller runs on the host without
  an outer network-isolation guarantee. This is a narrower contract, not equivalent to the old
  end-to-end network-isolated cleanroom. Controller downloads/source uploads, AppArmor/sysctl/
  capability changes, degraded backend fallback and broader product/Slurm exceptions remain
  unauthorized. **Revised DEP-01 acceptance is NOT_RUN pending new exact-source CI**; original
  DEP-01 FAIL/BLOCKED and option-1 selected-scope evidence stay unchanged. Full qualification
  remains **BLOCKED / UNQUALIFIED**; strict JUnit equality and separate EDA statuses are unchanged.

## 8. Graph export (ADR-014)

* Connection edges are shared-signal links from direct port associations, oriented by the declared
  port direction. They are not a netlist: no driver/load analysis, no bit level, no cross-hierarchy
  continuous assignments, `tran`/`alias`, or UVM runtime topology.
* Signals used by more instances than `--max-fanout` are listed as hubs, not drawn; instances beyond
  `--max-instances` are not drawn (shallowest first). Neither is proof of absence.
* The page is a snapshot of one build and does not refresh. Layout is a plain tree; very large
  designs need `--max-instances` or collapsing to stay readable.
* Only the user's browser renders it; the viewer is not part of the qualification gates.

## 9. Impact and diff impact

* `svweave query impact` and `svw_impact` use the elaborated instance tree and resolved references
  of ONE build. A module never instantiated under the build's tops (inactive generate, never loaded
  library member) has no instances and no dependents here. Counts are counts, not a risk score.
* `svweave query diff-impact` / `svw_diff_impact` map the OLD-side lines of `git diff -U0` onto the
  build's snapshot. That is exact only for a file whose snapshot bytes equal the base revision
  (`files[].aligned`); otherwise it is flagged approximate. A new file is not in the build.
* A change inside an included header or a macro definition is attributed to declarations in that
  file only; the files that include it are not re-expanded. Changed files that are not part of the
  build (testbench, docs) are listed under `unmapped_files`, not analysed.
* A diff is read against a workspace that has changed since the build, so the command never exits 5
  for STALE; `freshness` in the envelope still says so.
* Comparing with the working tree makes git run the repository's clean/process filters, so that mode
  is refused (`POLICY_DENIED`, `git_filters_configured`) when any is configured; `--head REF`
  (commit to commit) reads blobs only. Only the project's own subtree is diffed.
* The tool runs `git diff` / `git cat-file` with a fixed argument list (no shell, no external diff
  driver, no textconv, no fsmonitor, no optional locks) in the project root; revisions are plain
  tokens (no leading `-`, no `..`). `--repo` exists on the CLI only and must stay inside the policy's
  allowed roots.

## 10. Rename preview

* It is a PREVIEW. Applying goes through the controlled edit loop (candidate copy, slang validation
  of baseline and candidate, human approval, compare-and-set commit), which is what can refuse. A
  rename that the preview calls ready can still fail validation; that is the intended safety net.
* Edit sites come from slang's resolved references plus the formal name of NAMED port connections
  (`.old(expr)`), and, for modules, interfaces and programs, `end<kw> : old` labels and `bind old`
  targets. Each site is checked against the snapshot bytes: the text at the span must be exactly the
  old name, else the site is skipped and the preview is blocked.
* Not edited automatically, and reported instead: connections made by `.*`, `.old` (implicit) or
  `.old()` (blockers); parameter overrides by name `#(.old(..))` and argument bindings by name
  `f(.old(x))` (blockers); `defparam` (listed under `unconfirmed_occurrences`); sites spelled in a
  macro body (blocker); references slang did not resolve (blocker); uses of a local name in its
  declaring scope (searched up to the enclosing module) or in included headers that slang did not
  resolve (blocker); an occurrence scan that stopped early (blocker); the profile still naming the
  old top module or top parameter (warning);
  filelists, file names, hierarchical strings, `$root`/`config` blocks and anything in an inactive
  `ifdef` or generate branch.
* `unconfirmed_occurrences` is a lexical scan of the snapshot for the old name outside the edit set.
  The same name elsewhere is usually a different entity (a local `clk`), so for a local name it is
  information; for a module, interface, program or package name any remaining use is a blocker.
* No edit request is produced above 50 edits, `editing.max_files_per_patch` files or
  `editing.max_changed_lines` lines (the request schema and the edit loop's limits); the diff is
  still shown. The preview needs a COMPLETE, CURRENT build to call itself ready.
* Names must be plain identifiers (no escaped identifiers) and not a reserved keyword; a declaration
  of the same name in the same scope is a blocker, one in a nested scope a warning. Capture by a
  same-named declaration visible from an outer scope or an imported package is not analysed.


## Optional external Verilator inventory

The Verilator JSON importer is an unbound, `EXTERNAL_DECLARED` / `PARTIAL` static inventory,
not a product runtime compiler backend or an extension of slang's resolved-reference coverage. It never runs
simulation, resolves UVM runtime behavior, or changes edit eligibility. The JSON format is
pinned to 5.052. A separate explicit `scripts/verify_verilator_native.py` validation harness
uses mandatory sandboxing for real static lint/JSON checks; it does not run during product
queries/imports or change slang authority. The revised harness passed its bounded five-case
subset in [run 40474142649](../evidence/verilator-native-run40474142649/README.md), exact source
`305e8c30747ba2eeb1b53a66eaa29e8255d8e245`: three real lint/JSON/import-query positives
(HDL-VLT-01/02/05) and two expected-error rejections (HDL-VLT-03/04), zero skips.
This is not full Verilator-language or project qualification.

Initial native run [40471175289](../evidence/verilator-native-run40471175289/README.md)
remains **FAIL**, because the legacy fixture exposed producer columns after macro expansion
that do not address original source tokens. Those locations remain **UNVERIFIED**, never
edit bindings. The new passing run explicitly checks that discrepancy; it does not correct
or promote producer locations. No authoritative merged references or additional Verilator
UVM reference coverage is claimed. The separate 21-case slang semantic/UVM regression is
not native Verilator UVM acceptance. Simulation/formal/coverage remain `NOT_RUN`.
The local sandbox remains **BLOCKED** by `NETLINK_ROUTE` permissions. See
[static supplement scope and evidence](verilator_static_supplement.md).

## Slurm is not yet a supported execution backend

Offline configuration, routing and safety primitives do not constitute remote support. Compute-node sandbox/toolchain checks, site-integrated launch/supervision, ownership discovery, cancellation races, real pyslang equivalence and P1 graph/Serena lifecycle are still pending qualification/implementation. No Slurm integration, speedup, simulation or formal result is claimed. See [Slurm status](srun.md).
