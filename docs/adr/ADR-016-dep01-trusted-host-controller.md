# ADR-016: DEP-01-R2 trusted host controller with isolated install and backends

Status: **acceptance-contract revision authorized; revised acceptance PASS at `cc931de`
in run 40627613557 (2026-10-28)**. See [retained evidence](../../evidence/r2-verilator-run40627613557/README.md).
Revision ID: **`DEP-01-R2-host-controller`**. Full qualification remains **BLOCKED / UNQUALIFIED**.

## Authority and superseded scope

At **2026-10-28 03:17:21 UTC**, after the narrower boundary was described, the user explicitly
instructed: **「改採先前方案 2 然後調整規則」**. This replaces the earlier option 1 decision for
future DEP-01 acceptance only. It does not rewrite the old contract, waive failures, approve
release qualification, authorize a scheduler network exception, or alter publication/approval
requirements.

The old cleanroom controller ran inside an outer network namespace, then tried to create the
mandatory backend sandbox. [Run 40490935466](../../evidence/dep01-nested-denial-run40490935466/README.md)
recorded AppArmor denial of `bwrap` capability 21 / `sys_admin` under `unpriv_bwrap`.
The original DEP-01 requirement remains **BLOCKED**, with its raw JUnit **FAIL** intact.
[Option 1's 234-test pass](../../evidence/remaining-regressions-run40617140613/README.md)
remains evidence for its exact selected scope, not a pass of this revised contract.

## Revised acceptance boundary

| Stage | Required boundary | What is not claimed |
|---|---|---|
| Artifact preparation | Existing explicitly authorized installation/download phase; pinned hashes and local artifact inventory. | No new runtime download permission or dependency upgrade. |
| Cleanroom offline install and verification | Empty destination, pinned local artifacts, `--offline` install and `--verify-only` under network-disabled isolation; versions/hashes must match. Missing artifacts or isolation fail closed. | A host controller is not a reason to resolve dependencies online. |
| Trusted probe controller | The reviewed controller executes on the host with an explicit environment excluding inherited secrets. It coordinates checks/reports and invokes the established backend launcher. | **No outer network-isolation guarantee for this controller.** It is trusted host code, not an air-gapped or end-to-end offline process. No intentional controller downloads, source uploads, arbitrary repository hooks or host HDL parsing are authorized. |
| Every native backend/helper | Mandatory bwrap, read-only source/tool/runtime inputs, no network, no inherited secrets, narrowly writable dedicated state, existing process/output bounds and cleanup. | No raw host backend launch, custom wrapper, weakened environment policy or degraded fallback. |

The only approved reduction is the controller's outer network-isolation guarantee. Do not
change AppArmor profiles, sysctls, capabilities, mandatory sandbox admission, immutable source
rules or backend network policy to make the test pass. Product runtime and Slurm requirements
remain unchanged; the original plan's offline-mode claim must not be applied to this host
controller. No process is entitled to secrets because it runs on the host.

## Revision-aware evidence and tests

The revised integration node is
`tests/integration/test_dep01_cleanroom.py::test_DEP_01_R2_offline_install_then_host_controlled_isolated_backend_probes`.
It must exercise the actual freshly installed backends, not a historical cleanroom summary or
mock. Required failure/skip gates remain fail-closed. Host-controller admission does not make
an unavailable backend acceptable.

Retain the new `.svweave/ci/dep01-r2/` evidence separately from original DEP-01 artifacts:
`contract.json`, `controller-sandbox-preflight.json`, `bootstrap.json`, `capability-report.json`
and the revised JUnit with exact source/lock/workflow identity. Contract provenance explicitly
identifies `revision=DEP-01-R2-host-controller`, `original_contract_status=BLOCKED`,
`controller_network_isolated=false`, `offline_install_verification_network_isolated=true`
and `backend_isolation=mandatory_bwrap`. Do not infer execution success from these declarations;
actual commands/results must substantiate each boundary.

Negative coverage must reject missing isolation, raw/degraded backend launch, inherited
secrets, writable original source and malformed/missing/stale revision evidence. Existing
HDL-CI-04/05 boundary tests remain regression anchors, not substitutes for the revised DEP-01
integration. The old option-1 `remaining_regressions=true` scope is R1-only and must be rejected
under this revision rather than silently deselecting the new R2 node or claiming its pass.

**Revised acceptance passed** at exact source `cc931de898069bed0a51497737d4ad60603e4c51`
in run 40627613557. Its 236 required cases include the actual R2 integration, and 62 boundary
checks include native controller-timeout cleanup. The original denial/failure remain preserved.
This pass verifies only the narrower acceptance boundary, not the original outer-controller
guarantee, a later documentation commit, or full release qualification.
Simulation/formal/RTL coverage, reference-scale performance and owner/license decisions remain
separate.
