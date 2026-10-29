"""Where a workspace lives; passed to every use case."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from pydantic import ValidationError

from svweave.config.context import WorkspaceContext
from svweave.config.execution import ExecutionSelection, resolve_execution
from svweave.domain.errors import ErrorCode, SvwError


@dataclass(frozen=True)
class WorkspaceOptions:
    root: Path
    manifest_path: Path | None = None
    policy_path: Path | None = None
    local_path: Path | None = None
    execution: ExecutionSelection | None = None
    execution_overrides: dict[str, str] | None = None
    # New shared-launcher sessions pin their startup directory. Legacy explicit CLI semantics
    # remain unchanged when this opt-in is None; policy remains checked on every load.
    bound_manifest_dir: Path | None = None

    def check_binding(self) -> None:
        if self.bound_manifest_dir is None:
            return
        from svweave.config.context import MANIFEST_FILENAME

        manifest = self.manifest_path or (self.root / MANIFEST_FILENAME)
        try:
            if manifest.parent.resolve(strict=True) != self.bound_manifest_dir or manifest.is_symlink():
                raise SvwError(
                    ErrorCode.POLICY_DENIED,
                    "bound workspace path changed; restart with an explicitly selected workspace",
                )
        except (OSError, RuntimeError, ValueError) as exc:
            raise SvwError(ErrorCode.POLICY_DENIED, "cannot verify bound workspace path") from exc

    def workspace_id(self) -> str | None:
        """Best-effort workspace id from the manifest alone (for error envelopes)."""
        from svweave.config.context import MANIFEST_FILENAME
        from svweave.config.loader import load_manifest

        try:
            self.check_binding()
            return load_manifest(self.manifest_path or (self.root / MANIFEST_FILENAME)).project.id
        except Exception:  # noqa: BLE001 - the id is decoration; never fail an error path
            return None

    def load(self) -> WorkspaceContext:
        self.check_binding()
        ctx = WorkspaceContext.load(
            self.root,
            manifest_path=self.manifest_path,
            policy_path=self.policy_path,
            local_path=self.local_path,
            execution=self.execution,
        )
        if self.execution_overrides:
            values = ctx.execution.selection.model_dump(mode="json")
            # An explicit rollback to local drops inherited Slurm selection only, never ledger.
            if self.execution_overrides.get("provider") == "local":
                values = ExecutionSelection().model_dump(mode="json")
            values.update(self.execution_overrides)
            try:
                selection = ExecutionSelection.model_validate(values)
            except ValidationError as exc:
                raise SvwError(
                    ErrorCode.INVALID_CONFIG, "invalid execution selection", {"reason": str(exc)}
                ) from exc
            ctx.execution = resolve_execution(selection, ctx.policy.execution)
        return ctx
