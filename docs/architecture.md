# Architecture as implemented (through M3)

Layering: thin surfaces (`cli.py`, `server.py`) -> `gateway/` (MCP contract) -> `application/`
(use cases, one function per externally visible operation) -> `adapters/` and `orchestration/`
(backends) and `storage/` (SQLite). The CLI and the MCP tools call the same functions in
`application/services.py`.

```
src/svweave/
  cli.py                  click CLI: parses argv, calls use cases, prints one envelope, exit codes 15.4
  server.py               MCP stdio server (lowlevel SDK Server), logging to stderr, lifespan cleanup
  gateway/
    schemas.py            input/output JSON Schemas of the 12 tools; writes schemas/mcp/*.json
    tools.py              ToolSpec registry (register_tool = M4 hook), validation, dispatch, redaction
  application/
    services.py           one function per use case used by CLI + MCP (status, build, job, search,
                          symbol, references, hierarchy, connections, assertions, diagnostics, evidence)
    session.py            Session: workspace options + lazy BackendRegistry + JobManager (+ close())
    jobs.py               JobManager: background build thread, jobs table, cancel, one build at a time
    status.py             svw_status service, isolation + client-boundary facts
    evidence.py           evidence import (schema/path/hash/link checks) + read + per-kind states
    report.py             `svweave report` (plan 28.2 template)
    debug_bundle.py       `svweave debug bundle`
    events.py             structured events.jsonl writer (timestamp, request/job/build ids, ...)
    keys.py               cursor signing key in the trusted dir
    doctor.py, doctor_wiring.py   doctor engine; backend + client-boundary checks registered here
    build.py, queries.py, semantic_common.py, search.py, toolchain.py, use_cases.py, init.py
                          M1/M2 use cases (manifest, snapshot, slang build, semantic queries, search)
  domain/                 states, error catalog + exit codes, ids, envelope, cursor, locations
  config/                 manifest/policy/local models, loader, trusted dir (policy, cursor key)
  manifest/, snapshot/    profile resolution (ledger) and immutable snapshots
  storage/                SQLite (db.py, migrations v1..v3, semantic.py, gateway_schema.py), locks, GC
  adapters/slang/         pyslang worker (separate process), runner (process-tree cancel), ingest
  adapters/codebase_memory, adapters/serena, adapters/mcp_client.py   upstream MCP clients
  orchestration/          backend registry, health, capability comparison, doctor backend checks
  security/               bwrap sandbox, redaction, safe IO
```

Data flow of `svw_build`: `Gateway.call` validates the arguments -> `services.build` ->
`JobManager.submit_build` (jobs row `QUEUED`, thread) -> `run_build` (manifest -> snapshot ->
worker -> staged publish; row `RUNNING` -> terminal) -> result stored in `jobs.result`, static
evidence recorded. `svw_job` reads the table; `cancel` calls `WorkerHandle.cancel()` which kills the
worker process group.

Storage added in M3 (migration v3): `jobs.{kind, profile, request_id, owner_pid, created_at,
result, cancel_requested}` and `evidence.{evidence_type, profile, test_name, seed, run_id,
tool_version, link_state, link_reason, claimed_snapshot_id, claimed_build_id, record,
imported_at}`.

Trust boundaries: the trusted dir (outside the repo) holds `policy.json` and `cursor.key`; the
repo-local `.svweave/` holds runtime state only (snapshots, DB, runs, bundles, backend state).
Backends are started only through the sandbox runner and only on first real use.
