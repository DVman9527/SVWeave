# Shared installation and workspace binding — run 40806959759

[Run 40806959759](https://github.com/DVman9527/SVWeave/actions/runs/40806959759) completed
**SUCCESS** in all three jobs on 2026-10-29. Tested source is exactly
**`ca6ebb571137f683a5354a1bd78169341d933de1`**, not a later documentation/evidence commit.
This is bounded installation, gateway and native-backend validation, **not full qualification**.

- Lock SHA-256: `ea20baf9bc2da3cb6b9dc41f0234c67f925ea37c5691b0bfbbb1ff30f86ef5e9`.
- Source-tree SHA-256: `a415e7e7754c07f87688a816d8145dde440e0f5404baf6289f5a31b7308b415a`.
- Workflow SHA-256: `3e1cf4f8ced4a344f60659f33f628a8c9dfa71f441b6cbb0a8a681025699ca9f`.
- [Run metadata](run-metadata.json), [artifact metadata](artifacts-metadata.json),
  [byte/hash manifest](artifact-manifest.json) and [derived audit](summary.json) preserve linkage.
  [SHA256SUMS](SHA256SUMS) covers all retained files except itself.

## Results and raw proofs

**These scopes overlap; do not add their counts into a unique-test total.**

| Scope | Result | Proof |
|---|---|---|
| Real wheel/sdist packaging checks | 10 PASS, 0 skipped | [Fast job log](fast-job.log), exact `SVWEAVE_REQUIRE_PACKAGE_BUILD=1` test block |
| Detached installed-wheel smoke | PASS; both console entry points, bundled assets, hostile-cwd rejection, shared-toolchain override | [Producer report](packaging/wheel-smoke.json); native backends NOT_RUN by this smoke |
| Unit suite | 1552 PASS, **1 SKIP**, no failure/error | [Fast job log](fast-job.log); quiet output does not identify the skip reason |
| MCP/CLI contracts | 49 PASS, 0 skipped, including GLB-09's two actual SDK stdio gateway processes | [Fast job log](fast-job.log); not an actual Claude/Codex application connection |
| Required integration / fault-injection / acceptance | 236 PASS = 205 + 19 + 12, zero failure/error/skip | [Required JUnit](backend/junit-required.xml) |
| Sandbox/controller boundaries | 62 PASS = 57 unit + 5 integration, zero failure/error/skip | [Boundary JUnit](backend/junit-probe-isolation.xml); the five integration cases overlap the 236 |
| Native Verilator 5.052 static harness | 5 PASS: 3 lint/JSON/import-query positives, 2 expected-error rejections | [Native report](native/report.json), [JUnit](native/junit.xml), [bootstrap report](native/bootstrap-report.json) |
| Real slang semantic/UVM / supplement gates | 21 / 298 PASS respectively, zero failure/error/skip | [Semantic JUnit](native/junit-semantic-uvm.xml), [supplement JUnit](native/junit-gates-supplement.xml) |
| Fresh critical mutations | **27/27 KILLED**, no survivor/error | [2026-10-29 mutation report](backend/mutation-2026-10-29.json), [backend job log](backend-job.log) |
| Performance smoke | All measured targets pass at scale 0.05, 4,844 lines / 25 files; graph search **SKIPPED** | [Benchmark report](backend/benchmark-smoke.json); not reference-scale qualification |

Fast counts are independently derived from pytest's raw quiet progress markers in each exact
command block; no fast-job JUnit was uploaded. The single unit skip is retained, not counted as
PASS or assigned an unrecorded reason. Ruff, mypy and schema synchronization also pass in that
log. All five retained JUnit files pass strict `scripts/qualify.py::parse_junit` accounting with
unique case names and unchanged producer totals.

The mutation report contains 27 successful baseline commands and 27 rejected mutants, including
M27's three baseline passes → three mutant failures for canonical-directory, manifest-symlink
and event-log binding guards. Its import proof identifies the throwaway source copy. The JSON
itself has no source SHA: attribution comes from run/artifact metadata, backend identity and the
raw backend job log, which includes checkout, execution and upload. The artifact's older
`evidence/mutation/2026-10-13.json` is historical carryover and is deliberately not copied here.

## Safety and evidence boundaries retained

The current [R2 contract](backend/dep01-r2/contract.json),
[cleanroom summary](backend/dep01-r2/cleanroom-r2.json) and
[controller preflight](backend/dep01-r2/controller-sandbox-preflight.json) retain
`DEP-01-R2-host-controller`: offline install/verification are network-isolated; the trusted
controller has **no outer network-isolation guarantee**; each backend still requires mandatory
read-only/no-network/no-inherited-secret bwrap isolation. Original DEP-01 remains BLOCKED and
is not reclassified; `release_qualified=false`. The six execution records retain rc=0 stages;
the [offline network check](backend/dep01-r2/offline-network-check.stdout.log) reports the
expected blocked connection. See [ADR-016](../../docs/adr/ADR-016-dep01-trusted-host-controller.md).

The [ordinary probe](backend/capability-report.json) reports pyslang/slang CLI READY and
codebase-memory/Serena **DEGRADED** for known semantic omissions, not degraded isolation.
[Backend identity](backend/identity.json) and [native identity](native/identity.json) both
record the tested clean source. Native bootstrap pins and hashes agree with the native report;
its later dirty status is generated evidence, not a source change. Native per-case ASTs/streams
are not duplicated in this compact bundle; they remain in the original
[native artifact](https://github.com/DVman9527/SVWeave/actions/runs/40806959759/artifacts/12869061314).
The report's per-case paths refer to that full artifact.

**Still developer preview / UNQUALIFIED.** No actual Claude/Codex application registration or
connection was performed; client configuration was not modified. Slurm site execution is
untested, and the older external `svweave-slurm` wrapper's source was not provided, so wrapper
compatibility is not established. These tests do not change those claims.

Full `scripts/qualify.py` release evaluation, simulation/formal/RTL coverage, protected-source
end-to-end and native Verilator UVM acceptance remain NOT_RUN. The 21 slang cases are not
native Verilator UVM acceptance. Benchmark page cache was warm, reference host/corpus were not
met, and graph search was skipped; its measured smoke passes do not establish 100k-line
performance. Owner/license gates remain open. Earlier runs and failures remain unchanged in
their original evidence directories.
