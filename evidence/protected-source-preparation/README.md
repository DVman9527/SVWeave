# Protected-source preparation evidence — 2026-10-23

Base commit: `3528124be2deaf314a6cf39299b6cf9c6c58126d`.
Branch: `feat/uvm-static-call-metadata`. Existing dirty UVM metadata/importer work retained.
Toolchain lock SHA256: `ea20baf9bc2da3cb6b9dc41f0234c67f925ea37c5691b0bfbbb1ff30f86ef5e9`.
No commit/push, install, decryption, native backend execution or simulation performed.

## PASS

Pinned tools: `/workspace/shared/uvm-ab/venv/bin/`; Python tests use `PYTHONPATH=src`.

- `python -m pytest tests/unit/test_protected_source.py tests/unit/test_supplement_application.py tests/unit/test_verilator_importer.py tests/unit/test_uvm_call_metadata.py tests/unit/test_gateway_cli.py::test_DOC_01_documented_commands_exist tests/unit/test_gateway_cli.py::test_DOC_01_edit_commands_are_documented_and_exist tests/unit/test_gateway_cli.py::test_schema_emit_check_covers_mcp_and_evidence_schemas --no-cov`: **121 passed**, see `pytest.txt`.
- `ruff check .`, `mypy`, `python -m svweave.cli schema emit --check`, `git diff --check`: PASS; see `checks.txt`.
- Independent read-only reviewer: 41 focused preparation tests passed. Fixed review findings:
  closing delimiter with unclosed block comment; malformed nested legacy delimiter; additional
  visible delimiter in metadata. Input iteration streams lines to avoid per-line-list amplification.

## FAIL / blocked / NOT_RUN — not hidden by focused results

An expanded selection additionally included `tests/unit/test_gateway_cli.py -k 'not builds'`:
**124 passed, 1 failed**. Existing `test_exit_codes_per_command` expects `doctor` exit 0;
actual exit 4, `BACKEND_UNAVAILABLE`, `binary missing`, `check=backend:cbm_binary`.
This backend availability check was not retried or bypassed, and unrelated code was not changed.

Real native Verilator/slang gateway, three-backend integration, EDA compile, simulation,
formal and coverage validation: **NOT_RUN**. Parent task reports mandatory bwrap gateway
previously blocked on NETLINK_ROUTE; this change does not retry or bypass that restriction.
Pure lexical preparation and imported synthetic/official static artifacts do not prove a design
compiles. No missing backend or denied sandbox action was worked around.

## Interface and limitations

`svweave supplement prepare-source --source rtl/vendor_ip.sv --allow-partial` writes a derived
source plus hashed audit report under `.svweave/supplements/prepared/<preparation_id>/`.
Omit `--allow-partial` for exit 3 (output is still generated). No native runner/compiler flags.
Explicit IEEE begin_protected/end_protected and legacy protected/endprotected only;
malformed/nested/mismatched/ambiguous or unsupported forms fail before publish. Macro/include
expansion is not performed. Preserve include context externally. Gaps and unknown interfaces/
definitions are reported; no blackbox port inference. No automatic manifest/importer association.
See `docs/verilator_static_supplement.md` for scope and requirement-to-test mapping.
