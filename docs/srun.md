# Slurm execution: offline foundations, not remote support

Status: **UNQUALIFIED; remote execution BLOCKED**. The implementation adds opt-in typed
configuration, provider routing and safety primitives. It does **not** yet execute a Slurm
worker or qualify a cluster. The confirmed target is **Slurm 22.05.9**, pinned by the trusted
profile field `slurm_version`. Official-source Slurm **22.05.9** and MUNGE **0.5.18**
are now installed locally; client version/help checks pass with an explicit offline config.
This is **not an operational cluster**.
Installing `srun` does not change qualification status. The full requirements
remain in [support_srun.md](../support_srun.md); its integration/acceptance table is a plan, not
passed evidence. No simulation, formal, coverage or native Verilator runner is added.

## Installed client versus operational test (2026-10-27)

The separately approved single-node smoke test stopped during MUNGE startup. Attempt 1
failed socket-directory execute-permission validation. After explicit approval for a dedicated
0711 socket directory, attempt 2 failed creating the Unix stream socket:
`socket(PF_UNIX, SOCK_STREAM, 0): Operation not permitted`, before bind. This is a
**MUNGE runtime FAIL / environment BLOCKED**, not a failed scheduler job. No Slurm daemon
or job started; scheduler smoke, authentication round-trip and remote acceptance are
**NOT_RUN**. Cleanup reported no owned PIDs/listeners and no remaining socket.

The existing **295 passed** focused tests are historical **offline** regression evidence,
not a scheduler result and not rerun by this documentation update. See
[`runtime-smoke.json`](../evidence/srun/runtime-smoke.json) for redacted installation,
failure, cleanup and requirement mapping; the prior offline report remains separately scoped.
No socket retry, authentication bypass, alternate execution environment, feature launch,
commit or push was performed for this update.

Continuation requires an explicitly authorized environment that permits the required MUNGE
Unix socket and Slurm daemon operations, with an approved site/resource/network scope.
No saved eligible environment is currently available. A working cluster alone would still
not qualify SVWeave: the launcher/query/reconcile adapter, compute-node isolation/toolchain
checks and real safety/equivalence/cleanup acceptance gates remain outstanding.

## Available interfaces

Old manifests/local configuration continue to select `local`; Slurm is not required. The
existing local worker remains the default. No explicit `srun` request falls back to local.
The following command checks configuration and authorization **offline**: it does not run
`srun`, query a controller, start a remote probe or consume an allocation.

```sh
svweave execution-preflight
```

Global CLI options are `--execution-provider`, `--execution-profile`, `--allocation-mode` and
`--allocation-id`. CLI fields override `svweave.local.yaml` selection fields, which select but
cannot authorize a trusted profile. Explicit local selection clears inherited Slurm selection:

```sh
svweave --execution-provider local execution-preflight
```

A selection in local configuration has this shape (names/IDs are illustrative, not a usable
site configuration):

```yaml
schema_version: '1.0'
execution:
  provider: srun
  site_profile: reviewed_site
  allocation_mode: existing_allocation
  allocation_id: '123'
```

The trusted, out-of-repository policy owns `execution.scheduler_network_approved` and
`execution.site_profiles`. These are never created/enabled automatically. Profile fields
include canonical absolute client/bootstrap/runtime/shared paths, resource limits, deadlines,
cluster, optional scheduler account/partition/qos/constraint/reservation, and executable hashes.
Arbitrary flags, shell templates, multi-task/node requests, invalid IDs and control characters
are rejected. Shared staging is not the local live database or approval store.

An absent network exception yields `POLICY_DENIED`. Even an explicitly approved exception and
a supplied qualification digest cannot enable launch: `scheduler_query_contract` currently
accepts only `unqualified`, and the provider retains a second no-launch guard. There is no
configuration switch to bypass the missing implementation or qualification.

MCP tool request schemas do not accept arbitrary scheduler options. MCP uses the application's
startup/local configuration; the same build guard applies before cache lookup. P0 editing and
candidate validation under an srun execution context are denied, without local fallback.

## Implemented versus pending

| Area | Current boundary |
|---|---|
| Config, CLI, policy, schemas | Offline selection and denial checks implemented |
| Local provider | Delegates existing runner; original isolation/stdio contract retained |
| Build/cache/join | Execution selection participates in remote identity; srun denied before reuse/dispatch |
| Ownership and ledger | Typed exact-target verification and durable local records; not a live scheduler adapter |
| Bounded transport/deadlines | Reusable offline-tested primitives, not qualified remote lifecycle |
| Shared inputs/bootstrap | Atomic no-replace bounded immutable staging and hash/path/runtime-closure validators; manifest/snapshot-to-final-path preparation and generic bounded bootstrap entrypoint/lifecycle implemented behind the closed gate, not controller integrated or remotely qualified |
| Remote sandbox/toolchain | BLOCKED: fresh compute-node validation still required |
| Pending ID, exact cancellation/recovery | BLOCKED: real-site scheduler query/ownership contract and race qualification required |
| Local/remote pyslang equivalence | NOT_RUN; mocks are not integration evidence |
| Graph/Serena remote MCP (P1) | NOT_IMPLEMENTED/NOT_RUN; slang foundations do not imply three-backend support |
| Performance/resource cost | NOT_MEASURED; no claimed speedup |

## Operator gates and recovery

Before any future live submission, the owner must approve a narrowly scoped scheduler
control-plane network exception and specific site/allocation/resource budget. Backend bwrap
must remain network-isolated and must not receive scheduler credentials, source-write access,
approval keys or the local SQLite database. A submit-host sandbox result is not compute-node
proof. Do not disable namespaces, install daemons or switch containers to work around a failed
probe.

Qualify same-absolute-path immutable staging/dependency closure, locked runtime and bootstrap,
remote rlimits, bounded stdio, queue/startup/worker/overall deadlines, and cleanup of descendants.
Existing allocations are borrowed: only the verified attempt's numeric job.step may be
cancelled, never its parent, `batch`, `extern`, another step or a username-wide selection.
New allocations require reliable unique pending ID discovery before they can be supported.
Test with an unrelated companion step that must remain alive.

Uncertain submission is not permission to resubmit. A dead local PID or killed launcher is not
proof that the remote step ended. Persist exact attempt ownership and keep cleanup UNKNOWN,
inputs and evidence pinned until verified terminal reconciliation. Never delete an unknown
ledger to make garbage collection succeed. Switching back to local disables new remote
selection but is not cancellation and must not erase records. Current offline preflight has
`submitted=false`. The read-only `execution-recovery` inspection command below is available;
there is still no live scheduler reconcile/cancel or unpin command.

See [ADR-015](adr/ADR-015-slurm-execution-boundary.md) for the non-approved network boundary.

## Test evidence boundaries

The normal mutation harness expects a repository-local `.venv`; that environment is absent on
this implementation host. Focused pure-copy mutation checks for the new editing, ownership and
GC-pin guards are recorded separately in `evidence/srun/mutations.json`. They
are unit safety checks, not the normal complete mutation suite or cluster qualification.
Check the recorded commands/results rather than inferring acceptance from this document.

## Requirement-to-code/test mapping

This matrix reports implementation coverage, **not** release qualification. `IMPLEMENTED
(offline)` means the stated offline behavior exists; `PARTIAL` means only a component of the
requirement exists. All real-cluster requirements remain **BLOCKED / NOT_RUN**. Tests whose
filename contains `integration_guards` are in `tests/unit/` and use unit/fault fixtures, not a
real integration. No mock, fake scheduler observation or synthetic transport establishes a
site contract.

Test abbreviations below refer to these files:

- **C**: `tests/unit/test_srun_config.py`
- **P**: `tests/unit/test_execution_provider.py`
- **G**: `tests/unit/test_srun_integration_guards.py`
- **S**: `tests/unit/test_srun_staging.py`
- **E**: `src/svweave/execution/` (provider primitives)

| Requirement | Implementation status and code | Unit evidence / remaining real gate |
|---|---|---|
| SRUN-01 old local configuration | IMPLEMENTED (offline): `config/execution.py`, `config/context.py`, `E/local.py`; local default and existing runner delegation | C `test_SRUN_01_old_configs_default_local`; local regressions do not qualify Slurm; full existing backend regression still required |
| SRUN-02 explicit srun unavailable | PARTIAL: `E/slurm.py` and `ResolvedExecution.require_srun_authorized` deny without submission or local fallback | C/P SRUN-02; real missing-client/authentication/path failures cannot yet be exercised through an enabled adapter |
| SRUN-03 typed resources/argv | IMPLEMENTED (offline) for supported typed fields: `config/execution.py`, `E/slurm.py::preview_argv` | C SRUN-03; reject shell flags/control characters/multi-task/node/all-cluster selector; final argv/environment semantics need 22.05.9 site verification |
| SRUN-04 remote bwrap/toolchain | PARTIAL: `E/remote_bootstrap.py` fixed worker preparation, fresh probe, no degraded isolation, runtime/interpreter/hash inventory and exact runtime-mount closure verification | P SRUN-04 checks probe freshness/no fallback and unrelated runtime inventory rejection using unit injection; actual compute-node namespace/hash negative integration BLOCKED; no qualified deployment |
| SRUN-05 write/read/network denial | BLOCKED: existing local `security/sandbox.py` is not remote proof | No remote security integration run; must demonstrate original write, approval read and network denial on compute node |
| SRUN-06 immutable visible inputs | PARTIAL: `E/staging.py::publish_inputs` copies hash-fixed read-only files plus coordinator metadata with quotas, directory-fd/no-follow operations, fsync/sealing and atomic no-replace publication; `prepare_slang_staging` integrates manifest/snapshot and final paths; bootstrap revalidates files and closure | S SRUN-03/06 tests injection, symlink parents, hash/quota failures, final group/request paths, DB exclusion, unchanged originals and collision refusal; P immutable-input test; `prepare_slang_staging` now renders normalized manifest/snapshot inputs into final shared group/request paths without copying DB; controller submission wiring, site dependency/runtime verification and actual shared-filesystem coherency remain BLOCKED/pending |
| SRUN-07 queue/startup/worker timeout | PARTIAL: `E/deadlines.py`, `E/supervisor.py` phase/deadline behavior; hard-gated `E/remote_bootstrap.py::run_bootstrap` reuses bounded runner for compute-side worker timeout/rlimits/process cleanup with scheduler margin checks | P SRUN-07 and mocked bootstrap lifecycle; actual scheduler start events, timeout races, compute limits and remote cleanup remain BLOCKED |
| SRUN-08 cancellation races | PARTIAL: `E/ownership.py`, `E/supervisor.py` exact-target/cancellation primitives | P SRUN-07/09; queued/starting/running/collecting real races, idempotent live cancellation and zero orphan proof remain BLOCKED |
| SRUN-09 borrowed allocation isolation | PARTIAL: `E/ownership.py` verifies exact numeric job.step and refuses parent/unrelated identity | P `test_SRUN_09_exact_borrowed_step_only`; real unrelated companion step and parent survival acceptance NOT_RUN |
| SRUN-10 crash/disconnect/recovery | PARTIAL: `E/ledger.py`, `E/supervisor.py`, `application/jobs.py`, `storage/db.py` preserve unresolved state/pins | P SRUN-10 and G SRUN-17; no real durable scheduler reconcile or uncertain-submission resolution yet; must not resubmit |
| SRUN-11 forged/stale ownership | PARTIAL: `E/models.py`, `E/ownership.py`, `E/ledger.py`, `E/control.py` and hard-gated `bootstrap_once` ownership comparison, bounded request/private sidecar records and backend-inaccessible control location checks | P SRUN-11; stale/reused real IDs, delayed accounting and authenticated site query contract still BLOCKED |
| SRUN-12 crash/OOM/node/preempt | PARTIAL (offline decoder): `E/accounting.py` parses version-pinned state/printed-exit diagnostic fields; supervisor and gated compute lifecycle retain separate failure causes without SIGKILL-to-OOM inference | `test_slurm_accounting.py` synthetic source-derived cases plus P SRUN-12; live authenticated accounting/state integration and compute-node failure injection remain BLOCKED/NOT_RUN |
| SRUN-13 bounded stdio | PARTIAL: `E/transport.py`, `E/supervisor.py` bounded output/stderr/partial-write/drain checks; hard-gated bootstrap reuses bounded local runner on compute host | P SRUN-13; S bounded staging quotas; real launcher/helper banner, flood, disconnect and backpressure qualification NOT_RUN |
| SRUN-14 local/remote semantic equivalence | BLOCKED: existing build ingest remains coordinator-local; no remote launch | No remote pyslang comparison; require real normalized semantic rows/diagnostics/limitations equality |
| SRUN-15 provenance/cache isolation | PARTIAL: `config/execution.py`, `domain/ids.py`, `application/build.py`, `E/models.py` namespace/evidence structures | C/G SRUN-15; execution fingerprint separation tested; real verified runtime/sandbox/accounting provenance still absent |
| SRUN-16 concurrent request join | PARTIAL: `application/jobs.py` adds execution fingerprint to effective job selection | Existing job tests plus code path; actual concurrent distinct remote resources/attempts and single-writer faults need qualification |
| SRUN-17 unknown cleanup/publication/GC | PARTIAL: `storage/db.py::plan_gc`, `E/ledger.py` pin unknown attempts and block corrupt-ledger GC | P/G SRUN-17; real failed remote build preservation and terminal reconcile NOT_RUN |
| SRUN-18 UVM metadata | BLOCKED remote comparison; existing static-call semantics unchanged | `tests/unit/test_uvm_call_metadata.py` is local-only; no remote UVM fixture run; must retain runtime_resolution=not_computed and compare limitations |
| SRUN-19 external inventory/prepared gaps | Existing paths remain local and unchanged; remote regression qualification BLOCKED | `tests/unit/test_verilator_importer.py` and `tests/unit/test_protected_source.py` are separate regressions, not remote evidence; no upgraded PARTIAL/UNKNOWN/NOT_RUN claims |
| SRUN-20 P1 remote MCP | NOT_IMPLEMENTED; `orchestration/registry.py` rejects remote selection instead of silently using local | G `test_SRUN_20_remote_registry_never_falls_back_local`; real three-backend initialize/query/EOF/cancel/helper-daemon cleanup NOT_RUN |
| SRUN-21 scheduler network exception | IMPLEMENTED (offline fail-closed): trusted policy default false plus unconditional unqualified-site/dispatch guard | C/P/G SRUN-21; no owner exception approved; real exception may cover only scheduler control plane, never backend network |
| SRUN-22 compatibility/docs/schema | IMPLEMENTED (offline): CLI options/preflight, optional local/policy fields, generated schemas, ADR and docs | C SRUN-22, schema emit check, DOC-01; read-only recovery inspection is available; live site migration/deployment/reconcile remain pending |
| SRUN-23 mutations | PARTIAL: `scripts/mutation_suite.py` entries M23–M25 and `evidence/srun/mutations.json` | Focused throwaway-copy editing/ownership/GC-pin mutants killed; full standard harness unavailable without repo `.venv`; remote sandbox-bypass qualification NOT_RUN |
| SRUN-24 cost/resources/accounting | NOT_IMPLEMENTED real measurement; result models keep unknown accounting distinct | No queue/CPU/RSS/cost measurements or performance claims; requires exact real IDs and station-approved budget |
| P0 editing guard | IMPLEMENTED (offline): `application/edit.py` and `editing/plan.py` reject srun before baseline/candidate/approval/apply | G `test_SRUN_P0_edit_guard_precedes_baseline_and_candidate` and `test_SRUN_P0_no_approval_or_apply_under_remote_context`; enabling remote editing requires a separate reviewed design and qualification |

The targeted command-line contract is Slurm **22.05.9**, reviewed against upstream source tag
`slurm-22-05-9-1`, not the latest online manual. A cluster named `all` (any case) is rejected
because it is a multi-cluster selector. Offline argv construction may use `--clusters` only for
new-allocation mode: the 22.05.9 option implies an allocation; borrowed-allocation mode must not
use it to create an accidental new allocation. Neither mode is currently enabled. Installed
client/server versions and site-specific behavior must still be verified before qualification.

### Version-specific source evidence (22.05.9)

The offline preview is constrained by the tagged
[srun manual](https://github.com/SchedMD/slurm/blob/slurm-22-05-9-1/doc/man/man1/srun.1):
`--cpus-per-task` implies `--exact`, memory with an `M` suffix is per-node MiB, and `--time`
is minute-granular with scheduler `OverTimeLimit` behavior. It is not the precise worker or
cleanup deadline. The preview is not a measured site contract. This version does not provide a
usable `--parsable` allocation-ID interface for this synchronous design; do not import a
new-version async/parsable design without review.

The tagged [scancel manual](https://github.com/SchedMD/slurm/blob/slurm-22-05-9-1/doc/man/man1/scancel.1)
describes job/step targets, but application ownership must still be verified independently;
the ability of a UID to cancel a job does not grant this application ownership of it.
Task-side `SLURM_JOB_ID`/`SLURM_STEP_ID` are execution outputs, not prelaunch ownership evidence.
In the tagged
[allocation implementation](https://github.com/SchedMD/slurm/blob/slurm-22-05-9-1/src/srun/libsrun/allocate.c),
internal `pending_job_id` and debug logging are not an approved bounded machine-readable
pending-ID query contract. Neither a launcher SIGKILL nor disappearance proves remote cleanup.

### Generic staging and compute lifecycle are not site integration

`execution/staging.py` now publishes only a fixed immutable file set and explicit generated
metadata to a separate private per-attempt directory. It hashes content, enforces byte/file
quotas, seals files/directories and uses Linux `renameat2(RENAME_NOREPLACE)` so an existing
attempt cannot be overwritten. Failure cleanup is restricted to its own unpublished temp tree.
`prepare_slang_staging` integrates `NormalizedManifest` and `SnapshotRecord`, checks their
identity, and renders group metadata plus the worker request against final shared absolute
paths before publishing. `adapters/slang/args.py` has an optional `published_group_dir`; its
default preserves the original local invocation behavior. Controller submission wiring and
shared-filesystem behavior remain unqualified. This API does
not copy the whole state directory; local SQLite, WAL, journal and approvals remain on the
coordinator. The low-level publication API still requires a legitimate immutable snapshot closure, not
arbitrary read-only private files; the higher-level slang staging helper selects the recorded
snapshot file hashes and does not stage live database state.

`execution/remote_bootstrap.py::run_bootstrap` contains a generic compute-host lifecycle behind
`require_srun_authorized()`; that gate still always blocks live use. Preparation validates the
input manifest, fixed interpreter/runtime hashes, exact read-only runtime mount inventory and
a fresh compute-host bwrap probe with no degraded mode. The lifecycle reuses the existing
bounded worker runner for compute-side memory/core limits, timeout and descendant cleanup,
requires resource margin for bootstrap/cleanup, and keeps scheduler accounting unknown. Unit
tests inject probes/worker outcomes; they are not remote execution. The generic `bootstrap_once` entrypoint accepts a bounded request, validates typed identities
and trusted bootstrap hashes, checks that the control directory is outside backend mounts,
and publishes a bounded private ready control record only after bootstrap preparation. It
remains a hard-gated API, not an enabled command-line entrypoint. Station-approved controller
submission, scheduler queries, trusted deployment and terminal reconcile are still missing
from an enabled end-to-end path. Sidecar/task environment matching is correlation only, never
independent cancellation authority. No helper success proves scheduler cleanup.

## Read-only recovery inspection

```sh
svweave execution-recovery --limit 200
```

This command reads bounded local execution-ledger and durable database attempt-reference
information without submitting, querying or cancelling a Slurm job. The limit is 1–1000
(default 200), with a 64 KiB report budget; truncated results explicitly report uncertainty and
pin-all status. It works under an unqualified srun-selected configuration: inspecting evidence
is not authorization to execute and does not call the launch preflight guard.

The command cannot repair/delete records, clear pins, change job state or write SQLite/WAL
files. It opens the database read-only/immutable and fails closed when a WAL makes that view
unsafe. Missing, corrupt or inconsistent recovery evidence is reported as blocked with
`RECOVERY_REQUIRED`; do not remove such evidence to make a later invocation succeed. A bounded
or empty result is **not** proof that there are no remote jobs. Recorded cleanup flags and
job/step IDs are historical local evidence, not fresh scheduler-terminal proof or cancellation
authority. Take the exact recorded attempt/cluster/job/step to the approved operator workflow;
never infer permission for parent-allocation or username-wide cancellation.


## Offline SRUN-12 diagnostic decoding

The version-pinned [accounting field decoder](srun_accounting.md) now classifies bounded
Slurm 22.05.9 sacct state and printed exit/signal fields as diagnostic evidence only. It does
not authenticate records, establish ownership/cleanup, infer success or query the scheduler.
SRUN-12 remains **PARTIAL (offline decoder)**; live accounting integration and remote failure
qualification are still blocked/not run. Synthetic unit fixtures are not real observations.
