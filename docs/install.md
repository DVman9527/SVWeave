# Installation

Plan references are to `SVWeave_plan.md` (7.2, 7.3, 26.3). SVWeave v0.1.0-dev is a developer
preview. Everything is pinned in `toolchain.lock.json`; nothing is installed from "latest".

## 1. Requirements

* Linux x86_64 (the only platform in the lock: `linux-x86_64`). Other platforms are not qualified.
* Python 3.12 for the venvs (`python_minor: 3.12` in the lock; `python3.12` on `PATH` or `--python`)
  and Python 3.11 or newer to run the installer. [`uv`](https://docs.astral.sh/uv/) does **not** have to
  be installed: the installer downloads the pinned uv release (lock section `uv`), verifies it and
  runs only that one.
* `bubblewrap` (`bwrap`), with unprivileged user namespaces enabled. Without it the backends do not
  run (or run only in an explicitly flagged degraded mode, see `docs/security.md`). Check:
  `bwrap --version && bwrap --ro-bind / / true`.
* `git` (used once, to fetch the pinned Serena commit), `tar`.
* RAM: the HDL worker is limited by `analysis.max_worker_memory_mib` (default 8192). The graph and
  Serena backends add little on the fixtures; large corpora need more. Start backends one at a time
  on small hosts and use `svweave serve --no-graph` if memory is short.

## 2. Install (online, once)

```bash
python3.12 scripts/bootstrap_locked.py --create-venv         # pinned uv, .venv, pinned backends, Serena
.venv/bin/python scripts/bootstrap_locked.py --verify-only   # no network; compare installed state with the lock
# development tools (optional): hash-pinned wheels committed in vendor/, no index access
.toolchain/uv/uv pip install --python .venv/bin/python --offline --no-index \
    --find-links vendor/downloads/wheelhouse-dev --require-hashes -r vendor/downloads/dev-requirements.txt
```

There is no `uv venv` step before the installer: `--create-venv` makes it create the gateway venv
(`.venv`, on `python3.12` from `PATH` or `--python PATH`) with the pinned uv, install the 31
hash-locked gateway wheels into it and link this source tree into it (a `.pth` file plus
`.venv/bin/svweave`, like an editable install).

What the installer runs, in this order:

1. **uv.** The pinned uv release tarball (lock section `uv`: 0.12.23, `x86_64-unknown-linux-gnu`,
   sha256 equal to the release's published checksum) is downloaded first, verified against the lock,
   extracted into `.toolchain/uv/` and the extracted binary is verified again. Every later uv command
   is that binary, with `UV_PYTHON_DOWNLOADS=never`. A wrong hash stops the run before anything else is
   downloaded. `--uv PATH` replaces it with another uv; that binary is only checked to be executable,
   is **not** verified, and the JSON report says so (`components.uv.verified: false` and a
   `warnings` entry). `--verify-only` checks the pinned uv like the other binaries.
2. Each archive or wheel named in `toolchain.lock.json`: SHA-256 checked **before** use, extraction with
   path-traversal protection, extracted binaries checked again.
3. **Serena.** The pinned commit is checked out (`uv.lock` and `LICENSE` hashes checked) and its venv is
   built from the locked `uv.lock`. `uv.lock` does not lock *build* dependencies, so the build backends
   are pinned separately (lock section `serena_build_wheelhouse`: hatchling with its dependencies for
   serena-agent itself, setuptools for the sdist-only `proxy-tools` 0.1.0). The installer verifies
   those 8 wheels, installs them into the new Serena venv from the local directory
   (`--no-index --require-hashes`) and then runs `uv sync --frozen --no-dev --no-build-isolation`:
   the builds use exactly those wheels, and a missing build dependency is a build error, never a
   download. The exact sync removes the build-only packages again.

Any mismatch aborts with a non-zero exit code; there is no silent fallback. The installer is the
**only** code that uses the network. Use `--skip-serena` to skip the Serena venv and
`--toolchain-dir DIR --no-venv` to reproduce the toolchain elsewhere.

Pinned components (details and hashes: `toolchain.lock.json`):

| Component | Version |
|---|---|
| pyslang / slang | 12.0.0 / v12.0 |
| codebase-memory-mcp | v0.11.0 (`linux-amd64-portable`) |
| Serena (`serena-agent`) | v1.7.0 (commit 949a27ef1e5f) |
| Verible | v0.0-4296-g0f262651 |
| MCP Python SDK | 2.3.0 |
| Accellera UVM (`uvm-core`) | 2020.3.1 |

The `svweave` command lives in the venv. Either `source .venv/bin/activate` once per shell or call
`.venv/bin/svweave`; the examples elsewhere in the documentation write plain `svweave`.

Then run the first checks:

```bash
.venv/bin/svweave doctor            # config, policy, backend presence, client boundary
.venv/bin/svweave doctor --probe    # starts each backend once with a real fixture query (slow)
.venv/bin/python scripts/probe_backends.py   # writes .svweave/probe/capability_report.json
```

## 2a. Shared installation and global MCP registration

### Install the runtime once, not inside each design

Use one operator-controlled SVWeave checkout, for example `/ABS/PATH`, outside the HDL projects.
Run section 2 (or the offline procedure in section 3) **once in that checkout**:

```bash
cd /ABS/PATH
python3.12 scripts/bootstrap_locked.py --create-venv
.venv/bin/python scripts/bootstrap_locked.py --verify-only
export PATH="/ABS/PATH/.venv/bin:$PATH"
```

Both `svweave` and `svweave-mcp` now use the same gateway venv and pinned toolchain. Keep this
checkout in place: the bootstrap links its `src/` into the venv, rather than copying a runtime
into each HDL project. Add the `PATH` setting to your own shell configuration only if desired;
SVWeave does not change shell or client configuration.

| Shared installation | Per workspace (not shared across projects) |
|---|---|
| Gateway `.venv`, `.toolchain`, lock/capability metadata, probe fixtures; Serena has its own pinned venv | `svweave.yaml`, optional `svweave.local.yaml`, `.svweave/` database/snapshots/jobs/candidates/backend state |
| Installed code and dependency versions | Trusted policy, approval records/key and cursor key under `$XDG_CONFIG_HOME/svweave/workspaces/<key>/` |

A standard package install exposes the same two console entry points and bundles frozen metadata
and probe fixtures under `svweave/_runtime`; it does **not** bundle or bootstrap native backends.
Do not treat `pip`/`pipx` installation alone as a complete pinned backend installation. To use a
separately provisioned shared toolchain, set `SVWEAVE_TOOLCHAIN_ROOT=/ABS/PATH` or pass
`--toolchain-root /ABS/PATH` to `svweave-mcp`. This is the directory containing
`toolchain.lock.json`, `capability_report.json` and `.toolchain/`, **not** `.toolchain/` itself.
Toolchain selection never searches the current HDL project's files for a replacement lock.

### Select one already-initialized workspace per process

Each project still needs the deliberate setup in section 5. Discovery never runs `init`, grants
trust or enables editing. `svweave-mcp` binds one canonical workspace before serving and keeps
that binding for the process lifetime; changing project requires a new client/server session.
There is no workspace-switch tool, project catalog or multi-workspace router.

Selection order:

1. `svweave-mcp --repo /ABS/PROJECT` or `--workspace /ABS/PROJECT`. The directory must contain
   `svweave.yaml`; if both are given they must resolve to the same directory.
2. `SVWEAVE_PROJECT_ROOT=/ABS/PROJECT`, an absolute directory containing `svweave.yaml`.
3. `CLAUDE_PROJECT_DIR` and/or the launch directory. Find the nearest ancestor `svweave.yaml`,
   stopping at a Git checkout/worktree/submodule boundary. If both select a workspace, they must
   agree. A bad explicit path or client hint fails instead of falling back to a different project.

Missing or conflicting selection, a symlinked manifest or missing trusted policy fails closed.
Project text, `.env` files and MCP roots cannot authorize a workspace; `allowed_roots` remains
unchanged. `svweave serve --discover-workspace` uses the same selector, with explicit top-level
`--root` or `serve --workspace` as its highest-priority inputs. A conflicting `--manifest` from
another directory is rejected. Without that flag, existing
`svweave --root /ABS/PROJECT serve --transport stdio` and
`svweave serve --workspace /ABS/PROJECT` retain their explicit-workspace behavior.

### Preview first; register only after review

```bash
svweave mcp-config --client claude
svweave mcp-config --client codex
# if launcher and native tools were installed separately:
svweave mcp-config --client codex --executable /ABS/VENV/bin/svweave-mcp --toolchain-root /ABS/PATH
```

These commands print a JSON envelope with `data.registration_shell`, `registration_argv`,
`client_config` and `applied: false`. They never read or edit client configuration, run the
registration, install dependencies or probe a client/backend. Review and manually apply **one**
entry named `svweave` in each client; do not register raw Serena or codebase-memory-mcp servers.
The generated arguments pin the absolute shared toolchain location, not a project root.

**Claude Code:** the reviewed user-scope form is:

```bash
claude mcp add --transport stdio --scope user svweave -- /ABS/PATH/.venv/bin/svweave-mcp --toolchain-root /ABS/PATH
```

The [Claude Code MCP reference](https://code.claude.com/docs/en/mcp) documents `--scope user`
and `CLAUDE_PROJECT_DIR` in local stdio servers. The launcher reads that hint; do not expand the registration shell's `$PWD`
into a permanent `--repo` value when you want discovery. Check `/mcp`, then ask for `svw_status`
and check its expected workspace ID, profiles and builds before analysis. IDs can be reused
across directories, so status alone is not proof of an absolute root; use an explicit override
when the project context is uncertain.

**Codex:** its user configuration (`~/.codex/config.toml`, or the configured `CODEX_HOME`) can
contain:

```toml
[mcp_servers.svweave]
command = "/ABS/PATH/.venv/bin/svweave-mcp"
args = ["--toolchain-root", "/ABS/PATH"]
env_vars = ["SVWEAVE_PROJECT_ROOT"]
```

The [Codex MCP reference](https://learn.chatgpt.com/docs/extend/mcp?surface=cli) documents user
configuration and `env_vars` forwarding. Its [configuration reference](https://learn.chatgpt.com/docs/config-file/config-reference)
allows an explicit `cwd` but does **not** specify that the default is the active project. When
needed, explicitly hand off the project in the environment when starting the client:

```bash
SVWEAVE_PROJECT_ROOT=/ABS/PATH/TO/PROJECT codex
```

The `env_vars` line is essential to forward that caller-supplied value; `codex mcp add svweave --
/ABS/PATH/.venv/bin/svweave-mcp --toolchain-root /ABS/PATH` alone does not add it. Alternatively,
put `--repo /ABS/PATH/TO/PROJECT` in the launcher args for a deliberately fixed-workspace entry.
A fixed `env.SVWEAVE_PROJECT_ROOT` in global config likewise pins every session to that project;
it is not automatic selection. `codex mcp list` checks registration, not correct workspace
binding; inspect `svw_status` after connection and use an explicit root if identity is ambiguous.

**Migration:** retain each project's manifest, trusted policy and state. Replace the existing
`svweave` entry rather than adding another; inspect local/project overrides that could shadow
the user entry. Remove a redundant project entry only after reviewing it. The specialist agent
and routing snippet are optional instructions, independent of runtime installation; existing
explicit `--workspace`/`--root` entries remain valid. Uninitialized/untrusted projects must be
initialized and reviewed separately, never auto-trusted by an agent.

Optional reusable guidance also need not be copied into every checkout. Claude supports
[user-level subagents](https://code.claude.com/docs/en/sub-agents) in `~/.claude/agents/`:
after review, the specialist can live there as `svweave-mcp.md` instead of in each project's
`.claude/agents/`. Codex supports [global instructions](https://learn.chatgpt.com/docs/agent-configuration/agents-md)
in its home `AGENTS.md`; adapt the safety/workflow guidance rather than copying a Claude-only
agent path. Preserve existing instructions and review project overrides. Neither guidance file
replaces workspace trust or changes the MCP binding.

**Verified installation scope (2026-10-29):** [run 40806959759](../evidence/global-workspaces-run40806959759/README.md)
at `ca6ebb571137f683a5354a1bd78169341d933de1` passed 10 real wheel/sdist checks and the detached
installed-wheel smoke (both console entry points, bundled assets and explicit shared-toolchain
override), plus 49 MCP/CLI contracts including two SDK stdio gateway processes. Required
native-backend regressions and all 27 critical mutations also passed in that run. These scopes
overlap; the separate unit suite retains one skip. This verifies neither live Claude/Codex
registration nor a Slurm site; full qualification remains UNQUALIFIED. See the linked raw proofs
for exact counts, limits and source identity rather than treating a later docs commit as tested.

Client documentation was checked on 2026-10-29; it is not a live-client test. This is a local
startup convenience, not native Claude/Codex qualification or Slurm acceptance.
Local execution remains the default; existing execution-policy checks still apply and Slurm
remote execution remains **BLOCKED / UNQUALIFIED**. A pre-existing `svweave-slurm` wrapper is
not supplied or validated by this repository; do not assume wrapper compatibility.

## 3. Offline bootstrap from `toolchain.lock.json`

The installer reuses any artifact already present with the right hash, so a machine without
internet access can be provisioned from a prepared `.toolchain/` directory:

1. On a connected machine fill the cache without installing anything:
   `python3.12 scripts/bootstrap_locked.py --fetch-only` (needs `git`; no venv and no uv are needed:
   the pinned uv is downloaded, verified and unpacked into `.toolchain/uv/` because it warms the cache).
   This stores, inside the repository directory,
   * every archive named in the lock (the pinned uv tarball included), the hash-pinned gateway
     wheelhouse (`gateway_wheelhouse` in the lock, 31 wheels including pyslang and mcp) and the 8
     Serena build-backend wheels (`serena-build-wheelhouse/`) under `.toolchain/downloads/`
     (change with `--artifact-dir`),
   * the pinned Serena checkout under `.toolchain/serena-src`, and
   * the uv cache holding Serena's locked dependencies under `.toolchain/state/uv-cache`
     (change with `--uv-cache-dir`), filled by the pinned uv with build isolation off (no build
     backend is taken from an index).

   About 200 MB in total. Nothing is installed besides the unpacked uv.
2. Copy the whole repository directory, `.toolchain/` included, to the offline machine (same
   architecture; `.venv` is not copied).
3. There run `python3.12 scripts/bootstrap_locked.py --offline --create-venv --venv-dir .venv`.
   `--offline` never touches the network: every artifact must already be cached and is verified
   against the lock sha256 before use (the pinned uv first), Serena is taken from the existing
   checkout and built only with the pinned build wheels, and the gateway venv installs only from the
   wheelhouse. It then links the svweave source tree into that venv
   (a `.pth` file and a `bin/svweave` launcher, like an editable install) because the wheelhouse has
   no build backend: `uv pip install -e .` cannot run offline. Finish with `--verify-only`.

   If the cache lives elsewhere, add `--artifact-dir`, `--uv-cache-dir` and, for a Serena checkout
   that is not at `.toolchain/serena-src`, `--serena-source <local git checkout>`.

   Notes for sandboxed or restricted hosts:
   * uv writes its cache and a lock file under `UV_CACHE_DIR` (default `~/.cache/uv`); point it at a
     writable directory if the default is read-only (`Could not acquire lock`).
   * `--serena-source` is cloned with `git clone --no-local file://...`, so the path must be visible
     to the process; the error for a bad path names it.

Current cleanroom acceptance is **`DEP-01-R2-host-controller`**, explicitly authorized on
**2026-10-28 03:17:21 UTC**; see [ADR-016](adr/ADR-016-dep01-trusted-host-controller.md).
`tests/integration/test_dep01_cleanroom.py` retains network-isolated offline installation into
an empty directory and network-isolated verification. Its reviewed probe controller runs on
the host, **without an outer network-isolation guarantee**; every native backend/helper still
requires read-only source/tool inputs, no network and no inherited secrets in mandatory bwrap.
The controller must not download dependencies or upload source. This is a narrower contract,
not an end-to-end offline or physically air-gapped cleanroom claim.

**Revised acceptance passed in [run 40627613557](../evidence/r2-verilator-run40627613557/README.md) at
`cc931de898069bed0a51497737d4ad60603e4c51`.** This is the narrower R2 boundary only, not
full qualification or a test of a later documentation commit. New revision evidence belongs
under `.svweave/ci/dep01-r2/`, including the contract, bootstrap, controller preflight and
capability reports. Original `evidence/dep01/cleanroom.json` is historical, not R2 evidence;
the later [nested-denial run](../evidence/dep01-nested-denial-run40490935466/README.md) remains
FAIL/BLOCKED under its original contract. No AppArmor/sysctl/capability relaxation or backend
fallback is allowed. The old option-1 `remaining_regressions=true` selector is R1-only, not a
way to skip the revised node or claim its acceptance.

Running DEP-01 needs two things on the host (otherwise it skips, or fails under
`SVWEAVE_REQUIRE_BACKENDS=1`):

* **The cached artifacts under `.toolchain/`**: `.toolchain/downloads/` with `wheelhouse/`,
  `serena-build-wheelhouse/` and the uv tarball, plus `.toolchain/serena-src/` and
  `.toolchain/state/uv-cache/`. A `--fetch-only` run creates all of them. After an install from
  `vendor/` (path (b)), `serena-src` and `uv-cache` already exist and the artifacts are in
  `vendor/downloads/`; link them instead of downloading:
  `ln -s ../vendor/downloads .toolchain/downloads`.
* **`python3` on `PATH` must be Python 3.11 or newer**: the test runs
  `python3 scripts/bootstrap_locked.py` inside the sandbox, and the installer needs 3.11. On hosts
  whose `python3` is older (RHEL 8: 3.6), put a newer one first on `PATH` for the test run only:

  ```bash
  mkdir -p /tmp/dep01-py && ln -sf /usr/bin/python3.12 /tmp/dep01-py/python3
  PATH=/tmp/dep01-py:$PATH SVWEAVE_REQUIRE_BACKENDS=1 \
      .venv/bin/python -m pytest tests/integration/test_dep01_cleanroom.py
  ```

What DEP-01 does not cover: its clean room holds `scripts/`, `fixtures/`, `src/` and the lock only,
so it does not exercise the `--fetch-only` Serena cache or the `svweave` CLI itself (it does exercise
the offline linking of `src/` into the fresh venv, which the probes need for the bounded MCP stdio
transport). Those were run by hand end to end (fetch-only, then `--offline` under
`bwrap --unshare-net` with no extra flags, then `svweave doctor --probe` and `svweave build`); the
linking is also unit-tested in `tests/unit/test_bootstrap_locked.py`.

DEP-01 runs the pinned uv from the artifact dir (no `--uv`) and builds Serena from the pinned build
wheels. Historically, `evidence/dep01/cleanroom.json` was refreshed with this lock on 2026-10-20 (in a scratch copy
whose `.toolchain/` came from the `vendor/` procedure). The `vendor/` procedure itself was also run end
to end under `bwrap --unshare-net` (see `CHANGELOG.md`). Those dated results do not validate the
R2 host-controller contract.

The PyPI wheels are also committed in `vendor/` (see `vendor/README.md`) for hosts that reach GitHub but not PyPI,
together with the pinned uv tarball and a standalone CPython; the other GitHub-hosted components are
not stored there.

Never let the gateway venv or the Serena venv resolve dependencies from the network at run time;
`uv sync --frozen`, `--no-build-isolation` with the pinned build wheels and the lock hashes exist to
prevent that.

A uv cache that an older installer (before the build backends were pinned) filled may hold a
`proxy-tools` or `serena-agent` wheel that was built with an unpinned backend, and uv reuses such
built wheels. Delete `.toolchain/state/uv-cache` (or the `--uv-cache-dir`) once after upgrading;
`vendor/uv-cache.tar.gz` was regenerated with the pinned backends.

## 4. The glibc note for codebase-memory-mcp

The upstream release has a standard build and a **portable (static)** build. The standard
`codebase-memory-mcp` binary needs glibc 2.38 and fails on older distributions, for example
Enterprise Linux 8 (glibc 2.28):

```text
/lib64/libc.so.6: version 'GLIBC_2.38' not found (required by cbm/codebase-memory-mcp)
```

The lock therefore pins the **`linux-amd64-portable`** tarball
(`codebase_memory_mcp-codebase-memory-mcp-linux-amd64-portable.tar.gz`, binary SHA-256 recorded in
the lock), which runs on this class of host. The UI and non-UI upstream tarballs are the same
binary; the UI (HTTP port 9749) and file watchers are switched off by SVWeave in a dedicated state
directory, and the process runs inside the network-less sandbox. Do not replace the pinned binary
with another build without re-probing (`scripts/probe_backends.py`) and updating the lock and
license review (ADR-010).

## 5. Per-project setup

```bash
svweave init                       # svweave.yaml template, .svweave/, policy.json in the TRUSTED dir
$EDITOR svweave.yaml               # see docs/manifest.md; map external roots in svweave.local.yaml
svweave manifest resolve --profile P
svweave build --profile P
```

The trusted policy is created **outside** the repository
(`$XDG_CONFIG_HOME/svweave/workspaces/<key>/policy.json`, see `docs/security.md`). Editing stays
disabled until you deliberately enable it there (`docs/editing.md`). This setup remains per
workspace even with a shared installation; do not rerun bootstrap in each project. Global MCP
registration is in section 2a; the explicit per-project alternative is in `docs/operations.md`
section 4.

After the first build, `svweave update --profile P` opts into snapshot I/O reuse for subsequent
work on that profile. It uses the same installation and per-workspace state; no separate cache
service, daemon, client registration or MCP schema change is needed. Add `--no-git` to skip the
optional Git advisory. Ordinary `svweave build --profile P` remains unchanged. Update still
hashes all relevant inputs and runs whole-profile semantic analysis unless the exact COMPLETE
build is already reusable; reflink support and speedup are not guaranteed. See the
[update workflow and report](operations.md#2a-opt-in-snapshot-io-update).
Upgrading a shared MCP installation does **not** automatically enable this path: existing
`svw_build` calls retain their default behavior. Run the CLI update explicitly against the same
workspace; after a COMPLETE update, its published result is available to that workspace's CLI
and MCP queries by the returned build ID, subject to the usual freshness checks.

## 6. Uninstall

For a shared installation, removing its virtual environment or `.toolchain/` affects every
project using that runtime. Remove a project's `.svweave/` and trusted directory
`$XDG_CONFIG_HOME/svweave/workspaces/<key>/` only if you also want to discard its state and trust.
Review/remove the client entry and any shell `PATH` setting yourself; SVWeave never edits an MCP
client's configuration.

## 7. Developer checks

```bash
.venv/bin/ruff check . && .venv/bin/ruff format --check .
.venv/bin/mypy
.venv/bin/svweave schema emit --check
.venv/bin/python -m pytest tests/unit tests/contract
SVWEAVE_REQUIRE_BACKENDS=1 .venv/bin/python -m pytest -m integration   # real backends
.venv/bin/python scripts/qualify.py                                    # release gates G0-G5
```
