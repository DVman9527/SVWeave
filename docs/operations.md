# Operations

## 1. Read-only onboarding order (plan 15.2)

Editing stays disabled during all of this (and in M3 it does not exist at all).

1. `svweave doctor` (add `--probe` to start each backend once with a real fixture query; slow).
   The output always contains a `client_boundary` item: **client boundary not qualified**.
2. Create the manifest: `svweave init`, then edit `svweave.yaml` (see `docs/manifest.md`).
3. `svweave manifest resolve --profile P` and review the argument ledger by hand: top, file order,
   compilation units.
4. `svweave build --profile P` (exit 3 means errors, PARTIAL or BLOCKED: read the diagnostics with
   `svweave query diagnostics --build B`).
5. Sample queries: `svweave query hierarchy --build B`, `svweave query symbol --build B --name X`,
   `svweave query search --snapshot S --query TEXT`.
6. Confirm graph and Serena work on the same snapshot (`svweave doctor --probe`, `svw_status`).
7. `svweave report --build B --format json` for the qualification report; unevaluated gates say
   `NOT_EVALUATED`.

## 2. Daily flow (plan 15.3)

1. Query the current state: `svw_status` / `svweave query ...`.
2. Rebuild when the workspace changed: `svweave build --profile P` or MCP `svw_build` then `svw_job`.
3. Analyse with the read tools; respect `freshness` and `analysis_coverage`.
4. Import runtime results you obtained in your own EDA environment:
   `svweave evidence import --manifest evidence.json`, inspect with `svweave query evidence --build B`.
5. Editing (disabled by default; see `docs/editing.md`):

6. Before a change lands: `svweave query diff-impact --build B` maps your `git diff` (base `HEAD`, working
   tree) onto the build: changed declarations, modules, instances, dependents. Build from the base
   revision so the lines align; the answer marks each file `aligned` or approximate.
7. Rename: `svweave query rename-preview --build B --name X --to Y` lists every edit (declaration,
   resolved references, named port connections, end labels and bind targets of modules), verified
   against the snapshot, with the diff and the blockers. When it is `ready_for_edit_request`,
   `--write-spec STEM` writes the request under `.svweave/exports/`; `svweave edit prepare --spec`
   then validates it with slang and a human approves it. The preview itself never writes.
8. Draw it: `svweave graph export --build B` writes one self-contained HTML page under
   `.svweave/exports/` (hierarchy plus port-association connections, nothing served, ADR-014).

```text
svweave edit prepare --spec spec.json
svweave edit validate --transaction T
svweave edit approve --transaction T --patch-hash H
svweave edit commit --transaction T --patch-hash H
svweave edit abort --transaction T
svweave edit recover --transaction T
```

## 2a. Opt-in snapshot I/O update

For one large top/profile with a few edits, the first phase can reuse **snapshot materialization**,
not incremental parsing/elaboration or graph-index deltas. It is a CLI-only opt-in; ordinary
`svweave build` and MCP `svw_build` keep their existing behavior and schemas.
Merely upgrading the shared installation does not make MCP builds use update automatically.
Run this CLI against the same authorized workspace as the MCP server. A successful COMPLETE
update uses that workspace's existing database and published pointer; clients can query its
returned `build_id` through the existing tools and freshness rules. This adds neither an MCP
update tool nor an automatic mode, and does not switch an already bound server to another root.

```bash
svweave build --profile P                    # initial ordinary build
svweave update --profile P                   # optional Git advisory defaults to HEAD
svweave update --profile P --base HEAD~1      # changes only the advisory Git comparison
svweave update --profile P --no-git           # same authoritative verification, no Git advisory
```

The donor hint is the **last published COMPLETE snapshot for this profile**, not the most recent
snapshot on disk and not `--base`. With no usable donor, update captures normally. Every current
relevant input is still enumerated and hashed, including dirty, untracked and ignored files,
external roots, includes and library lookup candidates. Lookup membership/order and build
configuration remain part of the existing identities. A clean Git advisory cannot bypass this
work, and a changed-file list never selects a semantic dependency closure.

An exact COMPLETE build can be reused. Otherwise update requests the ordinary **whole-profile**
semantic pipeline with the same sandbox, policy, lock, freshness and publication checks.
Failure or PARTIAL/BLOCKED analysis does not replace the last usable published build; historical
rows, build-bound cursors and edit approval/CAS remain unchanged. The usual build exit codes
apply; exit 0 is not simulation, formal, coverage or design signoff.

### Reading the update report

After successful snapshot capture, the usual build envelope includes request-local `data.update`:

| Field | Meaning |
|---|---|
| `baseline`, `baseline_snapshot_id` | Published-profile donor metadata and availability/reason; unavailable means an optimization could not be used, not that inputs are unchanged. |
| `changes` | Authoritative current-capture comparison with the published snapshot: counts and bounded lists of added, modified, removed or metadata-changed logical paths, plus `config_changed`, `lookup_changed` and `file_order_changed`. With no baseline, `comparison` is `initial_capture`; that is not a historical diff. |
| `materialization` | Actual capture mode, logical hash/copy/clone counters, verification work, attempts, reflink availability, fallback reasons and capture-stage timings. See below. |
| `git` | Optional advisory status, bounded path hints, limitations and unavailable reason. It is not the source of `changes` or cache invalidation. |
| `analysis_scope` | `whole_profile`, including when an exact stored result is reused; no partial elaboration is promised. |
| `semantic_action`, `semantic_status` | `exact_build_reuse` / `reused`, `full_profile_analysis` / `completed`, or `full_profile_analysis_requested` / `no_validated_result`. A request or failed attempt is not proof that the compiler started or completed. |
| `limitations`, `truncated` | Scope limits and omitted report entries. Also inspect nested `changes.truncated`, `git.truncated` and envelope pagination; absence from a bounded list is not proof of no change. |

`changes` compares logical file content/metadata separately from configuration and lookup order;
zero modified files does not prove an identical build. Reports describe this request and do not
rewrite an old build's stored run metadata. Errors before capture retain the ordinary failure
envelope and may have no update report.
Semantic `completed` means the pipeline returned a validated result, which can still be
PARTIAL/BLOCKED; read the envelope completeness, diagnostics and publication state. It is not
design PASS. `no_validated_result` deliberately covers pre-worker failure as well as failed
attempts rather than claiming analysis ran solely because a job or build ID exists.

Materialization modes are `full_copy` (cold path), `reused_existing` (verified full snapshot
already exists) and `delta` (donor-aware path). **`delta` may still copy every file** when reflinks
are unsupported. Reflinks are independent copy-on-write files, never shared writable hardlinks;
new snapshots remain complete trees rather than chains of overlays.

`files_hashed`/`bytes_hashed` count source content-hashing passes; `snapshot_files_verified`/
`snapshot_bytes_verified` count cached/donor-destination verification. These are not unique-file
counts or all filesystem reads: include-directive scans and metadata reads are excluded.
`files_copied`/`bytes_copied` and `files_cloned`/`bytes_cloned` include failed/retried work. Clone
bytes are logical bytes, not measured physical writes or allocated-space savings. Read
`reflink_attempts`, `reflink_skipped`, `reflink_supported`, `fallback_reasons` and `attempts` rather
than inferring clone support from the mode. Timings describe capture, not whole-build speedup.

### Git advisory limits

Git's default base is `HEAD`; `--base` changes only that comparison and never checks out a
revision. `--no-git` reports `DISABLED` and does not evaluate a supplied base. An `AVAILABLE`
advisory combines staged-vs-base differences, conservative unstaged metadata-vs-index hints and
untracked names. Renames are deletion plus addition. Unstaged hints can over-report changes or
miss metadata-preserving edits; untracked hints do not apply repository ignore rules and can
include ignored names. External roots and submodule contents are not covered.

Missing Git, an invalid/unavailable base, an unborn repository, unsafe configuration, unsupported
metadata indirection (including linked-worktree gitfiles), non-UTF-8 names or resource limits can
report `UNAVAILABLE`. This is not an unchanged-files result and does not stop independent snapshot
verification. Repository hooks, filters, fsmonitor, external diff/textconv and config includes
are not executed; no fetch or network access is used. This advisory is separate from
`svweave query diff-impact`, which maps a diff onto a selected build's semantic result.

No speedup is guaranteed, especially on copy-fallback filesystems. Snapshot-only measurements
are not full semantic/graph performance or release qualification. See
[ADR-018](adr/ADR-018-snapshot-io-update.md) for requirement/test mapping and evidence boundaries.

## 3. Evidence manifest

`schemas/evidence.schema.json`; one record or a list. Artifact paths must be inside the trusted
policy roots (relative paths resolve against the manifest file) and their sha256 must match, else
the whole import is rejected. Records are stored as `EXTERNAL_DECLARED`; without a provable
snapshot/build/profile correspondence they are stored `unlinked` and never shown for a build.

```json evidence-manifest
{
  "schema_version": "1.0",
  "kind": "simulation",
  "producer": "external_runner",
  "trust_class": "EXTERNAL_DECLARED",
  "profile": "uvm_smoke",
  "snapshot_id": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
  "build_id": "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
  "test_name": "svw_smoke_test",
  "seed": "12345",
  "run_id": "demo-run-001",
  "tool_version": "TO_BE_RECORDED_BY_RUNNER",
  "result": "NOT_RUN",
  "artifacts": []
}
```

## 4. MCP client configuration (template)

SVWeave never edits a client's configuration. Add an entry like this to your own client (the
JSON shape used by most MCP clients; adapt names to your client):

```json mcp-client-template
{
  "mcpServers": {
    "svweave": {
      "command": "/ABS/PATH/.venv/bin/svweave",
      "args": ["--root", "/ABSOLUTE/PATH/TO/PROJECT", "serve", "--transport", "stdio"]
    }
  }
}
```

Use `--no-graph` in `args` on small hosts (search then uses the literal-text fallback only).
Expose only these tools to the agent; do not also register the raw codebase-memory-mcp or Serena
servers (plan 6.1). A client that has its own shell or file tools can still write the source tree:
SVWeave's guarantees cover operations through its gateway only (plan 23.2).

## 5. Logs and debugging

* Per build: `.svweave/runs/<job-id>/` (plan 24.1), `events.jsonl` with timestamp, request/job/build
  ids, component, event, duration, outcome, error code.
* Per server session: `.svweave/runs/serve-*/events.jsonl` and `server.log`; stderr mirrors the log.
* `svweave debug bundle --build B` writes `.svweave/bundles/<build>/` with hashes, the normalized
  manifest, a diagnostics summary (no message text), a source inventory (no contents) and a
  `semantic_digest`; paths are redacted. Sending sources is a separate manual decision.
* Local indexing is not "end-to-end offline": snippets returned to a cloud-model MCP client leave
  the machine (plan 23.3).

## 6. Exit codes (plan 15.4)

0 success of that command (not "design PASS"), 2 usage/schema/config, 3 build errors/PARTIAL/
BLOCKED or missing input, 4 backend/resource/timeout, 5 stale/conflict, 6 policy/approval refusal.
Semantic `query` commands additionally exit 5 for a STALE/HISTORICAL build and 3 for a PARTIAL one
unless `--allow-stale` / `--allow-partial` is given; the JSON result carries the state either way.

## Slurm execution boundary

Use the offline `svweave execution-preflight` to inspect the selection/authorization boundary without starting or querying Slurm. Live srun remains BLOCKED; local remains default. Never clear an unknown attempt ledger or cancel a whole borrowed allocation to recover a step. See [Slurm status and operator gates](srun.md).


### Read-only Slurm recovery inspection

Use `svweave execution-recovery --limit 200` to inspect bounded local attempt records and
immutable database references without network access, submission, cancellation, database
mutation or unpinning. It remains available for an unqualified srun selection. Missing/corrupt
records or an unsafe WAL-backed database view fail closed with `RECOVERY_REQUIRED`. An empty
or truncated listing does not prove absence of remote jobs; recorded IDs/cleanup are not fresh
scheduler evidence or cancellation authority. See [recovery boundaries](srun.md#read-only-recovery-inspection).
