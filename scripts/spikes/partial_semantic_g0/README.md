# Native expression-invalidation micro-prototype

Experimental only. This directory does not alter SVWeave's production adapter, frozen pins,
public schemas, or full-rebuild contract. The original exact constant-RHS regression remains
mandatory (`MICRO_PROTOTYPE_PASS`). The reference expansion requires a separate
`REFERENCE_MICRO_PROTOTYPE_PASS`; `general_g0` remains `NOT_ESTABLISHED` in both cases.

Both fixtures have one top, two changed-leaf instances and one control-leaf instance. The
constant fixture admits only `1'b0` ↔ `1'b1`. The reference fixture admits only the exact
`a & a` ↔ `b & b` RHS transition, retaining an unchanged `k = a` consumer in the same leaf.
It exercises duplicate references, preservation of a shared read, and removal of an exclusive
read on revert. With this shared-consumer fixture alone, clearing the final `a` read on the
forward transition is not tested: instead the native event ledger must show `a` owner
contributions decrease while its unchanged consumer remains. Declaration-keyed native usage flags are aggregate across concrete instances;
AST read/write occurrence counts and qualified resolved targets are recorded separately.
Neither fixture establishes general leaf edits, arbitrary dependency invalidation, incremental
diagnostics, or large-design performance. At most two updates per session; no speed claim.

The reference transaction copies the entire event-count, owner and usage-status metadata
before committing with nonthrowing swaps. This O(all reference metadata) safety scaffolding
is not evidence of an end-to-end performance improvement. Accepted fixtures must remain
zero-diagnostic; invalid names are rejected before binding, and unexpected diagnostics
poison/discard the session rather than demonstrating diagnostic withdrawal.

## Reproduction

The optional `partial_semantic_g0` boolean on `.github/workflows/ci.yml` is false by default
and runs only through an explicitly authorized manual dispatch. The job uses Ubuntu 24.04,
read-only repository permission and credential-free checkout; no self-hosted runner/secrets.

1. Explicit installation step installs the host build tools and reviewed bwrap profile,
   then `setup.py` fetches only the four official archives in `sources.json`. Every archive
   is SHA-256 verified before safe extraction; no code from the archives runs during setup.
2. `run.py` applies `core.patch` to a dedicated copy. CMake configure and build both run in
   mandatory no-network bwrap with read-only source/dependency/tool inputs, dedicated writable
   build directories, minimal environment, private `/tmp`, and no repository/host-home mount.
   `FETCHCONTENT_SOURCE_DIR_*` plus fully-disconnected mode forbids runtime fetches.
3. Stock and patched use the identical compiler and sanitizer flags. Only the patched library
   and the oracle's explicit `PCU_STOCK` compilation define differ. Mimalloc is disabled on both.
   ASan and UBSan are enabled with non-recovering sanitizer errors. This is a source-built
   experimental artifact, not the production pinned wheel or evidence of bit-reproducible output.
4. Native tests run under bwrap with only executable and synthetic oracle source read-only;
   no writable host path. An isolation smoke test verifies source write refusal, host-canary
   invisibility, allowed environment, an empty network route table, and private temporary files.
5. Constant and reference full A/B are separate stock/fork processes and must match
   byte-for-byte. Each incremental A→B→A payload must also match its separate-process full
   outputs, including all usage flags, per-symbol occurrence counts and qualified targets.
   The reference experiment additionally runs fork-only `--reference-ledger-full a|b` in two
   fresh processes: all incremental native event ledgers must equal those independent full
   ledgers. The harness checks shared `a` contributions decrease but remain positive, `b`
   contributions grow from zero then return to zero, removed/rebound multiplicities match,
   and all writes/unaffected reads remain unchanged. These compiler event counts are not
   mislabeled as AST occurrence counts. Missing/unknown evidence,
   failed native assertions, repeated control semantic work, absent changed-leaf native work,
   malformed JSON, sanitizer errors, nonzero exit, resource overflow or sandbox failure fail.

Controller stage-start/end records are flushed immediately; long native stages emit bounded
30-second heartbeat metadata (elapsed time, sampled RSS and process count), never raw native
output or environment contents. Detailed native stdout/stderr remains in bounded artifacts.

All processes have bounded process-group lifetime, timeout, per-pipe output and sampled
process-tree RSS/process-count watchdogs (50 ms sampling, not a hard kernel RSS cgroup).
Builds use two workers, 10 GiB RSS limit and 3600 s per build; tests default to 3 GiB/180 s.
Bwrap's PID namespace also kills detached descendants when its supervisor terminates.

Evidence lives at `.svweave/ci/partial-semantic-g0/evidence/`: every command, stdout/stderr,
return code, actual isolation mode, elapsed time, sampled RSS and limits, archive identities,
patch/oracle/binary/workflow/production-lock hashes and run commit. The workflow uploads this
bounded evidence on success or failure, not binaries, source trees, secrets or production RTL.

## Requirements to tests

| Requirement | Test/evidence |
|---|---|
| Plan 23.1 supply-chain identity | `sources.json`; checksum and hostile-archive pure tests |
| Plan 6.4 / SEC-05 mandatory isolation | pure launch-boundary and mount tests; runtime isolation-check |
| No runtime downloads | disconnected configure flags; network namespace; no setup import in runtime |
| Three-way semantic oracle | stock/fork full A/B for both fixtures; type-sensitive external generation comparison |
| Reference-use removal and preservation | qualified targets, AST occurrence counts, native aggregate read/write flags; shared a read retained and exclusive b read removed on revert |
| Semantic work not wrapper timing | native AST expression/name/assignment/conversion counters |
| Rejection/lifetime bounds | native named assertions, immutable control identity, stale-generation refusal |
| Fail-closed evidence | pure corrupted/missing/unknown/duplicate JSON tests |
| No production scope creep | optional manual input, unchanged production lock, NOT_ESTABLISHED general G0 |

Pure tests can run without bwrap or any backend:

```sh
python -m unittest discover -s tests/unit -p test_partial_semantic_g0_harness.py -v
```

Never run a compiler, native oracle or backend locally without the mandatory sandbox. A local
host without bwrap is blocked for native validation; pure test success is not native success.

## Historical constant-only validation

[Run 40835759346](https://github.com/DVman9527/SVWeave/actions/runs/40835759346)
passed at `219f9b15ddf4b136b30446b917732195d07ddbda`.
[Raw evidence and scope audit](../../../evidence/partial-core-prototype-run40835759346/README.md)
retain the independent stock/fork/full/incremental outputs and unchanged control work.
This is **MICRO_PROTOTYPE_PASS**, not general G0, production support or a speedup claim.
The earlier isolation-guard failure remains recorded as FAIL, before any native build.

The reference expansion has not yet been compiled or run natively. Its current status is
**NOT_RUN**, pending authorized isolated CI; the historical run above does not validate it.
