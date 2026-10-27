# Slurm 22.05.9 accounting evidence decoder

Status: **offline diagnostic utility; SRUN-12 remains PARTIAL / UNQUALIFIED**.
The implementation is `execution/accounting.py`; it is not a scheduler adapter, raw table
parser, CLI command, recovery action or live accounting probe. It is not wired into submission,
`SchedulerObservation`, cancellation, reconciliation, GC pin clearing or qualification.

## Bounded input contract

`decode_accounting(payload, expected_scope=...)` accepts at most **8192 bytes** describing one
row using SVWeave's own `svweave-sacct-fields-v1` JSON envelope. This is **not native sacct JSON**.
Only version `22.05.9` and `sacct_job_fields` / `sacct_step_fields` sources are supported. It
rejects unknown/duplicate fields, non-UTF-8 input, banners, malformed JSON and other versions.
The expected scope is lexical correlation supplied by the caller, **not verified ownership**.

| Field | Contract |
|---|---|
| `contract`, `slurm_version`, `source` | Exact versioned values above |
| `scope.cluster` | Exact bounded single-cluster token, never `all` |
| `scope.job_id` | Full `JobID`, not merely `JobIDRaw`; regular numeric job or numeric job.step |
| `job_id_raw` | Required separately; must equal the full regular JobID and expected scope |
| `state` | Optional bounded displayed `State`; independently classified |
| `exit_code` | Optional displayed `ExitCode`; decoded as printed components only |
| `derived_exit_code` | Optional displayed `DerivedExitCode`, job rows only |

Arrays, heterogeneous jobs, ranges, named special steps, reserved numeric step IDs and
federated/out-of-range job IDs are unsupported and rejected. Numeric `JobIDRaw` alone cannot
establish that a record is not an array/heterogeneous component. Source and expected job/step
scope must agree. Padding spaces are tolerated on state/exit values; control characters are not.

## Meaning and non-meaning of results

- Exact full 22.05.9 state names are recognized. Flag-selected strings such as `COMPLETING`,
  `CONFIGURING`, `STAGE_OUT` and requeue states remain **transitional**, not cleanup proof.
- Unknown, malformed or overlong state values remain unknown. A terminal `+` is treated as
  truncation—even if the remaining prefix spells `COMPLETED` or `CANCELLED` exactly.
- `CANCELLED by <uid>` reports cancellation attribution, not the job owner or cancellation
  authority. The source may also append this attribution to a flag-selected string, such as
  `COMPLETING by <uid>`; that remains transitional.
- Slurm 22.05.9's `sacct` JOB/STEP `ExitCode` printer subtracts 128 from exited statuses at
  least 128. The decoder therefore accepts printed exit components **0–127** and does not
  reconstruct a process wait status. `DerivedExitCode` has a separate **0–255** contract.
  A valid printed pair has either an exit component or signal component, not both nonzero.
  Signal 127 is rejected because the wait-status stopped marker cannot satisfy the printer's
  `WIFSIGNALED` branch; accepting a representable printed signal does not authenticate an OS event.
- Signal **9 is not OOM evidence**. `OUT_OF_MEMORY` is classified only as a scheduler-reported
  OOM state; it is not independently authenticated. Missing accounting/exit fields remain
  unavailable rather than becoming zero. Derived `0:0` can itself represent missing internal
  derived-exit data in this version's printer.
- `COMPLETED` with `0:0` is **not worker success**. Parsed fields do not validate stdout, HDL
  diagnostics, ingest, descendants, remote cleanup, ownership or evidence authenticity.
  Historical preemption/requeue state does not prove a logical job cannot execute again.
- Output stays `EXTERNAL_DECLARED`, `diagnostic_only`, `remote_cleanup=UNKNOWN` and
  `worker_success=NOT_ESTABLISHED`. `parse_status=PARSED` means only the supported fields parsed.
  No timing, CPU/RSS accounting, timestamps or ownership inference is added.

`None` or empty bytes mean unavailable accounting. Individual absent/malformed state and exit
fields are represented independently. Serialization, version and identity mismatches reject the
envelope rather than silently selecting another row or falling back to another format.

## Source and license provenance

Behavior was checked against the installed official **Slurm 22.05.9** source archive
(SHA-256 `0b732f293ca7ba226c21a58981dee37d668afdedad2fb2a485e8b3eff585d0d2`) and its
corresponding upstream tag **`slurm-22-05-9-1`**:

- [`src/common/slurm_protocol_defs.c`](https://github.com/SchedMD/slurm/blob/slurm-22-05-9-1/src/common/slurm_protocol_defs.c), `job_state_string`: full state spellings and flag precedence.
- [`src/sacct/print.c`](https://github.com/SchedMD/slurm/blob/slurm-22-05-9-1/src/sacct/print.c), state/exit/derived-exit and JobID printing: decorated state and differing printed exit semantics.
- [`src/common/slurmdb_defs.c`](https://github.com/SchedMD/slurm/blob/slurm-22-05-9-1/src/common/slurmdb_defs.c), `slurmdb_get_job_id_str`: array/heterogeneous identity formatting.
- [`src/common/print_fields.c`](https://github.com/SchedMD/slurm/blob/slurm-22-05-9-1/src/common/print_fields.c): fixed-width `+` truncation versus parsable field rendering.
- [`slurm/slurm.h`](https://github.com/SchedMD/slurm/blob/slurm-22-05-9-1/slurm/slurm.h): normal job/step bounds and special-step identifiers.
- [`doc/man/man1/sacct.1`](https://github.com/SchedMD/slurm/blob/slurm-22-05-9-1/doc/man/man1/sacct.1): field descriptions and truncation caveats.

The referenced source headers state GPLv2-or-later with the applicable OpenSSL linking
exception. The decoder is a separate implementation of field semantics: no upstream source
code is copied, linked or vendored, and no dependency/lock/license-manifest upgrade is made.
Local source-file hashes are recorded in `evidence/srun/accounting-verification.json`.

## Verification boundary

All examples in `tests/unit/test_slurm_accounting.py` are tiny hand-authored **synthetic,
source-derived unit fixtures**, not captured or authenticated scheduler records. They exercise
all recognized states, field independence, malformed/truncated values, version/scope rejection,
printed-exit semantics and authority boundaries. No `sacct`, socket, daemon or scheduler query
is executed by this work. The installed-client status and MUNGE runtime restriction remain
unchanged; native scheduler/remote accounting acceptance is **NOT_RUN**.
