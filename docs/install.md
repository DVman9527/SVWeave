# Installation

Plan references are to `SVWeave_plan.md` (7.2, 7.3, 26.3). SVWeave v0.1.0-dev is a developer
preview. Everything is pinned in `toolchain.lock.json`; nothing is installed from "latest".

## 1. Requirements

* Linux x86_64 (the only platform in the lock: `linux-x86_64`). Other platforms are not qualified.
* Python 3.12 (`python_minor: 3.12` in the lock) and [`uv`](https://docs.astral.sh/uv/).
* `bubblewrap` (`bwrap`), with unprivileged user namespaces enabled. Without it the backends do not
  run (or run only in an explicitly flagged degraded mode, see `docs/security.md`). Check:
  `bwrap --version && bwrap --ro-bind / / true`.
* `git` (used once, to fetch the pinned Serena commit), `tar`.
* RAM: the HDL worker is limited by `analysis.max_worker_memory_mib` (default 8192). The graph and
  Serena backends add little on the fixtures; large corpora need more. Start backends one at a time
  on small hosts and use `svweave serve --no-graph` if memory is short.

## 2. Install (online, once)

```bash
uv venv --python 3.12 .venv
uv pip install --python .venv/bin/python -e '.[dev]'     # this package and its pinned dependencies
.venv/bin/python scripts/bootstrap_locked.py             # pinned backends into .toolchain/
.venv/bin/python scripts/bootstrap_locked.py --verify-only   # no network; compare installed state with the lock
```

`bootstrap_locked.py` downloads each archive or wheel named in `toolchain.lock.json`, verifies its
SHA-256 **before** use, extracts with path-traversal protection, verifies the extracted binaries
again, checks out the pinned Serena commit and builds its venv from the locked `uv.lock`
(`uv sync --frozen`). Any mismatch aborts with a non-zero exit code; there is no silent fallback.
It is the **only** code that uses the network. Use `--skip-serena` to skip the Serena venv and
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

Then run the first checks:

```bash
.venv/bin/svweave doctor            # config, policy, backend presence, client boundary
.venv/bin/svweave doctor --probe    # starts each backend once with a real fixture query (slow)
.venv/bin/python scripts/probe_backends.py   # writes .svweave/probe/capability_report.json
```

## 3. Offline bootstrap from `toolchain.lock.json`

The installer reuses any artifact already present with the right hash, so a machine without
internet access can be provisioned from a prepared `.toolchain/` directory:

1. On a connected machine fill the artifact cache without installing anything:
   `.venv/bin/python scripts/bootstrap_locked.py --fetch-only`. This stores every archive named in
   the lock plus the hash-pinned gateway wheelhouse (`gateway_wheelhouse` in the lock, 31 wheels
   including pyslang and mcp) under `.toolchain/downloads/` (change with `--artifact-dir`), and the
   pinned Serena checkout plus its uv cache under `.toolchain/`.
2. Copy the repository and that cache to the offline machine (same architecture).
3. There run
   `.venv/bin/python scripts/bootstrap_locked.py --offline --create-venv --venv-dir .venv`
   (add `--artifact-dir`, `--serena-source <local checkout>` and `--uv-cache-dir` if the cache lives
   elsewhere). `--offline` never touches the network: every artifact must already be cached and is
   verified against the lock sha256 before use, and the gateway venv installs only from the
   wheelhouse. Finish with `--verify-only`.

Status of this procedure: DEP-01 is exercised by `tests/integration/test_dep01_cleanroom.py`, which
bootstraps into an empty directory with `--offline` under `bwrap --unshare-net` (no network
namespace) and then probes all three backends from the fresh install. Evidence:
`evidence/dep01/cleanroom.json`. That is a network-isolated clean-room run on the build host, not a
run on a separate physical air-gapped machine.

Never let the gateway venv or the Serena venv resolve dependencies from the network at run time;
`uv sync --frozen` and the lock hashes exist to prevent that.

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
