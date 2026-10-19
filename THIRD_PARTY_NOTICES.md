# Third-Party Notices

SVWeave M0 uses the following third-party components. None of them is redistributed by this
repository: they are downloaded and installed locally by `scripts/bootstrap_locked.py` from the
URLs and SHA-256 values in `toolchain.lock.json`. License texts captured at the pinned versions are
stored under `evidence/m0/licenses/`; their SHA-256 values are in `license_manifest.json`.

This file is an engineering inventory, not a legal review (plan §3.2). Legal review status:
**NOT_DONE**. SVWeave's own license has not been chosen by the owner.

| Component | Pinned version | License | License text |
|---|---|---|---|
| slang (CLI) | v12.0, commit 545f88b2ed3a | MIT | `evidence/m0/licenses/slang-LICENSE` |
| pyslang | 12.0.0 | MIT | `evidence/m0/licenses/pyslang-12.0.0-LICENSE` |
| Serena (serena-agent, includes SolidLSP) | v1.7.0, commit 949a27ef1e5f | MIT (the whole repository at this commit) | `evidence/m0/licenses/serena-v1.7.0-LICENSE` |
| codebase-memory-mcp | v0.11.0, commit 8972ea69c6ad, linux-amd64-portable | MIT, plus vendored components (see below) | `evidence/m0/licenses/cbm-v0.11.0-LICENSE`, `evidence/m0/licenses/cbm-THIRD_PARTY.md` |
| Verible | v0.0-4296-g0f262651 | Apache-2.0 | `evidence/m0/licenses/verible-LICENSE` |
| MCP Python SDK | mcp 2.3.0 | MIT | `evidence/m0/licenses/mcp-2.3.0-LICENSE` |
| Accellera UVM (uvm-core) | 2020.3.1 | Apache-2.0 (with NOTICE) | `evidence/m0/licenses/uvm-core-2020.3.1-LICENSE.txt`, `...-NOTICE.txt` |
| bubblewrap | 0.4.0 (host OS package) | LGPL-2.0-or-later (from upstream; host package files not checked) | host system |

## Serena license version note

The plan (§3.2) describes Serena as "application GPL-3.0-or-later, SolidLSP MIT". That matches the
current `main` branch (checked at commit 737ccc29a72a, text stored as
`evidence/m0/licenses/serena-main-LICENSE-737ccc29`). The **pinned v1.7.0 commit is entirely MIT**
(`git log -- LICENSE`: "Switched to MIT license"). Any Serena upgrade past v1.7.0 must be gated by a
license review (ADR-010), because the Serena application files become GPL-3.0-or-later and the
combined `serena-agent` distribution is then GPL-3.0-or-later as a whole.

## codebase-memory-mcp vendored components

The release tarball ships `THIRD_PARTY_NOTICES.md` (564 KiB; SHA-256
57df11de7391283832f4b0d1f015acddbd93bbd94ce1b49e8e05ab68fbad3e8c, kept in `.toolchain/cbm/`). It covers
the tree-sitter runtime and about 150 grammars (relevant to SVWeave:
`gmlarumbe/tree-sitter-systemverilog` MIT and `tree-sitter/tree-sitter-verilog` MIT), vendored C/C++
libraries, static token embeddings derived from `nomic-ai/nomic-embed-code` (Apache-2.0), and an
embedded graph UI.

## Not yet inventoried

- Third-party code statically linked into the slang binary, the pyslang native module, and the Verible static binaries.
- Transitive Python dependencies of the Serena venv (`uv.lock` SHA-256 48f88af6...) and of the gateway venv (mcp 2.3.0 dependencies).
