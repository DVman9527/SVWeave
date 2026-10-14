# ADR-003: pyslang is the primary semantic extraction path; the slang CLI is for reproduction

- Status: Accepted
- Date: 2026-10-13 (M0)
- Pinned: pyslang 12.0.0 (wheel `cp311-abi3-manylinux_2_27_x86_64`, sha256 3c24dfad…), slang CLI
  12.0.0+545f88b2e (same upstream tag v12.0, so the two are a matched pair)
- Evidence: `scripts/spikes/pyslang_spike.py`, `evidence/m0/pyslang_spike.json`

## Context
The plan (§10) requires build-aware extraction: diagnostics, hierarchy, port actuals, binds, SVA,
class methods, references, dependency files and byte-accurate source mapping. The plan also forbids
guessing the binding API. Every call listed below was run against the pinned wheel. The stubs
(`site-packages/pyslang/*.pyi`) are the API reference.

## Decision
The HDL worker uses pyslang 12.0.0 in a **separate child process**, one build per process, and
returns JSON DTOs. M0 measured peak RSS and wall time per child: demo LANES=2 and LANES=4 builds
0.12–0.15 s, about 34 MiB; UVM 2020.3.1 + fixture 0.59–0.77 s, 129–132 MiB. The slang CLI binary is
used only to reproduce results (plan §10.6) and for `--ast-json` / `--all-deps` cross-checks. The
plan §10.6 command was run and reported `Build succeeded: 0 errors, 0 warnings`.

The contract for the semantic-core implementer follows.

### 1. Building: Driver from an argv list (mirrors the CLI exactly)
```python
import shlex, pyslang
from pyslang import driver as drv, ast, syntax

d = drv.Driver()
d.addStandardArgs()
line = " ".join(shlex.quote(a) for a in ["slang", "-f", "fixtures/demo/filelists/rtl.f",
        "-I", "fixtures/demo/include", "-DSVW_DEMO=1", "--top", "demo_top", "-G", "LANES=4"])
ok = d.parseCommandLine(line, drv.CommandLineOptions())   # takes ONE string; quote each arg
ok = ok and d.processOptions() and d.parseAllSources()
comp = d.createCompilation()          # pyslang.ast.Compilation
sm = comp.sourceManager               # owned by d: keep d alive while any AST object is in use
```
- Parameter overrides use `-G NAME=VALUE`. Relative paths in `-f` files are resolved against the
  process cwd, so set `cwd` explicitly. UVM profile:
  `+incdir+<uvm>/src <uvm>/src/uvm_pkg.sv fixtures/uvm/svw_demo_uvm_pkg.sv fixtures/uvm/svw_uvm_top.sv --top svw_uvm_top`.
- Lower-level alternative (not needed so far): `SyntaxTree.fromFiles(paths, sm, bag)` +
  `Compilation(bag).addSyntaxTree()`.

### 2. Diagnostics
```python
eng = d.diagEngine
for dg in comp.getAllDiagnostics():                 # pyslang.Diagnostic
    sev = eng.getSeverity(dg.code, dg.location)     # pyslang.DiagnosticSeverity
    if sev == pyslang.DiagnosticSeverity.Ignored:   # QUIRK, see below
        continue
    msg = eng.formatMessage(dg); opt = eng.getOptionName(dg.code); code = str(dg.code)
    dg.isError(); dg.location; dg.ranges; dg.args
```
**Quirk:** `getAllDiagnostics()` also returns default-off diagnostics. For UVM it returned 1096
entries, but only 1 had non-Ignored severity: `UnknownEscapeCode` in UVM's
`uvm_config_db_implementation.svh:375`, which is the same single warning the CLI prints. Filter on
`getSeverity`. Results: demo builds 0 diagnostics; UVM 0 errors, 1 warning.

### 3. Hierarchy / instances (generate)
```python
def cb(n):
    if isinstance(n, ast.InstanceSymbol):
        n.hierarchicalPath        # 'demo_top.g_lane[0].u_lane'
        n.definition.name         # 'demo_lane'
        n.body.parameters         # parameter symbols of this specialization
    return ast.VisitAction.Advance           # Skip / Interrupt also available
comp.getRoot().visit(cb)                     # visits every elaborated instance body
inst = comp.getRoot().lookupName("demo_top.g_lane[0].u_lane")   # direct hierarchical lookup
```
LANES=2 gives 2 `demo_lane` instances and LANES=4 gives 4, with paths `demo_top.g_lane[i].u_lane`.
For high-volume walks, `visit(lookup_table={kind: fn})` dispatches by kind in native code (see the
`visit` docstring).

### 4. Port connections (actual expression text and range)
```python
for pc in inst.portConnections:              # ast.PortConnection
    pc.port.name, pc.port.direction, pc.port.type, pc.port.location
    e = pc.expression                        # ast.Expression or None (unconnected)
    rng = e.sourceRange                      # exact span of the actual, inputs and outputs
    if e.kind == ast.ExpressionKind.Assignment and e.syntax is None:
        e_text_node = e.left                 # QUIRK: output actual = Assignment(left=actual, right=EmptyArgument)
    else:
        e_text_node = e
    text = str(e_text_node.syntax).strip()   # 'data_i[lane]'
    e.visit(lambda x: isinstance(x, ast.NamedValueExpression) and refs.append(x.symbol.hierarchicalPath))
```
For `g_lane[0].u_lane`, the `data_i` actual is `data_i[lane]` at bytes 780–792 of `demo_top.sv`. It
references `demo_top.data_i` and `demo_top.g_lane[0].lane`; the genvar is resolved per generate
iteration.

### 5. Bind + SVA
- A bound instance is an ordinary `InstanceSymbol`, for example
  `demo_top.g_lane[0].u_lane.u_stream_hold_sva`, located in `demo_bind.sv`. To get the bind source,
  walk `inst.syntax.parent…` until `node.kind == syntax.SyntaxKind.BindDirective`; then
  `node.sourceRange` is the whole directive and `node.target` is the target text (`demo_lane`). M0
  found no dedicated bind accessor on InstanceSymbol.
- `ast.PropertySymbol`: `p_stall_holds`, one per bound instance, at `stream_hold_sva.sv` offset 269.
- `ast.ConcurrentAssertionStatement`: `.assertionKind` is `AssertionKind.Assert` and `.syntax` is
  `AssertPropertyStatement`. The label comes from `stmt.syntax.label.name`, giving `ap_stall_holds`
  (range 395–443). Lookup by name: `inst.body.find("ap_stall_holds")` returns a
  `SymbolKind.StatementBlock`.

### 6. Classes and same-name methods (UVM)
```python
pkg = comp.getPackage("svw_demo_uvm_pkg")
cls = pkg.find("svw_driver")                 # ast.ClassType; cls.baseClass -> uvm_driver#(...)
m = cls.find("run_phase")                    # ast.SubroutineSymbol
m.hierarchicalPath                           # 'svw_demo_uvm_pkg::svw_driver::run_phase'
m.isVirtual, m.override.hierarchicalPath     # True, 'uvm_pkg::uvm_component::run_phase'
m.location.offset                            # name token: 312 (monitor: 637)
m.syntax.sourceRange                         # 'task ... endtask': driver 307-405, monitor 632-731
```
**Quirk:** `m.syntax` is the Task/FunctionDeclaration *without* leading qualifiers. The range starts
at `task`/`function`, not at `virtual`. Verible/Serena ranges start at `virtual`, so allow for this
when cross-checking spans.

### 7. Location → file + byte offset (incl. macros)
```python
loc = sym.location                           # pyslang.SourceLocation(buffer, offset)
if sm.isMacroLoc(loc):
    orig = sm.getFullyOriginalLoc(loc)       # where the token text lives (macro def or macro arg)
    exp  = sm.getFullyExpandedLoc(loc)       # the macro usage site in the user file
    sm.getMacroName(loc)
sm.getFileName(loc)        # proximate path (relative to cwd), e.g. 'fixtures/uvm/svw_demo_uvm_pkg.sv'
sm.getFullPath(loc.buffer) # absolute pathlib.Path; use this for identity
loc.offset                 # BYTE offset into the buffer
sm.getLineNumber(loc), sm.getColumnNumber(loc)
sm.getSourceText(loc.buffer)   # str; slice its UTF-8 bytes with offsets
```
Example: tokens from `` `uvm_info `` in `svw_driver::run_phase` have an original location in UVM's
`uvm_message_defines.svh` and an expanded location at fixture offset 346 (line 11).

### 8. Dependency / included-file list (files actually read)
```python
for b in sm.getAllBuffers():                 # BufferID list
    kind = sm.getBufferKind(b)               # BufferKind.DesignFile / IncludeFile / Macro
    sm.getRawFileName(b), sm.getFullPath(b), sm.getIncludedFrom(b)
for t in d.syntaxTrees:
    for inc in t.getIncludeDirectives():     # syntax.IncludeMetadata: .path, .buffer, .syntax
        ...
```
Quirks: (a) every `` `include `` creates a **new** BufferID, so UVM produced 18 572 buffers for 169
unique files. Dedupe on `getFullPath`. (b) `-G`/`-D` text and similar create `<unnamed_bufferN>`
buffers, and macro expansions create `BufferKind.Macro` buffers; exclude both. (c) The `-f` filelist
itself (`rtl.f`) appears as a `DesignFile` buffer. Keep it, because it is a real input for snapshot
hashing. Demo dependency set: rtl.f, demo_top.sv, demo_lane.sv, stream_hold_sva.sv, demo_bind.sv,
and demo_defs.svh (IncludeFile, included from demo_top.sv offset 0). This matches the CLI
`--all-deps` output plus rtl.f. `Driver.optionallyWriteDepFiles()` also exists if `--all-deps` is in
argv.

### 9. Reference enumeration
```python
def rcb(n):
    if isinstance(n, ast.NamedValueExpression):
        n.symbol.hierarchicalPath, n.symbol.kind, n.sourceRange
    elif isinstance(n, ast.CallExpression):
        if n.isSystemCall: n.subroutineName
        else: n.subroutine.hierarchicalPath     # resolved static target
    return ast.VisitAction.Advance
pkg.find("svw_env").visit(rcb)               # classes are visited explicitly (they are not instances)
```
For the four fixture classes plus `svw_uvm_top`, this found 60 resolved references. Examples:
`svw_env::build_phase` calls `uvm_pkg::uvm_component_registry#(svw_driver,"svw_driver")::create`;
`svw_uvm_top` calls `uvm_pkg::run_test`. Macro-generated references are included. Classify them by
whether the original or expanded location falls in the user file.

## Missing / not provided by the binding (record, do not emulate)
- No "find all references to symbol X" index. SVWeave must build it from the visitor (§9). It only
  covers elaborated/visited code: uninstantiated generic specializations and inactive generate or
  `ifdef branches are not covered (plan §10.4).
- No dedicated bind accessor on `InstanceSymbol`; use the syntax-parent walk (§5).
- `Driver.parseCommandLine` accepts a single string only, with no argv-list overload, so quoting is
  the caller's responsibility.
- Runtime virtual dispatch targets are not resolvable statically. `override` gives only the
  overridden base method.
- Not exercised in M0: library mapping (`-v`/`-y`/libmap), `--single-unit` versus per-file CU
  semantics in pyslang, and the memory ceiling on a real SoC.

## Consequences
The semantic core can be implemented directly on the APIs above. The HDL worker must serialize DTOs
and never return native objects across process boundaries.
