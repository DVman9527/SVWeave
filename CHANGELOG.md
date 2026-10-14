# Changelog

All notable changes to SVWeave. Format loosely follows Keep a Changelog. Version numbers are
pre-release until the qualification report says `ACCEPTED_STATIC_PREVIEW`.

## [0.1.0-dev] - unreleased (developer preview, UNQUALIFIED)

### Fixed

* **Installer usability** (found by installing as a new user): `scripts/bootstrap_locked.py` no longer
  defaults `--uv` to a `.venv/bin/uv` that `uv venv` never creates; it uses `.venv/bin/uv` if present, else
  `uv` on `PATH`, and stops before downloading when there is none. `--fetch-only` now also materializes
  the pinned Serena checkout and warms the uv cache (the documentation already claimed this), so an
  offline host needs no `--serena-source`. `--offline` links the svweave source tree into the gateway
  venv (`.pth` plus `bin/svweave`) because the hash-locked wheelhouse has no build backend. A bad
  `--serena-source` now says so instead of `exit status 128`.
* **Effective error severity**: a slang diagnostic reported at error severity through a warning option
  (for example `format-too-many-args`) had `is_error: false`, was not counted in `errors` and could leave
  a build `NO_ERRORS`. `is_error` now follows the effective severity, like slang's own error count.
  Builds with such diagnostics become `HAS_ERRORS`/`PARTIAL`.
* **Resource figures were the launcher's**: `resource_usage.peak_rss_kib`, `user_cpu_s` and `sys_cpu_s`
  came from `wait4` on the bwrap launcher (about 60 MB and 0 s for every build). On a build whose worker
  peaked near 3.1 GB and ran 136 s they said 62 MB and 0.002 s, which misleads anyone tuning
  `analysis.max_worker_memory_mib`. The worker now reports its own `getrusage` on its last stderr line and
  the runner uses it (`resource_usage.rusage_source` is `worker`; `launcher`, a lower bound, when the
  worker was killed before reporting).
* **Policy errors say what to change**: errors about a root outside the trusted policy now carry a `hint`
  (edit `allowed_roots` in the trusted `policy.json`), and `not inside any declared source root` points
  at declaring a root or using an external `@id` root.
* **Installer-independent pyslang pin**: `record_sha256` in `toolchain.lock.json` was the hash of the
  raw dist-info `RECORD`, which differs between pip, uv and hosts (`INSTALLER`, `REQUESTED`,
  `uv_cache.json`, `direct_url.json`, compiled `.pyc` entries), so a correct install failed with
  `CAPABILITY_MISMATCH: installed pyslang RECORD differs from the lock`. It is now the hash of a
  canonical RECORD (`canonical_record_sha256`: installer-added entries removed, lines sorted); the
  wheel's own RECORD, a pip install and a uv install all give `52a97ff1...`. Every installed file is
  still verified against its RECORD hash. **`toolchain_id` (and therefore `build_id`) values change**
  versus builds made with the previous algorithm; rebuild instead of reusing old builds.

### Added

* **Profile `ignored_options`**: declare simulator run-time options (for example `+notimingcheck`) that
  do not affect elaboration. Previously any unknown filelist option blocked the build and the only
  workaround was to edit a copy of the filelist. Zero-argument options only (an argument would swallow
  the next token); recorded in the `ignored_options` ledger as `profile_declared` and included in
  `config_id` when present; options that change what is analysed (`-f -F -v -y`, `+incdir+ +define+
  +libext+`, `-D -I -G`, any `--long` slang option) cannot be declared.
* The build verdict now links `ParameterDoesNotExist` errors on encrypted (`pragma protect`) modules to
  the envelope instead of presenting them as plain design errors. The envelope must lie inside the
  module's own `full` source span and the module name must be unique; otherwise nothing is attributed.
  The line only adds text to `reasons`, it never changes completeness or the diagnostics. An envelope
  that comes from an `include`d file or a macro is not attributed (documented limitation).
* `svweave.local.example.yaml` and a "Using a project that lives elsewhere" section in
  `docs/manifest.md` (external roots, trusted-policy `allowed_roots`, `@id` cwd, filelist strictness,
  vendor UVM).
* **M0 toolchain pinning**: `toolchain.lock.json` (pyslang 12.0.0, slang v12.0, codebase-memory-mcp
  v0.11.0 portable, Serena v1.7.0 + Verible v0.0-4296, MCP SDK 2.3.0, UVM 2020.3.1) with SHA-256
  for every artifact, `scripts/bootstrap_locked.py` (install and `--verify-only`),
  `scripts/probe_backends.py` and `capability_report.json`, `license_manifest.json`,
  `THIRD_PARTY_NOTICES.md`; ADR-001 to ADR-012; spike evidence in `evidence/m0/`.
* **M1 manifest and snapshots**: pure-data `svweave.yaml` and trusted `policy.json` outside the
  repository, filelist dialect `svweave-filelist-v1`, argument ledger, root allowlist and path
  policy, immutable content-addressed snapshots with header/lookup inventory and freshness checks.
* **M2 semantic core**: pyslang worker in its own process; declarations, references, hierarchy
  with generate scopes, direct port associations, SVA inventory, diagnostics, inactive regions;
  COMPLETE/PARTIAL/BLOCKED verdicts; SQLite semantic store; hand-written goldens.
* **M3 backends and gateway**: sandboxed codebase-memory-mcp and Serena adapters (bwrap, no
  network), backend registry with probe-based health, text-search fallback marked as such, ten MCP
  tools over stdio, common envelope, signed pagination cursors, evidence import, `svweave report`.
* **M4 controlled edits**: candidate copy, Serena function-body replacement (ADR-012) and labelled
  exact-range patches, slang validation of baseline and candidate, human approval bound to the
  patch hash (HMAC), compare-and-set commit, write-ahead journal, crash recovery, post-apply
  build; edits disabled by default.
* **M5 qualification tooling** (this milestone, in progress): synthetic corpus generator
  (`scripts/gen_corpus.py`), benchmark (`scripts/benchmark.py`, PERF-01), critical mutation suite
  (`scripts/mutation_suite.py`, plan 20.5), qualification runner (`scripts/qualify.py`) with the
  plan 28.2 report, end-to-end acceptance tests (`tests/acceptance/`), CI workflow, and the
  documents `README.md`, `AGENTS.md`, `docs/limitations.md`, `docs/security.md`, `docs/install.md`,
  `docs/qualification_report_template.md`.
* **Name-based task / function / constructor edits** (ADR-013): edit kind `slang_symbol` locates the
  declaration by qualified name or `symbol_id` in the slang semantic database and runs the normal
  exact-range CAS / approval path (`edit_path: "slang_symbol"`, never counted as a Serena edit);
  validation rejects a candidate that changes declarations outside the target
  (`slang_symbol_scope_escape`). Mutation suite extended to 22 mutations (M14 to M22).
* Replacement text with terminal control characters, DEL or bidi overrides is now refused at
  planning for all edit kinds (`POLICY_DENIED` / `control_character_in_edit`) instead of later as
  `candidate_rejected`.

### Fixed

* `freshness_of()` no longer calls the published build HISTORICAL as soon as a newer build row of
  the profile exists. The decision follows the published pointer (plan 9.6): the published build
  stays `STALE` (inputs changed) while a newer build is stored but unpublished (PARTIAL, failed,
  not promoted); only a build that is no longer the published one is `HISTORICAL`. Edit/commit
  gates still require `CURRENT` and are unchanged. (`stage_build` inserts a row and moves the
  pointer in one transaction, so API-PAR-02 could not hit this; it is covered by unit tests.)
* `svw_status` read the published pointer, the build rows and each freshness at different
  instants, so a build publishing concurrently could yield a `current_build` reported HISTORICAL
  or a `latest_build` that disagreed with it (seen once in API-PAR-02). The whole view now runs in
  one deferred SQLite read transaction (one WAL snapshot).
* Test measurement: API-PAR overlap intervals get a 2 ms margin (event `ts` is wall clock
  truncated to 1 ms, `duration_s` is monotonic), the threshold is 4, and the lock mutation now
  turns all three tests red.

### Known issues and limits

See `docs/limitations.md`. Highlights: graph extraction of SystemVerilog is partial (DEGRADED);
Serena edits only `function` declarations; approval is forgeable by a same-UID agent with an
unrestricted shell, so editing is off by default; no EDA runner (EDA compile, simulation and
formal are `NOT_RUN`); no incremental build; Linux only. Benchmark findings are recorded in
`evidence/bench/`.

### License

SVWeave's own license: TBD by owner. No license is granted by this repository yet.
