"""bubblewrap (bwrap) sandbox runner (ADR-011, plan 6.4 / 7.1 / 23.1, test SEC-05).

Every upstream backend process is started through a bwrap argv prefix built as an argument array
(never a shell string). The backend sees only:

* a minimal read-only system (``/usr``, ``/lib*``, ``/bin``, selected ``/etc`` files),
* paths explicitly bound read-only (pinned toolchain, immutable snapshot, optionally the original
  source *read-only*; by default the original source is not mounted at all),
* paths explicitly bound read-write (candidate directory, the backend's own state directory).

Network is off by default (``--unshare-net``). If bwrap cannot be used on the host, nothing runs in
a silently weaker mode: :class:`SandboxRunner` either raises ``BACKEND_UNAVAILABLE`` or, only when
the caller opted in with ``allow_degraded=True``, returns a command whose ``isolation`` is
``DEGRADED_NO_ISOLATION``. Callers must surface that flag (health reasons, envelope warnings).
"""

from __future__ import annotations

import os
import shutil
import subprocess
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Literal

from svweave.domain.errors import ErrorCode, SvwError

DEFAULT_BWRAP = "/usr/bin/bwrap"

#: /etc entries that dynamic linking / Python / name resolution of local users need.
MINIMAL_ETC: tuple[str, ...] = (
    "/etc/ld.so.cache",
    "/etc/ld.so.conf",
    "/etc/ld.so.conf.d",
    "/etc/passwd",
    "/etc/group",
    "/etc/nsswitch.conf",
    "/etc/localtime",
    "/etc/alternatives",
)


class IsolationMode(StrEnum):
    BWRAP = "bwrap"
    DEGRADED_NO_ISOLATION = "degraded_no_isolation"


@dataclass(frozen=True)
class HostCapability:
    """Result of probing whether unprivileged bwrap works on this host."""

    available: bool
    bwrap_path: str | None
    version: str | None
    reason: str | None  # why unavailable (raw, unredacted tool output tail)


_PROBE_CACHE: dict[str, HostCapability] = {}


def resolve_bwrap(bwrap_path: str | None = None) -> str | None:
    if bwrap_path:
        return bwrap_path if os.access(bwrap_path, os.X_OK) else None
    if os.access(DEFAULT_BWRAP, os.X_OK):
        return DEFAULT_BWRAP
    return shutil.which("bwrap")


def probe_host(bwrap_path: str | None = None, *, refresh: bool = False) -> HostCapability:
    """Run ``bwrap ... true`` with the real unshare flags we depend on; cached per bwrap path."""
    exe = resolve_bwrap(bwrap_path)
    if exe is None:
        return HostCapability(False, None, None, "bwrap executable not found")
    if not refresh and exe in _PROBE_CACHE:
        return _PROBE_CACHE[exe]
    version: str | None = None
    try:
        v = subprocess.run([exe, "--version"], capture_output=True, text=True, timeout=10, check=False)
        version = v.stdout.strip() or None
        argv = [
            exe,
            "--die-with-parent",
            "--unshare-user",
            "--unshare-pid",
            "--unshare-ipc",
            "--unshare-uts",
            "--unshare-net",
            "--ro-bind",
            "/usr",
            "/usr",
            "--symlink",
            "usr/bin",
            "/bin",
            "--symlink",
            "usr/lib",
            "/lib",
            "--symlink",
            "usr/lib64",
            "/lib64",
            "--proc",
            "/proc",
            "--dev",
            "/dev",
            "--new-session",
            "--",
            "/bin/true",
        ]
        p = subprocess.run(argv, capture_output=True, text=True, timeout=20, check=False)
        cap = (
            HostCapability(True, exe, version, None)
            if p.returncode == 0
            else HostCapability(False, exe, version, (p.stderr or p.stdout).strip()[-500:] or f"rc={p.returncode}")
        )
    except (OSError, subprocess.SubprocessError) as e:
        cap = HostCapability(False, exe, version, f"{type(e).__name__}: {e}")
    _PROBE_CACHE[exe] = cap
    return cap


@dataclass(frozen=True)
class SandboxSpec:
    """What a sandboxed backend may see. All paths absolute."""

    ro_paths: tuple[Path, ...] = ()
    rw_paths: tuple[Path, ...] = ()
    chdir: Path | None = None
    network: bool = False
    #: ORIGINAL source roots that must never become writable inside the sandbox. An rw path equal
    #: to / inside / containing one of these is refused (a ro bind is allowed, not required).
    protected_paths: tuple[Path, ...] = ()
    #: rw paths that may sit INSIDE a protected root (the backend's own state directory under the
    #: workspace's ``.svweave``). Matched exactly; never allowed to contain a protected path.
    rw_exempt: tuple[Path, ...] = ()
    env: Mapping[str, str] = field(default_factory=dict)
    etc: Literal["minimal", "full"] = "minimal"
    #: Empty private directories created on the sandbox's ephemeral ``/tmp`` (e.g. a short
    #: socket directory); invisible to the host and gone with the sandbox.
    tmp_dirs: tuple[str, ...] = ()
    #: Symlinks recreated inside the sandbox as ``(target, link path)`` after the binds (e.g. the
    #: ``bin/python3 -> python3.12`` link a venv's interpreter resolves through: ``ro_paths`` are
    #: bound by realpath, so an intermediate link would otherwise not exist). A link path that is
    #: already mounted (:func:`mount_providing`) is refused: it would be created on the host.
    symlinks: tuple[tuple[str, Path], ...] = ()


_FORBIDDEN_RW = {Path("/"), Path("/usr"), Path("/etc"), Path("/proc"), Path("/dev"), Path("/bin"), Path("/lib"), Path("/lib64")}


def _norm(p: Path) -> Path:
    if not p.is_absolute():
        raise SvwError(ErrorCode.PATH_NOT_ALLOWED, "sandbox paths must be absolute", {"path": str(p)})
    return Path(os.path.realpath(p))


def _overlaps(a: Path, b: Path) -> bool:
    return a == b or a in b.parents or b in a.parents


#: Top-level paths that are links into the read-only ``/usr`` or special mounts inside the sandbox.
_SYSTEM_MOUNTS: tuple[Path, ...] = (
    Path("/usr"), Path("/bin"), Path("/sbin"), Path("/lib"), Path("/lib64"), Path("/proc"), Path("/dev"),
)


def _etc_binds(etc: Literal["minimal", "full"]) -> list[str]:
    return ["/etc"] if etc == "full" else [e for e in MINIMAL_ETC if os.path.exists(e)]


def mount_providing(spec: SandboxSpec, path: Path) -> Path | None:
    """The mount under which ``path`` (absolute, as seen inside the sandbox) already exists: the
    system view (``/usr`` and its aliases, the ``/etc`` binds, ``/proc``, ``/dev``) or one of the
    spec's binds. ``None`` means the path lives on the sandbox's private root, where a symlink can be
    created without touching the host."""
    binds = [*_SYSTEM_MOUNTS, *map(Path, _etc_binds(spec.etc)), *map(_norm, (*spec.ro_paths, *spec.rw_paths))]
    for b in binds:
        if b == path or b in path.parents:
            return b
    return None


def validate_spec(spec: SandboxSpec) -> tuple[list[Path], list[Path]]:
    """Normalize paths and enforce the write-surface rules. Returns (ro, rw) realpaths."""
    ro = [_norm(p) for p in spec.ro_paths]
    rw = [_norm(p) for p in spec.rw_paths]
    protected = [_norm(p) for p in spec.protected_paths]
    exempt = {_norm(p) for p in spec.rw_exempt}
    home = Path(os.path.realpath(Path.home()))
    for w in rw:
        if w in _FORBIDDEN_RW or w == home or w in home.parents:
            raise SvwError(
                ErrorCode.POLICY_DENIED,
                "refusing a writable sandbox bind that exposes system or home directories",
                {"path": str(w)},
            )
        for pr in protected:
            if w in exempt and pr in w.parents:
                continue  # exact state dir below a protected root; does not contain one
            if _overlaps(w, pr):
                raise SvwError(
                    ErrorCode.POLICY_DENIED,
                    "writable sandbox path overlaps the protected original source",
                    {"path": str(w), "protected": str(pr)},
                )
        for r in ro:
            if r == w:
                raise SvwError(
                    ErrorCode.POLICY_DENIED,
                    "path bound both read-only and read-write",
                    {"path": str(w)},
                )
    for r in ro:
        if r in (Path("/"), home):
            raise SvwError(
                ErrorCode.POLICY_DENIED, "refusing to expose / or $HOME to a backend", {"path": str(r)}
            )
    for _target, link in spec.symlinks:
        if not link.is_absolute() or ".." in link.parts:
            raise SvwError(ErrorCode.PATH_NOT_ALLOWED, "sandbox symlink path not allowed", {"path": str(link)})
        bound = mount_providing(spec, link)
        if bound is not None:
            raise SvwError(
                ErrorCode.POLICY_DENIED,
                "sandbox symlink inside a bound path would be created on the host",
                {"path": str(link), "bound": str(bound)},
            )
    return ro, rw


def build_bwrap_argv(
    spec: SandboxSpec, inner_argv: Sequence[str], *, bwrap: str = DEFAULT_BWRAP
) -> list[str]:
    """Build the full argv (``bwrap ... -- inner_argv``). Pure function; touches no processes.

    Binds are emitted shallow-to-deep so that a nested bind (a RW state dir inside the RO
    toolchain) stacks on top of its parent instead of being masked by it.
    """
    ro, rw = validate_spec(spec)
    argv: list[str] = [
        bwrap,
        "--die-with-parent",
        "--new-session",
        "--unshare-user",
        "--unshare-pid",
        "--unshare-ipc",
        "--unshare-uts",
        "--unshare-cgroup-try",
    ]
    if not spec.network:
        argv.append("--unshare-net")
    argv += [
        "--ro-bind",
        "/usr",
        "/usr",
        "--symlink",
        "usr/bin",
        "/bin",
        "--symlink",
        "usr/sbin",
        "/sbin",
        "--symlink",
        "usr/lib",
        "/lib",
        "--symlink",
        "usr/lib64",
        "/lib64",
    ]
    for e in _etc_binds(spec.etc):
        argv += ["--ro-bind", e, e]
    argv += ["--proc", "/proc", "--dev", "/dev", "--tmpfs", "/tmp"]
    for d in spec.tmp_dirs:
        if not d.startswith("/tmp/") or ".." in d.split("/"):
            raise SvwError(ErrorCode.PATH_NOT_ALLOWED, "tmp_dirs must live under /tmp/", {"path": d})
        argv += ["--dir", d]
    binds: list[tuple[Path, bool]] = [(p, False) for p in ro] + [(p, True) for p in rw]
    binds.sort(key=lambda t: (len(t[0].parts), t[1]))
    for path, writable in binds:
        argv += ["--bind" if writable else "--ro-bind", str(path), str(path)]
    for target, link in spec.symlinks:
        argv += ["--symlink", target, str(link)]
    if spec.chdir is not None:
        argv += ["--chdir", str(_norm(spec.chdir))]
    # bwrap 0.4.0 has no --clearenv: clear the environment with env -i inside the sandbox.
    argv += ["--", "/usr/bin/env", "-i", *[f"{k}={v}" for k, v in spec.env.items()], *inner_argv]
    return argv


@dataclass(frozen=True)
class WrappedCommand:
    argv: list[str]
    isolation: IsolationMode
    #: Human-readable, never empty when ``isolation`` is degraded; callers must surface it.
    warnings: tuple[str, ...] = ()
    env: dict[str, str] = field(default_factory=dict)


class SandboxRunner:
    """Decides sandbox vs explicit degraded mode and wraps argv accordingly."""

    def __init__(
        self,
        *,
        allow_degraded: bool = False,
        bwrap_path: str | None = None,
        capability: HostCapability | None = None,
    ) -> None:
        self.allow_degraded = allow_degraded
        self.capability = capability if capability is not None else probe_host(bwrap_path)

    @property
    def mode(self) -> IsolationMode:
        return IsolationMode.BWRAP if self.capability.available else IsolationMode.DEGRADED_NO_ISOLATION

    def wrap(self, spec: SandboxSpec, inner_argv: Sequence[str]) -> WrappedCommand:
        env = dict(spec.env)
        if self.capability.available and self.capability.bwrap_path:
            return WrappedCommand(
                build_bwrap_argv(spec, inner_argv, bwrap=self.capability.bwrap_path),
                IsolationMode.BWRAP,
                (),
                env,
            )
        reason = self.capability.reason or "bwrap unavailable"
        if not self.allow_degraded:
            raise SvwError(
                ErrorCode.BACKEND_UNAVAILABLE,
                "process isolation (bwrap) is unavailable and degraded mode was not allowed",
                {"isolation": "UNAVAILABLE", "reason": reason},
            )
        # Still validate the write surface so a bad spec fails the same way in both modes.
        validate_spec(spec)
        argv = ["/usr/bin/env", "-i", *[f"{k}={v}" for k, v in env.items()], *inner_argv]
        warn = (
            "DEGRADED ISOLATION: backend runs WITHOUT a filesystem/network sandbox "
            f"({reason}); editing guarantees do not hold",
        )
        return WrappedCommand(argv, IsolationMode.DEGRADED_NO_ISOLATION, warn, env)

    def run_once(
        self,
        spec: SandboxSpec,
        inner_argv: Sequence[str],
        *,
        timeout: float = 60.0,
        input_text: str | None = None,
    ) -> subprocess.CompletedProcess[str]:
        """Run a short one-shot command (e.g. ``cbm config set``) under the same sandbox."""
        cmd = self.wrap(spec, inner_argv)
        return subprocess.run(
            cmd.argv,
            capture_output=True,
            text=True,
            timeout=timeout,
            input=input_text,
            env=cmd.env if self.mode is IsolationMode.DEGRADED_NO_ISOLATION else {"PATH": "/usr/bin:/bin"},
            check=False,
        )
