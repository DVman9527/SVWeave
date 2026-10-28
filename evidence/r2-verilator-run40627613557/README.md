# R2 cleanroom and native Verilator validation — run 40627613557

**Verified scope: R2 PASS; 236 required regressions, 62 boundary checks, native Verilator 5,
slang semantic/UVM 21 and supplement 298 named cases PASS. Full qualification remains
BLOCKED / UNQUALIFIED.** These suites overlap; their counts are not an additive unique total.

- [Run 40627613557](https://github.com/DVman9527/SVWeave/actions/runs/40627613557) completed
  **SUCCESS** in all three jobs (fast, real backends, native Verilator).
- Tested source: **`cc931de898069bed0a51497737d4ad60603e4c51`**, not a later documentation commit.
- Lock SHA-256: `ea20baf9bc2da3cb6b9dc41f0234c67f925ea37c5691b0bfbbb1ff30f86ef5e9`.
- Workflow SHA-256: `03447a65ae1a8665936799e8baa372d0cf87bde87607ce06ccf63be282ba8b6a`.
- [Run metadata](run-metadata.json), [artifact metadata](artifacts-metadata.json) and
  [member manifest](artifact-manifest.json) retain original source/run attribution and hashes.

## Required regressions and the narrower DEP-01-R2 contract

[Required JUnit](backend/junit-required.xml) has **236 unique cases: 205 integration,
19 fault-injection, 12 acceptance**, with zero failures/errors/skips. The revised test is
`tests.integration.test_dep01_cleanroom::test_DEP_01_R2_offline_install_then_host_controlled_isolated_backend_probes`.
[Boundary JUnit](backend/junit-probe-isolation.xml) has **62 cases: 57 pure and 5 native**,
all passing; those five native cases also occur in the 236. They include HDL-CI-05 and
`test_DEP_01_R2_controller_timeout_reaps_native_detached_descendant`.
Strict `qualify.parse_junit` accepts both raw files without count normalization.

The [R2 contract](backend/dep01-r2/contract.json) records actual execution from
**2026-10-28 03:38:42.802259 to 03:39:08.612526 UTC**, `status=PASS`,
`ci_identity_verified=true`, identical before/after fixture digests, and:

- `revision=DEP-01-R2-host-controller`;
- `controller_network_isolated=false`;
- `offline_install_verification_network_isolated=true`;
- `backend_isolation=mandatory_bwrap`;
- `original_contract_status=BLOCKED`, `original_contract_reclassified=false`;
- `release_qualified=false`.

This is the explicitly authorized [ADR-016](../../docs/adr/ADR-016-dep01-trusted-host-controller.md)
boundary: offline install/verification remain network-isolated, every native backend still has
mandatory read-only/no-network/no-inherited-secret isolation, but the trusted host controller
has **no outer network-isolation guarantee**. No AppArmor/sysctl/capability relaxation or
backend fallback was introduced. It is not an end-to-end offline/air-gapped controller result.

[Cleanroom summary](backend/dep01-r2/cleanroom-r2.json), [bootstrap](backend/dep01-r2/bootstrap.json),
[host-controller preflight](backend/dep01-r2/controller-sandbox-preflight.json), six raw
`*.execution.json` files and [offline network check](backend/dep01-r2/offline-network-check.stdout.log)
record rc=0 setup/metadata/verify/probe stages and `BLOCKED errno=101` for the offline connection
check. Fresh installed [backend capability report](backend/dep01-r2/capability-report.json)
and the [ordinary probe report](backend/capability-report.json) retain mandatory bwrap and no
required-backend errors. Pyslang/slang CLI are READY; codebase-memory and Serena remain
**DEGRADED** for declared UVM class/task extraction limitations, not degraded isolation.

The [original DEP-01 denial](../dep01-nested-denial-run40490935466/README.md) remains raw
**FAIL / original contract BLOCKED**. The earlier [option-1 run](../remaining-regressions-run40617140613/README.md)
remains 234 selected PASS plus one blocked original DEP-01; neither artifact is rewritten or
used as R2 evidence.

## Fresh native static verification

[Native report](native/report.json), [native JUnit](native/junit.xml),
[bootstrap report](native/bootstrap-report.json) and [clean pre-install identity](native/identity.json)
bind the checks to the tested source and official Verilator **5.052**, pinned commit
`ea338be98e1e838d3518809ce8899f85a009963c`. Bootstrap before/after source pins and binary/wrapper
hashes agree. The later native report's `source_dirty` is only generated
`?? evidence/verilator-native-bootstrap/`, not an unexplained source modification.

- **HDL-VLT-01/02/05: 3 PASS** — actual lint/JSON acceptance followed by real import/store/query.
- **HDL-VLT-03/04: 2 PASS** — specific expected-error rejections, not successful invalid HDL.
- [Real slang semantic/UVM JUnit](native/junit-semantic-uvm.xml): **21 PASS**;
  these are **not native Verilator UVM acceptance**.
- [Supplement/gate JUnit](native/junit-gates-supplement.xml): **298 independently named cases
  PASS**, matching the producer aggregate. All five retained JUnit files have zero
  failures/errors/skips and pass strict accounting; old 195-node/270-aggregate XML remains
  unchanged in its own historical evidence directory.

The three positive raw tree/meta pairs are retained. Other native outputs, including streams,
are member-hashed but not copied. Macro-expanded source columns remain **UNVERIFIED**, not edit
bindings; external inventory remains `EXTERNAL_DECLARED/PARTIAL`, snapshot/build-unbound and
not slang's authoritative references. Initial [run 40471175289](../verilator-native-run40471175289/README.md)
remains FAIL; [run 40474142649](../verilator-native-run40474142649/README.md) remains its separate
bounded historical PASS. Protected preparation end-to-end and native Verilator UVM remain NOT_RUN.

## Fresh mutations and reduced-scale benchmark

[Mutation JSON](backend/mutation-2026-10-28.json) records **26/26 KILLED**, no survivors/errors,
with 26 successful baseline commands. It was freshly executed **03:46:34–03:56:05 UTC**.
The JSON has no embedded source SHA: attribution comes from the exact run/artifact metadata
and [bounded original job-log excerpt](backend-job-log-excerpt.txt), which retains checkout,
mutation execution/outcomes, benchmark and upload lines. It is **not the full job log**;
exact source line numbers, full-source hash and excerpt hash are in the manifest.
The artifact's October 13 mutation JSON is historical carryover, not counted or copied.

[PERF-01](backend/benchmark-s0.05.json) ran **03:56:05–03:56:19 UTC** at **scale 0.05,
4,844 lines / 25 files**, with five fresh builds and 30 samples per hot category.
Measured smoke targets pass; graph search is **SKIPPED (`--no-graph`)**.
The host has four logical CPUs / 15.61 GiB RAM and warm page cache;
`reference_host_met=false`, `reference_corpus=false`. This is not the plan's
100k-line/500-file or graph-performance qualification. No leaked backend process was reported.

## Retention and remaining gates

The manifest inventories **37 backend + 79 native artifact members**. It retains **21 backend
+ 12 native raw members**, two raw API metadata files and one exact bounded log excerpt;
nonretained members are explicitly `copied=false`, with source artifact links and hashes.
Raw bytes/counts are never normalized. This evidence does not assert that later documentation
commits were natively rerun.

`qualify.py` did not execute a new full release qualification; reading its strict JUnit parser
is not running its gates. Owner G0 waiver / ADR-012 / license sign-off and reference-scale
performance remain open. Simulation, formal and RTL coverage remain **NOT_RUN**. No full-language,
Slurm, native Verilator UVM, or release qualification is claimed.
