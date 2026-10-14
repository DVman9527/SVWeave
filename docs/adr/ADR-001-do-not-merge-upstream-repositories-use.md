# ADR-001: Do not merge upstream repositories; use process/MCP adapters

- Status: Accepted
- Date: 2026-10-13 (M0)

## Context
SVWeave needs slang, Serena and codebase-memory-mcp, which have independent release cycles, languages and licenses (plan §3, §6). Merging or vendoring them would couple upgrades and mix license obligations.

## Decision
Each backend runs as its own process: pyslang in a separate worker process, Serena and codebase-memory-mcp as MCP stdio servers reached through the official `mcp` Python SDK client. SVWeave does not import Serena internals or copy upstream code. M0 verified this with Serena v1.7.0 and codebase-memory-mcp v0.11.0 over stdio (evidence/m0/serena_spike.json, cbm_spike.json).

## Consequences
Upgrades are driven by toolchain.lock.json plus re-running scripts/probe_backends.py. The adapter layer has to tolerate schema drift, which is detected through per-tool schema SHA-256 values. A process boundary does not settle license questions on its own (see ADR-010).
