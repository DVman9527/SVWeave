# ADR-009: v1 has no public HTTP endpoint, no multi-tenancy and no automatic Git push

- Status: Accepted
- Date: 2026-10-13 (M0)

## Context
v1 is a local developer preview (plan §1.4). Upstream servers can start HTTP UIs: codebase-memory-mcp has `ui_enabled=true` by default on port 9749, and Serena's web dashboard is on by default.

## Decision
The gateway uses stdio only. Upstream UIs are disabled in dedicated state: cbm `config set ui_enabled false`, Serena `web_dashboard: False` plus `--enable-web-dashboard false`. There are no network listeners and no push.

## Consequences
Remote and multi-user deployments are out of scope until a later ADR.
