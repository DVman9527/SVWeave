"""Dedicated cbm state, environment and safe settings (M0 facts, plan 11.2, ADR-011)."""

from __future__ import annotations

import os
import tempfile
from dataclasses import dataclass
from pathlib import Path

from svweave.security import safeio

#: Settings forced OFF in the dedicated state before the first launch (cbm defaults are unsafe:
#: ui_enabled=true opens an HTTP UI on :9749, watchers/auto-index are on).
SAFE_SETTINGS: tuple[tuple[str, str], ...] = (
    ("ui_enabled", "false"),
    ("auto_watch", "false"),
    ("watcher_enabled", "false"),
    ("auto_index", "false"),
)

#: Fixed runtime-socket directory inside the sandbox's private /tmp. cbm creates a unix socket
#: below CBM_RUNTIME_DIR and fails with "secure CLI coordination could not be created (endpoint)"
#: when the full socket path exceeds the sockaddr_un limit (~108 bytes), which deep workspace
#: paths easily do. The sandbox's /tmp is ephemeral and invisible to the host.
SANDBOX_RUNTIME_DIR = "/tmp/cbm-rt"


@dataclass(frozen=True)
class CbmPaths:
    """State layout. ``state_dir`` is HOST-ONLY (never mounted into the sandbox): raw/redacted
    logs and ``mappings.json`` live there, so a backend can neither forge nor symlink-trick them.
    Only ``work`` (HOME / cache of the backend) is bind-mounted read-write."""

    state_dir: Path

    @property
    def work(self) -> Path:
        return self.state_dir / "work"

    @property
    def home(self) -> Path:
        return self.work / "home"

    @property
    def cache(self) -> Path:
        return self.work / "cache"

    @property
    def logs(self) -> Path:
        return self.state_dir / "logs"

    @property
    def stderr_log(self) -> Path:
        return self.logs / "cbm.stderr.log"

    @property
    def mapping_file(self) -> Path:
        return self.state_dir / "mappings.json"

    def ensure(self) -> None:
        """Create the private state tree (mode 0700 everywhere; cbm checks directory privacy)."""
        for d in (self.state_dir, self.logs):
            d.mkdir(parents=True, exist_ok=True)
            d.chmod(0o700)
        # work/* is backend-writable: create / chmod it without following planted links
        for rel in ("work/home", "work/cache"):
            safeio.ensure_private_dir(self.state_dir, rel)

    def env(self, runtime_dir: str) -> dict[str, str]:
        return {
            "PATH": "/usr/bin:/bin",
            "HOME": str(self.home),
            "XDG_CONFIG_HOME": str(self.home / ".config"),
            "XDG_CACHE_HOME": str(self.cache),
            "CBM_CACHE_DIR": str(self.cache),
            "CBM_RUNTIME_DIR": runtime_dir,
            "LANG": "C.UTF-8",
        }


def default_state_dir(workspace_root: Path) -> Path:
    """``<workspace>/.svweave/backends/cbm`` (one dedicated state per workspace)."""
    return Path(workspace_root) / ".svweave" / "backends" / "cbm"


def make_host_runtime_dir() -> str:
    """Short private host directory for the degraded (no sandbox) mode; caller removes it."""
    d = tempfile.mkdtemp(prefix="svwcbm-")
    os.chmod(d, 0o700)
    return d
