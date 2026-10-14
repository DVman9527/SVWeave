# ADR-008: Keep static, runtime and externally declared evidence separate

- Status: Accepted
- Date: 2026-10-13 (M0)

## Context
Static analysis passing is not simulation or formal evidence (plan §1.2, §16).

## Decision
Every result carries an evidence category. SVA found by pyslang, such as p_stall_holds and ap_stall_holds, is reported as static only, and assertion execution stays NOT_RUN.

## Consequences
The schema needs separate status fields. None of them may be collapsed into PASS.
