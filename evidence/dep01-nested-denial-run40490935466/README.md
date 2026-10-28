# DEP-01 nested-sandbox denial — 2026-10-27

**DEP-01 is blocked by a measured nested sandbox denial; its diagnostic test remains FAIL.**
No policy relaxation or controller-topology change is made or authorized by this evidence.
Full qualification remains **BLOCKED / UNQUALIFIED**.

## Exact-source results

[Run 40490935466](https://github.com/DVman9527/SVWeave/actions/runs/40490935466) used
`4a7a360b455a4dc4db5cffa2b711beb1baf5e266`. Fast CI succeeded; the focused DEP-01 job failed.
[identity.json](identity.json) records clean Git status, lock/workflow/source hashes and
`cleanroom_diagnostic_only="true"`. This is diagnostic evidence, not full qualification.

| Scope | Observed result | Evidence |
|---|---|---|
| HDL-CI-04/05 probe isolation | **40 PASS, 0 failures/errors/skips**: 36 pure guard cases and 4 real native boundary cases. The native cases check read-only inputs/private network/no inherited secret, plus descendant cleanup on timeout/stdout/stderr failure. | [junit-probe-isolation.xml](junit-probe-isolation.xml), `tests/unit/test_ci_probe_isolation.py`, `tests/integration/test_int_probe_isolation.py`. |
| DEP-01 cleanroom | **1 FAIL, 0 errors/skips**, at the nested sandbox preflight. Failure is preserved; environment blockage does not convert the JUnit result to SKIP or PASS. | [junit-dep01-cleanroom.xml](junit-dep01-cleanroom.xml), `test_DEP_01_offline_bootstrap_into_empty_dir_then_probe_all_three_backends`. |
| Current full backend probe / broad suites / mutation / benchmark | **NOT_RUN in this diagnostic-only job**: their workflow conditions explicitly exclude `cleanroom_only`. The 40 passing boundary cases are not a new full-suite result. | `.github/workflows/ci.yml` at the exact SHA above. The artifact's dated `evidence/mutation/2026-10-13.json` is historical carryover, not a new mutation run. |

## Bounded denial evidence

[nested-sandbox-preflight.json](nested-sandbox-preflight.json) records the exact interval:

- Start: **2026-10-27 10:46:27.646820 UTC**.
- Finish: **2026-10-27 10:46:27.717638 UTC**.
- Return code **1**, `ready=false`, inner sandbox `available=false`, bubblewrap 0.9.0.
- Child process label: `bwrap//&unpriv_bwrap (enforce)`; outer invocation retains `--unshare-net`.

The bounded [kernel diagnostic log](nested-sandbox-kernel.log) contains an event at
**10:46:27.708 UTC**, inside that interval: AppArmor **DENIED**, operation `capable`,
profile `unpriv_bwrap`, command `bwrap`, capability **21 / `sys_admin`**. The log also retains
`kernel.apparmor_restrict_unprivileged_userns = 1`; no global user-namespace restriction was
disabled. Namespace maps, process label and mount observations remain in the raw preflight.

The checked official policy in
[`scripts/ci_prepare_bwrap_apparmor.py`](../../scripts/ci_prepare_bwrap_apparmor.py) deliberately
denies child capabilities (`audit deny capability`) under `unpriv_bwrap`. The observed audit
denial, matching label and exact timing support the diagnosis that the retained outer
controller sandbox cannot launch the required inner sandbox under this policy. This is not
evidence of a missing executable or authorization to allow `sys_admin`, change AppArmor,
remove isolation or treat backend execution as successful.

The DEP-01 traceback shows offline bootstrap/hash checks and offline verification had
already passed before `assert preflight["ready"]` failed. The subsequent cleanroom backend
probes did not run. This does **not** complete DEP-01's full offline-install-and-query contract.

## Earlier failed broad run and later fixes remain separate

[Run 40485644533](https://github.com/DVman9527/SVWeave/actions/runs/40485644533) used
`2d26630ab278c22079c0750832cacfcb8d5f00db`, not the current SHA:

- [Historical identity](historical-run40485644533/identity.json) and
  [boundary JUnit](historical-run40485644533/junit-probe-isolation.xml): **34 PASS**, zero skips.
- [Historical backend report](historical-run40485644533/capability-report.json): pyslang/slang
  CLI `READY`; graph/Serena `DEGRADED`. This is not a claim that all backends were fully READY.
- [Historical broad JUnit](historical-run40485644533/junit-required.xml): **235 nodes,
  229 PASS, 6 FAIL, 0 errors/skips**. Failures were API-PAR-02, DEP-01, DIF-I01/02/03 and
  OPI-F4/F10. That run remains failed; it is not a current-source full-suite pass.
- Follow-up review found legacy direct-native/degraded test paths and a direct native
  registry-health compile. Containment/contract fixes were subsequently reviewed; the earlier
  passing boundary tests therefore cannot certify isolation of every broad-suite path.
- [Local pure JUnit](local-diagnostic-pure.xml) records **195 PASS**, zero adverse outcomes,
  for intermediate fixes. It embeds no exact source identity; it is explicitly local pure
  evidence, not native/full-suite acceptance and not attributed to `4a7a360`.
- The updated full integration/fault-injection/acceptance suite has **not passed a rerun**.
  Prior [305e8c3 native Verilator 5 + slang semantic/UVM 21 PASS](../verilator-native-run40474142649/README.md)
  is another bounded, exact-source result and does not supersede either failed run.

## User decision: option 1 selected on 2026-10-28

At **2026-10-27 10:50:39 UTC**, the user was asked to choose between:

1. Preserve the original controller-plus-backend isolation guarantee, keep DEP-01 **BLOCKED**,
   and finish the other authorized regressions without declaring full qualification.
2. Explicitly authorize a narrower contract: trusted host controller without its outer
   network namespace, while keeping offline installation/verification and isolation of
   every backend. This removes the controller's outer network-isolation guarantee; it is
   not an equivalent fix and requires explicit approval before implementation.

The decision was pending when these 2026-10-27 artifacts were captured. On **2026-10-28**, the
user explicitly replied **`1.`** and **`做1.`**, selecting **option 1**: retain the original
controller-plus-backend isolation guarantee, keep DEP-01 **BLOCKED**, and continue other
authorized regressions. **Option 2 was not selected**; removing the controller's outer network
namespace, granting capabilities, relaxing AppArmor or adding degraded fallback is not
authorized by this decision.

A separately scoped remaining-regressions CI path is being prepared with an explicit blocked
ledger for DEP-01. Its new execution/evidence is pending; this decision does not alter any raw
JUnit result, rerun the failed test, waive DEP-01 or make a selected-subset PASS a qualification
PASS. Full qualification remains **BLOCKED / UNQUALIFIED**. Simulation/formal/RTL coverage and
owner waiver/license signoff remain separate and unestablished.

### Follow-on result: option 1 remaining regressions, 2026-10-28

[Run 40617140613 at `4b6454417ca1f92fae5063116f9458cbfbca443d`](../remaining-regressions-run40617140613/README.md)
subsequently passed 234 selected regressions with zero failures/errors/skips. Its identity-bound
ledger accounts for 235 collected as 234 selected + the exact DEP-01 **BLOCKED**, unwaived,
acceptance-unchanged exclusion. This resolves the preparation/pending-run state above, not the
DEP-01 denial or the original acceptance requirement. Raw failure evidence in this directory
remains unchanged; full qualification remains BLOCKED / UNQUALIFIED.

### Later acceptance revision: option 2, 2026-10-28 03:17:21 UTC

The user subsequently instructed **「改採先前方案 2 然後調整規則」**, explicitly authorizing
[DEP-01-R2-host-controller](../../docs/adr/ADR-016-dep01-trusted-host-controller.md) for future
acceptance. Offline install/verify and every backend remain isolated; only the trusted host
controller loses its outer network-isolation guarantee. No AppArmor/sysctl/capability or backend
fallback change is authorized. **Revised acceptance is NOT_RUN pending new exact-source CI**.
This changes the later acceptance contract, not this directory's original raw FAIL/BLOCKED,
audit evidence, option-1 history or hashes; it does not turn either historical run into R2 PASS.

## Retention

[artifact-manifest.json](artifact-manifest.json) records original member paths, source run/SHA,
byte counts and SHA-256. Five current and four explicitly historical raw files are retained,
plus the separately labelled local pure JUnit. Raw JSON/log/JUnit bytes and outcomes are
unchanged. Members marked `copied=false` are not retained here; no dated mutation artifact
is promoted to current evidence. This documentation work performs no backend execution,
policy/topology mutation, commit/push or Library write.
