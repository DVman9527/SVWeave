# vendor/ — PyPI packages for hosts that cannot reach PyPI

Only what cannot be fetched from GitHub. Everything GitHub-hosted is NOT stored here and is
downloaded by `bootstrap_locked.py` (sha256-verified against `toolchain.lock.json`): slang, Verible,
codebase-memory-mcp, uvm-core, the Serena checkout and `uv` itself.

Coverage is checked against the tool list in `install_IT.md` section 7 (items 8 to 41).

| Path | Content | install_IT.md |
|---|---|---|
| `downloads/wheelhouse/` | the 31 hash-pinned gateway wheels (incl. pyslang, mcp) | 7.3, #8-#38 |
| `downloads/wheelhouse-dev/` | the whole `[dev]` extra of `pyproject.toml` (`pytest`, `pytest-cov`, `hypothesis`, `ruff`, `mypy`, `types-*`) and their dependencies, 21 wheels | 7.4, #39-#41 (+ruff, mypy, types-*) |
| `runtime/cpython-3.12.15+20261013-...-install_only_stripped.tar.gz` | standalone CPython 3.12.15 (python-build-standalone, sha256 equals the published `SHA256SUMS`); GitHub-hosted, kept here on request so a host without `uv python install` access still has the interpreter | 7.2, #7 |
| `downloads/dev-requirements.txt` | hash-pinned requirements for the dev wheels (cp312, manylinux_2_28) | 7.4 |
| `SHA256SUMS` | checksums of all files here (`cd vendor && sha256sum -c SHA256SUMS`) | |

Not included on purpose: Serena's Python dependencies (`--skip-serena`, install_IT.md decision
D6; editing stays off).

## Install (GitHub reachable, PyPI not)

Run the script with Python 3.11 or newer; the gateway venv itself is 3.12.

```bash
(cd vendor && sha256sum -c SHA256SUMS)
mkdir -p runtime && tar -C runtime -xzf vendor/runtime/cpython-*.tar.gz     # runtime/python/bin/python3 = 3.12.15
uv venv --python runtime/python/bin/python3 .venv                          # the gateway venv, built on that interpreter
python3.11 scripts/bootstrap_locked.py --skip-serena --artifact-dir vendor/downloads
python3.11 scripts/bootstrap_locked.py --skip-serena --verify-only
# dev wheels (optional), no index access:
uv pip install --python .venv/bin/python --offline --no-index --find-links vendor/downloads/wheelhouse-dev \
    --require-hashes -r vendor/downloads/dev-requirements.txt
```

Do not pass `--venv-python runtime/python/bin/python3`: that option names the *venv's* Python, so the
wheels would be installed into the bundled interpreter itself. Without Serena `svweave doctor` reports
`backend:serena_exe FAIL` (exit 4); that is expected.

For a run with no network at all, first cache the GitHub artifacts online
(`bootstrap_locked.py --skip-serena --fetch-only --artifact-dir .toolchain/downloads`, then copy
`vendor/downloads/wheelhouse` next to them) and add `--offline`. If `~/.cache/uv` is read-only, set
`UV_CACHE_DIR` to a writable directory.

The installer reuses any artifact already present with the right sha256, so the wheels come from
`vendor/` and only the GitHub artifacts are downloaded. Regenerate the gateway wheels with
`bootstrap_locked.py --fetch-only` when the lock changes.
