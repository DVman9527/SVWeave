# Official Verilator static JSON fixtures

Pinned release: v5.052. Commit: ea338be98e1e838d3518809ce8899f85a009963c.
Annotated tag object: efa4927be48e75c3cd08fc848b198d1d9d237f00.
Retrieved via HTTPS git clone of https://github.com/verilator/verilator.git.
No Verilator executable was installed, built, or run to prepare these fixtures.

The `.out`, `.v`, and `.py.txt` files are byte-for-byte official sources from:
https://github.com/verilator/verilator/tree/ea338be98e1e838d3518809ce8899f85a009963c/test_regress/t
SHA256SUMS.json records copied upstream file digests. `.out` files contain actual
upstream golden JSON, not locally fabricated JSON. `.py.txt` files (original upstream `.py`, renamed to inert text) document original
--json-only invocation and comparison against these goldens; do not execute them.

## Licensing
Preserve upstream notices in copied sources. Test drivers carry
LGPL-3.0-only OR Artistic-2.0 notices; associated test HDL carries its own
CC0-1.0 notice. Golden outputs have no embedded notice; they are distributed
as upstream fixtures with associated original source and project dual-license
terms retained here. Included license texts are unmodified upstream copies.

## Metadata provenance
The `.synthetic.tree.meta.json` files ARE HAND-AUTHORED FIXTURE ADAPTERS, NOT
actual Verilator output. No paired metadata goldens were found upstream. They
map file ID `a` to the synthetic `<built-in>` label and file ID `d` to the associated source fixture, established by matching
source locations against HDL. They intentionally omit runtime
pointers, and pointer-field enumeration; they are sufficient only for testing
inventory decoding (not source binding). `realpath` is fixture-relative, not an actual original runtime
path. Shape follows docs/internals.rst (.tree.meta.json Output) and
src/V3FileLine.cpp fileNameNumMapDumpJson. Never claim this metadata was emitted
by a real tool invocation.

## Coverage and limits
- t_constraint_json_only: CLASS Packet, VAR members, FUNC method declarations,
  CLASSREFDTYPE.classp, unpacked array type. Constraint expressions have already
  disappeared; no METHODCALL golden is present.
- t_json_only_tag: TASK + TASKREF.taskp; REFDTYPE.refDTypep, struct/unpacked types.
- t_json_only_first: CELL.modp, PIN.modVarp, VARREF.varp, ports, parameter
  specialization. Source [WIDTH-1:0] becomes BASICDTYPE range 3:0 with empty
  rangep. The original WIDTH reference is erased: never infer a source-level
  dimension dependency from that numeric range. Some CONST nodes preserve
  origParamName, but this does not restore every folded expression.
- t_var_port_json_only: multiple port forms and unpacked dimensions.

## Format references
No formal JSON Schema was located. Official format documentation points to
src/V3AstNodes.cpp dumpJson methods plus generated fields from AST definitions.
https://github.com/verilator/verilator/blob/ea338be98e1e838d3518809ce8899f85a009963c/docs/internals.rst
https://github.com/verilator/verilator/blob/ea338be98e1e838d3518809ce8899f85a009963c/docs/guide/exe_verilator.rst

Root is NETLIST; addr identifies nodes and pointer-valued fields reference addr.
Missing/false booleans are omitted. loc format is
fileid,firstLine:firstCol,lastLine:endCol (end column exclusive).
The --json-only format is evolving and captures final elaborated AST, not an
unmodified parser tree. Module names may be specialized (mod1__W4); origName
retains mod1. addr values are scoped to each dump, not durable source IDs.

METHODCALL is source-supported by AstMethodCall : AstNodeFTaskRef in
src/V3AstNodeExpr.h (fromp child, inherited taskp pointer and argsp children).
No actual METHODCALL JSON golden was found in the pinned release; do not present
handwritten method-call unit samples as genuine Verilator output.
