"""codebase-memory-mcp adapter (plan 11, tests INT-01 / INT-03 / INT-04, API-04 / API-05, DEP-02).

Rules enforced here:
* the upstream process runs inside the bwrap sandbox (snapshots read-only, dedicated state RW,
  network off); with no bwrap it runs only if the caller allowed degraded isolation, and
  :attr:`CbmAdapter.isolation` / :attr:`CbmAdapter.warnings` say so;
* only allow-listed tools are ever called (``delete_project``, ``manage_adr``, ``ingest_traces``,
  cross-repo indexing, ... are unreachable);
* the upstream ``project`` argument is never supplied by callers: it comes from the
  ``snapshot_id <-> project`` mapping, which is re-verified against the live backend (root path,
  node/edge counts) before every query (INT-03). Names alone never establish identity.
"""

from __future__ import annotations

import contextlib
import os
import shutil
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from svweave.adapters.codebase_memory import config as cfg
from svweave.adapters.codebase_memory import normalize as nz
from svweave.adapters.codebase_memory.models import (
    CbmCandidate,
    IndexMapping,
    MappingStore,
    tree_fingerprint,
)
from svweave.adapters.mcp_client import (
    InitInfo,
    McpLaunch,
    McpSession,
    ToolCaller,
    ToolInfo,
    ToolResult,
)
from svweave.domain.errors import ErrorCode, SvwError
from svweave.orchestration.capability import (
    CBM_ALLOWLIST,
    CBM_CORE_FEATURES,
    CBM_FEATURES,
    CapabilityComparison,
    compare_capabilities,
)
from svweave.orchestration.toolchain import Toolchain, sha256_file
from svweave.security.redaction import Redactor
from svweave.security.sandbox import IsolationMode, SandboxRunner, SandboxSpec

BACKEND = "codebase-memory-mcp"
REPORT_KEY = "cbm"

SessionFactory = Callable[[McpLaunch], ToolCaller]


def _default_factory(launch: McpLaunch) -> ToolCaller:
    s = McpSession(launch)
    s.start()
    return s


@dataclass
class CbmSearchResult:
    snapshot_id: str
    candidates: list[CbmCandidate]
    total: int
    has_more: bool
    offset: int
    limit: int
    limitations: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    mapping: IndexMapping | None = None
    elapsed_s: float = 0.0
    #: upstream rows returned for this page, and the row index of each kept candidate: callers
    #: advance their cursor by ROWS CONSUMED, not by items kept after post-filtering.
    rows_consumed: int = 0
    raw_positions: list[int] = field(default_factory=list)


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


class CbmAdapter:
    def __init__(
        self,
        toolchain: Toolchain,
        state_dir: Path,
        snapshots_root: Path,
        sandbox: SandboxRunner,
        *,
        protected_paths: Sequence[Path] = (),
        redactor: Redactor | None = None,
        session_factory: SessionFactory | None = None,
        expected_hashes: dict[str, str] | None = None,
        index_mode: str = "full",
        index_timeout: float = 180.0,
        poll_interval: float = 0.5,
        init_timeout: float = 90.0,
        call_timeout: float = 120.0,
        verify_binary: bool = True,
    ) -> None:
        self.toolchain = toolchain
        self.paths = cfg.CbmPaths(Path(state_dir))
        self.snapshots_root = Path(snapshots_root)
        self.sandbox = sandbox
        self.protected_paths = tuple(protected_paths)
        self.redactor = redactor or Redactor([self.paths.state_dir, self.snapshots_root, toolchain.root])
        self._factory = session_factory or _default_factory
        self._expected_hashes = expected_hashes
        self.index_mode = index_mode
        self.index_timeout = index_timeout
        self.poll_interval = poll_interval
        self.init_timeout = init_timeout
        self.call_timeout = call_timeout
        self.verify_binary = verify_binary
        self.mappings = MappingStore(self.paths.mapping_file)
        self.comparison: CapabilityComparison | None = None
        self.warnings: list[str] = []
        self._session: ToolCaller | None = None
        self._failure: SvwError | None = None
        self._host_runtime: str | None = None
        #: The ONE snapshot directory mounted into the sandbox (never the whole snapshots dir).
        self._bound_root: Path | None = None
        self.isolation: IsolationMode = sandbox.mode

    # ------------------------------------------------------------------ lifecycle
    def _spec(self, runtime_dir: str, bind_root: Path | None = None) -> SandboxSpec:
        sandboxed = self.sandbox.mode is IsolationMode.BWRAP
        return SandboxSpec(
            ro_paths=(
                Path(os.path.realpath(self.toolchain.cbm_binary.parent)),
                *([bind_root] if bind_root is not None else []),
            ),
            rw_paths=(self.paths.work,),  # host-only logs / mappings are NOT mounted
            chdir=self.paths.work,
            network=False,
            protected_paths=self.protected_paths,
            rw_exempt=(self.paths.work,),
            env=self.paths.env(runtime_dir),
            tmp_dirs=(cfg.SANDBOX_RUNTIME_DIR,) if sandboxed else (),
        )

    def _check_binary(self) -> None:
        exe = self.toolchain.cbm_binary
        if not exe.is_file():
            raise SvwError(
                ErrorCode.BACKEND_UNAVAILABLE,
                "codebase-memory-mcp binary is missing",
                {"backend": BACKEND, "reason": "binary_missing", "path": str(exe)},
            )
        if self.verify_binary:
            actual = sha256_file(exe)
            if actual != self.toolchain.cbm_binary_sha256:
                raise SvwError(
                    ErrorCode.CAPABILITY_MISMATCH,
                    "codebase-memory-mcp binary hash differs from toolchain.lock.json",
                    {
                        "backend": BACKEND,
                        "reason": "binary_hash_mismatch",
                        "expected": self.toolchain.cbm_binary_sha256,
                        "actual": actual,
                    },
                )

    def start(self, bind_root: Path | None = None) -> CapabilityComparison:
        """Launch cbm. Only ``bind_root`` (one snapshot directory) is mounted read-only."""
        if self._session is not None:
            assert self.comparison is not None
            return self.comparison
        self._failure = None
        self._check_binary()
        self.snapshots_root.mkdir(parents=True, exist_ok=True)
        bound = self._check_snapshot_root(bind_root) if bind_root is not None else None
        self.paths.ensure()
        if self.sandbox.mode is IsolationMode.BWRAP:
            runtime = cfg.SANDBOX_RUNTIME_DIR
        else:
            self._host_runtime = cfg.make_host_runtime_dir()
            runtime = self._host_runtime
        spec = self._spec(runtime, bound)
        exe = os.path.realpath(self.toolchain.cbm_binary)
        self._configure_state(spec, exe)
        wrapped = self.sandbox.wrap(spec, [exe])
        self.isolation = wrapped.isolation
        self.warnings = list(wrapped.warnings)
        launch = McpLaunch(
            backend=BACKEND,
            argv=wrapped.argv,
            env=wrapped.env,
            stderr_log=self.paths.stderr_log,
            cwd=None,
            expected_protocol=self.toolchain.pinned_protocol(REPORT_KEY),
            init_timeout=self.init_timeout,
            call_timeout=self.call_timeout,
            redactor=self.redactor,
        )
        try:
            self._session = self._factory(launch)
        except BaseException:
            self._cleanup_runtime()
            raise
        self._bound_root = bound
        hashes = self._expected_hashes or self.toolchain.pinned_tool_hashes(REPORT_KEY)
        self.comparison = compare_capabilities(
            BACKEND, self._session.tools, hashes, CBM_FEATURES, CBM_CORE_FEATURES
        )
        return self.comparison

    def _config_listing(self, spec: SandboxSpec, exe: str) -> dict[str, str]:
        p = self.sandbox.run_once(spec, [exe, "config", "list"], timeout=60)
        return {
            ln.split("=")[0].strip(): ln.split("=", 1)[1].strip()
            for ln in p.stdout.splitlines()
            if "=" in ln
        }

    def _configure_state(self, spec: SandboxSpec, exe: str) -> None:
        """Force the unsafe cbm defaults off in the dedicated state (idempotent, persisted).

        Each cbm CLI invocation costs ~3 s, so a warm state (config DB already present) is only
        verified with one ``config list``; a cold state sets each key and checks the echoed value.
        """
        warm = (self.paths.cache / "_config.db").exists()
        listing = self._config_listing(spec, exe) if warm else {}
        for key, value in cfg.SAFE_SETTINGS:
            if listing.get(key) == value:
                continue
            p = self.sandbox.run_once(spec, [exe, "config", "set", key, value], timeout=60)
            if p.returncode != 0 or f"{key} = {value}" not in p.stdout:
                raise SvwError(
                    ErrorCode.BACKEND_UNAVAILABLE,
                    f"cannot apply safe cbm setting {key}={value}",
                    {
                        "backend": BACKEND,
                        "reason": "unsafe_defaults" if p.returncode == 0 else "config_failed",
                        "stderr_tail": self.redactor.text((p.stderr or p.stdout)[-500:]),
                    },
                )

    def _cleanup_runtime(self) -> None:
        if self._host_runtime:
            shutil.rmtree(self._host_runtime, ignore_errors=True)
            self._host_runtime = None

    def _shutdown_session(self) -> None:
        s, self._session = self._session, None
        self._bound_root = None
        try:
            if s is not None:
                s.stop()
        finally:
            self._cleanup_runtime()

    def stop(self) -> None:
        try:
            self._shutdown_session()
        finally:
            self._publish_redacted_log()

    def _publish_redacted_log(self) -> None:
        """Raw stderr stays host-private (0600); a redacted copy is what evidence may use."""
        raw = self.paths.stderr_log
        with contextlib.suppress(OSError, SvwError):
            self.redactor.redact_file(raw, raw.with_name("cbm.stderr.redacted.log"))

    def _ensure_bound(self, root: Path) -> None:
        """Make ``root`` the single mounted snapshot, restarting cbm if another one is mounted."""
        root = root.resolve()
        self._require_running()  # never auto-start or resurrect a dead backend here
        if self._bound_root == root:
            return
        self._shutdown_session()
        self.start(root)

    def __enter__(self) -> CbmAdapter:
        self.start()
        return self

    def __exit__(self, *exc: object) -> None:
        self.stop()

    # Narrow read-only views. The raw session is deliberately NOT exposed: it would bypass the
    # tool allowlist and the per-snapshot project binding.
    @property
    def pid(self) -> int | None:
        return getattr(self._session, "pid", None)

    @property
    def tool_infos(self) -> dict[str, ToolInfo]:
        return dict(self._session.tools) if self._session else {}

    @property
    def init_info(self) -> InitInfo | None:
        return self._session.init if self._session else None

    @property
    def alive(self) -> bool:
        return self._session is not None and self._failure is None and self._session.alive

    # ------------------------------------------------------------------ guarded calls
    def _require_running(self) -> ToolCaller:
        if self._session is None:
            raise SvwError(
                ErrorCode.BACKEND_UNAVAILABLE,
                "codebase-memory-mcp is not running",
                {"backend": BACKEND, "reason": "not_running"},
            )
        if self._failure is not None:
            raise self._failure
        if not self._session.alive:
            violation = getattr(self._session, "violation", None)
            self._failure = SvwError(
                ErrorCode.BACKEND_UNAVAILABLE,
                "codebase-memory-mcp "
                + ("wrote non-protocol data to stdout" if violation else "process is gone"),
                {
                    "backend": BACKEND,
                    "reason": "protocol_error" if violation else "process_exited",
                    "protocol_error": violation or {},
                    "stderr_tail": self.redactor.tail_file(self.paths.stderr_log, 2000),
                },
            )
            raise self._failure
        return self._session

    def _call(self, tool: str, args: dict[str, Any], feature: str) -> ToolResult:
        if tool not in CBM_ALLOWLIST:
            raise SvwError(
                ErrorCode.POLICY_DENIED,
                f"cbm tool {tool!r} is not on the gateway allowlist",
                {"backend": BACKEND, "tool": tool},
            )
        session = self._require_running()
        assert self.comparison is not None
        self.comparison.require(feature)
        try:
            res = session.call_tool(tool, args, self.call_timeout)
        except SvwError as e:
            if e.code is ErrorCode.BACKEND_UNAVAILABLE:
                self._failure = e
            raise
        if res.is_error:
            raise SvwError(
                ErrorCode.BACKEND_UNAVAILABLE,
                f"cbm tool {tool} reported an error",
                {"backend": BACKEND, "reason": "tool_error", "tool": tool,
                 "text": self.redactor.text(res.text[:500])},
            )
        return res

    def _json(self, res: ToolResult, project_hint: str | None = None) -> dict[str, Any]:
        payload = res.json()
        if not isinstance(payload, dict):
            raise SvwError(
                ErrorCode.BACKEND_UNAVAILABLE,
                f"cbm tool {res.tool} returned non-JSON output",
                {"backend": BACKEND, "reason": "bad_output", "sample": self.redactor.text(res.text[:200])},
            )
        if "error" in payload:
            msg = str(payload.get("error"))
            if "not found" in msg or "not indexed" in msg:
                raise SvwError(
                    ErrorCode.STALE_SNAPSHOT,
                    "cbm no longer has an index for this snapshot",
                    {"backend": BACKEND, "reason": "project_not_found", "message": self.redactor.text(msg)},
                )
            raise SvwError(
                ErrorCode.BACKEND_UNAVAILABLE,
                f"cbm tool {res.tool} failed: {self.redactor.text(msg)}",
                {"backend": BACKEND, "reason": "tool_error"},
            )
        return payload

    # ------------------------------------------------------------------ indexing
    def _check_snapshot_root(self, snapshot_root: Path) -> Path:
        root = snapshot_root.resolve()
        base = self.snapshots_root.resolve()
        if root != base and base not in root.parents:
            raise SvwError(
                ErrorCode.PATH_NOT_ALLOWED,
                "snapshot root is outside the snapshots directory bound into the sandbox",
                {"snapshot_root": self.redactor.text(str(root))},
            )
        if not root.is_dir():
            raise SvwError(
                ErrorCode.MISSING_INPUT,
                "snapshot root is not a directory",
                {"snapshot_root": self.redactor.text(str(root))},
            )
        return root

    def ensure_indexed(
        self, snapshot_id: str, snapshot_root: Path, *, fingerprint: str | None = None
    ) -> IndexMapping:
        """Index the immutable snapshot (or reuse a verified index) and record the mapping."""
        root = self._check_snapshot_root(snapshot_root)
        self._ensure_bound(root)
        fp = fingerprint or tree_fingerprint(root)
        existing = self.mappings.get(snapshot_id)
        if existing is not None and existing.content_fingerprint == fp and existing.snapshot_root == str(root):
            try:
                return self.verify_binding(snapshot_id)
            except SvwError as e:
                if e.code is not ErrorCode.STALE_SNAPSHOT:
                    raise
                self.mappings.drop(snapshot_id)  # index vanished / replaced: rebuild below
        res = self._call(
            "index_repository", {"repo_path": str(root), "mode": self.index_mode}, "index"
        )
        report = nz.parse_index_report(self._json(res))
        if not report.project:
            raise SvwError(
                ErrorCode.BACKEND_UNAVAILABLE,
                "cbm index_repository returned no project",
                {"backend": BACKEND, "reason": "bad_output"},
            )
        status = self._poll_ready(report.project)
        mapping = IndexMapping(
            snapshot_id=snapshot_id,
            snapshot_root=str(root),
            project=report.project,
            content_fingerprint=fp,
            nodes=status.nodes or report.nodes,
            edges=status.edges or report.edges,
            indexed_at=status.indexed_at,
            index_mode=self.index_mode,
            cbm_version=self._session.init.server_version if self._session and self._session.init else "",
            cbm_binary_sha256=self.toolchain.cbm_binary_sha256,
            parse_unusable=status.parse_unusable or report.parse_unusable,
            parse_partial=status.parse_partial or report.parse_partial,
            created_at=_now(),
        )
        self.mappings.put(mapping)
        return mapping

    def _poll_ready(self, project: str) -> nz.IndexReport:
        deadline = time.monotonic() + self.index_timeout
        last = ""
        while True:
            res = self._call(
                "index_status", {"project": project, "format": "json", "diagnostics": "summary"}, "index"
            )
            rep = nz.parse_index_report(self._json(res))
            last = rep.status
            if rep.status.lower() == "ready":
                return rep
            if rep.status.lower() in ("error", "failed"):
                raise SvwError(
                    ErrorCode.BACKEND_UNAVAILABLE,
                    "cbm indexing failed",
                    {"backend": BACKEND, "reason": "index_failed", "status": rep.status},
                )
            if time.monotonic() >= deadline:
                raise SvwError(
                    ErrorCode.BUILD_TIMEOUT,
                    f"cbm index not ready after {self.index_timeout:.0f}s (status {last!r})",
                    {"backend": BACKEND, "reason": "index_timeout", "status": last},
                )
            time.sleep(self.poll_interval)

    def mapping(self, snapshot_id: str) -> IndexMapping | None:
        return self.mappings.get(snapshot_id)

    def verify_binding(self, snapshot_id: str, *, deep: bool = False) -> IndexMapping:
        """INT-03: confirm the live upstream project is the index of THIS snapshot.

        Compares the mapped project's reported root path and node/edge counts with the recorded
        mapping; ``deep`` re-hashes the snapshot tree against the recorded content fingerprint.
        """
        m = self.mappings.get(snapshot_id)
        if m is None:
            raise SvwError(
                ErrorCode.STALE_SNAPSHOT,
                "no cbm index is recorded for this snapshot",
                {"backend": BACKEND, "reason": "snapshot_not_indexed", "snapshot_id": snapshot_id},
            )
        res = self._call(
            "index_status", {"project": m.project, "format": "json", "diagnostics": "none"}, "index"
        )
        rep = nz.parse_index_report(self._json(res))
        mismatch: list[str] = []
        if rep.project and rep.project != m.project:
            mismatch.append("project_name")
        if rep.root_path is None or Path(rep.root_path).resolve() != Path(m.snapshot_root).resolve():
            mismatch.append("root_path")
        if rep.status.lower() != "ready":
            mismatch.append(f"status={rep.status}")
        if (rep.nodes, rep.edges) != (m.nodes, m.edges):
            mismatch.append("graph_counts")
        if deep and tree_fingerprint(Path(m.snapshot_root)) != m.content_fingerprint:
            mismatch.append("content_fingerprint")
        if mismatch:
            raise SvwError(
                ErrorCode.STALE_SNAPSHOT,
                "the cbm index does not correspond to the requested snapshot; refusing to query",
                {
                    "backend": BACKEND,
                    "reason": "index_snapshot_mismatch",
                    "snapshot_id": snapshot_id,
                    "mismatch": mismatch,
                },
            )
        return m

    # ------------------------------------------------------------------ queries
    def search(
        self,
        snapshot_id: str,
        query: str,
        *,
        label: str | None = None,
        file_pattern: str | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> CbmSearchResult:
        m = self.verify_binding(snapshot_id)
        args: dict[str, Any] = {
            "project": m.project,
            "query": query,
            "limit": limit,
            "offset": offset,
            "format": "json",
        }
        if label:
            args["label"] = label
        if file_pattern:
            args["file_pattern"] = file_pattern
        res = self._call("search_graph", args, "search")
        payload = self._json(res)
        rows = nz.parse_search_rows(payload)
        cands, dropped, positions = nz.normalize_hits(rows, m)
        total = int(payload.get("total", len(cands)) or 0)
        has_more = bool(payload.get("has_more", False)) or bool(payload.get("truncated", False))
        limits = list(nz.STATIC_LIMITATIONS) + nz.index_limitations(m)
        return CbmSearchResult(
            snapshot_id=snapshot_id,
            candidates=cands,
            total=total,
            has_more=has_more,
            offset=offset,
            limit=limit,
            limitations=limits,
            warnings=dropped + list(self.warnings),
            mapping=m,
            elapsed_s=res.elapsed_s,
            rows_consumed=len(rows),
            raw_positions=positions,
        )

    def code_snippet(self, snapshot_id: str, upstream_qualified_name: str) -> dict[str, Any]:
        m = self.verify_binding(snapshot_id)
        self._ensure_bound(Path(m.snapshot_root))  # get_code_snippet reads the source file
        if not upstream_qualified_name.startswith(m.project + "."):
            raise SvwError(
                ErrorCode.STALE_SNAPSHOT,
                "qualified name belongs to a different cbm project than the requested snapshot",
                {"backend": BACKEND, "reason": "foreign_project", "snapshot_id": snapshot_id},
            )
        res = self._call(
            "get_code_snippet",
            {"project": m.project, "qualified_name": upstream_qualified_name, "source_mode": "full"},
            "snippet",
        )
        meta = nz.parse_snippet(res.text)
        # absolute host path / project-derived name: callers use the candidate's relative fields
        meta.pop("file_path", None)
        meta.pop("qualified_name", None)
        return meta

    def graph_schema(self, snapshot_id: str) -> dict[str, Any]:
        m = self.verify_binding(snapshot_id)
        res = self._call(
            "get_graph_schema", {"project": m.project, "format": "json", "diagnostics": "none"}, "schema"
        )
        return self._json(res)

    def index_coverage(self, snapshot_id: str, paths: Sequence[str]) -> dict[str, Any]:
        m = self.verify_binding(snapshot_id)
        safe = [p for p in paths if nz.safe_rel_path(p)]
        res = self._call(
            "check_index_coverage",
            {"project": m.project, "format": "json", "diagnostics": "summary", "paths": safe},
            "coverage",
        )
        return self._json(res)

    # ------------------------------------------------------------------ status
    def describe(self) -> dict[str, Any]:
        s = self._session
        return {
            "backend": BACKEND,
            "alive": self.alive,
            "isolation": self.isolation.value,
            "warnings": list(self.warnings),
            "bound_snapshot": self._bound_root.name if self._bound_root else None,
            "protocol_version": s.init.protocol_version if s and s.init else None,
            "server_version": s.init.server_version if s and s.init else None,
            "features": dict(self.comparison.features) if self.comparison else {},
            "failure": self._failure.message if self._failure else None,
        }
