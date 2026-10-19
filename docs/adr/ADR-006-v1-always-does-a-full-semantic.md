# ADR-006: v1 always does a full semantic rebuild; incremental elaboration is not promised

- Status: Accepted
- Date: 2026-10-13 (M0)

## Context
Incremental elaboration with slang is complex. M0 measured small costs: demo build 0.12–0.15 s with about 34 MiB peak RSS, UVM 2020.3.1 build 0.6–0.8 s with about 129–132 MiB peak RSS, each in a child process.

## Decision
Every build profile is re-elaborated from scratch for each snapshot. Results are cached by build identity.

## Consequences
This is simple and correct. Cost must be re-measured on a real IP before any scale claim.
