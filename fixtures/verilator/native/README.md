# Native static verification fixtures (HDL-VLT-01–05)

These are original, hand-reviewed SVWeave sources, not upstream JSON goldens.
They are deliberately small. A successful harness run verifies only the listed
static subset, not full language compatibility, simulation or UVM acceptance.
No expected JSON is manufactured: the harness must obtain both AST and metadata
from the actual pinned executable before calling the importer and application query.

| ID | Profile | Positive/negative expectation |
|---|---|---|
| HDL-VLT-01 | `1364-2005`; `native_legacy.v`, included `native_defs.vh`; top `native_legacy_top`; one CU, no command-line defines | Lint and JSON succeed. Header macro selects WIDTH=4. Macro-free module/parameter/leaf-input tokens match original source; macro-affected cell/top-port columns explicitly do **not**. Cell points to specialized leaf in this artifact. Input dtype range is `3:0`; no reconstructed symbolic WIDTH reference. |
| HDL-VLT-02 | `1800-2017`; `native_class.sv`; top `native_class_top`; one CU, no command-line defines | Lint and JSON succeed. `nibble_t`, `Packet`, `payload`, and `get_payload` inventory and exact source tokens survive; field/function containment is correct. METHODCALL count is observational, not a required coverage claim. No expected runtime result, dynamic dispatch or factory assertion. |
| HDL-VLT-03 | `1800-2017`; `native_missing.sv`; top `native_missing_top` | Both lint and JSON reject the undefined `missing_unit` with a specific `%Error`; arbitrary failure, signal, warning, isolation denial or missing executable cannot count as expected rejection. No blackbox or inferred ports. Failed compilation output is never imported. |
| HDL-VLT-04 | `1800-2017`; `native_syntax.sv`; top `native_syntax_top` | Both modes reject the deliberately malformed module header with `%Error` and `syntax error`. Failed compilation output is never imported. |
| HDL-VLT-05 | `1364-2005`; `native_plain.v`; top `native_plain_top`; no macros | Lint and JSON succeed. Macro-free cell, modules and leaf-input exact original source tokens match; cell pointer names this artifact's leaf. This is the positive counterpart to HDL-VLT-01's macro-location negative. |

For the three valid cases, real emitted files are parsed, stored using
`import_verilator`, and retrieved using `show_verilator`, including an empty negative
name filter. Every envelope stays `EXTERNAL_DECLARED`, `PARTIAL`, snapshot/build-unbound,
with no authoritative database created. The empty filter is **not** proof that a
source construct is absent. Native source-location checks are fixture-specific and
do not change the general importer's unverified source-binding contract.

## Observed macro-column limitation and preserved regression

Actual run [40471175289](https://github.com/DVman9527/SVWeave/actions/runs/40471175289)
on source `d709bb92f22768c95db0f534e93dd90f43670354` built and ran the pinned tool.
Both HDL-VLT-01 native commands succeeded, but the original harness incorrectly
assumed every location addressed original source bytes. In its unchanged actual
tree, `u_leaf` is at line 4 columns 28–34: original bytes are `IVE_WI`, while the
known fixture expansion of `` `NATIVE_WIDTH `` to `4` places `u_leaf` there.
Top ports `d`/`q` likewise report columns selecting `_` in original source.
This is explicit negative evidence against trusting macro-expanded producer columns
as original source/edit locations. It is not an importer defect or a semantic
resolution edge. No location is corrected and no general macro mapper is added.

The byte-identical emitted tree and metadata are retained in
`recorded/run40471175289/`, with hashes, exact source/tool identity and provenance.
The failed report remains failed in `evidence/verilator-native-run40471175289/`.
Pure replay checks the precise mismatch and rejects altered columns, including
columns that would happen to select the original identifier. Such replay is **not**
a new native execution. HDL-VLT-05 adds the separate macro-free positive check.

`pin.json` freezes official v5.052 commit, Git tree and the SHA-256 of
`git archive --format=tar HEAD`, obtained from the pre-existing clean official
checkout. A fresh GHA bootstrap verifies all three before executing build scripts.
This optional pin does not change `toolchain.lock.json` or replace slang.

The verifier copies these sources, mounts them and the tool read-only, grants writes
only to the exact generated output directory, clears the environment and unshares
the network. It limits each command to 120 wall seconds, 90 CPU seconds, 2 GiB virtual
memory and 32 MiB per output file. It never uses `--binary`, `--build`, `--cc`, warning
suppression, degraded isolation, or upstream regression drivers.
