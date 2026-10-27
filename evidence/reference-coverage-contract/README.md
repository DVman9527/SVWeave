# HDL-R01 reference coverage contract — 2026-10-27

Implemented feature/scope/query metadata does not expand extraction or certify absence.
New output envelopes are **1.1**; strict 1.0 clients must upgrade output schemas. Historical
1.0 payloads remain readable by the new model. No manifest/config/lock/semantic-store version
was changed. See `docs/supported_features.md` and `docs/mcp_contract.md`.

## Evidence and limits

- `report.json` identifies the base commit, dirty migration sources by SHA-256, lock and host.
- `local_smoke.py` / `local-smoke.txt`: **31 independent assertions PASS** for synthetic DTO
  ingestion, real SQLite storage and common envelope behavior, using system Python 3.12.14 /
  Pydantic 2.13.4. Reproduce with `PYTHONPATH=src python
  evidence/reference-coverage-contract/local_smoke.py`. This is not pinned pytest or a real
  parser/backend run. It never starts slang, Verilator, Serena or the graph backend.
- Independent schema comparison and strict-schema check passed with those system dependencies:
  the exact historical `e7684ad` references output schema rejects the new field, new generated
  schemas validate it, and explicit historical 1.0 payloads still deserialize.
- Pinned fast CI **PASS** at **0525bdc22f981b0c21a1b05ead783f4b50d86de3**:
  [run 40467190687](https://github.com/DVman9527/SVWeave/actions/runs/40467190687)
  (ruff, mypy, schema, unit, contract). That run precedes the dirty **1.1 migration** recorded
  here, so it is not migration verification; a new exact-source CI run is still required.
- Local query smoke stopped during import because `mcp_types` is absent; no dependency was
  installed or stubbed. The mandatory sandbox is independently BLOCKED by NETLINK_ROUTE
  permissions; real targeted semantic/native execution is **NOT_RUN**, pending authorized CI
  dispatch and retained results. No simulation, formal or functional-coverage result is claimed.

## Requirement mapping

- **HDL-COV-01/02**, `tests/unit/test_reference_coverage.py`: 24 parametrized cases for known
  omissions, positive data, old/malformed metadata, project/external unsupported and dropped
  occurrences, library inventory gaps, stale/partial/blocked results, scope restrictions,
  first/final pages, oversized items, bounded impact and rename-diff truncation.
- **HDL-COV-01**, `tests/unit/test_domain.py::test_HDL_COV_01_output_version_requires_strict_schema_upgrade`:
  explicit output version, old strict-schema rejection and new-reader/historical-payload support.
- **HDL-COV-01/02**, `tests/integration/test_reference_coverage.py`: four real mandatory-worker
  cases for dimension/type omissions under COMPLETE, retained exact positive call occurrence,
  inactive/unloaded scopes and project/external unsupported nodes. Hand-reviewed source
  rationale: `fixtures/language/refs_coverage/README.md`. No golden was auto-approved.

These counts describe test definitions, not future execution outcomes. Slang remains semantic
authority; the separate native Verilator harness and imported external inventory cannot fill
these reference gaps or promote completeness/edit eligibility.
