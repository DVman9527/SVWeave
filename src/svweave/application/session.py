"""A gateway session: one workspace, lazily created heavy collaborators.

Used by the MCP server (one per process) and by CLI commands that need a registry. Nothing heavy
is created on construction: the backend registry (which can start sandboxed backend processes) is
only built on first real use, and ``close()`` shuts every launched backend down.
"""

from __future__ import annotations

import json
import threading
from collections.abc import Callable
from pathlib import Path
from typing import Any

from svweave.application.events import EventLog
from svweave.application.jobs import JobManager
from svweave.application.keys import cursor_key
from svweave.application.options import WorkspaceOptions
from svweave.domain.errors import ErrorCode, SvwError
from svweave.orchestration.registry import BackendRegistry
from svweave.security import safeio
from svweave.security.redaction import Redactor

PROBE_FILE = "backend_probe.json"


class Session:
    def __init__(
        self,
        opts: WorkspaceOptions,
        *,
        use_graph: bool = True,
        events: EventLog | None = None,
        registry_factory: Callable[[], BackendRegistry] | None = None,
        jobs: JobManager | None = None,
    ) -> None:
        self.opts = opts
        self.use_graph = use_graph
        self.events = events
        self._registry_factory = registry_factory
        self._registry: BackendRegistry | None = None
        self._registry_error: SvwError | None = None
        self._registry_execution_fingerprint: str | None = None
        self._lock = threading.Lock()
        self.jobs = jobs or JobManager(opts, events=events)
        #: Probe results gathered by this session (in memory only; see services.status).
        self.session_probe: dict[str, Any] | None = None

    # ------------------------------------------------------------------ lazily built parts
    @property
    def registry(self) -> BackendRegistry | None:
        """The backend registry, or None when the toolchain is unavailable (text fallback)."""
        with self._lock:
            # A long-lived MCP session must not reuse a local backend after its effective
            # execution selection changes. Revalidate even when a registry/error is cached,
            # and apply the same provider guard to injected factories as production ones.
            try:
                ctx = self.opts.load()
                fingerprint = ctx.execution.fingerprint
                if fingerprint != self._registry_execution_fingerprint:
                    self._discard_registry()
                    self._registry_error = None
                    self._registry_execution_fingerprint = fingerprint
                if ctx.execution.provider != "local":
                    raise SvwError(
                        ErrorCode.BACKEND_UNAVAILABLE,
                        "remote graph/Serena MCP execution is not qualified (P1)",
                        {"reason": "srun_mcp_not_supported", "submitted": False},
                    )
                if self._registry is not None or self._registry_error is not None:
                    return self._registry
                if self._registry_factory is not None:
                    self._registry = self._registry_factory()
                else:
                    self._registry = BackendRegistry.for_workspace(ctx)
            except SvwError as e:
                self._discard_registry()
                self._registry_error = e
                # Retry configuration validation on the next access even if a malformed
                # config is repaired without changing its eventual execution fingerprint.
                if e.code != ErrorCode.BACKEND_UNAVAILABLE:
                    self._registry_execution_fingerprint = None
            return self._registry

    def _discard_registry(self) -> None:
        """Invalidate before teardown; caller holds the session lock."""
        reg, self._registry = self._registry, None
        if reg is not None:
            reg.shutdown()

    @property
    def registry_created(self) -> bool:
        return self._registry is not None

    @property
    def registry_error(self) -> SvwError | None:
        return self._registry_error

    def workspace_id(self) -> str | None:
        return self.opts.workspace_id()

    def cursor_key(self) -> bytes:
        return cursor_key(self.opts.load())

    def redactor(self) -> Redactor:
        ctx = self.opts.load()
        return Redactor([ctx.project_root])

    def close(self) -> None:
        try:
            self.jobs.shutdown()
        finally:
            with self._lock:
                reg, self._registry = self._registry, None
            if reg is not None:
                reg.shutdown()


# ---------------------------------------------------------------------- persisted probe record
def probe_path(trusted_dir: Path) -> Path:
    """The last-probe record lives in the TRUSTED directory (outside the repository): a file
    inside the repo could be committed or edited to make unprobed backends look READY."""
    return trusted_dir / PROBE_FILE


_STATUSES = {"READY", "DEGRADED", "UNAVAILABLE"}


def save_probe(trusted_dir: Path, health: dict[str, Any]) -> None:
    trusted_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    safeio.write_atomic(
        probe_path(trusted_dir),
        json.dumps(health, indent=2, sort_keys=True).encode("utf-8"),
        0o600,
    )


def load_probe(trusted_dir: Path) -> dict[str, Any] | None:
    """Sanitised last-probe record: unknown statuses are dropped, free text is redacted."""
    try:
        raw = safeio.read_regular(probe_path(trusted_dir), 1 << 20)
        data = json.loads(raw)
    except (OSError, ValueError, SvwError):
        return None
    if not isinstance(data, dict):
        return None
    red = Redactor([])
    out: dict[str, Any] = {}
    for name, h in data.items():
        if not isinstance(h, dict) or h.get("status") not in _STATUSES:
            continue
        out[str(name)] = {
            "status": h["status"],
            "probed": bool(h.get("probed")),
            "checked_at": red.text(str(h.get("checked_at") or ""))[:40] or None,
            "reasons": [red.text(str(x))[:300] for x in (h.get("reasons") or [])][:20],
            "limitations": [red.text(str(x))[:300] for x in (h.get("limitations") or [])][:20],
            "version": red.text(str(h.get("version") or ""))[:64] or None,
            "features": {str(k): bool(v) for k, v in (h.get("features") or {}).items()}
            if isinstance(h.get("features"), dict)
            else {},
        }
    return out
