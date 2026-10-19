# ADR-004: SVWeave keeps its own SQLite semantic model and does not write to upstream graph databases

- Status: Accepted
- Date: 2026-10-13 (M0)

## Context
codebase-memory-mcp keeps a private graph store, and `query_graph` is read-only. slang facts (instances, ports, binds, scopes) do not map onto its generic Class/Function/CALLS schema. In M0 it labelled SystemVerilog modules as `Class` and did not extract the UVM classes svw_driver and svw_monitor.

## Decision
slang extraction results go into SVWeave's own versioned SQLite schema. The graph backend's results are joined to it through an explicit crosswalk on file path and line range and are tagged as source-level evidence.

## Consequences
There are two stores, keyed by snapshot ID. No SQL is ever written into upstream state directories.
