"""WorkspaceContext: loaded config + trusted roots + logical path mapping.

Logical paths (plan 9.4): project-internal files use their POSIX path relative to the project
root; files under an external root (outside the project root, or declared without a path and
mapped through svweave.local.yaml) use `@<root_id>/<relative path>`. Logical paths, not host
absolute paths, feed config_id and snapshot_id, so identities survive moving a checkout.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from svweave.config.execution import ExecutionSelection, resolve_execution
from svweave.config.loader import load_local_config, load_manifest
from svweave.config.models import LocalConfig, Manifest, Policy
from svweave.config.paths import PathPolicy, glob_match, is_within, lexical_join
from svweave.config.policy import (
    ROOT_HINT,
    SOURCE_ROOT_HINT,
    EffectivePolicy,
    check_policy_tightening,
    policy_roots,
)
from svweave.config.trusted import check_state_dir, load_trusted_policy, trusted_dir
from svweave.domain.errors import ErrorCode, SvwError

STATE_DIRNAME = ".svweave"
MANIFEST_FILENAME = "svweave.yaml"
LOCAL_FILENAME = "svweave.local.yaml"
POLICY_FILENAME = "policy.json"


@dataclass(frozen=True)
class RootInfo:
    id: str
    abs: Path | None
    internal: bool
    include: tuple[str, ...]
    exclude: tuple[str, ...]
    writable: bool


@dataclass(frozen=True)
class ResolvedPath:
    abs: Path  # lexical, normalised absolute path (what the user named)
    real: Path  # fully resolved
    logical: str
    symlink_target: str | None  # logical path of the real target when it differs


def snapshot_rel(logical: str) -> str:
    """Location of a logical path inside <snapshot>/files/."""
    if logical.startswith("@"):
        rid, _, rest = logical[1:].partition("/")
        return f"_ext/{rid}/{rest}"
    return logical


def _abs(root: RootInfo) -> Path:
    if root.abs is None:
        raise SvwError(
            ErrorCode.MISSING_INPUT,
            f"external root {root.id!r} has no mapping in {LOCAL_FILENAME}",
            {"reason": "root_unmapped", "root": root.id},
        )
    return root.abs


class WorkspaceContext:
    def __init__(
        self,
        project_root: Path,
        manifest: Manifest,
        local: LocalConfig | None,
        policy: Policy,
        manifest_dir: Path | None = None,
        execution: ExecutionSelection | None = None,
    ) -> None:
        self.project_root = Path(os.path.realpath(project_root))
        self.manifest_dir = Path(os.path.realpath(manifest_dir or project_root))
        check_state_dir(self.project_root / STATE_DIRNAME)
        self.manifest = manifest
        self.local = local
        self.policy = policy
        self.execution = resolve_execution(
            execution or (local.execution if local else ExecutionSelection()), policy.execution
        )
        self.path_policy = PathPolicy(policy_roots(policy, self.project_root))
        # project.root is repo-controlled: it may not lift the workspace out of the directory
        # holding svweave.yaml unless the trusted policy lists that exact root.
        if not is_within(self.project_root, self.manifest_dir) and self.project_root not in (
            self.path_policy.roots
        ):
            raise SvwError(
                ErrorCode.POLICY_DENIED,
                "project.root leaves the manifest directory and is not listed in policy "
                "allowed_roots",
                {"project_root": str(self.project_root)},
            )
        if not self.path_policy.contains_real(self.project_root):
            raise SvwError(
                ErrorCode.PATH_NOT_ALLOWED,
                "project root is outside policy allowed_roots",
                {"project_root": str(self.project_root), "hint": ROOT_HINT},
            )
        self.effective: EffectivePolicy = check_policy_tightening(
            manifest, local, policy, self.project_root
        )
        mapping = {m.id: m.path for m in (local.root_mappings if local else [])}
        roots: list[RootInfo] = []
        for sr in manifest.source_roots:
            raw = sr.path if sr.path is not None else mapping.get(sr.id)
            if raw is None:
                roots.append(
                    RootInfo(sr.id, None, False, tuple(sr.include), tuple(sr.exclude), sr.writable)
                )
                continue
            absr = Path(os.path.realpath(lexical_join(self.project_root, raw)))
            if not self.path_policy.contains_real(absr):
                raise SvwError(
                    ErrorCode.PATH_NOT_ALLOWED,
                    f"source root {sr.id!r} is outside policy allowed_roots",
                    {"root": sr.id, "hint": ROOT_HINT},
                )
            internal = sr.path is not None and is_within(absr, self.project_root)
            roots.append(
                RootInfo(sr.id, absr, internal, tuple(sr.include), tuple(sr.exclude), sr.writable)
            )
        self.roots = roots
        self._by_id = {r.id: r for r in roots}

    # ------------------------------------------------------------------ loading
    @classmethod
    def load(
        cls,
        workdir: Path,
        *,
        manifest_path: Path | None = None,
        local_path: Path | None = None,
        policy_path: Path | None = None,
        execution: ExecutionSelection | None = None,
    ) -> WorkspaceContext:
        mpath = manifest_path or (workdir / MANIFEST_FILENAME)
        manifest = load_manifest(mpath)
        project_root = Path(os.path.realpath(lexical_join(mpath.parent, manifest.project.root)))
        lpath = local_path or (mpath.parent / LOCAL_FILENAME)
        local = load_local_config(lpath) if lpath.exists() else None
        _, policy = load_trusted_policy(mpath, manifest, policy_path)
        return cls(project_root, manifest, local, policy, mpath.parent, execution)

    @property
    def workspace_id(self) -> str:
        return self.manifest.project.id

    @property
    def trusted_dir(self) -> Path:
        """Out-of-repo directory for trusted per-workspace state (policy, future approvals)."""
        return trusted_dir(self.manifest_dir)

    @property
    def state_dir(self) -> Path:
        return self.project_root / STATE_DIRNAME

    def profile(self, name: str | None) -> tuple[str, object]:
        pname = name or self.manifest.project.default_profile
        if pname not in self.manifest.profiles:
            raise SvwError(
                ErrorCode.INVALID_CONFIG,
                f"unknown profile {pname!r}",
                {"profiles": sorted(self.manifest.profiles)},
            )
        return pname, self.manifest.profiles[pname]

    # ------------------------------------------------------------------ paths
    def root_abs(self, root_id: str) -> Path:
        r = self._by_id.get(root_id)
        if r is None:
            raise SvwError(
                ErrorCode.PATH_NOT_ALLOWED,
                f"unknown root id {root_id!r}",
                {"root": root_id},
            )
        if r.abs is None:
            raise SvwError(
                ErrorCode.MISSING_INPUT,
                f"external root {root_id!r} has no mapping in {LOCAL_FILENAME}",
                {"reason": "root_unmapped", "root": root_id},
            )
        return r.abs

    def to_abs(self, logical: str) -> Path:
        if logical.startswith("@"):
            rid, _, rest = logical[1:].partition("/")
            return self.root_abs(rid) / rest
        return self.project_root / logical

    def _governing_root(self, lexical: Path) -> RootInfo | None:
        best: RootInfo | None = None
        for r in self.roots:
            if (
                r.abs is not None
                and is_within(lexical, r.abs)
                and (best is None or best.abs is None or len(r.abs.parts) > len(best.abs.parts))
            ):
                best = r
        return best

    def _logical(self, lexical: Path, root: RootInfo) -> str:
        root_abs = _abs(root)
        if is_within(lexical, self.project_root) and root.internal:
            rel = lexical.relative_to(self.project_root).as_posix()
            if rel == "_ext" or rel.startswith("_ext/"):
                # `_ext/<id>/...` is where EXTERNAL roots live inside a snapshot: a project file
                # there would collide with @<id>/... and be treated as external (not blocking)
                raise PathPolicy.deny(
                    rel,
                    "a project path may not start with the reserved directory '_ext/'",
                    "rename the directory, or declare it as an external root and address it as @<id>",
                )
            return "" if rel == "." else rel
        rel = lexical.relative_to(root_abs).as_posix()
        return f"@{root.id}/{'' if rel == '.' else rel}"

    def check_trusted(
        self, raw_abs: str, base: Path, shown: str | None = None
    ) -> tuple[Path, Path, RootInfo]:
        """Path-policy + symlink checks without include/exclude rules."""
        shown = shown if shown is not None else raw_abs
        lexical, real = self.path_policy.check(raw_abs, base)
        root = self._governing_root(lexical)
        if root is None or root.abs is None:
            raise PathPolicy.deny(shown, "not inside any declared source root", SOURCE_ROOT_HINT)
        if not any(r.abs is not None and is_within(real, r.abs) for r in self.roots):
            raise PathPolicy.deny(shown, "symlink target is outside every source root")
        return lexical, real, root

    def dir_excluded(self, lexical: Path) -> bool:
        root = self._governing_root(lexical)
        if root is None or root.abs is None:
            return True
        rel = lexical.relative_to(root.abs).as_posix() + "/"
        return any(glob_match(p, rel) for p in root.exclude)

    def _enforce_rules(self, raw: str, path: Path, root: RootInfo, is_dir: bool) -> None:
        rel = path.relative_to(_abs(root)).as_posix()
        rel = "" if rel == "." else rel
        probe = rel + "/" if is_dir or rel == "" else rel
        if not any(glob_match(p, probe) for p in root.include):
            raise PathPolicy.deny(raw, f"not matched by include rules of root {root.id!r}")
        if any(glob_match(p, probe) for p in root.exclude):
            raise PathPolicy.deny(raw, f"excluded by rules of root {root.id!r}")

    def resolve(
        self, raw: str, base: Path, *, is_dir: bool = False, rules: bool = True
    ) -> ResolvedPath:
        """Apply the path policy to `raw` and map it to a logical path.

        Raises PATH_NOT_ALLOWED for `..` escapes, outside absolute paths, symlink escapes,
        paths outside every source root, and paths excluded by source-root rules.
        """
        if raw.startswith("@"):
            rid, _, rest = raw[1:].partition("/")
            raw_abs = str(self.root_abs(rid) / rest)
        else:
            raw_abs = raw
        lexical, real, root = self.check_trusted(raw_abs, base, raw)
        if rules:
            self._enforce_rules(raw, lexical, root, is_dir)
        logical = self._logical(lexical, root)
        target = None
        if real != lexical:
            rroot = self._governing_root(real)
            if rroot is not None and rroot.abs is not None:
                if rules:  # the link target must satisfy the rules too (not only the link)
                    self._enforce_rules(raw, real, rroot, is_dir)
                target = self._logical(real, rroot)
            else:
                target = str(real)
        return ResolvedPath(lexical, real, logical, target)

    def allows_file(self, lexical: Path) -> bool:
        """Include/exclude check for enumerated files (no exception)."""
        root = self._governing_root(lexical)
        if root is None or root.abs is None:
            return False
        rel = lexical.relative_to(root.abs).as_posix()
        return any(glob_match(p, rel) for p in root.include) and not any(
            glob_match(p, rel) for p in root.exclude
        )

    def logical_of(self, lexical: Path) -> str:
        root = self._governing_root(lexical)
        if root is None:
            raise PathPolicy.deny(
                str(lexical), "not inside any declared source root", SOURCE_ROOT_HINT
            )
        return self._logical(lexical, root)
