# vendor/ — PyPI packages for hosts that cannot reach PyPI

Only what cannot be fetched from GitHub, plus the tools the installer itself needs. The GitHub-hosted
backends are NOT stored here and are downloaded by `bootstrap_locked.py` (sha256-verified against
`toolchain.lock.json`): slang, Verible, codebase-memory-mcp and uvm-core. Included on request although
GitHub-hosted: Serena (its PyPI dependencies are what a host without PyPI access really needs), a
standalone CPython, and the pinned `uv` release tarball, which the installer verifies against the lock
(section `uv`) and then runs for every uv command; no `uv` has to be installed on the host.

Coverage is checked against the tool list in `install_IT.md` section 7 (items 8 to 41).

| Path | Content | install_IT.md |
|---|---|---|
| `downloads/wheelhouse/` | the 31 hash-pinned gateway wheels (incl. pyslang, mcp) | 7.3, #8-#38 |
| `downloads/serena-build-wheelhouse/` | the 8 hash-pinned build-backend wheels of the Serena venv (hatchling 1.32.4 + editables, packaging, pathspec, pluggy, tomlkit, trove-classifiers; setuptools 84.0.0 for proxy-tools), lock section `serena_build_wheelhouse`; Serena is built with `--no-build-isolation` from exactly these | Serena |
| `downloads/uv-uv-x86_64-unknown-linux-gnu.tar.gz` | the official uv 0.12.23 release tarball for linux x86_64 (19.9 MB; sha256 equals the release's published `.sha256` and the lock section `uv`) | uv |
| `downloads/wheelhouse-dev/` | the whole `[dev]` extra of `pyproject.toml` (`pytest`, `pytest-cov`, `hypothesis`, `ruff`, `mypy`, `types-*`) and their dependencies, 21 wheels | 7.4, #39-#41 (+ruff, mypy, types-*) |
| `runtime/cpython-3.12.15+20261013-...-install_only_stripped.tar.gz` | standalone CPython 3.12.15 (python-build-standalone, sha256 equals the published `SHA256SUMS`); GitHub-hosted, kept here on request so a host without `uv python install` access still has the interpreter | 7.2, #7 |
| `downloads/dev-requirements.txt` | hash-pinned requirements for the dev wheels (cp312, manylinux_2_28) | 7.4 |
| `uv-cache.tar.gz` | uv cache (format of the pinned uv 0.12.23) holding Serena's locked PyPI dependencies, filled by `bootstrap_locked.py --fetch-only` with the pinned build wheels and no build isolation | Serena (D6 reversed: included) |
| `serena-src.bundle` | git bundle of the pinned Serena checkout (v1.7.0, commit 949a27ef) | Serena |
| `SHA256SUMS` | checksums of all files here (`cd vendor && sha256sum -c SHA256SUMS`) | |

Serena (`--skip-serena` is no longer needed) adds 35 MB, the uv tarball 20 MB.

## Install (GitHub reachable, PyPI not)

Run the script with Python 3.11 or newer; the venvs themselves are 3.12. No `uv` and no `uv venv`
step: the installer verifies and unpacks the pinned uv from `vendor/downloads/` into `.toolchain/uv/`
and creates the gateway venv with it (`--create-venv`).

```bash
(cd vendor && sha256sum -c SHA256SUMS)
mkdir -p .toolchain/state && tar -C .toolchain/state -xzf vendor/uv-cache.tar.gz   # Serena's locked deps
git clone vendor/serena-src.bundle .toolchain/serena-src                          # pinned Serena checkout
mkdir -p .toolchain/runtime && tar -C .toolchain/runtime -xzf vendor/runtime/cpython-*.tar.gz   # .toolchain/runtime/python/bin/python3 = 3.12.15
python3.11 scripts/bootstrap_locked.py --artifact-dir vendor/downloads --create-venv \
    --python .toolchain/runtime/python/bin/python3                         # venvs built on that interpreter
python3.11 scripts/bootstrap_locked.py --verify-only --artifact-dir vendor/downloads
# dev wheels (optional), no index access, with the pinned uv:
.toolchain/uv/uv pip install --python .venv/bin/python --offline --no-index --find-links vendor/downloads/wheelhouse-dev \
    --require-hashes -r vendor/downloads/dev-requirements.txt
```

`--python` names the *base* interpreter of the venvs the installer creates (gateway `.venv` and the
Serena venv). Do not pass `--venv-python .toolchain/runtime/python/bin/python3`: that option names the *venv's*
Python, so the wheels would be installed into the bundled interpreter itself. Without Serena
`svweave doctor` reports `backend:serena_exe FAIL` (exit 4); that is expected.

For a run with no network at all, first cache the four GitHub archives once online
(`bootstrap_locked.py --fetch-only --artifact-dir <dir>` on a connected host, then copy the
`slang-*`, `verible-*`, `codebase_memory_mcp-*` and `uvm_core-*` tarballs into `vendor/downloads/`),
then run the same steps with `--offline` added to the install line. The installer never needs any
other download: uv, the gateway wheels, the Serena build wheels and Serena's dependencies all come
from `vendor/`. If `~/.cache/uv` is read-only, set `UV_CACHE_DIR` to a writable directory.

The installer reuses any artifact already present with the right sha256, so the wheels and uv come
from `vendor/` and only the GitHub archives are downloaded. Regenerate the gateway wheels, the Serena
build wheels and the uv tarball with `bootstrap_locked.py --fetch-only` when the lock changes, and
`uv-cache.tar.gz` from the `--fetch-only` cache
(`tar -C .toolchain/state --sort=name --owner=0 --group=0 --numeric-owner -czf vendor/uv-cache.tar.gz uv-cache`);
a cache written by another uv version uses other cache buckets and does not work offline.
