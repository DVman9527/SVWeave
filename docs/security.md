# Security model

Plan references are to `SVWeave_plan.md` (sections 6.4, 23). Status: developer preview,
**UNQUALIFIED**. This document says what the controls are, what they guarantee and, with equal
weight, what they do not.

## 1. Threat model

Assets: the original source tree, the approval store, the trusted policy, secrets in the
environment, and the user's host.

| Threat | Control | Where |
|---|---|---|
| An AI client or a backend writes the original source | Original source is never mounted writable into a backend. Edits happen in a **candidate** (a real byte copy of the snapshot). Only the gateway write coordinator changes the source, after a valid approval and compare-and-set checks. | `editing/`, ADR-007 |
| Repository configuration executes commands | `svweave.yaml` is pure data; no hooks, Make, Tcl, shell substitution or `eval`. Unknown fields are rejected. A project-level Serena `.serena/` folder is rejected. | `config/`, `manifest/` |
| Prompt injection from comments, READMEs, AGENTS.md, tool output | Text from sources and tools is data. It cannot change the trusted policy, create an approval or skip validation (SEC-03). The MCP edit schema has no approval field. | `gateway/`, `editing/` |
| Reading outside the project | Lexical normalisation, `realpath`, root allowlist from the trusted policy, include/exclude globs, symlink target checks. Any failure is `PATH_NOT_ALLOWED`. | `config/paths.py`, `security/safeio.py` |
| An agent forges "approved" | No boolean approval is accepted anywhere. Approval is created only by the human CLI (`svweave edit approve`, interactive TTY), bound to patch hash, base snapshot, profile set, policy hash, transaction id and expiry, and signed with HMAC-SHA256. See the non-guarantees below. | `editing/approval.py` |
| Parser crash or resource abuse | The HDL worker is a separate process in its own process group with `RLIMIT_AS`, a wall timeout and process-tree cancellation; a crash does not take the gateway down. | `adapters/slang/runner.py` |
| Credential or IP exfiltration | No network for backends (`--unshare-net`), environment allowlist, redaction of keys, tokens and host paths in logs and evidence, bounded result sizes and snippets, evidence bundles without source text. | `security/` |
| Supply-chain drift | Exact versions and SHA-256 in `toolchain.lock.json`; the installer verifies every artifact; normal operation never downloads or auto-updates. A capability or toolchain mismatch is `CAPABILITY_MISMATCH`. | `scripts/bootstrap_locked.py`, ADR-010 |
| Runtime files end up in Git | `.gitignore` covers `.svweave/`, `.toolchain/`, `.venv/`; candidates and snapshots live under `.svweave/`. | `.gitignore` |
| Stale or concurrent modification | Immutable snapshots with content hashes, expected-hash CAS at prepare and commit, exclusive workspace lock, write-ahead journal, crash recovery that never overwrites bytes it did not write. | `editing/`, `docs/editing.md` |

## 2. Trust boundaries and the trusted directory

* The **trusted directory** `$XDG_CONFIG_HOME/svweave/workspaces/<workspace_key>/` (fallback
  `~/.config`) holds `policy.json`, the cursor-signing key, `approval.key` (0600), the approval
  records and the last backend probe record. It is **outside the repository**; a `--policy` path
  inside the project tree is `POLICY_DENIED`, and a repository-local policy file is ignored.
  `workspace_key = sha256(realpath(dir of svweave.yaml))[:16]`.
* The repository can only tighten the policy. It cannot add roots, enable editing that the policy
  disables, drop required human approval or list writable paths outside the policy. There is no
  network field in `svweave.yaml`.
* `.svweave/` (runtime state) must be a real directory owned by the current user. It holds
  snapshots, the semantic database, candidates, journals, run logs and backend state; it is never
  committed.

The trusted `policy.json` is refused (`policy_writable_by_others`) when it or its directory is
writable by other users; `svweave init` creates it 0600 in a 0700 directory. Group-write is allowed,
because user-private groups (umask 002) make the group the owner alone. A snapshot directory that
already exists is re-hashed before it is reused, so a `.svweave/` shipped inside a repository cannot
substitute other bytes, and cleaning up a snapshot never changes the permissions of a symlink
target. A project path may not start with `_ext/`, the prefix reserved for external roots inside a
snapshot.

## 3. Sandbox (bubblewrap) - ADR-011

Every upstream backend (codebase-memory-mcp, Serena with Verible) is started through an argument
array prefixed by `bwrap`:

| Guarantee (verified in the M0 spike and by `tests/integration/test_int_sandbox.py`) | Mechanism |
|---|---|
| Original source is not writable from a backend | read-only bind (or not mounted at all) |
| The pinned toolchain is read-only | read-only bind of `.toolchain/` |
| The backend writes only to its candidate and its own state directory | read-write binds limited to those two |
| No network (no DNS, no TCP) | `--unshare-net` |
| Backend dies with the gateway | `--die-with-parent`, own PID namespace |
| The user's real `$HOME` and `~/.ssh` are not visible | `$HOME` is not bound; state dirs are dedicated |
| cbm and Serena state is isolated from the user's global state | dedicated `HOME`, `XDG_*`, `SERENA_HOME`, `CBM_CACHE_DIR` |

**Non-guarantees:**

* `bwrap` 0.4.0 applies no CPU/memory/pids limits and no seccomp filter (not configured). Memory
  is limited for the HDL worker only (`RLIMIT_AS`, `analysis.max_worker_memory_mib`).
* `/usr` and parts of `/etc` are visible read-only.
* If the host forbids unprivileged user namespaces, bwrap fails. The gateway then reports
  isolation as unavailable and runs a backend only in an explicitly requested degraded mode that
  is flagged in health and in envelopes; Serena symbol edits are refused in degraded mode unless
  the caller opts in.
* The HDL worker (pyslang) runs inside bwrap as well (`adapters/slang/runner.py`): read-only
  Python runtime, venv and snapshot, a private tmpfs `/tmp`, no network, its own PID namespace,
  plus an isolated Python (`-I`), a clean environment, `RLIMIT_AS` and a wall timeout. A file
  outside the snapshot is not readable at all (an absolute `` `include`` of it fails as a missing
  include); a read of a sandbox-visible file that is not in the snapshot inventory (e.g. under
  `/etc`) is still caught by the dependency-leak check and the build is BLOCKED. Without bwrap the
  worker is not started (fail closed) unless `SVWEAVE_ALLOW_DEGRADED_WORKER=1` is set, and then
  the build envelope carries `isolation=degraded_no_isolation` and a warning.
* The sandbox protects against backend behaviour. It does not protect against an MCP client with
  its own shell.

## 4. Approval and the client boundary (plan 23.2)

SVWeave guarantees the behaviour of operations that go **through its gateway**. The approval flow
prevents:

* approval by an MCP agent (no approve tool, no approval field, unknown fields rejected);
* reuse of an approval for another patch, snapshot, profile set, policy or transaction, or after
  expiry (default 30 minutes);
* editing the approval record without the key (HMAC-SHA256);
* commit over a source that changed after validation (CAS, base-snapshot freshness).

It does **not** prevent, and `svweave doctor` says so in the always-present
**`client_boundary: not qualified`** item:

* An agent that runs as the **same UID with unrestricted host access** can read `approval.key`
  and write a valid approval, read `policy.json`, or edit the workspace directly. Such a
  deployment must not be described as unforgeable.
* An MCP client that has its own shell or file tools can write the original source tree.

A controlled deployment must give the agent only read-only snapshots/candidates (or an OS policy
that denies it the original source and the trusted directory: separate UID, read-only mounts, an
approver on another host) and approve in a real terminal. Until that is qualified, **editing is
disabled by default** (`editing_enabled: false` in the trusted policy and `editing.enabled: false`
in the repo). `SVWEAVE_TEST_CONFIRM_TOKEN` (test harness only; ignored on the command line) records
`method: test_token` and is never qualified.

## 5. Redaction and data egress

* Raw backend stderr stays in the backend's private state directory (mode 0600). Everything that
  is surfaced (errors, health details, evidence, doctor output, debug bundles) passes through the
  redactor: credential-looking assignments, bearer tokens, well-known token formats, private-key
  blocks, URL credentials, and absolute host paths outside system prefixes.
* `svweave debug bundle` writes hashes, the normalized manifest, a diagnostics summary without
  message text, an inventory without file contents and a semantic digest. Source inclusion is a
  separate manual decision; nothing is uploaded automatically.
* Results are bounded (128 KiB per response, limit <= 500 per page).
* **Local indexing is not end-to-end offline.** If your MCP client uses a cloud model, any source
  snippet returned by a tool is sent to that model service. Decide the endpoint and data
  classification before connecting protected source.

## 6. Network

The backends have no network. The gateway has no listening socket (no HTTP, ADR-009); stdio MCP
only. The **install phase** (`scripts/bootstrap_locked.py`) is the only code that downloads, from
the URLs in `toolchain.lock.json`, and verifies SHA-256 before use. Normal operation never
downloads and never updates.

What the installer executes, and how each piece is verified:

* **uv**: the pinned release tarball (lock section `uv`, sha256 equal to the release's published
  checksum) is verified before it is opened and the extracted binary before it runs; every uv call
  uses it, with `UV_PYTHON_DOWNLOADS=never` (uv never fetches an interpreter). `--uv PATH` is an
  explicit override that is NOT verified; the report marks it (`components.uv.verified: false`,
  `warnings`).
* **Serena's build backends**: `uv.lock` does not lock build dependencies, so hatchling (and its
  dependencies) and setuptools (for the sdist-only `proxy-tools`) are hash-pinned in the lock
  (`serena_build_wheelhouse`), verified, installed into the Serena venv with
  `--no-index --require-hashes`, and the venv is synced with `--no-build-isolation`: no build backend
  comes from an index or an unverified cache entry.
* Serena's runtime dependencies come from `uv.lock` (hashes checked by uv), the Serena source from the
  pinned commit (`uv.lock` and `LICENSE` hashes checked), the gateway venv from the hash-locked
  wheelhouse.

Residual trust (not a gap in the lock, but worth knowing): uv reuses wheels it built earlier from its
cache. The cache shipped in `vendor/uv-cache.tar.gz` and one filled by `--fetch-only` were built with the
pinned backends; a `.toolchain/state/uv-cache` filled by an installer from before 2026-10-20 may hold
`proxy-tools` / `serena-agent` wheels built with an unpinned backend: delete it once. The committed
`vendor/` files are protected by `vendor/SHA256SUMS` and git history, not by the lock.

**Backend output is bounded** (gap found by the 2026-10-20 review, fixed the same day). The MCP SDK's
stdio client concatenates a backend's stdout until it sees a newline, with no length limit, so one
huge line (or one that never ends) grew the gateway's memory until the backend was stopped; a call
timeout did not stop the reader. The gateway now uses its own transport
(`src/svweave/adapters/bounded_stdio.py`): the SDK's `stdio_client` contract (same launch in a new
process group, same streams, JSON-RPC parsing and shutdown sequence) with a reader that reads raw
bytes and never buffers more than the limit plus one 64 KiB read.

* A stdout line longer than `MAX_MESSAGE_BYTES` = 16 MiB is a protocol error: the reader stops,
  the backend's process group is terminated at once (SIGTERM, then SIGKILL), the client reaps it like
  `stop()` does, and the call (or start-up) fails with `BACKEND_UNAVAILABLE`,
  `details.reason = "message_too_large"`, `details.protocol_error = {kind, limit_bytes,
  received_bytes_at_least}`.
* A tool result whose concatenated text exceeds `MAX_RESULT_TEXT_CHARS` = 16 Mi characters (checked
  before concatenating) fails with `OUTPUT_LIMIT`, `details.reason = "result_too_large"`, and the
  backend is stopped the same way.
* The limits sit above what the pinned backends can legitimately send (measured 2026-10-20): cbm
  0.11.0 itself refuses any response above 10,485,744 bytes (the largest real response seen was a
  7.7 MB `get_code_snippet` of a 7 MiB single-line module), and Serena 1.7.0 truncates each answer to
  150,000 characters (`default_max_tool_answer_chars`).
* The limit counts the payload: a CRLF terminator's `\r` is not counted, and a line of exactly the
  limit is accepted. Concurrent calls on the same backend that did not cause the violation fail with
  `BACKEND_UNAVAILABLE`, `details.reason = "backend_terminated"`; only the offending call reports
  `result_too_large` with its own size. A flood the backend writes on its own (not in answer to a
  call) is also caught: the next call finds the session stopped and reports the violation.
* The developer probe (`scripts/probe_backends.py`, through `scripts/spikes/_mcp_common.py`) uses
  the same bounded transport.

## 7. Git and CI boundary (plan 23.4)

SVWeave never pushes, creates PRs, changes branch protection or configures secrets. The provided
workflow uses no secrets and no self-hosted runner. Untrusted pull requests must not reach private
EDA licence environments or company source. Real-backend jobs run only on manual dispatch.

### Qualification probe isolation (HDL-R10)

`scripts/probe_backends.py` routes all native backend processes through the mandatory
`SandboxRunner` boundary in `scripts/spikes/_sandbox.py`, including version/config helpers,
pyslang per-build workers, slang CLI and both MCP servers. Source and exact tool/runtime inputs
are read-only (including Serena's locked editable source tree); only dedicated state and CLI
artifact directories are writable. Environments are explicit, host secrets are not inherited,
and runtime networking is disabled. Unknown runtime layouts and unavailable isolation fail
closed; no degraded-mode flag or custom `--wrap` prefix bypasses this boundary.

Host orchestration and metadata-only lock verification remain trusted host code, not HDL
parsing. Each probe invocation creates a fresh `run-*` directory; the host writes its reports,
checks before/after source manifests, and rejects symlink/oversized dependency evidence.
One-shot native stdout/stderr and MCP stdout messages are bounded. MCP stderr goes to a
host-owned file and is not currently size-capped; no bound is claimed for that file.
Source immutability failure is never an accepted degraded subset.
The historical standalone `isolation_probe.py` and `verible_lsp_probe.py` experiments are **not**
part of qualification or this guarantee; the former's old custom-wrapper calls are now refused.
Do not use either as an alternative to the reviewed probe path.

The manual backend-validation job requires the host sandbox gate and
`tests/integration/test_int_probe_isolation.py` before any backend probe. Its native checks cover
read-only inputs, a distinct network namespace, secret exclusion, and detached-descendant
cleanup on timeout/output overflow. Pure boundary regressions live in
`tests/unit/test_ci_probe_isolation.py`; both modules map to G0. A green job is isolated backend
validation, not a release decision or a substitute for `scripts/qualify.py`, owner waivers,
license sign-off, or EDA qualification.

## 8. Reporting issues

Report security problems privately to the repository owner (contact: TBD by owner). Include the
version, `toolchain.lock.json` hash and a redacted `svweave doctor` output.

## Slurm control-plane exception is not approved

The new typed scheduler policy field grants nothing by default. Runtime scheduler networking requires separate owner approval; backend network isolation remains mandatory. Current site contracts remain unqualified and no live launch is enabled. Repository local configuration cannot approve a site, and srun editing/candidate validation is denied. See [ADR-015](adr/ADR-015-slurm-execution-boundary.md) and [Slurm boundaries](srun.md).
