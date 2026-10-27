# Optional Verilator static JSON inventory

## Scope and trust boundary

This is an **external artifact importer and inventory query**, not an automatic Verilator
backend and not a replacement for slang. It does not run Verilator, build a simulator,
evaluate assertions, or execute HDL. The separately authorized native verification harness
below can test actual static output without changing this importer contract. UVM coverage
gains remain **NOT_RUN / unverified**. Existing mandatory isolation rules are unchanged.

The importer accepts the **5.052** JSON format only. That is a declared format version,
not proof of which executable generated a file. Both artifact hashes identify the exact
imported bytes; they do not authenticate their producer, successful elaboration, or source
configuration. Output is always `EXTERNAL_DECLARED`, `PARTIAL`, and
`runtime_resolution=not_computed`. Snapshot and build association are absent. An imported
filename is an opaque producer label: it is never opened as a source path or used as an edit
location. Imported data does not enter slang declarations/references, rename, impact safety,
published-build state, or EDA/simulation/formal/coverage results.

## Useful information and important limits

The inventory exposes surviving class, member, function/task, type, variable-reference,
module, cell and port AST occurrences, their declared source spans, containment IDs, and a
bounded set of type/pointer attributes. Pointer IDs are artifact-local labels, **not verified
semantic edges**. Some nodes are compiler-generated and can share an original source location.
Name, node-kind and source-label filtering help inspect a second compiler's representation
without conflating it with the authoritative index.

This foundation does **not** establish that Verilator fills a particular slang extraction gap.
It cannot recover source information already erased by elaboration. For example, the official
5.052 `t_json_only_first` golden turns `[WIDTH-1:0]` into a numeric `3:0` range with no symbolic
range subtree. It would be incorrect to infer a resolved source reference to `WIDTH` from that
range. Likewise, missing generic bodies, inactive branches, constraints, runtime factory
choices, config-db hits, virtual dispatch and phase execution are not reconstructed.

## CLI usage

Import artifacts already produced elsewhere (paths are relative to the initialized project):

```bash
svweave supplement verilator --tree artifacts/design.tree.json --meta artifacts/design.tree.meta.json --tool-version 5.052
svweave supplement show --supplement <supplement_id> --name Packet --node-type CLASS --allow-partial
```

The import returns a content-addressed `supplement_id`. Retrieve it later with `show`;
optional `--name` (substring), `--node-type` (exact), and `--source-file` (exact producer label)
filters combine. `--offset` and `--limit` page the stored inventory; `next_offset` signals more
results. The default page is bounded to at most 100 items. A show query exits 3 for its PARTIAL
result unless `--allow-partial` is explicit. Import exit 0 means storage completed, **not** HDL
analysis passed. Both operations retain unbound, external provenance.

Artifacts must pass the workspace trusted-path policy; input symlinks and `..` traversal are
rejected. Raw files and a normalized record are stored under
`.svweave/supplements/verilator/<supplement_id>/`. Retrieval checks content hashes; no imported
paths are followed and no native tool is launched. Deletion/retention is not managed by the
semantic-build cache in this initial slice.

## Input format

Two files are required: a `.tree.json` object rooted at `type=NETLIST`, and a
`.tree.meta.json` object with a nonempty `files` map. AST nodes carry `type`, `name`, `addr`,
`loc`, optional `verilogName`, scalar attributes and child-node lists. Locations use
`fileid,startLine:startColumn,endLine:endColumn`; the end column is exclusive. Each location's
file ID must exist in metadata. Metadata filenames can be absolute or relative because they
are labels only; neither `filename` nor `realpath` is dereferenced.

Unknown format versions, malformed or conflicting duplicate JSON keys, repeated node IDs,
unknown location file IDs and reversed spans fail closed. Identical duplicate keys are
accepted because the pinned upstream golden emits identical `name` keys for some nodes.
Limits are 16 MiB tree, 2 MiB metadata, 100,000 AST nodes, JSON/AST depth 128, 1,000,000 JSON values per artifact, 10,000 inventory items,
and 4,096 characters per captured string. Exceeding a limit is an error, not silent success.

## Evidence and reproducibility

`fixtures/verilator/README.md` records the pinned official source commit and source licensing.
Four byte-identical upstream JSON goldens are covered by checksums and parser tests. Their
metadata companions are **synthetic test adapters**, explicitly marked as such; no native
Verilator was invoked to create or verify these local fixtures.

Verified fixture decoding includes CLASS/FUNC/member/CLASSREFDTYPE,
TASK/TASKREF, REFDTYPE, VARREF, CELL/PIN and packed/unpacked type metadata. METHODCALL handling
is generic node inventory only, not a native integration claim. Parser tests cover malformed
input, conflicting duplicates, resource bounds, unknown versions and no metadata-path reads.
These are unit/interchange tests, not UVM acceptance or design validation.

## Official references

- [Verilator JSON-only options](https://verilator.org/guide/latest/exe_verilator.html#cmdoption-json-only): downstream parser/elaboration output; evolving format. XML options were removed in 5.046.
- [Pinned 5.052 AST and metadata format](https://github.com/verilator/verilator/blob/ea338be98e1e838d3518809ce8899f85a009963c/docs/internals.rst): node fields, location encoding and metadata mapping.
- [Class-language limitations](https://verilator.org/guide/latest/languages.html#class): class support is limited and under active development; it is not a promise of broader support than slang.

## Separately authorized native static verification

`scripts/verify_verilator_native.py` runs the **real** pinned 5.052 executable through
mandatory `SandboxRunner`, first with `--lint-only`, then separately with `--json-only`
and explicit tree/metadata filenames. It imports and queries those exact output bytes.
This is an opt-in test harness, not a new application backend or automatic compiler runner.
The earlier byte-identical upstream goldens and synthetic metadata tests remain interchange
tests; they do not count as native verification.

After explicit authorization, the manual GitHub-hosted `native-verilator` CI job installs
build dependencies and invokes the separate install-phase network boundary
`scripts/bootstrap_verilator.py`. It fetches only the official repository's pinned commit,
checks the commit/tree/archive SHA-256 from `fixtures/verilator/native/pin.json`, and builds
the optimized compiler under network-disabled bwrap. No system-wide Verilator installation,
simulation executable, solver, or UVM runtime is needed. The bootstrap refuses non-GitHub-hosted
environments and does not modify the frozen mandatory backend lock.

```bash
.venv/bin/python scripts/bootstrap_verilator.py --destination "$RUNNER_TEMP/svweave-verilator-5.052" --output evidence/verilator-native-bootstrap
.venv/bin/python scripts/verify_verilator_native.py --verilator-root "$RUNNER_TEMP/svweave-verilator-5.052" --output evidence/verilator-native
```

The `.v/.vh` 1364-2005 hierarchy/macro/parameter fixture, 1800-2017 typedef/class fixture,
missing-module negative and syntax negative are specified in
`fixtures/verilator/native/README.md` (HDL-VLT-01–04). Real JSON source-token and
artifact-local pointer checks are deliberately fixture-specific, not an upgrade of general
imported locations to trusted edit locations. No claim is made about erased source references,
all SystemVerilog classes, assertions, UVM, design correctness, or runtime behavior.

The report records source revision/dirty state, lock/fixture/harness/tool hashes, exact commands,
sandbox argv, version, host, stdout/stderr hashes and native artifacts. `junit.xml` treats missing
prerequisites and invariant failures as failures, never skips. Negative fixture verification can
pass only when the actual compiler rejects with the specific expected error. A passing harness
does not mean the deliberately invalid designs compiled. Simulation/formal/coverage remain
`NOT_RUN`; release qualification remains a separate decision.

Local preparation evidence is in `evidence/verilator-native-preflight/`: the dot host's real
bwrap probe is **BLOCKED** by `NETLINK_ROUTE` permissions and native execution was not attempted
there. Only a subsequently retained CI `report.json` with passing native cases establishes a
native result; the presence of this harness or its unit-test success does not.

## Explicit protected-source preparation (static only)

```bash
svweave supplement prepare-source --source rtl/vendor_ip.sv --allow-partial
```

This **SVWeave lexical preparation step**, not a Verilator flag or runner, reads one explicitly
selected file through trusted-path/no-symlink checks. It writes only a derived `source.sv` and
`report.json` under `.svweave/supplements/prepared/<preparation_id>/`; the original is unchanged.
The response supplies both relative paths and hashes. Pass the derived file to your separately
managed static compiler only after reviewing the report. **No compiler is invoked here.**
The ordinary command exits 3 for PARTIAL; `--allow-partial` permits exit 0 for preparation only.
Neither exit means HDL compile/elaboration passed.

Supported standalone delimiters are `` `pragma protect begin_protected `` /
`` `pragma protect end_protected `` and the legacy `` `protected `` / `` `endprotected ``.
These forms are present in the pinned official
[V3PreLex.l](https://github.com/verilator/verilator/blob/ea338be98e1e838d3518809ce8899f85a009963c/src/V3PreLex.l).
Other forms (including unbounded standalone `data_block` or `pragma protect begin/end`), nested,
unmatched, mismatched, unterminated, inline and macro-continuation boundaries fail closed without
publishing a prepared result. Directives in plaintext comments, strings (including triple-quoted
strings) and escaped identifiers are not boundaries. A boundary cannot open/close a multiline
comment across the skipped region. Payload inside a supported envelope is opaque; it is not
parsed as HDL or decrypted. A standalone boundary line in that opaque payload is reserved as a
delimiter, so ambiguous/vendor-specific envelope formats must not use this preparation path.

Every byte from the opening through closing delimiter is replaced with a space except CR/LF;
plaintext before/after is byte-identical, including when it shares the same file. Original byte
offsets, LF/CRLF endings, line numbers and final-newline presence are retained. A completely
protected file becomes same-sized whitespace **with an explicit gap**, never a silently omitted
input. Bare CR and NUL/binary inputs are rejected. Limits are 16 MiB per file and 100 gaps.

Reports always carry `PARTIAL`, absent snapshot/build association, and `static_compile`,
`simulation`, `formal`, `coverage` = `NOT_RUN`. Each skipped range carries byte/line locations
and `definitions`, `interfaces`, `ports` = `UNKNOWN`; `missing_definitions` and
`encrypted_interfaces` are `UNKNOWN` when gaps exist, not empty “verified” inventories.
Without gaps they are `NOT_CHECKED`, because preprocessing is not semantic analysis. No guessed
blackbox ports, modules or interfaces are created. Plaintext references to encrypted definitions
remain in the output: downstream missing-definition/type/parameter errors must remain errors.
There is no automatic manifest replacement or propagation of this report into imported external
JSON: keep the sidecar alongside any downstream artifact and do not infer completeness from it.

This is not a macro preprocessor: includes and conditional compilation are not evaluated, and
protection in every textual branch is conservatively skipped. Prepare each source/include
explicitly. A new output directory changes relative include lookup; callers must preserve the
original include search context. Macro-generated protection directives are unsupported. Neither
the authoritative slang pipeline nor its mandatory process sandbox is changed or bypassed.
Native-tool/end-to-end qualification remains **NOT_RUN / blocked by the existing host sandbox
restriction**, not covered by these pure preparation/API tests.

### Requirement-to-test mapping

`tests/unit/test_protected_source.py` covers plan A01/4.3 (opaque skip, no decryption), 9.5
(PARTIAL, separate NOT_RUN statuses), plaintext preservation and line/byte mapping, full-file
skip reporting, legacy/multiple regions, string/comment/identifier negatives, malformed and
ambiguous fail-closed cases, original-source immutability, no publish on invalid input, path
containment, deterministic reports and CLI partial-result acceptance. The two synthetic fixtures
under `fixtures/verilator/protected/` contain no licensed IP and are not real encryption samples.
These tests do not establish native Verilator or UVM acceptance.
