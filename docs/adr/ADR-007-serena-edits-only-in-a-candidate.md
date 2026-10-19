# ADR-007: Serena edits only in a candidate directory; the gateway applies changes to the work tree

- Status: Accepted
- Date: 2026-10-13 (M0)

## Context
Upstream backends must never write to the user's source (plan §2.2 rule 4).

## Decision
Serena runs with its project set to a candidate copy and its project metadata stored in a dedicated state directory outside the candidate. The gateway diffs the candidate, checks scope against pyslang ranges, and applies an approved patch with CAS. In M0 Serena's `replace_symbol_body` on `svw_env/build_phase` changed only that method in the candidate. The original fixtures were unchanged, verified by SHA-256 manifest, also under bwrap.

## Consequences
Serena's SystemVerilog symbol model depends on Verible. Verible v0.0-4296 does not report `task` declarations as document symbols, so a Serena symbol edit of the task `svw_driver::run_phase` is BLOCKED. Task edits need the gateway's exact-range patch path (plan §12.2).
