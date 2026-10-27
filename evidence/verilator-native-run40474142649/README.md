# Exact-source targeted static validation — 2026-10-27

**Native static subset PASS; real slang semantic/UVM subset PASS; full qualification
remains BLOCKED / UNQUALIFIED.** These are bounded results, not full-language or EDA signoff.
This directory was reconstructed after workspace recovery from the freshly downloaded original
CI artifact. Its raw files are checked against that artifact, not claimed identical to a lost
local workspace or an unavailable document copy.

- [Run 40474142649](https://github.com/DVman9527/SVWeave/actions/runs/40474142649)
  completed successfully on `305e8c30747ba2eeb1b53a66eaa29e8255d8e245`.
- [Original artifact, `svweave-native-verilator-evidence`](https://github.com/DVman9527/SVWeave/actions/runs/40474142649/artifacts/12720067577).
- Lock SHA-256: `ea20baf9bc2da3cb6b9dc41f0234c67f925ea37c5691b0bfbbb1ff30f86ef5e9`.
  Mandatory backend pins are unchanged.
- [identity.json](identity.json) records clean pre-install Git status, source-tree/workflow
  hashes, Python 3.12.15 and Linux x86_64/glibc 2.39. The later [native report](native-report.json)
  truthfully records `source_dirty="?? evidence/verilator-native-bootstrap/"`: generated
  bootstrap evidence existed by then. Neither field is rewritten to hide the distinction.
- Verilator 5.052 source: `ea338be98e1e838d3518809ce8899f85a009963c`.
  [bootstrap-report.json](bootstrap-report.json) records verified commit/tree/archive hashes
  before and after the network-disabled sandbox build. Native commands use mandatory bwrap
  (0.9.0 on this host), not degraded fallback.

## Results and requirement mapping

| Requirement / test IDs | Measured result | Exact evidence |
|---|---|---|
| HDL-VLT-01/02/05 | Three valid fixtures accepted by both real lint and real JSON modes, then their exact emitted bytes passed importer/storage/query checks. Fixtures cover `.v/.vh` 1364-2005 macro/parameter/hierarchy, 1800-2017 typedef/class, and macro-free 1364-2005 hierarchy. | [native-report.json](native-report.json), [native-junit.xml](native-junit.xml), [fixture expectations](../../fixtures/verilator/native/README.md). |
| HDL-VLT-03/04 | Missing module and syntax-error fixtures rejected in both modes with the specific expected errors; no invalid artifact imported. Negative PASS does **not** mean invalid designs compiled. | Same native report/JUnit: **5 cases total, 0 failures/errors/skips**. |
| HDL-R01 / HDL-COV-01/02 | Four real mandatory-worker coverage cases passed: COMPLETE does not certify absent dimension/type references; positive call retained; inactive/unloaded scopes explicit; project/external unsupported origins distinct. | [junit-semantic-uvm.xml](junit-semantic-uvm.xml), `tests/integration/test_reference_coverage.py`. |
| SEM-01–10, class-member inventory, repository profiles, UVM-01/02/03 | Remaining 17 cases passed, including `test_UVM_02_03_named_arguments_and_same_name_negative` and pinned-library smoke. These are **slang/pyslang UVM static regressions, not native Verilator UVM acceptance**. | Same semantic JUnit: **21 cases total, 0 failures/errors/skips**, across `test_reference_coverage.py`, `test_sem.py`, `test_repo_profiles.py`, `test_uvm.py`. |
| HDL-CI-01/02/03, SEC-05, HDL-COV-01/02 and supplement/metadata/ingest/impact/rename guards | **195 parent testcase nodes**, no failure/error/skip nodes or aggregate counts; producer aggregate `tests="270"`. Additional 75 outcomes are reconciled below, not 75 extra named testcase nodes. | Unmodified [junit-gates-supplement.xml](junit-gates-supplement.xml). Exact command/module list is also in `.github/workflows/ci.yml` at the source SHA above. |

## Pytest subtest accounting: preserve 270 and 195

The pre-recovery job-log review recorded **pytest 9.1.1**. Its
[unittest subtest reporter](https://github.com/pytest-dev/pytest/blob/9.1.1/src/_pytest/unittest.py#L394-L430)
emits subreports sharing the parent's node ID; its
[JUnit reporter](https://github.com/pytest-dev/pytest/blob/9.1.1/src/_pytest/junitxml.py#L461-L512)
reuses that parent's serialized node while incrementing outcome statistics per report.
Static reconciliation of the exact source's subtest loops gives:

| Exact-source module | Passing subtest outcomes |
|---|---:|
| `test_ci_sandbox_gate.py` | 5 |
| `test_ci_qualification.py` | 31 |
| `test_ci_required_backends.py` | 5 |
| `test_ci_bwrap_apparmor.py` | 34 |
| Total | **75** |

This source-and-producer interpretation explains **195 parents + 75 subtest outcomes = 270**;
it is not a fabricated console summary or evidence of 270 independently named tests. The
double-quiet log previously showed progress dots, not a `195 passed, 75 subtests passed` line.
During recovery a fresh log request returned zero bytes, so **no recovered log excerpt is
retained or claimed newly verified**. The raw XML's 195 nodes, aggregate 270 and zero failure/
error/skip counts are independently rechecked from the newly downloaded artifact.

At `305e8c3`, strict `qualify.parse_junit` rejects this aggregate-versus-node mismatch, so this
artifact is **not** a passing qualification-parser/gate result. Subsequent unpublished work
replaces subtest matrices with independently named cases and preserves strict equality and
negative/missing-evidence guards; it cannot retroactively validate this report. No unreported
subtest is inferred as a passing named case.

## Preserved limitations and open gates

- [First actual native run 40471175289](../verilator-native-run40471175289/README.md)
  remains **FAIL**. Its over-broad original-token assumption failed; this later PASS does not
  rewrite that report or turn its unexecuted import/query into success.
- HDL-VLT-01 retains `source_binding=UNVERIFIED`: `u_leaf` columns select original `IVE_WI`,
  and top-port columns select `_`, even though those positions select the expected tokens
  after known fixture macro expansion. The macro-free positive grants no general mapper/edit
  authority. No locations are corrected or promoted.
- Imported inventory remains `EXTERNAL_DECLARED`, `PARTIAL`, snapshot/build-unbound, with no
  authoritative database created; no merged slang references, absence proof, generic-body
  coverage or UVM runtime resolution is established.
- Native `uvm_acceptance`, `simulation`, `formal`, `coverage` remain `NOT_RUN`. These fixtures
  do not exercise protected-source preparation end-to-end acceptance.
- At this exact historical source, full real-backends CI stopped before legacy unsandboxed
  probes. The unpublished next slice was reconstructed after recovery: **133 fresh local pure
  checks PASS** (103 accounting + 30 isolation), with **10 pytest-dependent checks BLOCKED_ENV**,
  not 143 PASS. Its proposed success-only order is mandatory host sandbox → required native
  boundary tests → backend probe; four HDL-CI-05 native cases remain **NOT_RUN**. Authorized
  publication/dispatch is parent-owned and pending final review. This old run cannot
  verify reconstructed code or provide current full integration/fault-injection/acceptance/
  mutation/benchmark qualification or owner waiver/license signoff.
- Local backend execution remains blocked by `NETLINK_ROUTE` permissions. This native evidence
  came from the authorized hosted run, not a local retry, install or backend execution.

## Retention and reproducibility

[artifact-manifest.json](artifact-manifest.json) records original paths, byte counts and freshly
computed SHA-256 for all **79** downloaded members. **12** are retained byte-identically here:
native report/JUnit, bootstrap report, CI identity, both CI JUnit files, and real tree/metadata
pairs for HDL-VLT-01/02/05. These pairs preserve the macro finding and positive inventory checks;
no synthetic JSON or rewritten locations replace them. Fixture/source/tool hashes, exact
commands and sandbox argv remain in the native report. Source files are available at its SHA.

Members marked `copied=false` (runtime workspaces, command/build logs and remaining envelopes)
are **not copied into the repository**; retrieve them through the original GitHub artifact
while available. This directory does not retain the entire archive. Prior upload-log metadata
is labelled previously observed in the manifest, not a newly verified archive hash; current
verification uses the actual downloaded member bytes. No raw report or count was normalized.
