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
disabled until you deliberately enable it there (`docs/editing.md`). MCP client configuration:
`docs/operations.md` section 4.

## 6. Uninstall

Remove the virtual environment, `.toolchain/`, `.svweave/` and, if you want, the trusted directory
`$XDG_CONFIG_HOME/svweave/workspaces/<key>/`. SVWeave does not modify any other user file and never
edits an MCP client's configuration.

## 7. Developer checks

```bash
.venv/bin/ruff check . && .venv/bin/ruff format --check .
.venv/bin/mypy
.venv/bin/svweave schema emit --check
.venv/bin/python -m pytest tests/unit tests/contract
SVWEAVE_REQUIRE_BACKENDS=1 .venv/bin/python -m pytest -m integration   # real backends
.venv/bin/python scripts/qualify.py                                    # release gates G0-G5
```
