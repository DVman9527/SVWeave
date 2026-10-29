# Snapshot I/O update — exact-source validation

[Run 40818546417](https://github.com/DVman9527/SVWeave/actions/runs/40818546417) completed all
three jobs **SUCCESS** on 2026-10-29 at **`9f86f35930f22d3e817b1be95324f171b9163009`**.
This is the tested source, not the later documentation/evidence-only commit.

**Correctness checks passed. The user's end-to-end performance goal was not demonstrated.**
Phase 1 is CLI-only opt-in snapshot I/O reuse, not partial semantic compilation or graph indexing.
Ordinary `build` and MCP `svw_build` retain their default paths. Full qualification is **UNQUALIFIED**.

- Source-tree SHA-256: `2eaedd085f82fb2ed45cba72cf3afdb17fa5b7516fad6ff982cb766e6a9da31c`.
- Lock SHA-256: `ea20baf9bc2da3cb6b9dc41f0234c67f925ea37c5691b0bfbbb1ff30f86ef5e9`.
- Workflow SHA-256: `98a9f021e9ee8993dd4628dcb411a0448d8b71443f67c24565d7ae7ceef28bb4`.
- [Run metadata](run-metadata.json), [artifact metadata](artifacts-metadata.json),
  [original-byte manifest](artifact-manifest.json), [derived audit](summary.json) and
  [SHA256SUMS](SHA256SUMS) preserve source/artifact linkage. Checksums cover every retained file
  except SHA256SUMS itself. Copied raw files are unmodified; summary/README are derived.

## Test results

**Scopes overlap; do not add these counts into a unique-test total.**

| Scope | Result | Raw proof |
|---|---|---|
| Unit suite | **1680 PASS + 1 SKIP**, no failure/error | [Fast log](fast-job.log); exact command block's quiet progress markers, not inferred from job success |
| MCP contracts | **49 PASS**, no failure/error/skip | [Fast log](fast-job.log); not a real Claude/Codex application connection |
| Required integration / fault-injection / acceptance | **238 PASS**, no failure/error/skip | [JUnit](backend/junit-required.xml), including both incremental-update integration cases |
| Sandbox/controller boundaries | **62 PASS**, no failure/error/skip | [JUnit](backend/junit-probe-isolation.xml) |
| Native Verilator 5.052 static harness | **5 PASS** (three positive cases, two expected-error rejections) | [Report](native/report.json), [JUnit](native/junit.xml), [bootstrap report](native/bootstrap-report.json) |
| Real slang semantic/UVM / supplement gates | **21 / 298 PASS**, no failure/error/skip | [Semantic JUnit](native/junit-semantic-uvm.xml), [supplement JUnit](native/junit-gates-supplement.xml) |
| Critical mutations | **29/29 KILLED**, no survivors/errors | [Mutation report](backend/mutation-2026-10-29.json), [backend log](backend-job.log); 29 successful baselines, including M28/M29 |

Fast-job JUnit was not uploaded; the derived audit counts the raw command-specific pytest markers.
The unit skip's reason is not identified by that quiet output and is not guessed or counted as PASS.
All five current JUnit files retain their producer totals and case records. Mutation attribution
uses run/artifact metadata, [backend identity](backend/identity.json) and the raw backend log:
the mutation report itself does not carry a source SHA. Older 27/27 evidence is unchanged.

The real integration oracle compares full-tree identities/bytes, semantic rows and queries for
no-op, source/header edits and lookup additions/shadowing/deletions, and checks historical results
and failed publication.
See [ADR-018](../../docs/adr/ADR-018-snapshot-io-update.md) for coverage anchors and limitations.

## Performance: no measured acceleration

[Real-worker paired report](backend/incremental-build.json): one `bench_top` / `bench` profile,
**500 generated source files / 100,374 lines**, two edited pairs with alternating order and
independent full/update workspaces. Timed update uses `--no-git`, so these observations exclude
Git-advisory overhead; they do not time the default Git-enabled update command. Pinned slang
runs under mandatory bwrap; no graph or Serena.
AMD EPYC 7763 host, four logical CPUs, 15.61 GiB RAM, ext4, warm page cache (not dropped).

| Median of two edited pairs | Full build | Update |
|---|---:|---:|
| Instrumented CLI end-to-end | 19.086518 s | 19.277576 s |
| Capture | 0.926423 s | 0.995548 s |
| Worker wall | 7.258 s | 7.331 s |

Update was about **1% slower end-to-end**, not faster. Two pairs are bounded observations, not a
stable performance estimate or PERF-01 qualification. Timed CLI includes Python startup and
capture-hook imports; the remainder is not attributed to SQLite or any single stage. Full/update
snapshot and semantic-table hashes match in both pairs; untimed forced reruns also verify the
stored results. [Per-pair raw evidence](backend/incremental-build/) retains CLI envelopes, requests,
dependencies, resource/timing reports, events and artifact digests. Baseline CLI/timing/resource
records are retained; bulky runtime databases and source snapshots are not. The producer records
`runtime_workspace_removed: true`.

[Snapshot-only report](snapshot/incremental-snapshot.json): **1,000 source files / 200,936 lines**,
one top/profile, five pairs per case (15 total); ext4, four logical CPUs, warm page cache. All
independent snapshot correctness checks PASS, but median paired update/full ratios were:

| Case | Update/full ratio | Observation |
|---|---:|---|
| No-op | 1.024460 | update 2.4% slower |
| One-file edit | 1.089361 | update 8.9% slower |
| Broader edit | 1.082623 | update 8.3% slower |

The report already contains every raw sample and pair, so its duplicate JSONL is omitted.
No-op avoids materialization but still hashes current inputs and verifies cached bytes. Changed
captures report `reflink_supported: false`, errno 95, zero cloned files and copy fallback; `delta`
does not mean only changed files were copied. Counts are logical I/O, not physical disk writes
or allocated-space savings. The separate [0.05-scale smoke](backend/benchmark-smoke.json) is not
these paired experiments or reference qualification; graph search is SKIPPED there.

## Earlier failure remains a failure

[Run 40816336648](https://github.com/DVman9527/SVWeave/actions/runs/40816336648) at
`c87a64e07de665265e4978cc244f4c6b849e172b` had **237 PASS, 1 FAIL, 0 errors/skips** in the required
suite. Its [raw failing JUnit](previous-run40816336648/junit-required.xml) and
[identity](previous-run40816336648/identity.json) are retained, not relabelled PASS.

The failed full/update oracle compared an otherwise identical `DriverMessage` warning containing
different absolute snapshot roots for the fixture's empty `include/preferred` directory. Both
capture paths materialize files, not empty lookup directories. The tested revision changes only
the comparison helper/test coverage (plus the documentation mutation count), **not product code**:
it relocates only that exact, location-free warning when the snapshot inventory proves the
expected empty include directory. Warning/code/severity, snapshot/logical path and all other
metadata remain compared; arbitrary messages, errors and real semantic differences are not
normalized away. Negative unit cases constrain this test-only rule. The product source-tree
hash is identical between runs; this is an oracle correction, not a product fix or suppressed
compiler diagnostic.

## Boundaries and retention

Native results are static lint/JSON/import-query tests, not simulation, formal, RTL coverage,
signoff or native Verilator UVM acceptance. Those fields remain NOT_RUN in the native report.
The 21 slang cases do not replace native Verilator UVM acceptance. `qualify.py` was not run;
owner/license gates remain open, graph/Serena retain their known DEGRADED semantic scope, and
the DEP-01-R2 trusted-host-controller boundary is unchanged.

No real Claude/Codex application registration/connection or Slurm site execution was qualified;
the external `svweave-slurm` wrapper source was not supplied. Upgrading shared MCP alone does
not enable the update path. Existing source editing, approval/CAS, immutable rows/cursors and
mandatory sandbox rules are not relaxed.

This compact bundle omits native ASTs/streams, generated source trees, runtime databases and
duplicate sample/log copies. Native report paths refer to the original
[native artifact](https://github.com/DVman9527/SVWeave/actions/runs/40818546417/artifacts/12874050372);
the full [backend artifact](https://github.com/DVman9527/SVWeave/actions/runs/40818546417/artifacts/12874408089)
retains remaining producer files. The native report's dirty status is generated evidence, not
a product-source change; [native identity](native/identity.json) records the tested clean checkout.
