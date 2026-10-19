# Qualification report template (plan 28.2)

`scripts/qualify.py` fills this template from real results and writes
`.svweave/qualification/report.json` (raw), `.svweave/qualification/report.md` (this template,
filled) and the step logs in `.svweave/qualification/logs/`. A hand-written report is not a
qualification. `svweave report --build B` fills the same template for one build and marks the gates
it cannot evaluate as `NOT_EVALUATED`.

## Template

```text
SVWeave version:
Source commit:
Toolchain lock hash:
Qualified platform:
Qualified feature subset:

G0 / G1 / G2 / G3 / G4 / G5:
P0 passed / failed / blocked / skipped:
Real graph integration:
Real Serena integration:
Real slang integration:
Editing isolation / approval / recovery:
Statement / branch coverage:
Critical mutation results:
Benchmark report:

EDA compile: NOT_RUN | BLOCKED | PASS | FAIL
Simulation:  NOT_RUN | BLOCKED | PASS | FAIL
Formal:      NOT_RUN | BLOCKED | PASS | FAIL
Coverage:    NOT_RUN | BLOCKED | MEASURED

Known limitations:
Semantic deviations:
Security / license review:
Evidence paths:
Release decision: ACCEPTED_STATIC_PREVIEW | BLOCKED
```

## How each field is computed

| Field | Source |
|---|---|
| SVWeave version, source commit | `pyproject.toml`; `git rev-parse HEAD` (reported as `NONE` when the repository has no commit, with `+dirty` when the tree has uncommitted changes) |
| Toolchain lock hash | SHA-256 of `toolchain.lock.json` |
| Qualified platform | the host class the steps ran on (this host only) |
| P0 passed / failed / blocked / skipped | sum over the junit files of the five suites; "blocked" = pytest errors. **SKIP is never PASS.** |
| Real graph / Serena / slang integration | PASS only if the integration modules of that backend ran with no failure and no skip, and the probe is not UNAVAILABLE |
| Statement / branch coverage | `coverage.py` over `src/svweave` with branch measurement, all suites appended; no exclusions added by the runner |
| Critical mutation results | `scripts/mutation_suite.py`: killed/total, survivors listed verbatim |
| Benchmark report | newest `scale 1.0` JSON (this run, else `evidence/bench/`); a reduced-scale run never satisfies the gate |
| EDA compile / Simulation / Formal | `NOT_RUN` unless `evidence/eda/*.json` evidence manifests exist; then `EXTERNAL_DECLARED`, never verified by SVWeave. SVWeave has no EDA runner. |
| Coverage | `MEASURED` refers to SVWeave's own software coverage, never to RTL coverage |

## Gates

| Gate | Pass condition (all must hold, evaluated from the actual results) |
|---|---|
| G0 | `bootstrap_locked.py --verify-only` exits 0; no null/empty field in the lock; probe: pyslang and slang CLI READY, codebase-memory-mcp and Serena not UNAVAILABLE (DEGRADED is recorded as a limitation, not hidden); sandbox isolation tests pass |
| G1 | manifest, snapshot, config, slang-argument, location and repo-profile test modules: no FAIL/ERROR/SKIP |
| G2 | semantic modules (SEM, LOC, SVA, UVM, build identity, extractor DTO, query envelope): no FAIL/ERROR/SKIP, real pyslang and real UVM library |
| G3 | graph, Serena, registry, stdio gateway and API modules plus the MCP contract suite: no FAIL/ERROR/SKIP |
| G4 | edit unit modules, fault-injection suite and real-backend edit flow pass with no skip, and all 22 critical mutations (plan 20.5 plus eleven extra write-safety ones) are killed |
| G5 | every P0 suite has zero FAIL/ERROR/SKIP; acceptance flow passes; statement coverage >= 85 %, branch coverage >= 80 %; docs check (CLI commands, schemas, samples, required files) passes; benchmark on the reference corpus meets the plan 22.3 budgets; README does not claim a license for SVWeave |

A gate computed from a step that did not run is `NOT_EVALUATED`, not PASS. The release decision is
`ACCEPTED_STATIC_PREVIEW` only when every gate is PASS **and** no release blocker is open
(currently: the license legal review status in `license_manifest.json` must not be `NOT_DONE`,
plan 26.3); otherwise `BLOCKED`, with every reason listed. `ACCEPTED_STATIC_PREVIEW` is the plan 26.2
`static-preview-qualified` for the stated feature subset and platform only; it says nothing about
EDA compile, simulation or formal status, and nothing about any SoC project.

## Running it

```bash
.venv/bin/python scripts/qualify.py                       # everything except the benchmark run
.venv/bin/python scripts/qualify.py --benchmark           # also run the scale 1.0 benchmark now
.venv/bin/python scripts/qualify.py --skip mutation,benchmark   # skipped steps => NOT_EVALUATED gates
```

Exit code 0 only for `ACCEPTED_STATIC_PREVIEW`. The full run needs the pinned backends installed
(`docs/install.md`), a host with enough RAM for one backend at a time, and takes a long time because
the integration suites start real backends.
