# Supported features and limitations (M2 semantic core)

Status: implemented in `src/svweave/adapters/slang/`, `application/build.py`,
`application/queries.py`. Existing semantic tests target pinned **pyslang 12.0.0** (ADR-003),
`fixtures/language`, `fixtures/demo` and the real UVM 2020.3.1 library. This is an implementation
inventory, not evidence that every feature has passed on the current source revision. New
HDL-COV-01/02 cases await pinned CI; see the coverage contract below and its dated evidence.
Goldens: `fixtures/expected/` (hand-written, see its README). Plan references are to
`SVWeave_plan.md`.

This is a **static** semantic core. Nothing here represents simulation, formal proof, UVM
runtime behaviour (factory resolution, `config_db` matching, phase scheduling), or EDA
(commercial simulator) semantics. `static-preview-qualified` needs the later milestones.

## 1. Pipeline

`svweave build --profile P`:

1. resolve manifest (M1) and capture an immutable snapshot (M1);
2. `toolchain_id` = H(pyslang version + wheel sha256 + canonical RECORD sha256, slang commit + binary
   sha256, extractor version + sha256 of the extractor sources, DTO schema) from
   `toolchain.lock.json` (found via `SVWEAVE_TOOLCHAIN_ROOT` or the installed tool only); the
   installed pyslang must equal the lock and its files must match its dist-info `RECORD`
   (`CAPABILITY_MISMATCH` otherwise). The pinned `record_sha256` is the sha256 of the sorted RECORD lines without installer-added entries (`INSTALLER`, `REQUESTED`, `direct_url.json`, `uv_cache.json`, `RECORD`, compiled `__pycache__`/`.pyc`), so pip, uv and the wheel's own RECORD give the same value; `build_id` = H(snapshot, config, toolchain, schema);
3. a COMPLETE build with that id is **reused** (no new job); anything else runs a job
   (`QUEUED/RUNNING/SUCCEEDED/FAILED/CANCELLED/TIMED_OUT`);
4. the HDL worker (`python -I -m svweave.adapters.slang.worker`, own process group, cwd = snapshot
   files root, `RLIMIT_AS` from `analysis.max_worker_memory_mib`, wall timeout
   `analysis.max_build_seconds`, clean environment) returns a JSON DTO; the gateway never loads
   pyslang;
5. dependency leak check (plan 8.4 step 9): every file slang read must be in the snapshot
   inventory (adapter-written `-C` group files are declared in the request); otherwise the build
   is stored as **BLOCKED**, semantic rows are withheld and nothing is published;
6. rows are written in one staging transaction; **only COMPLETE builds move the published
   pointer**; run artifacts are written to `.svweave/runs/<job-id>/` (plan 24.1).

Job state, completeness and diagnostics stay separate: a design with errors is job `SUCCEEDED`,
analysis `PARTIAL`, diagnostics `HAS_ERRORS` (SEM-07).

### Opt-in snapshot I/O reuse (Phase 1)

The update path is for one large top/profile with a few edited inputs. It preserves the ordinary
build pipeline and exact identities, but can avoid materializing an already verified full snapshot
or use verified copy-on-write donor files while creating a new full snapshot. Every current
relevant input and lookup candidate is still enumerated and content-checked independently of Git.
No writable hardlinks, semantic dependency-closure rebuild or graph-index delta is introduced.

Only an exact existing COMPLETE build can reuse semantic results. All other cases request
whole-profile analysis; source/header/library membership, file order, defines, parameters,
include order and toolchain changes are not reduced to a Git changed-file set. Publication,
freshness, historical queries, cursors and edit approval/CAS retain the existing rules. An
unchanged snapshot alone does not establish an unchanged semantic build identity.

Materialization reports distinguish full copies, verified existing-tree reuse and the donor-aware
path, and record actual copy/clone/hash work and fallback reasons. A donor-aware path can still
copy the whole tree when reflinks are unavailable. This is an implementation inventory, not a
new performance or qualification claim. See [operations](operations.md#2a-opt-in-snapshot-io-update)
for the request-local report and [ADR-018](adr/ADR-018-snapshot-io-update.md) for requirement/test
mapping and validation boundaries. Ordinary `build` and the frozen MCP tools remain unchanged.
Shared MCP users must invoke the update CLI explicitly; upgrading the runtime does not change
`svw_build` into an update. A COMPLETE update publishes to the same workspace state, and its
returned build ID can then be queried through the existing CLI/MCP tools.

### Completeness verdict

`COMPLETE` iff all of these hold:

* no error diagnostics (slang or driver errors) and every slang driver step succeeded
  (`parseCommandLine`, `processOptions`, `parseAllSources` are read from the worker status, never
  inferred from stderr text);
* no unresolved or unsupported reference (`candidate_source` / `unsupported`);
* no blackbox boundary: no explicit stub (`svw_stub` attribute or manifest `analysis.stubs`
  record), no implicit empty-bodied module with ports (`empty_module_body`) and no protected /
  encrypted envelope (`protected_envelope`); plan 16.5;
* every port association was exposed by slang with a consistent span;
* **per-occurrence scope rule (B1/M1):** unsupported symbol kinds, extractor exceptions and
  *dropped reference occurrences* are counted per occurrence and split by where the occurrence
  is spelled. Any occurrence in a **project** file makes the build `PARTIAL` (a reason is stored
  and "negative reference conclusions" are not claimed, plan 10.4). Occurrences in files of an
  **external source root** (`@root/...`, for example the pinned UVM library) are counted and
  listed in `analysis_coverage.limitations` without blocking, **except extractor exceptions,
  which block regardless of origin**. The verdict therefore never depends on which instance
  slang happened to visit first. Systematic omissions that were never visited need not increment
  any occurrence counter; a zero count does not establish complete references.

Warnings, `dynamic_unresolved` virtual calls, generic classes never specialized, definitions not
elaborated, `ifdef-disabled regions and unloaded library members are **limitations**, not
blockers.

Reference occurrences that cannot be recorded (iterator variables of array-method `with` clauses,
symbols without a source declaration, compiler-generated variables, missing locations) are
counted in `dropped_refs` (reason + scope). The function return variable and `this` ARE resolved
(`return_value` / `this` references to the function / class declaration).

## 2. What is extracted (plan 10.3)

| Category | Content |
|---|---|
| Declarations | kind, name, **qualified scope** (`Symbol.lexicalPath`: `pkg::class::method`, `module.signal`), name span, full span, compilation unit, library (definitions), type text, macro origin, kind-specific attributes. Kinds: module/interface/program definitions, package, class (generic and plain), field, variable, net, parameter, type_parameter, port, interface_port, argument, genvar, generate_block, modport, modport_port, property, sequence, typedef, constraint, covergroup, coverpoint, cover_cross, clocking, enum_value, struct_field, let, checker, instance. |
| Class/member | base class (text + declaration id), abstract/interface flag, per-specialization parameters (`specializations` table), methods (virtual/pure/static/constructor/visibility/arguments, `override` link, DPI import + spec string), `rand` mode, constraints, covergroups/coverpoints. A generic class has **one** declaration and N specializations. |
| References | source occurrence span, kind (`value`, `hier_value`, `call`, `system_call`, `type`, `extends`, `new`, `import`, `instantiation`, `unresolved`, `wildcard_connection`), target declaration, status, reason, enclosing declaration, context (call arguments **as written**, callee specialization path). Deduplicated per (span, kind, target) across instances and specializations. |
| Resolution status | `resolved_static`, `dynamic_unresolved` (virtual method called through a handle: the static target is the declared method; `override_candidates` lists overriding methods, none is chosen), `candidate_source` (a name slang could not resolve, from the diagnostic range), `unsupported` (reserved), `inactive_for_profile` (inactive regions table). `super.f()` and `Class::f()` are static. |
| Instances | path **components** (name + generate/array indices; identity), display path (SV-escaped), hierarchical path, definition (+ declaration id), parent, elaborated parameter values, generate context, top flag, interface flag, explicit stub flag (`(* svw_stub = "..." *)` attribute), blackbox candidate (ports but empty body), bind origin. |
| Port association | per instance and formal: direction, port type, association style (`named`, `named_implicit`, `positional`, `wildcard`, `open`, `omitted`), actual expression **text taken from the source bytes**, its span, expression kind, type/bit width, constant flag, every symbol referenced inside the actual, concatenation parts (a `{a,b}` is **never** collapsed to one signal), interface instance + modport for interface ports. Output actuals use the ADR-003 unwrap quirk. |
| SVA | per build: source entries (property/sequence declarations, assert/assume/cover/restrict/expect statements), **bind directives** (source span, target text, elaborated targets), and per-instance **elaborated** assertions (one per bound lane). Clock and `disable iff` text from the property spec when written. Execution/proof status is always `NOT_RUN`. |
| Diagnostics | severity, code, message, primary location, ranges, macro notes, option name. Default-off (`Ignored`) diagnostics are filtered per ADR-003. Driver stderr messages are kept as `DriverMessage` entries. |
| Dependencies | unique files from the SourceManager (macro and unnamed buffers excluded, repeated `include buffers deduplicated by full path), include directives, library members present but never loaded. |
| Inactive source | `ifdef/`else-disabled regions (from slang's own disabled-token lists; modules/interfaces/packages/classes inside them are found by parsing the region with slang), definitions not elaborated under the tops, generate branches not selected. All `inactive_for_profile`. |
| Unsupported inventory | symbol kinds the extractor does not handle (with count and a sample location) and extractor exceptions. |

### Locations (plan 13.4)

Canonical spans are **zero-based half-open byte offsets into the ORIGINAL physical file
bytes** (the snapshot copy; `logical` path in results). `line`/`col` are 1-based display values,
`position_encoding` is `utf-8` (column counts bytes). `lsp_positions()` converts to LSP
(zero-based line, UTF-16 or UTF-8 or UTF-32 character) and back
(`domain/locations.py`; lines end at `\n`, `\r\n` or `\r`; offsets inside a multi-byte character
raise instead of being rounded). Macro-expanded locations carry `macro` = `{name, spelled_in:
macro_argument|macro_body, expansion:{file,start,line,col}}`: the span is where the token text is
spelled; the expansion is the invocation site. `` `line `` directives only add a separate
`display:{file,line}`; they never move the canonical span. A spelling inside a macro **body**
is shared by every expansion, so a location query on it returns all scoped candidates and
`AMBIGUOUS_SYMBOL` (editing must refuse; the edit path is M4).

## 3. Queries (plan 14.3) and CLI (plan 15.1)

All return the common envelope: `snapshot_id`, `build_id`, `profile`, `freshness`
(`CURRENT` = the snapshot equals the workspace now by the M1 check **and** the build's `config_id`
and `toolchain_id` equal what the workspace resolves to now, so a change that only touches
`svweave.yaml` (defines, parameters, include order, CU mode) or the toolchain makes the old build
`STALE`; `STALE` = not current and this is the build the profile serves (its published build; a profile
with no published build serves its latest stored one), also while a newer build is still running
or failed, because only a fully validated build moves the published pointer; `HISTORICAL` = not
current and not the served build (another build of the profile is published, which may be a newer
build or an older one re-published by build reuse; or this build was never published while
another is), or the snapshot was garbage-collected), `analysis_coverage{completeness, scope, basis, unresolved_count, limitations, reference_coverage}`,
`provenance{backend: slang, toolchain_id, evidence_kind: HDL_STATIC_RESOLVED}`, pagination.

| CLI | Service | Notes |
|---|---|---|
| `svweave build --profile P [--no-reuse]` | `run_build` | exit 0 COMPLETE; **3** errors/PARTIAL/BLOCKED; 4 timeout/crash/resource; 2 config |
| `svweave update --profile P [--base REF] [--no-git]` | `run_update` | opt-in snapshot I/O reuse and advisory Git report; same semantic/publish/exit policy as build, no new MCP tool |
| `svweave query symbol --build B (--symbol S \| --file F --byte N \| --name X)` | `symbol` | several candidates -> `AMBIGUOUS_SYMBOL` (exit 3), never auto-picked |
| `svweave query references --build B --symbol S [--scope PREFIX]` | `references` | resolved + unresolved-name matches + virtual-dispatch candidates; `searched_scope`, `resolution_basis`; gaps appear in limitations |
| `svweave query hierarchy --build B [--top T \| --instance I] [--depth N]` | `hierarchy` | pre-order, instance by id or display path |
| `svweave query connections --build B --instance I [--port P]` | `connections` | direct associations only |
| `svweave query assertions --build B [--scope I] [--kind source\|bind\|elaborated]` | `assertions` | |
| `svweave query diagnostics --build B` | `diagnostics` | |

CLI exit codes for the query commands (plan 15.4; `search` is snapshot-bound and not covered):
a `STALE`/`HISTORICAL` build exits **5**, a `PARTIAL`/`BLOCKED` build exits **3**, unless the user
passes `--allow-stale` / `--allow-partial` (BLOCKED always exits 3). The structured result carries
`freshness` / `analysis_coverage` either way; when both apply, 5 wins. MCP tools return the
envelope itself (not a protocol error) with the same fields.

Scope filters (`references --scope`, `assertions --scope`) are exact, case-sensitive prefix
matches with no wildcards (`%`, `_` are literal; `top.u` does not match `top.U`). The CLI prints
pretty JSON only while the printed bytes stay within the 128 KiB cap; otherwise it prints the
compact form the page was sized for.

Read-path freshness is computed once per query and cached per (workspace, build) behind a stat
fingerprint of every input (`semantic_common.input_fingerprint`); any change falls back to the
full hash check, and `freshness_of(..., strict=True)` plus all build/commit paths always do the
full check. Limitation: an edit preserving size, mtime, inode and ctime is undetectable on the
read path only (see `docs/limitations.md`).

Run directories (`.svweave/runs/<job-id>/`) always contain all 13 plan-24.1 files; a stage that
never ran leaves `{"status": "not_available", "reason": ...}` and an aborted job's `result.json`
carries the error.

Location precision: a declaration whose name span does not contain the name bytes (token pasting
such as `r_``n`) has `exact: false` plus `inexact_reason`; consumers (editing) must refuse it.
A port actual that starts or ends inside a macro expansion is reported at its **use site**
(`macro.use_site`, spelling kept in `macro.spelling`); if no consistent use-site range exists the
binding is `supported=false` with no span.

Pagination: `limit` default 100, max 500 (otherwise `INVALID_CONFIG`); the opaque cursor is
`base64url(build, query hash, sort, offset) + HMAC` (key in `.svweave/cursor.key`, mode 0600) and
is rejected for another build, another query, another sort order or a tampered token; responses
are capped at 128 KiB: the page is cut, `truncated=true` and `next_cursor` are set, and
`total_matches` is always present, so an empty or short page is never mistaken for completeness.
A single item above the cap is replaced by a placeholder (`truncated_item`, `reason`,
`size_bytes`, `id`); the ENVELOPE then says so too: `pagination.truncated=true`,
`data.oversize_items`, outcome `PARTIAL`, and `error.code=OUTPUT_LIMIT` when no real item fits.
`svw_symbol` candidates are paged (default 50, `limit`/`cursor`) and class specializations are
bounded to 20 per class (`specialization_count`, `specializations_truncated`).

Build result extras: `data.semantic_deviations` (where the analysis knowingly differs from the
original tree or a vendor flow: snapshot `__FILE__`, external-root exemptions in effect, manifest
language options, declared stubs) is separate from `data.reasons` (why the build is not
COMPLETE). `diagnostic_policy` accepts only `no_errors` (errors make the build PARTIAL; warnings
never block); the former `report_only` value had no effect and was removed.

## 4. Known limitations and capabilities NOT implemented

### Machine-readable reference coverage (HDL-R01; output envelope 1.1)

Build completeness and reference coverage are independent. A successful `COMPLETE` / `OK`
build can have `analysis_coverage.reference_coverage.status=PARTIAL`; positive indexed edges
remain available. This does not change publication or turn every build into a failure.

The reference contract (`contract_version="1"`) exposes:

| Field | Meaning |
|---|---|
| `features` | Elaborated occurrences and aggregate-member type coverage are `PARTIAL`; packed/unpacked dimensions, return-type names and class scope-prefix names are `UNSUPPORTED`. These are capability boundaries, not per-symbol occurrence counts. |
| `scopes` | Elaborated bodies are `PARTIAL`; inactive branches, unused generic bodies, unelaborated definitions, unloaded library members and call-only specialization bodies are `NOT_SEARCHED`. |
| `observations` | Recorded unsupported nodes and dropped references, separately for project/external origins; unresolved/dynamic references; counts by gap/inactive-region kind. Zero observed entries is not an exhaustive census of omitted references. Dependency-inventory library gaps are included before summary generation. |
| `query`, `searched_scope`, `depth_limit` | Reference/impact/rename-preview context, logical path-prefix restriction and bounded impact depth; the existing references `data.searched_scope` remains available. |
| `result_truncated`, `blockers` | Explicit truncation and stable reasons including known omissions, unsearched scopes, unsupported/dropped occurrences, partial/blocked builds, non-current freshness, restricted query scope and preceding cursor pages. A final page does not prove earlier pages empty. |
| `absence_proof_supported` | Always `false` in contract v1. Empty references/impact cannot prove unused, safe-to-delete or complete-rename status, even for a COMPLETE build. A rename candidate still needs the controlled validation/approval/CAS flow. |

Historical reports without valid v1 metadata return `status=UNKNOWN`, `observations=null` and
`coverage_metadata_unavailable`; they are not silently upgraded to complete coverage. Existing
prose limitations, warnings and stale/truncation safeguards remain in place. Non-semantic/error
envelopes can leave `reference_coverage=null`, which is also not proof of absence.

**Client upgrade:** output envelope version is now **1.1**. Strict 1.0 schemas forbid the new
field (even when null); clients must use the updated output schemas. The new reader still accepts
historical 1.0 payloads without the field, which is not a promise that old strict readers accept
1.1. Manifest, config, lock and semantic-store schema versions are unchanged. See
[MCP migration notes](mcp_contract.md#envelope).

Implementation: `adapters/slang/coverage.py`, `ingest.py`, `application/build.py`,
`semantic_common.py`, `queries.py`, `impact.py`, `rename.py` and `domain/envelope.py`.
Requirement mapping: **HDL-COV-01/02** in `tests/unit/test_reference_coverage.py` and
`tests/integration/test_reference_coverage.py`; strict-client migration in
`tests/unit/test_domain.py::test_HDL_COV_01_output_version_requires_strict_schema_upgrade`.
The four integration cases cover dimension/type omissions with retained positive calls,
inactive/unloaded scopes, and project/external unsupported nodes through the real mandatory
worker. All four passed in the 21-case real-worker semantic/UVM subset of
[run 40474142649](https://github.com/DVman9527/SVWeave/actions/runs/40474142649), exact source
`305e8c30747ba2eeb1b53a66eaa29e8255d8e245`, with zero failures/errors/skips. See the
[retained JUnit and evidence mapping](../evidence/verilator-native-run40474142649/README.md).
This verifies the explicit coverage boundary, not extraction of the missing references or
full qualification. Earlier local pure checks and their then-current limits are preserved
in `evidence/reference-coverage-contract/`.

### Extraction boundaries

These are reported as limitations/gaps in the outputs; none is emulated with regular expressions.

* No "find all references" index exists in pyslang (ADR-003). References are the occurrences found
  by visiting elaborated code (packages, classes, instance bodies, class specializations reached
  through types). Not covered: uninstantiated generic class bodies, definitions not elaborated,
  inactive generate/`ifdef branches, library modules never instantiated, and **class
  specializations that are only reached as the static scope of a call**
  (`uvm_config_db#(int)::set` is recorded as a call with its `specialization_path`, but the body of
  that specialization is not walked unless the specialization is also used as a type).
* Not extracted as references: packed/unpacked dimension expressions, subroutine return-type
  names, class names used only as scope prefixes (`Cls::member`), enum/struct member type names,
  `export`/`import` of DPI beyond the declaration, clocking-block signals, bind
  target instance lists (`bind mod : inst_list`), checker instances, primitives, config blocks
  (some encountered unsupported nodes are counted in `unsupported_nodes`; these systematic
  omissions are **not** all guaranteed to increment that inventory).
* Multiple (macro-expanded) origins of one occurrence are reported as candidates; picking one is
  an edit concern (M4).
* `explicit_groups` compilation units use slang `-C` files; groups may not declare library
  inputs (`CAPABILITY_MISMATCH`). Top parameter overrides are passed as `-G <top>.NAME=VALUE`
  (slang 12 top-scoped form, verified against the pinned build); the DefParam symbol slang creates
  for it in its `<command-line>` buffer is not reported as an unsupported node.
* `defparam` in source: the assignment's name is recorded as a `defparam` reference to the overridden
  parameter (slang applies the override during elaboration, so the hierarchy's parameter values
  already reflect it; checked in `tests/integration/test_vcs_compat_defparam.py`). A `defparam` whose
  target slang cannot resolve is still counted in `unsupported_nodes`; no reference is invented.
* `language_options.compat: vcs` passes `--compat vcs` to slang (use-before-declare, enum/string
  conversions, hierarchical const, ... as VCS tolerates them). It is part of `config_id`, listed in
  `semantic_deviations`, and off by default; use a separate profile so strict and relaxed results
  do not mix. It does NOT fix VCS-only constructs slang rejects regardless (`string.match()`,
  covergroup cross/binsof over sample formals or class members, ...): those stay errors.
* `-D NAME` (no value) defines the macro as slang does (value `1`); the manifest keeps the
  `NAME` versus `NAME=` distinction in `config_id`. Snapshot files live under the snapshot
  directory, so `` `__FILE__ `` shows that path (plan 9.4): not bit-identical to the original tree.
* Interface/modport: associations name the interface **instance** and modport; virtual interface
  handles are typed candidates (`virtual_interface: true`) and are never bound to an instance.
* UVM: factory overrides and `config_db` set/get are listed as call sites (callee specialization,
  argument text as written). Whether a `get` matches a `set` is runtime behaviour and is never
  claimed.
* Reading the **disabled** side of an `ifdef` uses slang's token list and a fragment re-parse for
  declarations; a fragment that is not a complete declaration (for example half a module body) is
  listed as a region without declarations.
* Peak worker RSS comes from `wait4` of exactly the child. Measured: demo builds about 50 MiB and
  0.3 s; UVM 2020.3.1 + fixture about 7 s wall (extraction), results about 20 MB of JSON.
* Cancellation (`cancel_job`) works inside the process that started the build (in-memory job
  registry); the cross-process job manager is M3.
* The worker fault-injection hook (`WorkerRequest.test_fault`) is set only by the explicit
  `test_fault=` keyword of `run_build`/`run_worker`; no configuration field maps to it.


### UVM static call metadata (UVM-02 / UVM-03)

Recognized calls additionally expose `context.uvm` through the existing references query:
`api`, `operation`, resolved lexical `declaration`, `specialization_path`, and written
`arguments`. Each argument retains syntax kind, optional source name, expression text,
associated formal name, and any association issue. Named arguments are associated by name,
not source order; ordered and explicitly empty arguments retain their positions. Omitted
arguments are not filled with defaults or evaluated. Existing `context.args` remains the
as-written text list. Calls also expose `argument_count` and `args_truncated`.

The allowlist recognizes `uvm_pkg::uvm_config_db::{set,get,exists,wait_modified}`,
component/factory/default-factory type and instance override APIs, and object/component
registry `create`, `set_type_override`, and `set_inst_override`. Recognition uses the
resolved declaration, not the spelling at the call site; a user method named `set` is not
classified. This namespace allowlist is not authentication of a replacement `uvm_pkg`.

`evidence=static_call_site` and `runtime_resolution=not_computed` are always explicit.
There is no config-db matching, precedence calculation, factory outcome, runtime topology,
or phase execution inference. Only visited, resolved call sites are eligible; this does
not expand generic-body traversal. At most 16 written arguments are captured per call;
`arguments_truncated` and `syntax_available` distinguish incomplete/missing syntax.
Expression text has the existing `max_text_bytes` cap (ellipsis on truncation); each
argument reports `text_truncated` from its byte span (`null` when unavailable, including
empty arguments). List truncation flags do not imply untruncated expression text. Expressions
are never evaluated and literal wildcards remain text.

Regression example: `fixtures/uvm/svw_uvm_static_calls.sv` imports the real pinned UVM
library, with reordered named config-db arguments, type/instance overrides, registry
creates with named and empty arguments, and non-UVM same-name methods. Its gateway test
is `test_UVM_02_03_named_arguments_and_same_name_negative`. Pure formatter tests do not
substitute for running this integration test on a host with mandatory bwrap isolation.
That case, UVM-01/02/03 and the pinned-library smoke profile passed in the exact-source
21-case semantic/UVM subset above; see [semantic JUnit](../evidence/verilator-native-run40474142649/junit-semantic-uvm.xml).
These are real **slang/pyslang** static UVM checks, not native Verilator UVM acceptance,
runtime execution or a complete inventory of all UVM references.

## Optional external static inventory

Verilator 5.052 JSON AST artifacts can be imported and inspected separately from the slang
index. Official regression goldens exercise class/member/method/type and selected reference,
module and port node inventory. This is `EXTERNAL_DECLARED`, `PARTIAL`, unbound to any build;
it does not establish resolved references or close unvisited-body/runtime UVM gaps.
The separate `scripts/verify_verilator_native.py` static lint/JSON verification harness is
explicit opt-in test infrastructure, not a product runtime backend.
[Run 40474142649](../evidence/verilator-native-run40474142649/README.md) at
`305e8c30747ba2eeb1b53a66eaa29e8255d8e245` passed all **five** native checks with zero skips:
HDL-VLT-01/02/05 are three real lint/JSON/import-query positives; HDL-VLT-03/04 are two
specific expected-error rejections, not successful compiles of invalid designs. Native UVM,
simulation, formal and coverage remain `NOT_RUN`.

Initial native run [40471175289](../evidence/verilator-native-run40471175289/README.md)
remains **FAIL**: the legacy fixture exposed macro-expanded columns that do not address
original source tokens. Those locations remain **UNVERIFIED**, never trusted edit bindings;
the new macro-free positive and exact discrepancy check do not change that limitation.
Local execution remains BLOCKED (`NETLINK_ROUTE` permissions). The native result neither
merges references into slang nor completes HDL-R02/R04 or full qualification. The retained
supplement JUnit reports 195 parent tests plus 75 pytest subtest outcomes (270 aggregate),
not 270 named testcases. Subsequent
[run 40490935466](../evidence/dep01-nested-denial-run40490935466/README.md) at `4a7a360`
passed 40 isolation checks (36 pure + 4 native), but DEP-01 failed on an audited nested-sandbox
denial. The historical broad run remains 235 cases / 229 PASS / 6 FAIL at `2d26630`.
Earlier on **2026-10-28 the user selected option 1**: preserve the original
controller-plus-backend isolation guarantee, retain DEP-01 as **BLOCKED**, and continue other
regressions with an explicit blocked ledger. Under that decision, no narrower controller
contract was selected. [Run 40617140613](../evidence/remaining-regressions-run40617140613/README.md)
at `4b6454417ca1f92fae5063116f9458cbfbca443d` now passes **234 selected regressions**, zero
failures/errors/skips; its ledger accounts for 235 collected as 234 selected + 1 DEP-01 BLOCKED,
unwaived. Separate boundary checks pass 40 cases (four overlap the selected suite), and fresh
mutations kill 26/26. The benchmark is only 0.05-scale smoke, with graph search SKIPPED and
reference host/corpus unmet. Native Verilator was not rerun. These results do not expand
HDL/reference coverage or complete full qualification.

At **2026-10-28 03:17:21 UTC**, the user explicitly changed future acceptance to
[DEP-01-R2-host-controller](adr/ADR-016-dep01-trusted-host-controller.md): offline install/verify
and every backend remain isolated, but the trusted host controller has no outer network-isolation
guarantee. No backend/policy relaxation follows. The revised acceptance **passed at
`cc931de898069bed0a51497737d4ad60603e4c51` in [run 40627613557](../evidence/r2-verilator-run40627613557/README.md)**:
236 required cases (including R2), 62 boundary checks, native Verilator 5, slang semantic/UVM
21 and 298 independently named supplement/gate cases all pass with zero failures/errors/skips.
Scopes overlap; fresh mutations kill 26/26, while the 0.05-scale benchmark still skips graph
and misses reference host/corpus. Original DEP-01 FAIL/BLOCKED and option-1 evidence remain
unchanged. This is not new HDL coverage, full qualification, native Verilator UVM acceptance
or verification of later documentation commits.
See [the format, trust boundary and evidence](verilator_static_supplement.md).
