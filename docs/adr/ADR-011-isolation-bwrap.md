# ADR-011: Process/filesystem/network isolation with bubblewrap (bwrap)

- Status: Accepted (M0 spike; qualification still pending)
- Date: 2026-10-13
- Evidence: `scripts/spikes/isolation_probe.py`, `evidence/m0/isolation_probe.json`,
  `evidence/m0/cbm_spike_bwrap.json`, `evidence/m0/serena_spike_bwrap.json`

## Context
Hiding MCP tools or setting a read-only mode is not an OS sandbox (plan §6.4, S07). Backends may
receive only the snapshot/candidate and their own state directory, with network off by default
(plan §7.1). Host: Linux 4.18 (EL8), `/usr/bin/bwrap` 0.4.0, `user.max_user_namespaces=78922`.
Unprivileged user namespaces **work** on this host (no setuid needed).

## Decision
Each upstream backend process is started through a bwrap argv prefix. The gateway builds it as an
argument array, never as a shell string:

```text
/usr/bin/bwrap --die-with-parent --unshare-user --unshare-pid --unshare-ipc --unshare-uts
  --unshare-cgroup-try --unshare-net
  --ro-bind /usr /usr --ro-bind /etc /etc
  --symlink usr/bin /bin --symlink usr/sbin /sbin --symlink usr/lib /lib --symlink usr/lib64 /lib64
  --proc /proc --dev /dev --tmpfs /tmp
  [--ro-bind /usr/local /usr/local]                    # Serena: python3.12 lives here on this host
  --ro-bind <repo>/.toolchain <repo>/.toolchain        # pinned binaries/venvs read-only
  --ro-bind <repo>/fixtures <repo>/fixtures            # original source read-only
  --bind <state-dir> <state-dir>                       # backend's dedicated HOME/cache (RW)
  --bind <candidate> <candidate>                       # Serena only (RW)
  --ro-bind <snapshot> <snapshot>                      # cbm: the immutable snapshot is RO
  -- <backend argv>
```
Later binds stack on top of earlier ones, so an RW state dir inside the RO `.toolchain` works. The
user's real `$HOME` is not bound: inside the sandbox only the empty intermediate path
`${HOME}/projects` exists, and `~/.ssh` is not visible.

## Results (real backends, M0)
| Check | Result inside bwrap | Baseline without bwrap |
|---|---|---|
| write into original `fixtures/` | DENIED `[Errno 30] Read-only file system` | writable |
| write into `.toolchain/verible/bin` | DENIED `[Errno 30]` | writable |
| write into candidate / state dir | WRITABLE | writable |
| write into repo root path | succeeds only in bwrap's ephemeral tmpfs root; `repo_root_write_reached_host_disk=false` | writable |
| TCP connect 1.1.1.1:443 | BLOCKED `[Errno 101] Network is unreachable` | CONNECTED |
| DNS pypi.org | BLOCKED `gaierror -2` | resolved |
| codebase-memory-mcp v0.11.0 full spike (index RO snapshot, queries) | rc=0, same 27 nodes / 36 edges, snapshot manifest unchanged | rc=0 |
| Serena v1.7.0 + Verible full spike (candidate edit) | rc=0, `svw_env/build_phase` edit PASS, original fixtures unchanged | rc=0 |

## Consequences / limitations
- The template must be built per backend from the manifest (snapshot, candidate and state paths).
  `--die-with-parent` ties the backend's lifetime to the gateway.
- Any path not bound into the sandbox is invisible to the backend, and so cannot be read either.
  Every include dir or library outside the snapshot must therefore be bound read-only explicitly,
  or it silently disappears. The adapter should treat "file not found" from a sandboxed backend as
  a configuration error, never as missing source.
- `/etc` and `/usr` are visible read-only. Resource limits are not applied by bwrap 0.4.0; use
  `prlimit`/`ulimit` or cgroups separately. seccomp is not configured (not attempted in M0).
- codebase-memory-mcp warns `daemon.private_dir_group_writable_ancestor mode=0775` because ancestors
  of the state dir (`${HOME}/projects`) are group-writable. The state dir itself is chmod 700.
- If a host forbids unprivileged user namespaces, bwrap fails at startup. That case was not observed
  here. The gateway must then report isolation as UNAVAILABLE and run only in the explicitly
  degraded mode (plan §6.4).
