# UVM static call metadata verification — 2026-10-22

Base commit: `3528124be2deaf314a6cf39299b6cf9c6c58126d`.
Local uncommitted branch: `feat/uvm-static-call-metadata`. No commit/push/merge.
Lock SHA256: `ea20baf9bc2da3cb6b9dc41f0234c67f925ea37c5691b0bfbbb1ff30f86ef5e9`.
Authoritative `SVWeave_plan.md`, toolchain pins, schemas and goldens unchanged.

## Requirement-to-test mapping

| Requirement | Implementation and tests | Status |
|---|---|---|
| Plan 16.1 / UVM-02: factory call sites, no actual factory outcome | Resolved-declaration API allowlist and `runtime_resolution=not_computed`; pure classifier tests, real fixture type/instance overrides and registry create | Pure tests PASS; real integration BLOCKED |
| Plan 16.1 / UVM-03: config-db source/type/context, not matching | Callee specialization retained; named arguments associated by formal name, ordered/empty arguments preserved without inserting defaults; real set/get fixture and query assertion | Pure tests PASS; real integration BLOCKED |
| Same-spelling non-UVM calls must not acquire UVM metadata | Negative classifier tests and `local_db::set/get` fixture | Pure tests PASS; real integration BLOCKED |
| Incomplete evidence must stay visible | Argument counts/list truncation, syntax availability, byte-based per-expression text truncation, invalid-association markers | Pure tests PASS; actual parser wiring NOT_RUN |

## Verification commands and outcomes

Python/tool prefix below: `/workspace/shared/uvm-ab/venv/bin/`.
Dependencies were installed offline from `vendor/downloads/wheelhouse{,-dev}` after
verifying their bytes against `vendor/SHA256SUMS`, using pip `--require-hashes` with
`scripts/gateway-requirements.txt` and `vendor/downloads/dev-requirements.txt`.
No backend bootstrap or network installation was run.

* PASS: `ruff check .`.
* PASS: `mypy` — 110 source files.
* PASS: `python -m pytest -o addopts='' tests/unit/test_uvm_call_metadata.py tests/unit/test_slang_ingest.py -q` — **53 passed** (28 new pure cases and 25 existing ingest cases).
* PASS: `python -m svweave.cli schema emit --check` — no out-of-sync schemas.
* PASS: `git diff --check`.
* BLOCKED / interrupted: combined focused-unit + `tests/contract` attempt. Contract fixtures
  require a real demo build; build reports show `BACKEND_UNAVAILABLE`, worker not started,
  `worker_isolation=UNAVAILABLE`, `wall_s=0.0`. The suite was interrupted after fixture
  errors; it is NOT a passing contract run. No protected worker executed.
* BLOCKED / NOT_RUN: new real UVM regression
  `tests/integration/test_uvm.py::test_UVM_02_03_named_arguments_and_same_name_negative`.
  Mandatory bwrap unavailable: `bwrap: loopback: Failed to create NETLINK_ROUTE socket:
  Operation not permitted`. No in-process extraction, degraded mode or other isolation
  bypass was used. Real syntax/elaboration and extractor wiring remain unverified here.
* NOT_RUN: full unit aggregate, full integration/acceptance, qualification,
  simulation, formal, coverage, EDA compile. No Verilator was added or executed.

Existing blocker evidence: `/workspace/shared/uvm-ab/RESULTS.txt`.
Contract blocked result (temporary workspace, not a source artifact):
`/tmp/pytest-of-agent/pytest-1/demo0/ws/.svweave/runs/124cf77a-5e7e-4a36-abcf-c8f51ed90f27/result.json`.

## Remaining boundary

A resolved API namespace is not proof of library authenticity. This change recognizes only
an explicit pinned-API-shaped allowlist; wrappers and other UVM methods are not classified.
It does not increase generic-body traversal coverage. List and text capture limits are
independent. There is no runtime factory resolution, config-db matching, phase execution,
or topology reconstruction. A host supporting required bwrap isolation is needed to verify
the real-UVM integration fixture; pure tests cannot substitute for that gate.
