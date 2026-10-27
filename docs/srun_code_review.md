# Slurm implementation code review — 2026-10-27

Scope: offline implementation against the Slurm **22.05.9** plan. Independent security and
integration reviewers inspected code and exercised pure regression tests. Findings below
concern executable safety primitives and application boundaries, not a claim that a Slurm
cluster has been qualified. No review/fix test starts a scheduler, MUNGE socket or HDL backend.

## Findings and resolutions

| Priority | Finding | Resolution / implementation | Regression evidence |
|---|---|---|---|
| P2 | Overall execution timeout consumed the entire cleanup deadline, leaving no cancellation grace | `execution/supervisor.py::supervise` reserves grace within the total deadline, does not begin expired reconciliation, and retains timeout separately from cleanup uncertainty | `test_srun_audit_regressions.py`: overall budget and expired-budget tests |
| P2 | Nonzero launcher exit reached reconciliation without cancellation intent; cleanup uncertainty masked the original error | Supervisor marks failed launcher cancellation before reconciliation and retains launcher/timeout reasons; internal stream errors remain `stdio_failure` | Audit tests for zero, positive and negative exit status, plus broken stream |
| P2 | NaN/infinite clock or age values bypassed ownership freshness comparisons | `execution/ownership.py::cancellation_target` requires finite clock/age/observation values | Nine finite-clock negative cases |
| P2 | Cached local registry survived a switch to an srun execution context | `application/session.py::Session.registry` rechecks execution identity, disposes stale registry/errors and blocks remote factories; malformed config also revokes cache | `test_srun_session.py`: local→srun→local, changed policy and invalid-config cases, fake registry only |
| P2 | Async build could reload a different execution context after submission | `application/jobs.py` binds submit-time fingerprint; `application/build.py` checks it again before locked build/snapshot work | Deterministic paused-thread and second-load races in audit tests |
| P2 | Failed or same-manager remote attempts could cease blocking new work; a missing file ledger erased uncertainty | Migration v5 retains immutable database attempt references; `execution/ledger.py` reconciles references and records independent of local job lifecycle/owner. Direct and asynchronous builds refuse unresolved workspace state. Missing/corrupt evidence retains pins | `test_execution_reconciliation.py`; six direct/async owner×evidence cases in audit tests |
| P2 | A clean legacy-database result bypassed concurrent-change checks during recovery inspection | `execution/recovery.py` routes legacy reads through the same final inode/stat/journal checks; a concurrent migration becomes UNKNOWN/ALL-pinned | Deterministic concurrent-writer test in `test_execution_recovery_inspection.py` |
| P3 | Literal `nodes=1` / `tasks=1` accepted booleans/floats despite strict model intent | `config/execution.py` validates exact integer type before literal validation | Eight invalid-type cases in `test_srun_config.py` |

`test_execution_reconciliation.py` covers historical database migration, durable/idempotent
references, replacement/update/delete refusal, missing/corrupt/mismatched records, all job
lifecycles, retained references after recorded cleanup, and a missing promised v5 table.
No command added by this work can clear those references or unpin an attempt.

## Evidence interpretation

- The earlier **295-test** implementation result remains historical in
  `evidence/srun/unit-regression.log`; it is not the current review run.
- Review/recovery checkpoint regression: **366 passed, 4 deselected**, recorded separately in
  `evidence/srun/review-regression.log`; the intermediate 344-test audit run is historical.
- Ruff, mypy (**130 source modules**), schema sync, DOC-01 (**2 passed**) and diff checks passed.
  Focused safety mutations **M23–M26 were killed** in a throwaway copy; this is not the full
  standard mutation suite. Exact commands and hashes are in `evidence/srun/review-verification.json`.
- The environment-dependent doctor test reports a missing codebase-memory binary. A focused
  offline pass is not a full backend regression or a release qualification pass.
- Mutation tests are pure throwaway-copy checks. Mocked scheduler observations, fake launchers,
  and memory streams never constitute remote cancellation or isolation evidence.

## Operational gates remain unresolved

Slurm 22.05.9 and MUNGE 0.5.18 are installed locally. The separately authorized runtime smoke
attempt stopped at MUNGE's Unix stream `socket()` call with `Operation not permitted`, before
bind. This is an actual runtime restriction, not a successful scheduler launch or an inferred
permission to bypass the environment. No retry was made by the review/fix work.

Live scheduler/query/reconcile integration remains disabled and unqualified. Exact site
ownership/pending-ID contracts, approved network endpoints and allocation/resources, trusted
shared deployment, compute-node sandbox/toolchain qualification, unrelated-step preservation,
and real local/remote semantic equivalence remain required. P1 graph/Serena remote execution
is still not implemented or qualified. EDA compile, simulation, formal and coverage are
**NOT_RUN**.

See [current implementation matrix](srun.md) and `evidence/srun/runtime-smoke.json`.


## Read-only recovery inspection addition

`svweave execution-recovery --limit 200` exposes bounded local recovery evidence without
calling the scheduler or changing ledger/database/job/pin state. It is available even when
srun execution is unqualified. Database reads use a read-only immutable view and reject a
present WAL rather than ignore potentially newer references. Missing or corrupt evidence
remains `RECOVERY_REQUIRED`; empty/truncated output is not evidence that no remote job exists.
CLI tests cover dispatch, bounds and srun-selected inspection without authorization; underlying
recovery tests separately verify storage behavior. This is operator inspection, not live
reconciliation, cancellation authorization or a cluster qualification result.


## Subsequent SRUN-12 decoder review

The separate [Slurm 22.05.9 accounting field decoder](srun_accounting.md) was independently
reviewed with **98 synthetic unit cases passed**. Review identified one representability edge:
signal 127 is the stopped wait-status marker, not an output of the printer's `WIFSIGNALED`
branch. It is now rejected and boundary tests cover 126/127. No remaining blocking correctness
or authority finding was reported for this bounded utility. It does not construct an ownership
observation, establish cleanup or enable the live provider. Current combined evidence is in
`evidence/srun/accounting-verification.json`; prior review counts above remain historical.
