# Native expression-invalidation micro-prototype

Experimental only. This directory does not alter SVWeave's production adapter, frozen pins,
public schemas, or full-rebuild contract. A successful run means `MICRO_PROTOTYPE_PASS`;
`general_g0` remains `NOT_ESTABLISHED`. The fixture has one top, two instances of the changed leaf, and one control-leaf instance;
only the exact one-bit constant RHS `1'b0` ↔ `1'b1` transition is admitted, twice. It does not
establish general leaf edits, reference-changing invalidation, incremental diagnostics, or
large-design performance. No speed claim is made.

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
5. Full A/B are separate stock/fork processes and must match byte-for-byte. Incremental A→B→A
   payloads must also match those separate-process full outputs. Missing/unknown evidence,
   failed native assertions, repeated control semantic work, absent changed-leaf native work,
   malformed JSON, sanitizer errors, nonzero exit, resource overflow or sandbox failure fail.

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
| Three-way semantic oracle | stock/fork full A/B; external incremental generation comparison |
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
