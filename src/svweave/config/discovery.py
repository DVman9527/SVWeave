"""Startup-only project selection for a shared MCP installation.

Discovery is not authorization. The selected canonical directory is bound once to a normal
WorkspaceOptions/Session, whose existing trusted policy must authorize it. No MCP request can
select or switch the directory. We never load .env files or accept MCP roots as policy.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from pathlib import Path

from svweave.config.context import MANIFEST_FILENAME
from svweave.domain.errors import ErrorCode, SvwError

PROJECT_ENV = "SVWEAVE_PROJECT_ROOT"
CLAUDE_PROJECT_ENV = "CLAUDE_PROJECT_DIR"


def _directory(path: Path, source: str) -> Path:
    try:
        root = path.resolve(strict=True)
        if not root.is_dir():
            raise ValueError("not a directory")
    except (OSError, RuntimeError, ValueError) as exc:
        raise SvwError(
            ErrorCode.INVALID_CONFIG,
            f"{source} must name an existing project directory; no fallback was used",
        ) from exc
    return root


def _has_manifest(root: Path) -> bool:
    manifest = root / MANIFEST_FILENAME
    # A symlink is not a second identity for a manifest. In particular, do not associate an
    # external manifest with the selected directory's trusted policy.
    try:
        if manifest.is_symlink():
            raise SvwError(ErrorCode.POLICY_DENIED, "workspace manifest must not be a symlink")
        if manifest.exists():
            if not manifest.is_file():
                raise SvwError(ErrorCode.INVALID_CONFIG, "workspace manifest is not a regular file")
            return True
        return False
    except OSError as exc:
        raise SvwError(ErrorCode.POLICY_DENIED, "cannot inspect workspace manifest") from exc


def _exact(path: Path, source: str) -> Path:
    root = _directory(path, source)
    if not _has_manifest(root):
        raise SvwError(
            ErrorCode.MISSING_INPUT,
            f"{source} has no {MANIFEST_FILENAME}; no fallback was used",
        )
    return root


def _nearest(path: Path, source: str) -> Path | None:
    current = _directory(path, source)
    for candidate in (current, *current.parents):
        if _has_manifest(candidate):
            return candidate
        # Both a normal checkout and a worktree/submodule are boundaries. Never borrow the
        # parent project's policy just because a nested checkout has not been initialized.
        if os.path.lexists(candidate / ".git"):
            break
    return None


def discover_workspace(
    *,
    repo: Path | None = None,
    workspace: Path | None = None,
    cwd: Path | None = None,
    environ: Mapping[str, str] | None = None,
) -> Path:
    """Resolve one project, or fail closed on missing/ambiguous selection.

    Explicit paths win; an explicit SVWEAVE_PROJECT_ROOT is next. Otherwise a valid Claude
    project hint and nearest cwd manifest must agree whenever both select a project. A bad
    explicit path/hint never falls through to cwd. Cwd discovery is best effort: clients need
    not spawn an MCP process in their active project, so a host-supplied override may be needed.
    """
    env = os.environ if environ is None else environ
    if repo is not None or workspace is not None:
        roots = [_exact(p, "explicit workspace") for p in (repo, workspace) if p is not None]
        if len(set(roots)) != 1:
            raise SvwError(ErrorCode.INVALID_CONFIG, "conflicting explicit workspace paths")
        return roots[0]
    if PROJECT_ENV in env:
        value = env[PROJECT_ENV]
        if not value or not Path(value).is_absolute():
            raise SvwError(ErrorCode.INVALID_CONFIG, f"{PROJECT_ENV} must be an absolute directory")
        return _exact(Path(value), PROJECT_ENV)

    hint = None
    if CLAUDE_PROJECT_ENV in env:
        value = env[CLAUDE_PROJECT_ENV]
        if not value or not Path(value).is_absolute():
            raise SvwError(
                ErrorCode.INVALID_CONFIG, f"{CLAUDE_PROJECT_ENV} must be an absolute directory"
            )
        hint = _nearest(Path(value), CLAUDE_PROJECT_ENV)
        if hint is None:
            raise SvwError(
                ErrorCode.MISSING_INPUT,
                f"{CLAUDE_PROJECT_ENV} has no workspace before its repository boundary; "
                "no fallback was used",
            )
    current = _nearest(cwd or Path.cwd(), "launch directory")
    if hint is not None and current is not None and hint != current:
        raise SvwError(
            ErrorCode.INVALID_CONFIG,
            "ambiguous workspace: client project hint and launch directory disagree; "
            f"set {PROJECT_ENV} or use --repo explicitly",
        )
    selected = hint or current
    if selected is None:
        raise SvwError(
            ErrorCode.MISSING_INPUT,
            f"no workspace selected: launch from a project containing {MANIFEST_FILENAME}, "
            f"set {PROJECT_ENV}, or use --repo; the client may not preserve project cwd",
        )
    return selected
