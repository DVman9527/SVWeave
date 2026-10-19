# ADR-002: The gateway is the only supported entry point; raw multi-server access is for diagnostics only

- Status: Accepted
- Date: 2026-10-13 (M0)

## Context
Upstream MCP servers expose tools that SVWeave must not hand to an AI client: codebase-memory-mcp has `delete_project`, `manage_adr` and `ingest_traces`, and Serena has shell, file-creation and project-switching tools (plan §11.2, §12.1).

## Decision
AI clients connect only to the SVWeave MCP gateway. The gateway calls a fixed allow-list of upstream tools whose schemas were frozen by the probe. Connecting a client directly to the upstream servers is reserved for diagnostics.

## Consequences
The gateway must re-validate every upstream response. Tool annotations and hidden tools are not a security boundary (plan §6.4); OS isolation is ADR-011.
