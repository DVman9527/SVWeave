# ADR-018: Opt-in snapshot I/O reuse, not incremental semantic compilation

Status: **implementation decision; developer preview / UNQUALIFIED** (2026-10-29).
Governing plan sections: 9.1–9.6, 15, 20.2, 22.3 and 23.1. This first phase preserves
ADR-006's full-semantic-build boundary. The protected plan, frozen toolchain/licenses and
14-tool MCP schemas are unchanged.

## Problem

The target workflow is **one large top/profile with a few modified files**, not many small
profiles. Copying an unchanged input tree before checking the exact build cache costs I/O.
However, Git changes and previously observed compiler dependencies cannot establish the
current input set: ignored/untracked files, external roots, include shadowing, library
membership, source order and configuration can change the result.

## Decision

- Add an opt-in CLI update path; keep ordinary `build`, snapshot creation and MCP build defaults.
  Use the profile's last published COMPLETE snapshot only as a donor hint, never a user-selected
  filesystem path or the authority for invalidation. Git's optional base is a different concept.
  A shared MCP installation upgrade alone does not opt in: `svw_build` keeps its legacy path.
  An explicit CLI update publishes to the same workspace database/pointer, making a successful
  COMPLETE result queryable by its returned build ID through the existing CLI/MCP tools.
- Enumerate and hash **all current relevant inputs** under the existing manifest and trusted
  roots, with the existing ordered lookup inventory and bounded race checks. Git, timestamps,
  previous dependencies and reported changed-file lists cannot narrow this set.
- Preserve the existing snapshot/config/toolchain/build identity rules. An unchanged, verified
  full snapshot can be reused. Otherwise materialize another complete immutable tree, optionally
  using verified copy-on-write reflinks for unchanged donor content. Unsupported reflinks fall
  back to ordinary copies; no writable shared inode/hardlink or overlay chain is introduced.
  Missing or unsafe donor metadata removes the optimization, not current-input verification.
- Reuse semantic results only for an exact existing COMPLETE build under the ordinary identity,
  freshness and publication rules. Otherwise run the **whole profile** through the existing
  sandboxed semantic pipeline. No partial parser/elaborator, dependency-closure compilation,
  semantic-row merge or graph-index delta is implemented.
- Keep immutable historical build rows, build-bound cursors, policy checks, execution selection,
  edit approval/CAS, locking, failed-publication behavior and retention unchanged. Capture is
  not authority to edit source or relax sandbox requirements. Corrupt snapshot cache bytes are
  not trusted as a donor or as evidence of a no-op.
- Return request-local reporting of authoritative snapshot differences, actual materialization
  work and semantic reuse/analysis outcome. Do not rewrite historical run metadata to attach a
  later request's advisory information. Mark bounded-list omissions explicitly.
- Provide a bounded optional Git advisory only. It may be disabled or unavailable without
  disabling authoritative capture. It does not prove absence of relevant changes, select the
  semantic rebuild set, or represent the previous built snapshot.

## Git boundary

The advisory reports staged differences against its requested base, unstaged metadata hints
against the index, and untracked paths. Renames may appear as deletion plus addition. Unstaged
hints may have false positives or miss metadata-preserving changes. Untracked hints do not apply
repository ignore rules and may include ignored names; external roots and submodule contents
are outside the advisory's coverage. Capture checks relevant files independently.

Repository-controlled hooks, filters, fsmonitor, external diff/textconv and configuration
includes must not execute. Environment and Git I/O are bounded; unsupported metadata indirection
and unsafe layouts make the advisory unavailable. In particular, linked-worktree/submodule
gitfiles are not an excuse to follow metadata outside the authorized root. Advisory failure is
not an unchanged-files result and is not permission to fetch objects or contact a remote.

## Performance and validation boundary

The optimization is snapshot **I/O reuse**. Current content is still fully hashed and verified;
changed-input fallback can cost more than legacy capture on a filesystem without reflinks.
Logical hash/copy/clone counters are not physical disk traffic, allocated disk savings or a
guaranteed speedup. A successful clone syscall alone is not enough: destination bytes must match.

`scripts/benchmark_incremental_snapshot.py` compares paired full and update captures on one
synthetic profile/top with no-op, one-file and broader edits. It records corpus/host/source
identity, actual materialization counters, stage timings and independent output equivalence.
This snapshot-only benchmark does not exercise semantic compilation or graph indexing and is
not the complete PERF-01 acceptance gate. Real worker equivalence, native Verilator static
checks and exact-source CI are separate evidence; unit mocks cannot replace them.

At this implementation-decision stage, new exact-source CI/native validation is **NOT_RUN**;
there is no new PASS or qualification claim here. Historical validated runs retain their
original source SHA, scope and limitations. Native application-client behavior, actual Claude
registration/connection and Slurm site execution are not established by this work. Full release
qualification remains **UNQUALIFIED**.

## Requirement-to-test mapping

Coverage anchors below describe intended checks, not an assertion that a run passed. Record
actual command, source/lock identity, status and retained evidence separately.

| Requirement | Coverage anchor | Boundary |
|---|---|---|
| Plan 9.1–9.3 / SNP-01–04: same full inventory and identity | `tests/unit/test_incremental_snapshot.py::test_SNP_INC_02_*`, `test_SNP_INC_03_*`, `test_SNP_INC_04_*`, `test_SNP_INC_10_*`, `test_SNP_INC_16_*` | Cold/changed capture equivalence; membership, dirty/untracked content and symlink metadata. No dependency-closure assumption. |
| SNP-05: bounded race detection and resource limits | `tests/unit/test_incremental_snapshot.py::test_SNP_INC_07_*`, `test_SNP_INC_08_*`, `test_SNP_INC_12_*`, `test_SNP_INC_15_*`, `test_SNP_INC_17_*`, `test_SNP_INC_19_*`, `test_SNP_INC_20_*` | Races, tampering, byte caps and root escape fail safely, including bounded/no-follow include scans; no mixed snapshot publication. |
| Plan 9.2: independently verified immutable reuse | `tests/unit/test_incremental_snapshot.py::test_SNP_INC_01_*`, `test_SNP_INC_05_*`, `test_SNP_INC_06_*`, `test_SNP_INC_09_*`, `test_SNP_INC_13_*`, `test_SNP_INC_14_*`, `test_SNP_INC_18_*` | No-op verification, corrupt donor/cache rejection, concurrent publication and faulty clone fallback. Injected clone paths are unit tests, not native reflink evidence. |
| Plan 20.5: mutation guards for reuse and races | `scripts/mutation_suite.py` M28–M29 | Removing existing-tree verification or changed-capture comparison must fail the mapped SNP-INC tests. The expanded inventory has 29 entries; that is not a claim that all 29 were executed or killed. Historical 27-entry results are unchanged. |
| Plan 9.6 / SNP-04, SNP-06: exact reuse or full profile; safe publication | `tests/unit/test_incremental_update_command.py::test_UPD_01_*` through `test_UPD_06_*`, `test_UPD_09_*`, `test_UPD_10_*` | Identity/config/toolchain changes, published-profile donor selection, locks, legacy defaults, expected snapshot and failed publication. |
| API/DOC-01: opt-in CLI and truthful bounded reports | `tests/unit/test_incremental_update_command.py::test_UPD_07_*`, `test_UPD_08_*`, `test_UPD_11_*`, `test_UPD_12_*`; `tests/unit/test_gateway_cli.py::test_DOC_01_*` | CLI options/exit policy, omissions and no claim that pre-worker failure completed analysis; no MCP schema addition. |
| SNP-03 / SEC-02: Git is inert, optional and non-authoritative | `tests/unit/test_update_git.py` | Dirty/staged/untracked paths, ignored limitations, invalid/missing Git/base, hostile executable config, symlink escapes, output/time bounds. |
| SNP-01–03 / plan 9.6: real semantic equivalence and historical queries | `tests/integration/test_incremental_update.py` | Mandatory-bwrap pinned worker, full-vs-update semantic rows, changed inputs and failed publication; distinct from unit results. |
| Plan 22.3: paired reproducible measurement | `tests/unit/test_incremental_snapshot_benchmark.py`; `scripts/benchmark_incremental_snapshot.py` | Independent snapshot equivalence and actual counters; elapsed time is an observation, not a speed gate. |

Operational details: [update workflow](../operations.md#2a-opt-in-snapshot-io-update).
