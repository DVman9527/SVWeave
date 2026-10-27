# Verilator external static inventory evidence

Scope: optional import/query foundation, not executed Verilator or automatic UVM gap closure.
Identity and frozen lock digest: `identity.txt`.

| Requirement | Evidence / test | Status |
|---|---|---|
| Static only, no runner | importer has no process or filesystem access; app stores external artifacts only | implementation inspected |
| Pinned interchange | `fixtures/verilator/` official v5.052 byte-identical goldens and SHA256SUMS; metadata explicitly synthetic | unit fixture decoding PASS |
| Class/type/member inventory | `test_SEM_verilator_class_method_and_type_metadata` | PASS |
| Do not infer erased dependencies | `test_SEM_verilator_does_not_reconstruct_erased_width_reference` | PASS |
| Never authenticate imports as resolved semantics | four fixture tests assert PARTIAL/EXTERNAL_DECLARED/not_computed | PASS |
| Resource/malformed/path defenses | `test_SEC_verilator_*` | PASS |
| Real native compiler integration / additional UVM coverage | No Verilator installed or invoked; mandatory sandbox limitation remains unchanged | NOT_RUN |
| Simulation / assertions / formal / coverage | Not requested; no execution stage added | NOT_RUN |

Parser command (2026-10-22):

```
PYTHONPATH=src /workspace/shared/uvm-ab/venv/bin/python -m pytest --no-cov tests/unit/test_verilator_importer.py
```

Result: 34 passed. `ruff check` and focused adapter `mypy` passed during development.
Final aggregate application/CLI checks are reported separately by the integrating task; this
record does not claim a full acceptance or qualification pass.

Plan mapping: 7.4 (real capability cannot be inferred from fixtures), 8.5 (tool differences),
9.1/9.5 (identity and status separation), 10.1/10.4 (raw AST not resolved-reference API),
28 (no false passes). No changes to authoritative builds, references, edits or lock files.

## Final aggregate checks

- `PYTHONPATH=src /workspace/shared/uvm-ab/venv/bin/python -m pytest --no-cov tests/unit/test_verilator_importer.py tests/unit/test_supplement_application.py tests/unit/test_uvm_call_metadata.py tests/unit/test_slang_ingest.py`: **102 passed** (34 importer, 15 application/CLI, 53 existing UVM metadata/ingest). See `final-unit-tests.txt`.
- `/workspace/shared/uvm-ab/venv/bin/ruff check .`: **PASS**, `final-ruff.txt`.
- `/workspace/shared/uvm-ab/venv/bin/mypy`: **PASS**, 113 source files, `final-mypy.txt`.
- `PYTHONPATH=src /workspace/shared/uvm-ab/venv/bin/python -m svweave.cli schema emit --check`: **PASS**, `out_of_sync=[]`, `final-schema.json`.
- `git diff --check`: **PASS**, empty `final-diff-check.txt`.
- Documentation command checks: **2 passed**, `test_gateway_cli.py -k DOC_01` only. No broader protected gateway/integration run was attempted.

Upstream fixture drivers were renamed from `.py` to `.py.txt` as inert provenance data;
bytes remain unchanged and SHA256SUMS keys track their stored filenames. The first repository
Ruff attempt flagged their upstream injected `test` global. Renaming the archival drivers
avoided executing/linting foreign harness code without changing it or adding lint exclusions.
An initial test invocation used nonexistent filenames and collected no tests; corrected final
command and results above supersede that invocation. These are unit/static checks, not native
backend integration, acceptance, or qualification.
