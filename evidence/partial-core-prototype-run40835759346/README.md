# Constant-expression semantic reuse micro-prototype evidence

[Run 40835759346](https://github.com/DVman9527/SVWeave/actions/runs/40835759346) completed SUCCESS at source **219f9b15ddf4b136b30446b917732195d07ddbda**.

## Result and boundary

**MICRO_PROTOTYPE_PASS; general G0 remains NOT_ESTABLISHED.** This is a source-built experimental slang core patch, not the production pinned pyslang wheel or a shipped incremental SVWeave API.

The exact allowlisted fixture has one top, two leaf definitions, two changed-leaf occurrences (top.u0/top.u2) and one untouched control (top.u1). Only equal-length one-bit constant RHS 0/1 transitions are accepted, for two generations A→B→A. Names, types, declarations, hierarchy, macros and arbitrary input changes are rejected.

Stock full and patched full A/B outputs are byte-identical. Incremental generations exactly match independent fresh A/B/A outputs, including full root AST, each concrete instance body, types, qualified targets, source spans and zero diagnostics. All eleven native assertions are true. Changed native expression counters are 6→8→10; assignment-conversion counters 2→4→6. Control counters stay expressions=3, lookups=2, assignments=1, conversions=1; counters for all unaffected scopes are checked unchanged.

Fourteen command records use mandatory bwrap and have zero exit status, empty failure arrays and empty stderr. Both builds use ASan/UBSan with nonrecovering errors, read-only inputs and network disabled. This is evidence of no sanitizer finding in this bounded test, not proof of absence of all memory defects. Two updates (three snapshots, A→B→A) are not a long-running memory test. No end-to-end speedup was benchmarked.

## Other checks

Fast checks passed (1690 unit pass markers plus 1 skip; the quiet log does not reveal the skip reason), type checking and schema checks passed. Existing isolated regressions passed: native Verilator 5, real-worker semantic/UVM 21, supplement 298, zero failures/errors/skips. These are the existing suites, not Verilator simulation or independent verification of the new C++ invalidation operation. Full required backend/acceptance/mutation qualification was not dispatched. Full release qualification remains UNQUALIFIED.

## Preserved first failure

Run 40835119259 at 8f6af75a6e68c2e349537cbedb141b89db097d2c failed the experimental isolation-guard Python syntax check, before native compilation or HDL backend execution. Its raw report and guard record remain FAIL under prior-guard-failure-run40835119259. Commit219f9b1 corrected only the guard and added a pure syntax regression; core patch and oracle hashes stayed unchanged. That run's fast/native Verilator jobs succeeded, but its overall status remains FAIL.

## Files and provenance

native-core/ contains all 15 original experimental artifact JSON files, unmodified. existing-regressions/ retains raw reports/JUnit from this exact run. audit-summary.json records source/run/artifact identity and scope. SHA256SUMS checks each retained file except itself. No compiler binaries, source archives, private RTL or credentials are redistributed.

This evidence-only addition follows the tested source; its later commit is not represented as having rerun native validation. Production lock, main and package publication are unchanged by this experiment. General dependency invalidation, diagnostic withdrawal, SourceManager generation support, production query/publication integration and performance remain unvalidated.
