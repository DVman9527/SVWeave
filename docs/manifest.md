# Manifest, filelist dialect and ledger (M1)

Status: implemented in `src/svweave/manifest/` and `src/svweave/config/`. Plan references are to
`SVWeave_plan.md` sections 8, 9 and 23.1. Nothing in this pipeline evaluates, executes or spawns
anything: no `eval`, no shell, no `subprocess`.

## 1. Files and trust

| File | Role | Location |
|---|---|---|
| `svweave.yaml` | project and profile declarations (data only) | repo root |
| `svweave.local.yaml` | host root mappings for external roots (not in Git) | next to `svweave.yaml` |
| `policy.json` | trusted policy, created by `svweave init` | **outside the repo**: `$XDG_CONFIG_HOME` (fallback `~/.config`)`/svweave/workspaces/<workspace_key>/policy.json` |
| `.svweave/manifests/<config_id>.json` | normalized manifest + ledger (generated) | state dir |

* **The trusted policy never lives in the repository.** `workspace_key = sha256(realpath(dir of
  svweave.yaml))[:16]`; `trusted_dir(...)` / `WorkspaceContext.trusted_dir` expose the directory
  (M4 puts the approval store there too). `--policy` may override the path but any path whose
  realpath is inside the project tree (or the manifest directory) is `POLICY_DENIED`. A
  repo-local `.svweave/policy.json` is ignored (doctor warns). `allowed_roots` must be absolute;
  `init` writes the project directory. `project.root` may not leave the manifest directory
  unless the policy lists that exact root (`POLICY_DENIED`). `.svweave/` must not be a symlink
  and must be owned by the current user; the policy file must be owned by the current user.
  `doctor` and `WorkspaceContext` use the same loader (`config/trusted.py::load_trusted_policy`).
* Every model forbids unknown fields. A typo is `INVALID_CONFIG` with the JSON path and (YAML) line.
  Duplicate YAML keys are rejected. Only plain YAML types are constructed.
* A repo profile can only tighten policy (`config/policy.py`): it cannot add roots (including via
  `svweave.local.yaml`), enable editing the policy disabled, drop `require_human_approval` when the
  policy requires it, or list `editing.allowed_paths` outside `policy.writable_paths`. There is no
  network field in `svweave.yaml` at all. Violations are `POLICY_DENIED` / `PATH_NOT_ALLOWED`.
* JSON Schemas are generated from the models: `svweave schema emit` writes `schemas/*.schema.json`,
  `svweave schema emit --check` fails (exit 2) when they are out of sync.

### Roots, logical paths and the path policy

Every path is checked by `WorkspaceContext.resolve`:

1. lexical normalisation (`..` collapsed) must stay inside an allowed root (`policy.allowed_roots`,
   relative to the project root);
2. the real path (symlinks resolved, also for the un-normalised spelling) must stay inside the
   allowed roots **and** inside a declared source root;
3. the file must match the governing source root's `include` globs and not its `exclude` globs.
   For a symlink, the **link target** must satisfy the same include/exclude rules as the link.

Any failure is `PATH_NOT_ALLOWED` (message contains the requested path, never file content).
A `source_roots` entry without `path` is external; its host location comes from
`svweave.local.yaml` (`root_mappings: [{id, path}]`) and it is addressed as `@<id>/relative/path`.
Using an unmapped external root is `MISSING_INPUT` (`reason: root_unmapped`).

Identities use **logical paths**: project-relative POSIX paths, or `@<id>/rel` for external roots.
Host absolute paths are recorded (`original_path`) but are not part of `config_id`/`snapshot_id`.

## 2. Filelist dialect `svweave-filelist-v1`

### Lexical rules

* The file is UTF-8 (optional BOM). Lines end with `\n` or `\r\n`.
* Tokens are separated by whitespace except inside double quotes. A quote may appear anywhere in a
  token (`dir/"a b".sv`, `-DMSG="x y"`). Quotes cannot span lines (unterminated quote =
  `INVALID_CONFIG`). There is no line continuation. Single quotes have no meaning.
* Inside double quotes `\"` and `\\` are escapes; any other backslash is literal.
* Comments: `//` or `#` at the **start of a token** runs to end of line. `a//b.sv` and `x#y` are
  ordinary tokens. A token starting with `/*` is `UNSUPPORTED_OPTION` (no block comments).
* Tokens keep their raw text (quotes included) in the ledger (`raw`).

### Interpretation

| Token | Meaning |
|---|---|
| anything not starting with `-` or `+` | source file (quotes removed, env expanded) |
| `-f FILE` | nested filelist; relative paths **inside it** resolve against the invocation cwd (the profile `cwd`) |
| `-F FILE` | nested filelist; relative paths inside it resolve against that filelist's own directory |
| `+incdir+a+b`, `-I DIR`, `-IDIR` | include directories, in order. No sorting, no de-duplication |
| `+define+N=V+M`, `-DN=V` | macros (see below) |
| `-v FILE`, `-y DIR`, `+libext+.v+.sv` | library file, library directory, library extensions |
| `-l FILE`, `-logfile FILE`, `-nologo`, `-quiet`, `-q` | allowlisted output-only options: dropped and recorded in `ignored_options` |
| any other `-x` / `+x` | `UNSUPPORTED_OPTION` with origin; never dropped silently, build is BLOCKED |

The argument path of `-f/-F/-v/-y/-I` is itself resolved against the base of the file that
contains it. The base of the top-level filelist contents is the cwd (`path_mode: cwd`, like `-f`)
or the filelist's directory (`path_mode: filelist`, like `-F`).

`+libext+` extensions must start with `.`; the profile's `library_extensions` come first, then
filelist additions in order (exact repeats are not added twice).

### Macros

`+define+` is split on `+` outside quotes; the part after the first `=` is the value, taken
**token-exact** (quotes and backslashes kept: `-DMSG="hello world"` has value `"hello world"`, i.e.
macro text that is a string literal). `NAME` (no `=`) has value `None`, `NAME=` has value `""`;
these are different. `-D NAME=V` (separate argument) is not supported. No env expansion is applied
to macro text. Redefining a macro in the same compilation scope with a different value is
`INVALID_CONFIG` (`reason: conflicting_define`, both origins reported); an identical repeat is
kept once in the semantics and logged as `duplicate_identical`. Same rule for `top_parameters`.

### Environment variables

`$VAR` and `${VAR}` are expanded in path-like tokens only. The variable must be in the profile's
`environment_allowlist`, present in the environment, and non-empty; otherwise the build is
`MISSING_INPUT` (`env_not_allowlisted` / `env_not_set`). Expansion never yields an empty string.
Nothing else is read from the environment.

### Substitution and shell syntax (SEC-02)

Backticks, `$(...)` and malformed `${` are rejected anywhere in a token (also inside quotes).
`; | < > &` are rejected when unquoted; inside double quotes they are plain data. Rejection is
`UNSUPPORTED_OPTION` (`reason: shell_syntax`) - nothing is executed. Export a literal filelist
instead of Make/Tcl/shell-generated ones.

### Errors versus blockers

Blockers (`MISSING_INPUT`, `UNSUPPORTED_OPTION`) are **collected**: the resolver keeps going and
raises once with `details.issues[]` (each: `code`, `message`, `origin{file,line,col}`, `chain`,
`reason`, and `path`/`option`/`variable`). Structural problems raise immediately: lexical errors and
bad macro names (`INVALID_CONFIG`), conflicting macros (`INVALID_CONFIG`), path policy
(`PATH_NOT_ALLOWED`), include cycles and depth (`MISSING_INPUT`, BLD-05).

Include cycles are detected by real path: `details.chain` lists the full chain including the
repeated file (`["a.f", "lists/b.f", "a.f"]`). Maximum nesting depth is 32 filelists
(`reason: filelist_depth_exceeded`). Missing sources, filelists, include/library directories and
library files are `MISSING_INPUT`.

## 2b. Using a project that lives elsewhere

To analyse a design kept outside this repository, keep `svweave.yaml` in a separate workspace
directory and attach the project as an **external root**. Steps, in order:

1. `svweave.yaml`: declare the root without a `path`, and address it as `@<id>`. A profile `cwd` of `"."`
   is the workspace directory, which is not inside any declared source root; use `cwd: "@<id>"`.

   ```yaml
   source_roots:
     - {id: "proj", include: ["**"], exclude: ["**/regression_result_*/**"], writable: false}
   profiles:
     rtl:
       cwd: "@proj"
       source_inputs: [{kind: "filelist", path: "@proj/filelist/top.f", dialect: "svweave-filelist-v1", path_mode: "cwd"}]
       environment_allowlist: ["project_home"]   # only if the filelist uses $project_home
       # ...
   ```
2. `svweave.local.yaml` next to it (see `svweave.local.example.yaml`; `schema_version` is required):
   `root_mappings: [{id: proj, path: /abs/path/to/project}]`.
3. **Trusted policy**: every mapped directory must also be added, as an absolute path, to `allowed_roots`
   in the trusted `policy.json` (`$XDG_CONFIG_HOME/svweave/workspaces/<workspace_key>/policy.json`, created
   by `svweave init`, outside the repository). There is deliberately no way to do this from
   `svweave.yaml` or `svweave.local.yaml`, and no CLI command: edit the file yourself. Skipping it gives
   `PATH_NOT_ALLOWED ... outside policy allowed_roots` (the error's `hint` names the file).
4. Export any environment variable a filelist uses (`export project_home=...`) before running `svweave`.

Things that differ from a simulator command line:

* **Filelists are strict.** A `+incdir+` (or `-y`/`-v`) naming a directory that does not exist is
  `MISSING_INPUT`, where VCS silently ignores it. Remove stale entries from the filelist.
* **Simulator options are not accepted.** `-ntb_opts`, `-timescale=`, `+lint=` and the like are
  `UNSUPPORTED_OPTION`; express defines, include dirs and tops in the profile. Two profile fields cover
  the common cases:
  * `language_options: {timescale: "1ns/1ps"}` is the equivalent of the simulator's default timescale.
    Without it, any design where some files carry a `` `timescale `` and others do not reports one
    `MissingTimeScale` error per file lacking it (hundreds, burying real problems).
  * `ignored_options` declares simulator run-time options that do not affect elaboration:

    ```yaml
    ignored_options:
      - {option: "+notimingcheck", reason: "simulation-only; no effect on static elaboration"}
    ```

    This is an explicit, reviewable decision: `option` is the exact token and takes **no argument** (a
    declaration that consumed the next token could swallow a source file); the option still appears in
    the manifest's `ignored_options` ledger (classification `profile_declared`) and the declared names
    are part of the build's `config_id`. Options the resolver interprets or already ignores cannot be
    redeclared (`-f -F -v -y -l -logfile -nologo -quiet -q`, `+incdir+`, `+define+`, `+libext+`, `-D`,
    `-I`), and neither can slang's own options (`-G` and any `--long` option), which change what is
    elaborated.
* **Several tops.** A testbench may be two top-level modules that refer to each other by hierarchical
  name (`tb` using `system.x.y`, with `system` a module of its own): list both in `top_modules`, or
  `CouldNotResolveHierarchicalPath` errors appear for every such reference.
* **Encrypted IP** (`` `pragma protect `` envelopes): slang skips the envelope, so the module's body,
  parameters and ports are unknown. Each envelope is a `protected_envelope` gap (the build is at most
  `PARTIAL`), and a `ParameterDoesNotExist` error naming a module with an envelope in its text is called
  out in the build's reasons as a likely artifact of the encryption. That attribution needs the envelope
  to be in the same file as the module's declaration; an envelope pulled in through `` `include `` or a
  macro is not attributed (a missed label, never a wrong one). The hierarchy below an encrypted module
  is not available.
* **UVM**: map the Accellera library that the installer provides (`.toolchain/uvm-core/src`), not the one
  bundled with a simulator. VCS's copy has `` `protected `` regions and `snps_macros.svp`, which slang
  cannot read, so a build fails inside UVM with errors unrelated to your code.
* **Strictness**: slang follows IEEE 1800. Constructs a simulator tolerates (a class used with `#(...)`
  that has no parameters, an unspecialized `Class::member` on a parameterized class) are errors here.
* A `PARTIAL` build still answers queries, but the `query` commands exit 3 for it unless you pass
  `--allow-partial`.
* In Verilog, `output reg x` yields two symbols (a port and a variable). `query symbol --name` is
  `AMBIGUOUS_SYMBOL`; pick the `symbol_id`. References are attached to the variable, so the port
  symbol shows none: that is not evidence the signal is unused.

## 3. Compilation units and profile-level inputs

* Effective order: profile-declared `include_dirs`/`defines`/`library_*` first, then those found in
  `source_inputs` in encounter order.
* `single`: one unit with all sources. `per_file`: one unit per source occurrence (includes,
  defines and library are shared). `explicit_groups`: one unit per group, in order; groups own their
  sources/include dirs/defines/library and nothing is inherited (profile-level fields must be
  empty). The same file in several groups (or twice in one list) is **never** de-duplicated.
* `generated_inputs`: must already exist (`MISSING_INPUT`, `reason: generated_input_missing`,
  nothing is generated or created). The file's SHA-256 is recorded; `output_hash` must match if
  declared; without `generator_command_id` and `output_hash` the entry is flagged
  `provenance_incomplete` (a warning, never an assumption about how to rebuild it).

## 4. Normalized manifest and ledger

`NormalizedManifest` (also written to `.svweave/manifests/<config_id>.json`):

* `units[]`: `id`, `group`, `sources[]` (`path`, `raw`, `origin`, `base`, `chain`), `include_dirs[]`,
  `defines[]` (`name`, `value`, `raw`, `origin`), `library_files[]`, `library_dirs[]`,
  `library_extensions[]`.
* `top_modules`, `top_parameters`, `generated_inputs`, `ignored_options`, `filelists` (visit
  order, repeats kept), `diagnostic_policy`, `semantic_deviations`.
* `ledger[]`: ordered argument/event log (`seq`, `kind`, `raw`, `unit`, `name`, `value`,
  `resolved`, `base`, `origin{file,line,col,field}`, `chain`, `classification`). Kinds: `source`,
  `include_dir`, `define`, `library_file`, `library_dir`, `library_ext`, `filelist`,
  `ignored_option`, `generated_input`.

`config_id = SHA-256(canonical JSON of NormalizedManifest.semantics())`. The projection contains
unit boundaries, resolved logical paths, include/library order, macro name/value pairs,
top parameters, generated-input declarations and diagnostic policy. It excludes origins, raw text,
whitespace/comments and the profile name. The built-in output-only ignored options (`-l`, `-q`, ...) are
not part of it; the names of options the profile declared ignorable (`profile_declared`) are, as
`declared_ignored_options`, and only when there are any. JSON object keys are sorted; arrays are
never reordered.

Snapshot and build identity: see `snapshot_id_for` (`snapshot/capture.py`) and
`domain/ids.py::compute_build_id`.

## 5. slang adapter input (draft)

`manifest/slang_args.py::to_slang_args(manifest, files_root)` returns an argv **list** (never a
shell string) for the future slang/pyslang adapter: `--single-unit` (single mode), `-I dir`,
`-D NAME=VALUE`, `--top T`, `-G TOP.NAME=VALUE`, `-v`, `-y`, `--libext`, then source files. Paths are
mounted under `files_root` (`<snapshot>/files`, external roots under `_ext/<id>/`). `per_file` is
slang's default and needs no flag. `explicit_groups` raises `CAPABILITY_MISMATCH` here (the adapter writes per-group `-C` files).
Top parameters use slang 12's top-scoped `-G TOP.NAME=VALUE`; `language_options` map to
`--timescale` / `--std` / `--compat vcs` (`language_options.compat: vcs`, off by default, recorded in
`config_id` and `semantic_deviations`). The flag spellings were verified against the pinned slang 12.0.0.

## 6. Snapshot lookup space (provisional) and concurrency

* The captured set is: filelists, sources, library files, generated inputs, plus the lookup
  space. Lookup space = the directory of every explicit input (non-recursive; slang resolves
  `include "x"` relative to the including file first), every `-I` directory (recursive), and every
  `-y` directory (non-recursive, library extensions only). Source directories come first, then
  the `-I` dirs in declared order. This is **provisional**: M2 will compare slang's actual
  dependency set with the captured set and BLOCK any leak instead of relying on this closure.
* The manifest records each filelist's SHA-256 while resolving (`filelist_hashes`). Capture
  verifies the captured bytes of filelists and generated inputs against them; a mismatch is
  `WORKSPACE_UNSTABLE` (exit 5), so a snapshot always corresponds to its `config_id`.
* Only `COMPLETE` builds are published (`Database.stage_build` returns whether the pointer
  moved); `PARTIAL`/`BLOCKED` builds are stored and addressable by `build_id` only.
* `.svweave/lock` (flock): capture holds it shared; GC takes it exclusive and non-blocking and
  deletes nothing (including `.tmp-*` dirs) while a capture or future edit commit holds it
  (`skipped_reason: workspace_busy`). GC keeps the last 3 snapshots (by latest capture order),
  active-job and pending-edit snapshots, snapshots with evidence (by snapshot or by build),
  published snapshots, and snapshots with non-empty `retention_refs`.
