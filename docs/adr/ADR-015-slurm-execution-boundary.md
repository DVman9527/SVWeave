# ADR-015: Opt-in Slurm provider with a closed live-execution gate

Status: accepted for offline implementation; scheduler network exception **NOT APPROVED**;
remote provider and site qualification **BLOCKED**.

## Context

Slurm transports/schedules processes; it is not a sandbox or HDL engine. The project currently
forbids runtime network. A local bwrap probe, launcher PID, installed client or successful local
unit test cannot establish compute-node isolation or remote job ownership. See
[support_srun.md](../../support_srun.md), especially §§3–10 and P0/P1 gates.

## Decision

Keep local execution default and preserve its process/stdio safety contract. Add strict
repository-adjacent selection data and operator-owned profiles in the existing trusted policy,
not arbitrary argv or credentials. Expose an offline preflight. Route explicit srun requests
through a fail-closed provider before cached build reuse. Block editing/candidate validation
under srun during P0; do not silently select local.

A dedicated scheduler control-plane exception is conceptually separate from backend networking.
Adding its policy field does not approve it. No existing policy is widened. Current code allows
only an unqualified site query contract and retains a second no-launch guard, so a digest or
boolean cannot unlock remote work. Any future contract and launch path needs reviewed site
qualification evidence and specific execution authorization.

Remote lifecycle must use exact job/step/UID/cluster/attempt ownership, separate control evidence,
bounded stdio, monotonic deadlines, durable attempt records and UNKNOWN-cleanup input pins.
Borrowed allocation cancellation never targets the parent. Local DB/journal/approval state stays
on the coordinator; only immutable validated inputs and bounded dedicated control/output state
may be shared. Remote bootstrap must freshly verify toolchain and bwrap, apply limits and
supervise cleanup. Provenance separates launcher, worker and scheduler accounting.

## Consequences

Offline unit tests can exercise primitives without submitting work, but cannot qualify the
transport or a site. Existing/new allocations, pyslang equivalence and remote MCP lifecycle
remain separate real-cluster gates. P1 is not implied by P0. These changes are not a complete
implementation of the design plan and do not authorize network-policy bypass, daemon setup,
credential creation or cluster jobs. See the [operator/status guide](../srun.md).
