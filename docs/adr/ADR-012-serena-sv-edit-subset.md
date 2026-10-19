# ADR-012: Serena symbol edits are qualified only for SystemVerilog `function` bodies; the rest goes through exact-range patches

- Status: Accepted (M3; qualification of the whole toolchain is still pending)
- Date: 2026-10-13
- Evidence: `docs/m0_spike_report.md` section 5.3 and 6, `evidence/m0/verible_lsp_probe.json`,
  `evidence/m0/serena_spike.json`, `capability_report.json` (Serena `checks`),
  `tests/integration/test_int_serena.py`

## Context
Plan 12.2 defines three edit paths: (1) Serena symbol edit, (2) gateway exact-range patch,
(3) refuse. The M0 probe showed that Serena v1.7.0 on top of the pinned `verible-verilog-ls`
(v0.0-4296-g0f262651) does not see every SystemVerilog symbol:

| SV construct | Verible `documentSymbol` | `find_symbol` | Result |
|---|---|---|---|
| package, class, module | reported (module has LSP kind Method) | found | locatable |
| `function` (incl. `virtual function void build_phase`) | reported, kind Function | found, unique name path `pkg/class/fn` | `replace_symbol_body` works (INT-02) |
| `task` (e.g. `svw_driver::run_phase`) | **not reported** | `[]` | not locatable |
| `function new` (constructor) | **not reported** | `[]` | not locatable |

The plan's original G0 target, the task `svw_driver::run_phase`, is therefore BLOCKED for the
Serena path; the same-name FUNCTION methods `svw_env/build_phase` and
`svw_smoke_test/build_phase` are a real, verified substitute (only the addressed method changes).

## Decision
1. **Serena semantic edit** (`edit_path: "serena_symbol"`) is allowed only when `find_symbol` returns
   exactly one symbol whose name path matches the request, whose kind is in the probe-derived
   qualified set (today: `Function`) and whose body is found verbatim in the candidate file bytes.
   The set is derived from the recorded probe checks in `capability_report.json`
   (`derive_serena_edit_subset`); a kind not proven by a PASS check is not qualified.
2. Everything Serena cannot locate (tasks, constructors, unknown names) or that is of an
   unqualified kind is planned as the **gateway exact-range patch** (`edit_path: "exact_range"`):
   the caller supplies explicit byte offsets and the SHA-256 of the expected old bytes; the
   gateway replaces them in the candidate only if the hash matches. These edits are labelled
   `counted_as_serena_semantic_edit = false` and must never be reported as Serena edits.
3. More than one match for the requested name path (for example the bare name `build_phase`)
   is **refused** with `AMBIGUOUS_SYMBOL` and the scoped candidates; the adapter never picks one.
4. After every Serena edit the adapter hashes the whole candidate and returns the list of changed
   files and the changed byte region. Files other than the target, or a changed region outside the
   symbol's byte span, make the outcome not-ok (`raise_if_surprising()` -> `POLICY_DENIED`); the
   editing layer rejects the candidate (EDT-06 hook).
5. Symbol edits additionally require real process isolation (ADR-011). In degraded isolation the
   adapter refuses `replace_symbol_body` unless the caller explicitly opts in.
6. The capability table is data, not assumption: tool availability and schema hashes come from the
   frozen `capability_report.json` (DEP-02); the editable-kind subset from its recorded fixture
   checks. If a future Verible exposes tasks and the re-run probe records a PASS, the subset
   widens without code changes; until then `task` is exact-range only.

## Consequences
- v1 claims one real SV method replacement through Serena (function bodies). Task bodies are
  editable only through exact ranges, which carry no semantic location guarantee from Serena.
- The adapter writes `verible.filelist` into the candidate root (cross-file references need it).
  Callers diffing the candidate against its snapshot must exclude `GENERATED_FILES`.
- A candidate containing `.serena/` is rejected (repository-controlled Serena configuration).
- Serena's own symbol cache can lag behind exact-range edits made outside Serena. The adapter
  verifies that the returned body matches the file bytes and raises `EDIT_CONFLICT`
  (`serena_stale_view`) instead of editing on a stale view.
