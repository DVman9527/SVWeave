"""Trusted policy vs repository config: a profile may only TIGHTEN policy (plan 8.1)."""

from __future__ import annotations

import os
from pathlib import Path

from pydantic import BaseModel

from svweave.config.models import LocalConfig, Manifest, Policy
from svweave.config.paths import glob_match, is_within, lexical_join
from svweave.domain.errors import ErrorCode, SvwError

#: Added to errors about a root the trusted policy does not allow (which file to edit, and why).
ROOT_HINT = (
    "add the absolute host path of this root to allowed_roots in the trusted policy.json "
    "(created outside the repository by `svweave init`; svweave.yaml and svweave.local.yaml "
    "cannot widen it, see docs/security.md)"
)
#: Added to 'not inside any declared source root' errors.
SOURCE_ROOT_HINT = (
    "declare a source_roots entry that contains this path; a profile cwd of '.' must lie inside one. "
    "For a project kept elsewhere use an external root (no path, mapped in svweave.local.yaml) "
    "and address it as '@<id>' (for example cwd: \"@proj\")"
)


class EffectivePolicy(BaseModel):
    """What is actually allowed: the intersection of trusted policy and repo config."""

    editing_enabled: bool
    approval_required: bool
    network_enabled: bool
    allowed_paths: list[str]


def _covered(pattern: str, policy_patterns: list[str]) -> bool:
    return any(pattern == p or glob_match(p, pattern) for p in policy_patterns)


def policy_roots(policy: Policy, project_root: Path) -> list[Path]:
    return [Path(os.path.realpath(lexical_join(project_root, r))) for r in policy.allowed_roots]


def _writable_location(
    root_id: str, path: str | None, local: LocalConfig | None, project_root: Path
) -> str | None:
    """Policy-comparable location of a source root: project-relative when inside the project,
    else the absolute path. External roots resolve through the root mapping (not their id)."""
    raw = path
    if raw is None:
        raw = next(
            (m.path for m in (local.root_mappings if local else []) if m.id == root_id), None
        )
    if raw is None:
        return None
    real = Path(os.path.realpath(lexical_join(project_root, raw)))
    if is_within(real, project_root):
        rel = real.relative_to(project_root).as_posix()
        return "." if rel == "." else rel
    return real.as_posix()


def check_policy_tightening(
    manifest: Manifest, local: LocalConfig | None, policy: Policy, project_root: Path
) -> EffectivePolicy:
    """Raise unless the repo config stays within the trusted policy; return effective policy."""
    roots = policy_roots(policy, project_root)

    def inside(p: Path) -> bool:
        real = Path(os.path.realpath(p))
        return any(is_within(real, r) for r in roots)

    for sr in manifest.source_roots:
        if sr.path is not None:
            abs_root = lexical_join(project_root, sr.path)
            if not inside(abs_root):
                raise SvwError(
                    ErrorCode.PATH_NOT_ALLOWED,
                    f"source root {sr.id!r} ({sr.path!r}) is outside policy allowed_roots",
                    {"root": sr.id, "path": sr.path, "hint": ROOT_HINT},
                )
        if sr.writable:
            rel = _writable_location(sr.id, sr.path, local, project_root)
            if rel is None:
                continue  # unmapped external root: nothing is reachable, so nothing is writable
            pat = "**" if rel in (".", "") else f"{rel.rstrip('/')}/**"
            if not _covered(pat, policy.writable_paths):
                raise SvwError(
                    ErrorCode.POLICY_DENIED,
                    f"source root {sr.id!r} is writable but policy.writable_paths does not cover it",
                    {"root": sr.id},
                )
    if local is not None:
        for m in local.root_mappings:
            if not inside(lexical_join(project_root, m.path)):
                raise SvwError(
                    ErrorCode.PATH_NOT_ALLOWED,
                    f"root mapping {m.id!r} points outside policy allowed_roots",
                    {"root": m.id, "path": m.path, "hint": ROOT_HINT},
                )

    ed = manifest.editing
    if ed.enabled and not policy.editing_enabled:
        raise SvwError(
            ErrorCode.POLICY_DENIED,
            "svweave.yaml editing.enabled=true but trusted policy disables editing",
            {"field": "editing.enabled"},
        )
    if not ed.require_human_approval and policy.approval_required:
        raise SvwError(
            ErrorCode.POLICY_DENIED,
            "svweave.yaml cannot drop human approval required by trusted policy",
            {"field": "editing.require_human_approval"},
        )
    for ap in ed.allowed_paths if ed.enabled else []:  # inert while editing is disabled
        if not _covered(ap, policy.writable_paths):
            raise SvwError(
                ErrorCode.POLICY_DENIED,
                f"editing.allowed_paths entry {ap!r} is not covered by policy.writable_paths",
                {"field": "editing.allowed_paths", "path": ap},
            )
    return EffectivePolicy(
        editing_enabled=ed.enabled and policy.editing_enabled,
        approval_required=ed.require_human_approval or policy.approval_required,
        network_enabled=policy.network_enabled,
        allowed_paths=list(ed.allowed_paths) if ed.enabled and policy.editing_enabled else [],
    )
