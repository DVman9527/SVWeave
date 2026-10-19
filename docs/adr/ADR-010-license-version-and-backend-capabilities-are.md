# ADR-010: License, version and backend capabilities are release gates

- Status: Accepted
- Date: 2026-10-13 (M0)

## Context
Upstream licenses differ between versions. Serena v1.7.0 is MIT, while Serena main is GPL-3.0-or-later for the application. Capabilities differ too: Serena on Verible lacks task symbols, and cbm cannot parse the UVM package file.

## Decision
A release requires a filled toolchain.lock.json with exact commits and SHA-256 values, a license_manifest.json reviewed for the exact versions, and a capability_report.json produced by scripts/probe_backends.py in which each backend status is computed from fixture checks, never from process liveness.

## Consequences
Upgrading any component means re-running the probe and the license review. M0 state is UNQUALIFIED: cbm=DEGRADED, serena=DEGRADED.
