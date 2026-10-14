"""Where a workspace lives; passed to every use case."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from svweave.config.context import WorkspaceContext


@dataclass(frozen=True)
class WorkspaceOptions:
    root: Path
    manifest_path: Path | None = None
    policy_path: Path | None = None
    local_path: Path | None = None

    def workspace_id(self) -> str | None:
        """Best-effort workspace id from the manifest alone (for error envelopes)."""
        from svweave.config.context import MANIFEST_FILENAME
        from svweave.config.loader import load_manifest

        try:
            return load_manifest(self.manifest_path or (self.root / MANIFEST_FILENAME)).project.id
        except Exception:  # noqa: BLE001 - the id is decoration; never fail an error path
            return None

    def load(self) -> WorkspaceContext:
        return WorkspaceContext.load(
            self.root,
            manifest_path=self.manifest_path,
            policy_path=self.policy_path,
            local_path=self.local_path,
        )
