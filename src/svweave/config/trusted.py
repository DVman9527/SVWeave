"""Trusted (out-of-repo) state: policy location, ownership checks.

The trusted policy must never be readable from, or writable through, the repository tree:
a committed `policy.json` could otherwise grant itself roots, editing and network access.
Layout: `$XDG_CONFIG_HOME` (fallback `~/.config`) `/svweave/workspaces/<workspace_key>/`
where `workspace_key = sha256(realpath(dir containing svweave.yaml))[:16]`. M4 will keep the
approval store in the same directory (`trusted_dir`).
"""

from __future__ import annotations

import os
from pathlib import Path

from svweave.config.loader import load_policy
from svweave.config.models import Manifest, Policy
from svweave.config.paths import is_within, lexical_join
from svweave.domain.errors import ErrorCode, SvwError
from svweave.domain.ids import sha256_hex


def config_home() -> Path:
    xdg = os.environ.get("XDG_CONFIG_HOME")
    if xdg and os.path.isabs(xdg):
        return Path(xdg)
    return Path.home() / ".config"


def workspace_key(manifest_dir: Path) -> str:
    return sha256_hex(os.path.realpath(manifest_dir))[:16]


def trusted_dir(manifest_dir: Path) -> Path:
    """Directory holding trusted per-workspace state (policy now, approval store in M4)."""
    return config_home() / "svweave" / "workspaces" / workspace_key(manifest_dir)


def policy_path_for(manifest_path: Path, override: Path | None = None) -> Path:
    return override if override is not None else trusted_dir(manifest_path.parent) / "policy.json"


def ensure_outside_tree(path: Path, *trees: Path) -> None:
    real = Path(os.path.realpath(path))
    for t in trees:
        if is_within(real, Path(os.path.realpath(t))):
            raise SvwError(
                ErrorCode.POLICY_DENIED,
                f"trusted policy path {path} is inside the project tree; policy must live outside "
                "the repository",
                {"path": str(path), "tree": str(t)},
            )


def check_state_dir(path: Path) -> None:
    """The runtime state dir must be a real directory owned by the current user."""
    try:
        st = os.lstat(path)
    except FileNotFoundError:
        return
    if os.path.islink(path):
        raise SvwError(
            ErrorCode.POLICY_DENIED,
            f"{path} is a symlink; refusing to use it as state dir",
            {"path": str(path)},
        )
    if st.st_uid != os.geteuid():
        raise SvwError(
            ErrorCode.POLICY_DENIED,
            f"{path} is not owned by the current user",
            {"path": str(path), "uid": st.st_uid},
        )


def load_trusted_policy(
    manifest_path: Path, manifest: Manifest, override: Path | None
) -> tuple[Path, Policy]:
    """The ONE way to find and load the policy (used by WorkspaceContext and doctor)."""
    mdir = manifest_path.parent
    project_root = lexical_join(mdir, manifest.project.root)
    ppath = policy_path_for(manifest_path, override)
    ensure_outside_tree(ppath, mdir, project_root)
    if not ppath.exists():
        raise SvwError(
            ErrorCode.INVALID_CONFIG,
            f"trusted policy not found at {ppath}; run `svweave init`",
            {"file": str(ppath)},
        )
    return ppath, load_policy(ppath)
