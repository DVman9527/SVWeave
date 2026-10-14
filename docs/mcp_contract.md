# MCP contract (M3 gateway)

Status: implemented in `src/svweave/server.py`, `src/svweave/gateway/`, `src/svweave/application/`.
Plan references: section 14 of `SVWeave_plan.md`. Schemas: `schemas/mcp/<tool>.input.json` and
`<tool>.output.json`, generated from the code by `svweave schema emit` and verified in sync by
`svweave schema emit --check` and `tests/contract/test_mcp_schemas.py`.

## Transport and session

* `svweave serve --transport stdio [--workspace DIR] [--no-graph]` is the only external entry point
  (ADR-002, ADR-009: no HTTP). Framing is done by the official MCP Python SDK (`mcp` 2.3.0).
* The session is bound to ONE workspace given at start. No tool accepts a filesystem root; paths in
  arguments are logical (`scope`, `location.file`) and checked against the snapshot/build.
* stdout carries protocol frames only. While serving, the SDK points fd 1 at stderr, so stray
  output of the process or of children cannot corrupt the stream (test API-04). Logs go to stderr
  and to `.svweave/runs/serve-*/{server.log,events.jsonl}`.
* The pagination-cursor signing key is stored in the trusted directory
  (`$XDG_CONFIG_HOME/svweave/workspaces/<key>/cursor.key`, mode 0600), never inside the repository.
* Exiting the server (stdin EOF) cancels a running build job and shuts every launched backend down.

## Tools

Exactly eleven tools: the ten read/build tools below plus `svw_edit` (registered through
`svweave.gateway.tools.register_tool`; schemas `schemas/mcp/svw_edit.{input,output}.json`, behaviour in `docs/editing.md`;
`prepare` edits are `symbol_replacement`, `slang_symbol` (ADR-013) or `exact_range`). There is
no approve tool over MCP: approval is a trusted local CLI step only.

| Tool | Required input | Optional input | Effect | Annotations |
|---|---|---|---|---|
| `svw_status` | - | `profile`, `probe` (default false) | Backend health, capabilities, current build + freshness per profile, pending edits, isolation, client boundary. `probe:true` starts backends (slow). | not read-only (probe starts processes), idempotent |
| `svw_build` | `profile` | `expected_snapshot_id`, `reuse` | Starts a background job, returns `job_id` immediately. One build at a time. | not read-only, not idempotent |
| `svw_job` | `job_id` | `action` = `status` (default) or `cancel` | Job lifecycle/progress/result; cancel kills the worker process tree. | not read-only |
| `svw_search` | `snapshot_id`, `query` | `scope`, `limit`, `cursor` | Graph candidates (when the graph backend is READY) or literal text fallback. `SOURCE_CANDIDATE` evidence only. | not read-only (graph index cache), idempotent |
| `svw_symbol` | `build_id` + one of `symbol_id`, `location{file,byte}`, `name` | - | Declaration lookup. Several matches -> `AMBIGUOUS_SYMBOL`. | read-only |
| `svw_references` | `build_id`, `symbol_id` | `scope`, `limit`, `cursor` | Resolved and unresolved occurrences. | read-only |
| `svw_hierarchy` | `build_id` | one of `instance_id`/`instance_path`/`top`; `depth`, `limit`, `cursor` | Pre-order instance tree. | read-only |
| `svw_connections` | `build_id` + one of `instance_id`/`instance_path` | `port`, `limit`, `cursor` | Direct formal/actual associations. | read-only |
| `svw_assertions` | `build_id` | `scope`, `kind` (`source`/`bind`/`elaborated`), `limit`, `cursor` | Static SVA inventory; execution status is never represented. | read-only |
| `svw_evidence` | one of `build_id`/`evidence_id` | `type`, `limit`, `cursor` | Static record, imported external records, NOT_RUN/EXTERNAL_DECLARED state per kind. | read-only |

Deviations from the plan table: `svw_symbol` also accepts `name` (matches the CLI), `svw_hierarchy`
and `svw_connections` take `instance_id` (64-hex) or `instance_path` (display path) instead of one
overloaded field. `readOnlyHint` is set only where the tool truly writes nothing; `svw_status` and
`svw_search` are not marked read-only because they can start backend processes or write a derived
index cache. `destructiveHint` is false everywhere; `openWorldHint` is false (no network).
Annotations are hints, not access control (plan 14.3, 23.2).

### Input rules (API-01)

`additionalProperties:false` everywhere; IDs are `^[0-9a-f]{64}$` (job ids are UUIDs); strings have
`maxLength`; `limit` is 1..500 (default 100); `scope` is a relative path without `..`; requests over
64 KiB are refused. Validation happens BEFORE any use case, backend, database or job manager is
touched; the offending value is not echoed back.

## Envelope

Every result (success and failure) is the plan 14.5 envelope, delivered both as MCP
`structuredContent` and as one text block holding the same JSON:

`schema_version, request_id, outcome (OK|PARTIAL|BLOCKED|ERROR), workspace_id, snapshot_id,
build_id, profile, freshness (CURRENT|STALE|HISTORICAL|UNKNOWN), analysis_coverage {completeness,
scope, basis, unresolved_count, limitations}, provenance {backend, toolchain_id, evidence_kind},
data, pagination {limit, next_cursor, truncated}, warnings, error {code, message, details}`.

Client rules: with `PARTIAL`, `STALE`, `HISTORICAL` or `truncated=true`, an item missing from the
page is NOT proven absent. `analysis_coverage` is about the extraction/query scope, not UVM
functional coverage.

### Three kinds of outcome

| Kind | How it appears |
|---|---|
| Protocol error | JSON-RPC error (unknown tool: code `-32602`). No envelope. |
| Tool execution error | `isError:true`, envelope `outcome=ERROR` (or `BLOCKED` without data) and `error.code`. Includes invalid arguments (`INVALID_CONFIG`). |
| Legitimate PARTIAL result | `isError:false`, `outcome=PARTIAL` with usable `data` (text-fallback search, PARTIAL build, truncated page). |

## Error catalog

Codes of plan 14.6 plus `IDEMPOTENCY_CONFLICT` (EDT-05, M4) and `INTERNAL` (unexpected exception;
details only on stderr). CLI exit codes follow plan 15.4. A STALE/HISTORICAL or PARTIAL semantic
result is NOT a protocol or tool error: MCP returns the envelope with its `freshness` and
`analysis_coverage` fields (the CLI maps them to exit 5 / 3 unless `--allow-stale` /
`--allow-partial`).

| Code | Meaning in M3 | CLI exit |
|---|---|---|
| `INVALID_CONFIG` | bad config, schema/argument violation, bad or foreign cursor, bad evidence manifest | 2 |
| `MISSING_INPUT` | unknown build/snapshot/job/evidence, missing file or root mapping | 3 |
| `UNSUPPORTED_OPTION` | tool option the slang adapter cannot honour | 3 |
| `PARTIAL_ANALYSIS`, `AMBIGUOUS_SYMBOL` | partial result / several scoped candidates | 3 |
| `WORKSPACE_UNSTABLE`, `STALE_SNAPSHOT`, `EDIT_CONFLICT`, `RECOVERY_REQUIRED`, `IDEMPOTENCY_CONFLICT` | stale input (`expected_snapshot_id` mismatch) / edit states (M4) | 5 |
| `BACKEND_UNAVAILABLE`, `CAPABILITY_MISMATCH`, `BUILD_TIMEOUT`, `RESOURCE_LIMIT`, `OUTPUT_LIMIT`, `INTERNAL` | backend/resource/timeout; `RESOURCE_LIMIT` also when another build job is active | 4 |
| `PATH_NOT_ALLOWED`, `POLICY_DENIED`, `APPROVAL_REQUIRED`, `APPROVAL_EXPIRED` | policy/approval refusal (also: cancelling a job owned by another process) | 6 |

## Pagination and cursors

* `limit` default 100, max 500; responses are capped at 128 KiB; when capped the page is cut,
  `pagination.truncated=true` and `next_cursor` is set (never invalid JSON). An item that alone
  exceeds the cap becomes a `truncated_item` placeholder and the envelope states it
  (`pagination.truncated=true`, `data.oversize_items`, outcome `PARTIAL`; `error.code=OUTPUT_LIMIT`
  when no item content fits): fetch it by id with a narrower dedicated query, there is no inline
  path above the cap. `svw_symbol` takes `limit`/`cursor` (default 50 candidates).
* `svw_build` / `svw_job` envelopes mirror the true job state at the top level: an active job has
  `outcome=OK` and no `analysis_coverage`; a finished job carries the build's `outcome`,
  `completeness` and `error` (for example `PARTIAL` + `PARTIAL_ANALYSIS`); `FAILED` / `TIMED_OUT`
  are `ERROR`; `CANCELLED` is `BLOCKED`. `data.lifecycle` stays the job lifecycle.
* Structured output is redacted like text: the gateway scrubs host paths and secrets in every
  string of `data` (verbatim `text`/`snippet`/`diff` excepted), `analysis_coverage.limitations`,
  warnings and error details.
* A cursor is `base64url(json).hmac` and carries build (or snapshot) id, a hash of the query
  (tool + normalised parameters, without limit/cursor), the sort name and the offset. It is signed
  with the workspace key. A cursor from another build, another query, another sort order, a
  tampered token or another workspace is rejected with `INVALID_CONFIG` (API-02).
* An empty page past the end carries a warning; it is not a statement about other queries.

## Jobs (`svw_build`, `svw_job`)

`svw_build` validates the profile synchronously and returns `{job_id, lifecycle: QUEUED, ...}`.
The job runs the same build use case as `svweave build` in one background thread. Lifecycle values
are `QUEUED/RUNNING/SUCCEEDED/FAILED/CANCELLED/TIMED_OUT` and are persisted in the `jobs` table;
`SUCCEEDED` only says the worker ran. The result summary (`completeness`, `diagnostic_outcome`,
`published`, `reused`, `counts`, `error`) is in `svw_job` -> `data.result`. A second `svw_build`
for the same profile joins the running job (`already_running:true`); another profile is refused
with `RESOURCE_LIMIT`. There is no retry. `expected_snapshot_id` fails the job with
`STALE_SNAPSHOT` (no build) when the workspace no longer matches. Jobs of a dead gateway process are
marked `FAILED` ("orphaned") at the next status call.

## Honest state

* `svw_status` never launches a heavy probe by default; backends read `NOT_PROBED` unless a probe ran
  in this session, a runtime failure was observed, or `svweave doctor --probe` stored a record.
* Editing exists but is disabled by default (both `svweave.yaml` `editing.enabled` and the trusted
  policy must enable it); `pending_edits` is read from `edit_transactions` and
  `client_boundary.qualified=false` (plan 23.2).
* Evidence: static analysis is recorded automatically; EDA compile, simulation, formal and SVA
  execution are `NOT_RUN` unless imported evidence exists, and imported evidence is only ever
  `EXTERNAL_DECLARED` (no trusted runner exists in v1). Unlinked records are never shown as a
  result of a build.
