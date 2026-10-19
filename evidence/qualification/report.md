# SVWeave qualification report

```text
SVWeave version: 0.1.0-dev
Source commit: b6ba45c747cb479cc975a5177794171791f994e0 +dirty
Source identity: git HEAD b6ba45c747cb479cc975a5177794171791f994e0; src/ tree sha256 21a97d0c3b543e44c842300ede67756da614ef32f4a357aecf5cec3e7836a81a
Toolchain lock hash: 5c02676b048119d50c2705d65e8d90e906f0f46f3325906e418849a62b3e828c
Qualified platform: Linux x86_64 4.18.0-553.74.1.el8_10.x86_64 py3.12.4 (this host only)
Qualified feature subset: static preview subset in docs/supported_features.md

G0 / G1 / G2 / G3 / G4 / G5: G0=PASS_WITH_OWNER_WAIVER_OPEN / G1=PASS / G2=PASS / G3=PASS / G4=PASS / G5=PASS
P0 passed / failed / blocked / skipped: 920 / 0 / 0 / 0
Real graph integration: PASS (18 tests, real backends, no skips) (probe: DEGRADED)
Real Serena integration: PASS (15 tests, real backends, no skips) (probe: DEGRADED)
Real slang integration: PASS (14 tests, real backends, no skips) (probe: READY)
Editing isolation / approval / recovery: PASS (41 tests, real backends, no skips)
Statement / branch coverage: 93.58% / 87.71%
Critical mutation results: 22/22 killed; survived=[] errors=[]
Benchmark report: .svweave/qualification/bench/20261014T085609Z-s1.0.json (scale 1.0, 500 files, 100374 lines; identity {'git_head': 'b6ba45c747cb479cc975a5177794171791f994e0', 'src_tree_sha256': '21a97d0c3b543e44c842300ede67756da614ef32f4a357aecf5cec3e7836a81a'}; lock 5c02676b048119d50c2705d65e8d90e906f0f46f3325906e418849a62b3e828c)

EDA compile: NOT_RUN
Simulation:  NOT_RUN
Formal:      NOT_RUN
Coverage:    MEASURED (SVWeave software QA only, not RTL coverage)

Known limitations: docs/limitations.md
Semantic deviations: docs/supported_features.md section 4
Security / license review: docs/security.md (client boundary NOT qualified; editing off by default); license legal review NOT_DONE
Evidence paths: .svweave/qualification/report.json, .svweave/qualification/report.md, .svweave/qualification/coverage.json, .svweave/qualification/mutation.json, .svweave/qualification/capability_report.json, .svweave/qualification/bench/20261014T085609Z-s1.0.json, evidence/m0/
Release decision: BLOCKED
```

## Gate details

* **G0: PASS_WITH_OWNER_WAIVER_OPEN**
  * cbm/Serena are DEGRADED within the declared qualified subset; the G0 waiver (owner item g0_waiver) is not signed, so G0 is not a plain PASS
* **G1: PASS**
* **G2: PASS**
* **G3: PASS**
* **G4: PASS**
* **G5: PASS**

## G0: DEGRADED backends

* **cbm: DEGRADED_WITHIN_DECLARED_SUBSET** (recorded cbm SystemVerilog extraction gaps (docs/limitations.md, graph backend); declared in `docs/limitations.md`); failed probe checks: search_graph finds class svw_driver; two run_phase methods extracted; no fixture file parse_unusable
* **serena: DEGRADED_WITHIN_DECLARED_SUBSET** (ADR-012: Serena symbol edits are qualified for SystemVerilog `function` bodies only; declared in `docs/adr/ADR-012-serena-sv-edit-subset.md`); failed probe checks: find_symbol svw_driver/run_phase (task method)

## Open owner sign-offs

* [OPEN] `g0_waiver`: G0 waiver: cbm/Serena are DEGRADED (not READY) within the declared qualified subset - not signed in evidence/owner_signoff.json
* [OPEN] `adr_012`: ADR-012 owner acceptance (Serena symbol edits limited to function bodies) - not signed in evidence/owner_signoff.json
* [OPEN] `license_review`: license / third-party notices legal review (license_manifest.json legal_review_status) - not signed in evidence/owner_signoff.json; license_manifest legal_review_status=NOT_DONE

## Release blockers outside G0-G5

* OPEN owner item g0_waiver: G0 waiver: cbm/Serena are DEGRADED (not READY) within the declared qualified subset (not signed in evidence/owner_signoff.json)
* OPEN owner item adr_012: ADR-012 owner acceptance (Serena symbol edits limited to function bodies) (not signed in evidence/owner_signoff.json)
* OPEN owner item license_review: license / third-party notices legal review (license_manifest.json legal_review_status) (not signed in evidence/owner_signoff.json; license_manifest legal_review_status=NOT_DONE)

## Why BLOCKED

* G0 PASS_WITH_OWNER_WAIVER_OPEN: cbm/Serena are DEGRADED within the declared qualified subset; the G0 waiver (owner item g0_waiver) is not signed, so G0 is not a plain PASS
* OPEN owner item g0_waiver: G0 waiver: cbm/Serena are DEGRADED (not READY) within the declared qualified subset (not signed in evidence/owner_signoff.json)
* OPEN owner item adr_012: ADR-012 owner acceptance (Serena symbol edits limited to function bodies) (not signed in evidence/owner_signoff.json)
* OPEN owner item license_review: license / third-party notices legal review (license_manifest.json legal_review_status) (not signed in evidence/owner_signoff.json; license_manifest legal_review_status=NOT_DONE)

## Steps

| Step | Result |
|---|---|
| lock verify | rc=0 |
| probe backends | rc=0 {'pyslang': 'READY', 'slang_cli': 'READY', 'cbm': 'DEGRADED', 'serena': 'DEGRADED'} |
| tests/unit | rc=0 passed=700 failed=0 errors=0 skipped=0 |
| tests/contract | rc=0 passed=42 failed=0 errors=0 skipped=0 |
| tests/fault_injection | rc=0 passed=19 failed=0 errors=0 skipped=0 |
| tests/integration | rc=0 passed=147 failed=0 errors=0 skipped=0 |
| tests/acceptance | rc=0 passed=12 failed=0 errors=0 skipped=0 |
| coverage | stmt 93.58% / branch 87.71% |
| mutation | killed 22/22 |
| benchmark (this run) | rc=0 .svweave/qualification/bench/20261014T085609Z-s1.0.json |
| docs | schema rc=0, doc tests rc=0 |

Generated by `scripts/qualify.py` from the actual step results. SKIP, BLOCKED and NOT_RUN are never counted as PASS.
