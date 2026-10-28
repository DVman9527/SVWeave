# Remaining regressions passed; DEP-01 remains BLOCKED — 2026-10-28

**234 selected tests PASS, 0 failures/errors/skips. DEP-01 remains explicitly BLOCKED and
unwaived; this is not full qualification.** The user-selected option 1 preserves the original
controller-plus-backend isolation guarantee. No controller topology or AppArmor relaxation
is established by this result.

## Identity and scope

[Run 40617140613](https://github.com/DVman9527/SVWeave/actions/runs/40617140613) completed
successfully, including fast CI and the remaining-regressions job, at exact source
**`4b6454417ca1f92fae5063116f9458cbfbca443d`**. These retained files come from its
[`svweave-evidence` artifact](https://github.com/DVman9527/SVWeave/actions/runs/40617140613/artifacts/12785928465).
[Raw run metadata](run-metadata.json) confirms the run ID, exact SHA and completed/success status;
the [bounded job-log excerpt](job-log-excerpt.txt) retains the checkout SHA and actual commands.

[identity.json](identity.json) records clean checkout status, workflow/source-tree hashes,
Python 3.12.15 and Linux x86_64/glibc 2.39. Lock SHA-256 remains
`ea20baf9bc2da3cb6b9dc41f0234c67f925ea37c5691b0bfbbb1ff30f86ef5e9`.
The [scope ledger](remaining-regressions-scope.json) embeds that identical identity and records:

- Collection **VALIDATED: 235 collected → 234 selected + 1 blocked**.
- The sole excluded node is
  `tests/integration/test_dep01_cleanroom.py::test_DEP_01_offline_bootstrap_into_empty_dir_then_probe_all_three_backends`.
- Its status is **BLOCKED**, `acceptance_unchanged=true`, `waived=false`, with the exact
  [earlier denial evidence](../dep01-nested-denial-run40490935466/README.md) linked.
- `full_qualification_status=BLOCKED`, `qualify_py_executed=false`, `release_qualified=false`.

This is an explicit scope exclusion, not a pytest skip, a DEP-01 pass or a silently waived gate.
The ledger and JUnit must be read together; zero skips among selected tests does not erase the
one blocked requirement outside that selection.

## Verified results

| Scope | Measured result | Raw proof |
|---|---|---|
| Remaining integration / fault-injection / acceptance | **234 PASS**: 203 integration, 19 fault-injection, 12 acceptance; zero failures/errors/skips and 234 unique testcase identities. | [junit-remaining.xml](junit-remaining.xml). |
| HDL-CI-04/05 boundary regression | **40 PASS**: 36 pure guards + 4 real native boundary cases; zero failures/errors/skips. The four native cases also appear in the 234-test suite, so these are not 274 distinct tests. | [junit-probe-isolation.xml](junit-probe-isolation.xml). |
| Real backend contract probe | `mandatory_bwrap`; required backend errors empty. pyslang/slang CLI **READY**, graph/Serena **DEGRADED**, not fully READY. Known graph UVM class/parse gaps and Serena task lookup limitations remain reported. | [capability-report.json](capability-report.json), generated `2026-10-28T01:49:33.737981+00:00`. |
| Critical mutations, M01–M26 | **26/26 KILLED**, no survivors/errors; all 26 baseline commands returned 0. The report proves the injected faults were detected, not that invalid designs passed. | [mutation-2026-10-28.json](mutation-2026-10-28.json), timestamp `2026-10-28T02:03:27+00:00`, with scratch-copy import proof. |
| PERF-01 smoke | Scale **0.05**, **4,844 lines / 25 files**; five fresh-state semantic builds succeeded, 30 samples per hot query/search category. All **measured smoke** targets passed; graph search is **SKIPPED** because of `--no-graph`. | [benchmark-s0.05.json](benchmark-s0.05.json), `02:03:27–02:03:38 UTC`, exact source/lock identity. |

The benchmark host had four logical CPUs, 15.61 GiB total RAM and **warm page cache**. It records
`reference_host_met=false` and `reference_corpus=false`; these measurements do **not** establish
the plan 22.3 100,000-line / 500-file target, graph performance, other-host/project performance
or token savings. Benchmark graph SKIPPED is separate from the selected suites' zero test skips.

The previously failing non-DEP cases—API-PAR-02, DIF-I01/02/03 and OPI-F4/F10—are present and
pass in the new JUnit. The old [235-case run at `2d26630`](../dep01-nested-denial-run40490935466/historical-run40485644533/junit-required.xml)
still records 229 PASS / 6 FAIL; it is not rewritten. The original DEP-01 focused failure also
remains a failure, with its requirement now carried forward as BLOCKED under the unchanged
acceptance contract.

## Freshness and retained artifacts

[artifact-manifest.json](artifact-manifest.json) hashes all **13** downloaded members and
identifies the **7** raw files retained byte-identically here. The other members are explicitly
not copied. In particular, `evidence/mutation/2026-10-13.json` is **historical carryover** and
is neither retained as new evidence nor added to the 26 current mutation results.

Freshness checks match CI identity to the scope ledger and exact checkout's lock/workflow
hashes; the benchmark independently embeds the same source and lock. The retained job log
records actual mutation execution from **01:56:11 to 02:03:27 UTC**, its **26/26 killed**
summary and dated output path, followed by the benchmark command/output and artifact upload.
Run metadata and that excerpt are retained separately from the seven artifact members;
their hashes, original log-line positions and full source-log hash are also in the manifest.
Mutation JSON has no separate commit field: attribution uses this run/artifact/job-log linkage,
not a claim that the mutation file independently authenticates its source. Raw counts, fields
and timestamps are not normalized or rewritten.

## What remains unqualified

- **DEP-01 remains BLOCKED**, with original outer controller and inner backend isolation
  required; no narrowed controller contract, capability grant or policy relaxation was selected.
- `scripts/qualify.py` was **not executed** and no full qualification/release acceptance is
  supplied. Owner waiver/ADR-012/license decisions and full reference-scale performance remain
  separate outstanding requirements.
- Native Verilator was **not rerun**. Its earlier [5-case native PASS and 21-case slang
  semantic/UVM subset](../verilator-native-run40474142649/README.md) belong to `305e8c3`, not
  this source. This new suite includes real slang regressions but is not native Verilator UVM.
- Macro-expanded Verilator columns remain **UNVERIFIED**; imported inventory is not merged
  into authoritative slang references or promoted to complete/edit-safe evidence.
- Simulation, formal and RTL coverage remain **NOT_RUN**. Software mutation results and
  analysis-performance timings are not EDA execution or signoff.

This curation only reads completed evidence and updates documentation. It does not execute
backends, rerun CI, change security policy, commit or push.

## Subsequent acceptance change, not a new execution result

At **2026-10-28 03:17:21 UTC**, the user explicitly selected option 2 and requested rule updates.
[DEP-01-R2-host-controller / ADR-016](../../docs/adr/ADR-016-dep01-trusted-host-controller.md)
retains isolated offline install/verify and mandatory read-only/no-network/no-inherited-secret
backend sandboxes, but the trusted controller runs on the host with no outer network-isolation
guarantee. **R2 is NOT_RUN pending new exact-source CI**. This option-1 run, its unwaived BLOCKED
ledger and all raw proofs remain unchanged; no selected-scope result is reused as R2 acceptance.
