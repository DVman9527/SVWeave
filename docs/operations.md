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
