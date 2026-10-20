# Changelog

All notable changes to SVWeave. Format loosely follows Keep a Changelog. Version numbers are
pre-release until the qualification report says `ACCEPTED_STATIC_PREVIEW`.

## [0.1.0-dev] - unreleased (developer preview, UNQUALIFIED)

### Added

* **Graph export** (`svweave graph export`, ADR-014): one self-contained HTML page per build, the
  elaborated instance tree plus connections derived from direct port associations. No server, no
  network (strict CSP), source text embedded as escaped JSON. ADR-009 amended.
* **Impact analysis** (`svweave query impact`, MCP `svw_impact`): what changing a module, port, signal
  or instance touches: instances, dependent and dependency definitions, port connection sites,
  references. A port and its implicit net are one entity.
* **Git diff impact** (`svweave query diff-impact`, MCP `svw_diff_impact`): the old-side lines of
  `git diff -U0` mapped onto the build's snapshot, attributed to declarations and modules; per-file
  `aligned` flag; STALE never changes the exit code.
* **Rename preview** (`svweave query rename-preview`, MCP `svw_rename_preview`): read-only; every edit
  site verified against the snapshot bytes, diff, blockers, an `edit_request` for the controlled edit
  loop. Verified end to end through prepare, slang validation, approval and commit.
* **Offline install package** `vendor/`: the hash-locked PyPI wheels, the `[dev]` wheels, Serena (source
  bundle and dependency cache) and a standalone CPython 3.12, with `SHA256SUMS` and install steps.

### Fixed (Opus whole-repository review, 2026-10-20; each finding verified, each fix has a test that fails without it)

* **Edit loop**: a `` `define `` could still be changed through the newline that ends it (exclusive
  bounds) or when it did not start a line; `exact_range` / `symbol_replacement` text could add
  `` `define `` / `` `undef ``; an abort landing between commit's checks and its lock left an orphan
  journal that blocked every later edit (the state is now re-read under the lock); post-apply
  published each clean profile before the others had passed.
* **Snapshots and config**: an existing snapshot directory was reused without re-hashing (a
  repository-shipped `.svweave/` could substitute bytes) and with another profile's metadata; snapshot
  cleanup chmod-ed symlink targets; project paths under `_ext/` collided with external roots and were
  treated as external (not blocking); a world-writable trusted policy was accepted; redaction missed
  `:`-joined paths, `file://` URLs, quoted secret keys, `Authorization: token ...` and dict keys.
* **Semantic core**: a declared stub whose model has a body came out COMPLETE; two files expanding
  the same macro at the same offset lost references to deduplication; a second profile with
  identical inputs reused the first one's build but was never published and was labelled with the
  first profile's name; a running build absorbed a request with a different `expected_snapshot_id`.
* **Queries and MCP**: `svw_symbol` could exceed the 128 KiB cap; `debug bundle` crashed on builds with
  diagnostics; a graph cursor was applied to the text fallback (skipping results) and the fallback
  minted cursors beyond its own offset limit; schema errors echoed request values; UNKNOWN freshness
  was reported as a workspace change; the `svw_symbol` schema advertised the wrong default page and
  `svw_evidence` accepted fields it ignored.
* **Backends and installer**: a FIFO in a candidate tree hung the gateway and unreadable directories
  were skipped silently; a backend that failed half-way through start-up was not stopped; malformed
  Serena output escaped as a bare exception; `--verify-only` passed without a venv; a changed Serena
  LICENSE was not an error; hardlinks in stripped archives raised an uncaught `KeyError`.
* **Analysis tools**: rename preview missed unresolved uses inside functions, generate blocks and
  included headers (a ready edit could commit and leave the old name behind), did not edit a
  function's own `endfunction : name` label, and treated named argument bindings and parameter
  overrides as warnings; a port picked by `symbol_id` lost its implicit-net twin; a truncated scan
  could still be ready. The graph connected different instances of the same module. Diff impact
  mistook content lines starting with `+++ ` for file headers, could run a repository's git clean
  filter on the working tree (now refused), and diffed beyond the project's subtree. Impact did not
  find the owning module through generate blocks and warned about descendants it had listed.

### Security (installer supply chain, 2026-10-20)

* **Serena's build backend is hash-pinned.** `uv.lock` does not lock build dependencies, so
  `uv sync --frozen` fetched `hatchling` (and, for the sdist-only `proxy-tools` 0.1.0, `setuptools`)
  from the index or an unverified uv cache and ran it. The lock now pins the 8 build wheels
  (`serena_build_wheelhouse`: hatchling 1.32.4, editables 0.6, packaging 26.3, pathspec 1.1.1,
  pluggy 1.6.0, tomlkit 0.15.1, trove-classifiers 2026.9.21.13, setuptools 84.0.0; sha256 and
  files.pythonhosted.org URLs); the installer verifies them, installs them into the Serena venv with
  `--no-index --require-hashes` and syncs with `--no-build-isolation`. `--fetch-only` and `--offline`
  work the same way; the fetch-only cache never touches an index for a build backend.
* **uv is pinned.** The installer downloads the uv 0.12.23 release tarball (lock section `uv`; sha256
  equal to the release's published checksum), verifies it before opening it and the binary before
  running it, and uses it for every uv command (`UV_PYTHON_DOWNLOADS=never`). `--uv PATH` still
  overrides it but is reported as unverified (`components.uv.verified: false`, `warnings`).
  `--verify-only` checks the pinned uv. The installer creates the gateway venv itself
  (`--create-venv`, new `--python` for the base interpreter), so the documented flows no longer start
  with `uv venv` and no uv has to be installed.
* `vendor/`: added the 8 build wheels and the uv tarball; `uv-cache.tar.gz` regenerated with the pinned
  uv (the old one was written by a newer uv's cache format and was built with unpinned backends).
  License texts and entries for uv and the build wheels added (`license_manifest.json`,
  `THIRD_PARTY_NOTICES.md`; pathspec is MPL-2.0). `toolchain.lock.json` changed (new sections, Serena
  `install_method`, `license_manifest_sha256`), so its sha256 changed; `toolchain_id` (pyslang, slang,
  extractor) did not.
* Verified end to end on 2026-10-20 in scratch copies of the repository: the `vendor/` procedure with
  `--offline` inside `bwrap --unshare-net` (no `uv` on `PATH`) and the GitHub-reachable variant both
  install Serena 1.7.0 with `--verify-only` ok; DEP-01 passes with the pinned uv
  (`evidence/dep01/cleanroom.json` refreshed: only `lock_sha256` changed).

### Security (backend output limits, 2026-10-20)

* **A backend can no longer exhaust the gateway's memory through stdout.** The MCP SDK's stdio reader
  concatenated a backend's stdout until a newline with no length limit (a huge line without a newline
  grew memory until the backend was stopped; the call timeout did not stop it), and `call_tool`
  joined result text without a limit. New `svweave.adapters.bounded_stdio.bounded_stdio_client` keeps
  the SDK's transport contract and launch / shutdown helpers and replaces the reader with a bounded
  byte reader. A line above 16 MiB (`MAX_MESSAGE_BYTES`) is `BACKEND_UNAVAILABLE` /
  `reason: message_too_large`; a result text above 16 Mi characters (`MAX_RESULT_TEXT_CHARS`) is
  `OUTPUT_LIMIT` / `reason: result_too_large`. Either way reading stops and the backend's process group
  is terminated before the error is raised. The limits are above cbm's own 10,485,744-byte response cap
  and Serena's 150,000-character answer cap (measured). Tests:
  `tests/unit/test_adapters_mcp_client.py::test_sec_*` (64 MiB flood with a 1 MiB limit: the reader
  peaks at limit + one 64 KiB read and fails in about 0.1 s, the old reader timed out after
  accumulating the flood). See `docs/security.md`, section 6.
* Review fixes (Opus and Codex reviews of the transport): the backend is terminated before the
  overflow is delivered, so a consumer that is not reading cannot keep a flooding backend alive; a
  CRLF line of exactly the limit is accepted; bystander calls on a terminated backend report
  `backend_terminated` instead of another call's `result_too_large`; a flood written outside any call
  is reported by the next call. `scripts/probe_backends.py` now uses the bounded transport too.

### Fixed (installer and Serena sandbox)

* Serena could not start when its venv was built on a python-build-standalone interpreter (the
  documented offline path, `--python .../runtime/python/bin/python3`): the sandbox binds the
  interpreter by realpath (`bin/python3.12`), so the `bin/python3 -> python3.12` link that the venv's
  `bin/python` points to did not exist inside it and every venv script died with
  `/usr/bin/env: '.../serena': No such file or directory`. `SandboxSpec.symlinks` now recreates the
  intermediate links inside the sandbox (`serena.config.python_links`; a link inside a bound path is
  refused, so nothing is created on the host). The docs unpack the bundled CPython to
  `.toolchain/runtime/` so the install stays inside the toolchain directory.
* `scripts/probe_backends.py` / `scripts/spikes/cbm_spike.py`: cbm was always `UNAVAILABLE` ("secure
  daemon endpoint could not be created") in a deep checkout (e.g. this one), because the
  daemon socket below `.toolchain/state/cbm*/runtime` exceeded the ~108-byte `sockaddr_un` limit. The
  probe now uses a short private temp dir, as the gateway already did (`SANDBOX_RUNTIME_DIR`).

### Fixed (continued)

* **Instance ids collided for instance-array elements**: slang names an array element `""`, so two
  arrays in one scope shared ids and the build died with `UNIQUE constraint failed:
  instances.build_id, instances.instance_id` (found on a 115k-instance SoC). Elements now carry the
  array's name.
* **Envelopes could be ~700 KB** on a big build: one limitation line per empty-bodied module (6637).
  New builds name 10 per kind and count the rest; every envelope also caps the list at 60 lines.
* **Codex whole-repository review** (each finding verified before patching):
  `safeio.read_regular_under` follows no link in any component (used for the backend-writable
  candidate tree); the worker's stdout is capped (default: its memory limit) and the worker group is
  killed on overflow; the snapshot size cap is enforced while copying; the include-directory walk is
  bounded in depth and directory count; `git` output is read with a running cap (and a timeout) rather
  than buffered; SQL `LIKE` patterns built from names escape `_` and `%`; a local rename is blocked
  when the declaring scope still has uses slang did not resolve (inactive `ifdef`, macros).
  Identifier validation uses `fullmatch` (a trailing newline passed `$`).

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
* **`language_options.compat: vcs`** (opt-in, default off): passes `--compat vcs` to slang. Part of
  `config_id` and listed in `semantic_deviations`, so relaxed and strict builds never share an
  identity; keep it in a separate profile. Measured on a 10x10 AXI VIP: it only turns 1 of 64 slang
  errors into a warning (`Redefinition`) and removes 3 warnings; VCS-only constructs slang rejects
  regardless (`string.match()`, covergroup cross over sample formals) remain errors by design.
* **Source `defparam` support**: recorded as a `defparam` reference to the overridden parameter
  (slang already elaborates the override); an unresolvable target stays an `unsupported_nodes` entry.
  Removes the `DefParam` PARTIAL reason for designs that use it.

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
