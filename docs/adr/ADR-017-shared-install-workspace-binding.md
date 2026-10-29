# ADR-017: Shared installation, startup-bound workspace

Status: **implementation decision; developer preview / UNQUALIFIED** (2026-10-29).
Governing plan sections: 2.1/A05, 6.4, 7.2–7.3, 14.2 and 15.1. This adds installation and
startup convenience without changing the one-authorized-workspace-per-session contract.
`SVWeave_plan.md`, frozen toolchain pins, licenses and the 14-tool MCP schemas are unchanged.

## Problem

A user should install the pinned runtime once and register one stable `svweave` MCP entry per
client, then use it in multiple initialized projects. A global entry that embeds one `--root`
is global only in visibility: it still binds all sessions to that one project. Conversely,
trusting an arbitrary client cwd or silently choosing a different project can cross the
workspace's policy boundary. Native-client cwd behavior must not be guessed.

## Decision

- Keep one operator-controlled bootstrap checkout with gateway venv, pinned toolchain and
  immutable metadata. Keep all mutable analysis/backend state, manifests and trusted policy
  scoped to the existing workspace. No per-project dependency installation is needed.
- Export `svweave-mcp` beside `svweave`. The launcher selects a canonical workspace exactly once,
  validates its existing trusted policy and calls the existing stdio server. It does not `chdir`,
  multiplex projects, accept a filesystem root in an MCP tool, or expose a new tool/schema.
- Selection is explicit `--repo`/`--workspace`, then `SVWEAVE_PROJECT_ROOT`, then the
  `CLAUDE_PROJECT_DIR` hint and nearest cwd manifest. Explicit values must name a manifest
  directory; automatic upward search stops at a Git checkout/worktree/submodule boundary.
  Conflicting explicit paths or hint/cwd results fail closed. Invalid nearer manifests are not
  skipped in favor of a parent. Directory symlinks are canonicalized; manifest symlinks fail.
- Selection is not authorization: no auto-init, new trust record, project catalog, `.env` load,
  allowed-root widening, policy override from repository text or bypass of approval/CAS/recovery.
  The existing policy is still checked during normal operations after startup.
- `svweave serve --discover-workspace` opts into the selector. Existing explicit
  `--root`/`--workspace` forms and the default non-discovery CLI behavior remain supported.
- `svweave mcp-config --client claude|codex` is an inert preview: stable name `svweave`, absolute
  launcher and toolchain paths, safely quoted registration/configuration, `applied: false`.
  It never reads/writes real client config or executes registration. Operators review and apply
  it; project/local shadowing must be checked rather than silently overwritten.
- Wheel installs carry frozen metadata/notices and bounded probe fixtures in `svweave/_runtime`.
  Source bootstrap installs retain the shared checkout. Neither discovers a lock in the HDL
  project. Native backends still require a separately provisioned pinned bootstrap root; a
  generic package installation is not a complete backend installation.

## Client contract and limits

The [Claude Code MCP reference](https://code.claude.com/docs/en/mcp) documents user scope and
`CLAUDE_PROJECT_DIR`. The [Codex MCP reference](https://learn.chatgpt.com/docs/extend/mcp?surface=cli)
and [configuration reference](https://learn.chatgpt.com/docs/config-file/config-reference) document
shared user configuration and explicit `env_vars` forwarding, but their published
configuration contract does not promise the default cwd of every MCP process. Therefore the
Codex template forwards `SVWEAVE_PROJECT_ROOT`; the operator can supply it at client startup or
use a fixed explicit `--repo`. Global configuration does not prove automatic project selection
in every host/client mode. One process never follows subsequent client directory changes.

This work does not qualify native Claude/Codex behavior, native backend execution, a Slurm site
or an external `svweave-slurm` wrapper. Local execution remains the default; Slurm remote
execution stays **BLOCKED / UNQUALIFIED**. Existing execution-policy gates are not relaxed.
There are no runtime downloads, raw host backend exceptions, permission changes or new network
permissions. Existing client-shell/same-UID approval limitations remain unchanged.

## Validation boundary

[Run 40806959759](https://github.com/DVman9527/SVWeave/actions/runs/40806959759) completed
SUCCESS at exact tested source **`ca6ebb571137f683a5354a1bd78169341d933de1`**. The full
27-entry critical mutation harness now records **27/27 KILLED**, no survivors/errors, including
M27's three baseline passes → three mutant failures. This supersedes this slice's earlier
pending mutation status, not the historical 22/22 or 26/26 evidence.

[Retained raw proofs and audit](../../evidence/global-workspaces-run40806959759/README.md) also
record real packaging 10 PASS, unit 1552 PASS + 1 SKIP, contracts 49 PASS (including GLB-09),
required native-backend regression 236 PASS, boundary checks 62 PASS, native Verilator static
5 PASS, slang semantic/UVM 21 PASS and supplement gates 298 PASS. Scopes overlap; the one unit
skip is not reclassified. The measured 0.05-scale smoke passes with graph search SKIPPED.

Native **application-client** registration/connection and Slurm site execution remain NOT_RUN;
SDK stdio and backend tests do not establish those claims. The external `svweave-slurm` wrapper
source was not supplied. Full release qualification remains UNQUALIFIED and `qualify.py` was
not run. Results belong to the tested SHA, not a later documentation/evidence commit.

## Requirement-to-test mapping

These are coverage anchors, not a declaration that a particular test run passed. Record actual
commands, source/lock identity and PASS/FAIL/BLOCKED/SKIP/NOT_RUN separately. Pure session/fake
server tests are not native client, backend or cross-process acceptance.

| Requirement | Coverage anchor | What it establishes / does not establish |
|---|---|---|
| GLB-01: deterministic precedence and no silent fallback | `tests/unit/test_global_launcher.py::test_GLB_01_*` | Two projects, nested cwd, explicit/env precedence, conflicting hints, missing/invalid roots. |
| GLB-02: bounded/canonical selection | `tests/unit/test_global_launcher.py::test_GLB_02_*` | Git/worktree boundaries, nearer invalid/symlink manifests, canonical directory identity, filesystem errors. |
| GLB-03: discovery never grants trust | `tests/unit/test_global_launcher.py::test_GLB_03_*` | Missing/excluding trusted policy prevents server invocation and state creation; policy is not widened. |
| GLB-04: separate session state and rechecked policy | `tests/unit/test_global_launcher.py::test_GLB_04_*` | Concurrent in-process sessions keep state/trusted directories, cursor keys and jobs separate even with identical project IDs; revoked policy and post-startup directory/manifest symlink retargets are refused, including redirected event-log writes. Not native multiprocess/backend acceptance. |
| GLB-05: opt-in CLI and local execution compatibility | `tests/unit/test_global_launcher.py::test_GLB_05_*` | New launcher flags, environment handoff, explicit legacy roots, manifest-selector conflicts. Fake server boundary, not Slurm execution. |
| GLB-06/07: inert registration and protocol-clean failure | `tests/unit/test_global_launcher.py::test_GLB_06_*`, `test_GLB_07_*` | Quotes/Unicode/spaces, absolute existing launcher/toolchain, no apply, no workspace needed to preview, stderr failures and no MCP root-selection field. |
| GLB-08 / M27: binding guard mutation | `tests/unit/test_global_launcher.py::test_GLB_08_workspace_binding_mutation_has_precise_anchor_and_pure_tests`, `scripts/mutation_suite.py` M27 | Removing the startup binding recheck must fail the three mapped GLB-04 retarget/event-log tests. A focused mutant check is not the full 27-mutation/native suite. |
| GLB-09: actual SDK stdio process isolation | `tests/contract/test_global_launcher_stdio.py::test_GLB_09_two_actual_stdio_processes_keep_independent_workspace_binding` | Two gateway processes start from unrelated cwd, bind separate authorized roots/cursor keys, initialize and list 14 tools, reject root-switch arguments, preserve protocol-only stdout and cleanly exit. No native backend or real Claude/Codex execution. |
| DEP-01 / SEC-03: install-owned assets and both entry points | `tests/unit/test_bootstrap_locked.py::test_link_self_makes_svweave_importable_in_a_bare_venv`, `tests/unit/test_installed_runtime_assets.py` | Bootstrap linkage, packaged asset mapping/discovery, hostile ancestor-lock rejection, explicit shared root. Actual wheel/sdist tests require the exact installed build backend; skipped artifact tests are not PASS. |
| DOC-01: commands stay real | `tests/unit/test_gateway_cli.py::test_DOC_01_documented_commands_exist` | README/installation/agent-guide command paths resolve; not client configuration execution. |

Operational instructions and client references: [installation, section 2a](../install.md#2a-shared-installation-and-global-mcp-registration).
