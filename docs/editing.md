# Controlled edits (plan 12, milestone M4)

Editing is **disabled by default**. It needs `editing_enabled: true` in the trusted `policy.json`
(outside the repository) AND `editing.enabled: true` in `svweave.yaml`, a source root with
`writable: true` that the policy `writable_paths` cover, and `editing.allowed_paths`.

## The flow

```text
prepare -> validate -> approve (human, CLI only) -> commit -> post-apply verification
```

State machine (`editing/machine.py`, every change is checked against the legal-transition table; an
illegal transition is an `INTERNAL` error):

`PREPARED -> CANDIDATE_READY -> VALIDATING -> VALIDATED -> AWAITING_APPROVAL -> APPROVED ->
COMMITTING -> APPLIED -> POST_APPLY_VERIFIED`; any not-yet-applied state may go to `ABORTED`,
`EXPIRED`, `CONFLICT`, `VALIDATION_FAILED`; `COMMITTING -> RECOVERY_REQUIRED -> APPLIED | ABORTED |
CONFLICT`.

1. **prepare** (`svweave edit prepare --spec spec.json`, MCP `svw_edit` action `prepare`). Input: base
   build id + snapshot id (the build must be COMPLETE and CURRENT, else `STALE_SNAPSHOT` /
   `PARTIAL_ANALYSIS`), `edits[]`, `validate_profiles[]` (the configured `editing.validate_profiles` are a mandatory floor: a request may
   only add profiles), an idempotency `request_key`
   (`schemas/edit.schema.json`). Each edit is either
   * `symbol_replacement {declaration_id, new_body}`: `new_body` replaces the declaration's source
     span from the semantic database. A bare name is never accepted (`AMBIGUOUS_SYMBOL` with scoped
     candidates); a declaration spelled in a macro body is refused;
   * `exact_range {logical_path, start_byte, end_byte, expected_sha256, replacement}`: compare-and-set
     on the original slice;
     for both this and `symbol_replacement`, a range that touches a `` `define `` directive (its
     text, the newline that ends it or the byte before it, wherever on the line it starts) and a
     replacement containing `` `define ``, `` `undef `` or `` `undefineall `` outside comments and
     strings are refused (`macro_definition_edit_unsupported`);
   * `slang_symbol {qualified_name | symbol_id, replacement, expected_sha256?}` (ADR-013): replace the
     whole `task` / `function` / constructor declaration located **by name** in the slang semantic
     database. Exactly one of `qualified_name` or a 64-hex `symbol_id`. Spell names as slang does:
     `pkg::cls::run_phase`, `pkg::cls::new`, `top.mtask`, `ifc.t`, `$unit::gt` (or `gt`); `::` and
     `.` are interchangeable and a scoped suffix (`cls::run_phase`) is accepted. Every task or
     function whose last segment matches counts: several matches (a bare `t` shared by a module, an
     interface and a class, or `build_phase`) are `AMBIGUOUS_SYMBOL` with scoped candidates and
     nothing is picked; none is `MISSING_INPUT`. `replacement` is exactly ONE declaration of the
     same kind, from the `task` / `function` keyword (no leading qualifiers: `virtual` / `static`
     stay outside the span and are preserved) through `endtask` / `endfunction` (optional
     `: label`); the check masks comments and strings, so a tail in a comment does not count, and a
     closer of another construct (`endclass`...) is refused (`replacement_shape`). Outside comments
     and strings the replacement may not contain compiler directives (`define`, `undef`, `include`,
     the `ifdef` family, `timescale`, `pragma`, `resetall`, `default_nettype`, `line`, keyword and
     cell directives), token pasting or stringification (`replacement_directive`); `__FILE__` and
     `__LINE__` are fine. A macro invocation is allowed only if the macro is defined in the base
     snapshot and neither it nor the macros it invokes (transitively) contain a closing keyword or a
     state-changing directive (`define`, `undef`, `include`, `timescale`, `pragma`, ...; conditionals
     are fine); unknown macros are refused. The optional
     `expected_sha256` must hash the current declaration bytes. The build must be the COMPLETE build
     of the base snapshot and the workspace CURRENT (`PARTIAL_ANALYSIS` / `STALE_SNAPSHOT`).
     Macro-spelled declarations, `extern` prototypes (an `extern` method resolves to its out-of-block
     definition, which is what slang stores), generated inputs, files outside `allowed_paths` /
     `writable_paths`, read-only roots, binary files and symlinks are refused with the same checks
     as `exact_range`. The edit then goes through the SAME exact-range CAS, validate, approval,
     commit and journal path. **Validation adds a decisive scope check**: after the candidate's
     slang build the target must keep its qualified name, kind and constructor flag, its declaration
     span must equal exactly the replacement text, and the declarations outside it must be
     unchanged, else `slang_symbol_scope_escape` (`POLICY_DENIED`,
     not committable). Not contained: what an allowed macro invocation expands to beyond "no closing keyword",
     and what the statements do, are covered only by diagnostics, the diff and review; declarations
     inside the target span are not compared. The plan record carries
     `edit_path: "slang_symbol"`, `counted_as_serena_semantic_edit: false` and `resolved_symbol
     {symbol_id, qualified_name, kind, constructor, file, start_byte, end_byte, build_id}`; the
     approval prompt prints it and the approval binds the patch hash, which covers the resolved range
     through the diff and the per-file before/after hashes.

   A candidate directory `.svweave/candidates/<txn>/` is created as a **real byte copy** of the base
   snapshot (no hardlinks, no symlinks). A `function` declaration is replaced by **Serena**
   (`replace_symbol_body`, sandboxed and bound to the candidate) when the Serena capability is
   present; everything else (tasks, constructors, explicit ranges, no Serena) is a gateway
   **exact-range** patch, labelled `edit_path: "exact_range"` (or `"slang_symbol"` when located by
   name, ADR-013) and never counted as a Serena edit (ADR-012). The edit path is recorded per edit. Renames, deletes, new files, binary files, symlinks
   and hardlinked targets are refused (EDT-10, SEC-01). Same `request_key` + same payload returns the
   same transaction; another payload is `IDEMPOTENCY_CONFLICT`.
2. **validate** (`svweave edit validate --transaction T`). Freezes the candidate (tree hash), diffs it
   against the base snapshot (Serena's generated `verible.filelist` excluded) and rejects any file
   outside the declared edits, outside `allowed_paths`, above `max_files_per_patch` /
   `max_changed_lines`, or any new / removed / symlinked / binary file (EDT-06). Builds a snapshot of
   the candidate (same logical paths) and runs the slang build for every validate profile on baseline
   and candidate: both must be `NO_ERRORS` + `COMPLETE` with no new non-waived warning, else state
   `VALIDATION_FAILED` while the diff stays reviewable (EDT-09). A PARTIAL baseline is reported as
   not auto-committable. The result carries the unified diff, per-file before/after sha256, the
   `patch_hash` (sha256 of the canonical patch record), the diagnostics delta and limitations;
   EDA compile, simulation and formal are `NOT_RUN`. The candidate build never moves the published
   pointer.
3. **approve** (`svweave edit approve --transaction T --patch-hash H`): **CLI only, there is no MCP
   approve tool** and the MCP surface has no approval field (the schema forbids unknown fields). It
   requires an interactive TTY on stdin: the diff is shown on stderr and the human types the first
   12 characters of the patch hash. The record binds patch hash, base snapshot id, validate-profile
   set, trusted policy hash, transaction id and an expiry (default 30 min). It is stored in
   `<trusted dir>/approvals/<txn>.json` and signed with HMAC-SHA256 using `<trusted dir>/approval.key`
   (mode 0600). Any change of patch bytes, profile set or policy makes the approval invalid (EDT-04).
   A test harness may supply the typed value through the environment variable
   `SVWEAVE_TEST_CONFIRM_TOKEN` (ignored on the command line). That path is **not qualified** and
   is accepted only when the trusted `policy.json` (outside the repository, set by a human) contains
   `"allow_test_approvals": true` (default `false`); `approve` and `commit` both refuse a
   non-`tty` approval otherwise. Such approvals are recorded with `method: test_token`; results show
   `approval_qualified: false`. The record also binds the resolved `config_id` of every validate
   profile. The approval key must be a regular file owned by the user with mode 0600 and the trusted
   directory (mode 0700) must be outside the project tree, else `POLICY_DENIED`.
4. **commit** (`svweave edit commit --transaction T --patch-hash H`, MCP action `commit`). Verifies the
   approval (missing/forged/expired: `APPROVAL_REQUIRED` / `APPROVAL_EXPIRED`), re-freezes the
   candidate (must equal the approved patch), takes the exclusive workspace lock, re-checks that the
   base snapshot and every validate-profile baseline still equal the workspace (sources, headers,
   include/library search space) and that the toolchain is unchanged (`EDIT_CONFLICT` otherwise),
   writes the **write-ahead journal** `.svweave/journal/<txn>.json` (fsync), then per file: verify
   current hash == before-hash, write a temp file in the same directory, fsync, rename, fsync the
   directory, keep the file mode, update the journal. Re-sending a commit for an applied transaction
   returns the original result and writes nothing (EDT-05).
   Before anything is written the commit also re-resolves every validate profile and requires its
   `config_id` to be unchanged, and re-checks the current path policy (`POLICY_DENIED` when
   `allowed_paths` was narrowed since approval).
5. **post-apply**: the bytes on disk must equal the validated after-hashes, the workspace snapshot id
   must equal the validated candidate snapshot (checked BEFORE any build starts), and the build of
   every validate profile must be `COMPLETE` + `NO_ERRORS`. Only then is it published, the old build
   becomes `HISTORICAL` and the state `POST_APPLY_VERIFIED`. If any check fails the state stays
   `APPLIED` with `post_apply_status: FAILED` and an error that says the source was already written:
   it is never reported as verified, and replaying the commit re-runs the verification. The result lists files changed, patch
   hash, static diagnostics delta and `NOT_RUN` for EDA/simulation.

`svweave edit abort --transaction T` removes the candidate and keeps the audit record; it never
touches applied changes. It takes the workspace lock and is refused with `EDIT_CONFLICT` while a commit
holds it, while the transaction is `COMMITTING`, `RECOVERY_REQUIRED` or `APPLIED`, or while its journal
is unfinished. Idempotency keys are scoped to the workspace: a commit `request_key` cannot be reused for
another transaction and a prepare key of an aborted transaction keeps its payload
(`IDEMPOTENCY_CONFLICT`).

## Interrupted commits (EDT-07/08)

Several file replacements are **not** one atomic transaction. We provide per-file atomic replace, a
journal and a cooperative lock; other editors can observe the intermediate state. While any journal is
unfinished (or a transaction is `COMMITTING` / `RECOVERY_REQUIRED`) every new prepare and commit fails
with `RECOVERY_REQUIRED` (deleting the journal does not lift it).

A crash between writing the journal and recording `COMMITTING` leaves the transaction `APPROVED`
with an unfinished journal; recovery treats that as `COMMITTING` (state moved to
`RECOVERY_REQUIRED` before any file is touched). A successful roll-forward runs the post-apply
verification. The journal is marked resolved only after the state update succeeded.

`svweave edit recover --transaction T` classifies each file: current == after-hash is *applied*,
== before-hash is *not applied*, anything else is a *conflict*. It never overwrites a file whose bytes
it did not write: conflict files keep the human's version and the candidate version is written to
`.svweave/recovery/<txn>/<path>.candidate`. The block is cleared only when every file is consistent
(all applied or none applied) or by an explicit choice: `--roll-forward` (no conflicts), `--rollback`
(restores originals only for files still equal to the after-hash, no conflicts) or `--accept-current`
(keep what is on disk). No `git reset` / `git checkout` is ever used.

## MCP

`svw_edit` takes a discriminated `oneOf` on `action` (`prepare`, `validate`, `commit`, `abort`) with
`additionalProperties: false` per branch. Annotations: not read-only, destructive. It cannot approve.

## Trust boundary (plan 23.2) - read this

SVWeave guarantees the behaviour of operations that go **through the gateway**. The approval store and
its HMAC key live in the user's own configuration directory with the user's UID. **An agent that runs as
the same UID with an unrestricted shell can read the key and forge an approval, or simply edit the
workspace directly.** The HMAC stops forged or edited files from processes that cannot read the key
and the missing approval input stops `approved=true` over MCP; it does not stop a same-UID agent with a
shell. `svweave doctor` reports the client boundary as not qualified, and editing stays disabled by
default. A controlled deployment must give the agent no shell/file tool on the original sources and
the approval store (separate UID, read-only mounts, or an approver on another host), and use a real
terminal for `svweave edit approve`. Prompt-injection text in sources, replacements or tool output is
treated as data (SEC-03): it cannot skip validation, create an approval or change policy.

## Known limits

* Static slang validation only. A passing result says nothing about simulation behaviour.
* Serena symbol edits are qualified for `function` declarations only; Serena's symbol may cover
  qualifiers such as `virtual` that slang's declaration span excludes - those bytes are preserved.
* A Serena edit and an exact-range or `slang_symbol` edit in the same file are refused (split the
  transaction).
* `slang_symbol` edits whole task / function / constructor declarations only (not classes, modules
  or partial bodies), needs a COMPLETE build, and like every edit is validated statically by slang
  only. Its containment checks are lexical (shape, directives, macros) plus a slang comparison of declarations and of the target span; they do not judge what statements or allowed macro invocations do (ADR-013).
* Edits to macro definitions (a `` `define`` directive, including continuation lines) are refused with
  `macro_definition_edit_unsupported`: the text is shared by every expansion and is outside the
  qualified v1 subset. Editing a macro invocation's arguments stays possible.
* A candidate diff that adds or alters diagnostic-control directives (`` `pragma diagnostic``,
  `pragma protect`, `lint_off`/`lint_on`-style comments) is rejected (`diagnostic_control_change`).
* Edits to filelists and generated inputs are refused (they feed the config identity).
* Snapshots of non-base validate profiles are not pinned against `cache gc`.
