# ADR-013: Tasks and constructors are edited by name through slang (`slang_symbol`), not through Serena

- Status: Accepted by the user request of 2026-10-13. This is a deviation from plan 12.2, which
  names Serena as the semantic edit path.
- Date: 2026-10-13
- Evidence: `tests/unit/test_edit_slang_symbol.py`, `tests/integration/test_edt_slang_symbol.py`
  (real pyslang worker, UVM fixture), mutation M14 in `scripts/mutation_suite.py`

## Context
ADR-012 limits the Serena semantic edit to `function` declarations: Verible does not report
`task` declarations or `function new` constructors, so the plan's own G0 target
`svw_driver::run_phase` could only be edited through `exact_range`, where the caller must supply
byte offsets and the SHA-256 of the old bytes. The slang semantic database already stores, per
build, every task and function (constructors carry `attrs.constructor`) with a name span, a full
declaration span, macro provenance and a `symbol_id`.

## Decision
1. Add the edit kind `slang_symbol` to `prepare` (CLI `svweave edit prepare --spec`, MCP `svw_edit`):
   `{kind, qualified_name | symbol_id, replacement, expected_sha256?}`. `replacement` is the whole
   declaration from the `task` / `function` keyword to `endtask` / `endfunction` inclusive, which is
   exactly what the stored full span covers (verified on a real build; leading qualifiers such as
   `virtual` lie outside the span and are preserved).
2. Resolution fails closed. The build must belong to the transaction's base snapshot,
   be COMPLETE (`PARTIAL_ANALYSIS` otherwise) and the workspace must be CURRENT (`STALE_SNAPSHOT`),
   because absence or ambiguity in a PARTIAL or stale result proves nothing (AGENTS rule 5). Names
   are spelled as slang spells them: `pkg::cls::name` for package and class members,
   `module.name` / `ifc.name` for module and interface members, and a compilation-unit subroutine
   is `name` or `$unit::name` (rooted: it never matches a nested declaration). Matching is on name
   segments split on both `::` and `.`, so the two separators are interchangeable (`top::t` finds
   `top.t`) and a partial suffix (`c::t`) is accepted. Every task or function whose LAST segment
   matches is a candidate and ambiguity is decided over all of them: exactly one must match; none is
   `MISSING_INPUT` (the message states the spelling), several are `AMBIGUOUS_SYMBOL` with scoped
   candidates and nothing is picked (a bare `t` shared by a module, an interface and a class is
   ambiguous). Declarations spelled in a macro body or defined inside a `` `define``, generated
   inputs, paths outside `allowed_paths` / `writable_paths`, read-only roots, symlinks, hardlinks and
   binary files are refused by the same function `symbol_replacement` uses. slang stores an `extern`
   method as its out-of-block definition, so that is what the name edits; a stored span that is only
   a prototype is refused (`extern_prototype`).
3. Containment of the replacement, in four layers; the limits are stated after them.
   (a) Lexical, before anything is written: the base bytes at the stored span and the replacement
   must each be exactly ONE declaration `task|function ... endtask|endfunction` (optional
   `: label`) of the declared kind, checked on the text with comments and string literals masked by
   a small SystemVerilog-aware scanner; the replacement may not start with a qualifier (the stored
   span excludes them and they are preserved), may contain exactly one `endtask|endfunction` token
   and no `endclass`, `endmodule`, `endpackage` etc. (`replacement_shape`).
   (b) Preprocessor, same stage (`replacement_directive`): outside comments and strings the
   replacement may not contain `` `define``, `` `undef``, `` `undefineall``, `` `include``,
   `` `ifdef``, `` `ifndef``, `` `elsif``, `` `else``, `` `endif``, `` `resetall``,
   `` `default_nettype``, `` `timescale``, `` `line``, `` `pragma``, `` `begin_keywords``,
   `` `end_keywords``, `` `celldefine``, `` `endcelldefine``, `` `unconnected_drive``,
   `` `nounconnected_drive``, token pasting (two backticks) or stringification. `` `__FILE__`` and
   `` `__LINE__`` are allowed. A macro INVOCATION is allowed only when the macro is defined in a
   file of the base snapshot and neither its body nor the bodies of the macros it invokes
   (transitively, every definition, depth 16) contain a closing keyword (`endtask`, `endfunction`,
   `endclass`, ...) or a state-changing directive (`` `define``, `` `undef``, `` `undefineall``,
   `` `include``, `` `resetall``, `` `default_nettype``, `` `timescale``, `` `line``, `` `pragma``,
   the keyword, cell and drive directives); conditionals (`` `ifdef`` family) and pasting stay
   allowed inside bodies, the latter because the span-equality check (d) covers it; an unknown macro (for example one from a command-line `+define+`) is refused.
   (c) A given `expected_sha256` must match the current bytes (`EDIT_CONFLICT` otherwise).
   (d) Slang-backed, after the candidate's validation build of each profile: the target must
   still exist exactly once with the same qualified name, kind and constructor flag; its full
   declaration span in the candidate must equal EXACTLY the replacement text (candidate
   coordinates, shifted by earlier edits of the same file); and the multiset of (qualified name,
   kind, file) of every declaration outside the target's own span must be identical between the
   baseline and the candidate build. Otherwise the candidate fails validation with
   `slang_symbol_scope_escape` (`POLICY_DENIED`, not auto-committable, so it cannot be approved).
   The planner makes the edit an exact-range edit: the SAME CAS, candidate, validation, approval,
   commit, journal and recovery path. There is no parallel write path.

   What this does NOT contain: a macro invocation of an existing, closer-free macro is opaque text;
   what it expands to (other than "no closing keyword") and what the statements of the body do are
   covered only by the slang diagnostics, the diff and the human review. Declarations whose name
   lies inside the target span (arguments, locals) are not compared. The scanner and the macro
   table are lexical: `` `define`` bodies are scanned textually after comments and strings are masked (a closing
   keyword or directive spelled by token pasting inside a body is not seen; the span-equality check
   is the backstop), and conditional compilation in the base files is not evaluated.
4. The plan record states `edit_path: "slang_symbol"`, `counted_as_serena_semantic_edit: false` and
   `resolved_symbol {symbol_id, qualified_name, kind, constructor, file, start_byte, end_byte,
   build_id}`; the approval prompt prints it. The approval binds the patch hash, which covers the
   resolved range through the diff and the per-file before/after hashes, so a different resolution
   cannot reuse an approval.
5. A `slang_symbol` edit and a Serena edit in the same file are refused, like any mix of Serena and
   gateway edits.

## Consequences
- Tasks and constructors can be edited without computing byte offsets. This is not a Serena
  semantic edit and does not count toward it.
- ADR-012 is unchanged for the Serena path (function only). The Serena backend remains DEGRADED, so
  the G0 waiver is still needed.
- The semantic location comes from slang, the same tool that validates the candidate; there is no
  independent second locator. The post-edit slang validation and the human diff review remain the
  safety net. Validation is static only (EDA, simulation and formal stay `NOT_RUN`).
- Residual gaps: see the end of item 3. A target that is absent from the baseline of an additional
  validate profile fails that profile closed.
- M14 to M22 protect the resolver's build checks, ambiguity refusal, replacement shape, the
  scope-escape check, the base-span shape check, the symbol-kind check, the directive refusal, the macro-body directive refusal and
  the span-equality check.
