# First actual native run: partial success, harness location finding

- Run: https://github.com/DVman9527/SVWeave/actions/runs/40471175289
- Source: `d709bb92f22768c95db0f534e93dd90f43670354`
- Lock SHA-256: `ea20baf9bc2da3cb6b9dc41f0234c67f925ea37c5691b0bfbbb1ff30f86ef5e9`
- Official Verilator source: `ea338be98e1e838d3518809ce8899f85a009963c` (5.052).
- Build and every native invocation used mandatory bwrap with network disabled.
- Simulation, formal, coverage and native UVM acceptance: **NOT_RUN**.

`bootstrap-report.json`, `native-report.json`, and `native-junit.xml` are unchanged
copies from the retained GitHub artifact. The overall native report remains **FAIL**:

| Case | Native lint | Native JSON | Original harness/import-query result |
|---|---|---|---|
| HDL-VLT-01 `.v/.vh` | ACCEPTED | ACCEPTED | FAIL: source-token assumption; import/query NOT_RUN in that invocation |
| HDL-VLT-02 `.sv` typedef/class | ACCEPTED | ACCEPTED | PASS, including import/query |
| HDL-VLT-03 missing module | expected REJECTED | expected REJECTED | PASS; no invalid artifact imported |
| HDL-VLT-04 syntax error | expected REJECTED | expected REJECTED | PASS; no invalid artifact imported |

## Finding and correction

In the real tree, the `u_leaf` CELL at line 4 columns 28–34 selects original bytes
`IVE_WI`. The known fixture expansion of `` `NATIVE_WIDTH `` to `4` places `u_leaf`
at those columns. Top ports `d`/`q` also have shifted columns selecting `_` in the
original. The harness's assumption of universally precise original tokens was wrong;
the importer correctly retained these unverified producer locations.

The `.v/.vh` sources and real raw JSON remain byte-identical. The exact tree/metadata
and provenance are in `fixtures/verilator/native/recorded/run40471175289/`.
The revised oracle requires these exact discrepancies, retains unaffected original
token checks and artifact-local pointers, and never rewrites/import-promotes locations.
It adds a separate macro-free cell positive (HDL-VLT-05), **NOT_RUN in this report**.

`replay-guards.log` records 11 passing local pure guard/replay tests, including the actual
native artifacts through import/query. Those are not a new native run and do not change
this run's FAIL or its import-query NOT_RUN. Changed columns (even a range selecting the
original `u_leaf` token) fail the frozen producer-column expectation. Revised end-to-end
verification requires a new approved native CI run.
