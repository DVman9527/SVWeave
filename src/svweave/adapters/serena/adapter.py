"""Serena adapter (plan 12, ADR-007, ADR-012; tests INT-02 / INT-03 / INT-04, EDT-06 hook).

* one Serena instance per candidate directory, serialized by a lock; the instance is bound to the
  candidate at construction and EVERY call re-checks that the requested candidate is the bound one
  (project identity is by candidate realpath + id, never by name);
* the process runs in the bwrap sandbox with the candidate and its own state as the only writable
  paths; the tool list is restricted to symbol read/edit tools by a custom context;
* results are LSP_RESOLVED evidence with zero-based line numbers; spans are converted to canonical
  byte offsets using the candidate file bytes (:mod:`svweave.adapters.serena.offsets`);
* Serena symbol edits qualify only for unique SystemVerilog ``function`` bodies; everything else is
  planned as the gateway ``exact_range`` path or refused (ADR-012).
"""

from __future__ import annotations

import contextlib
import hashlib
import os
import subprocess
import threading
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

from svweave.adapters.mcp_client import (
    SIZE_VIOLATIONS,
    InitInfo,
    McpLaunch,
    McpSession,
    ToolCaller,
    ToolInfo,
    ToolResult,
)
from svweave.adapters.serena import config as cfg
from svweave.adapters.serena import guard as gd
from svweave.adapters.serena import offsets
from svweave.domain.errors import ErrorCode, SvwError
from svweave.orchestration.capability import (
    SERENA_ALLOWLIST,
    SERENA_CORE_FEATURES,
    SERENA_FEATURES,
    SERENA_FORBIDDEN,
    CapabilityComparison,
    SerenaEditSubset,
    compare_capabilities,
    derive_serena_edit_subset,
)
from svweave.orchestration.toolchain import Toolchain, sha256_file
from svweave.security import safeio
from svweave.security.redaction import Redactor
from svweave.security.sandbox import IsolationMode, SandboxRunner, SandboxSpec

BACKEND = "serena"

SessionFactory = Callable[[McpLaunch], ToolCaller]


def _default_factory(launch: McpLaunch) -> ToolCaller:
    s = McpSession(launch)
    s.start()
    return s


@dataclass
class SymbolRecord:
    name_path: str
    kind: str
    relative_path: str
    start_line: int  # zero-based, inclusive
    end_line: int  # zero-based, inclusive
    body: str | None = None
    byte_start: int | None = None
    byte_end: int | None = None
    #: "exact" (body text located in the file), "line" (whole-line window), "none"
    span_precision: str = "none"
    backend: str = BACKEND
    evidence_kind: str = "LSP_RESOLVED"
    position_base: str = "zero_based_lines"

    def to_json(self) -> dict[str, Any]:
        d = dict(self.__dict__)
        d.pop("body", None)  # bodies are source text: only returned on request via .body
        return d


@dataclass
class ReferenceRecord:
    relative_path: str
    kind_group: str
    name_path: str
    start_line: int
    end_line: int
    content_around_reference: str
    backend: str = BACKEND
    evidence_kind: str = "LSP_RESOLVED"


@dataclass
class SymbolEditPlan:
    #: "serena_symbol" | "exact_range" (decision), or raises AMBIGUOUS_SYMBOL (refuse)
    edit_path: str
    reason: str
    symbol: SymbolRecord | None = None
    matches: list[SymbolRecord] = field(default_factory=list)


def _line_no(v: Any) -> int | None:
    """A line number from untrusted backend output (bool and non-integers refused)."""
    if isinstance(v, bool):
        return None
    if isinstance(v, int):
        return v
    if isinstance(v, str) and v.isdigit():
        return int(v)
    return None


def _bad_output(redactor: Any, what: str, sample: Any) -> SvwError:
    """Malformed Serena output is BACKEND_UNAVAILABLE (what probes and callers handle), never a
    bare AttributeError / ValueError that escapes as a traceback."""
    return SvwError(
        ErrorCode.BACKEND_UNAVAILABLE,
        f"Serena returned unexpected output: {what}",
        {"backend": BACKEND, "reason": "bad_output", "sample": redactor.text(repr(sample)[:200])},
    )


class SerenaAdapter:
    def __init__(
        self,
        toolchain: Toolchain,
        state_root: Path,
        candidate: Path,
        sandbox: SandboxRunner,
        *,
        protected_paths: Sequence[Path] = (),
        redactor: Redactor | None = None,
        session_factory: SessionFactory | None = None,
        expected_hashes: dict[str, str] | None = None,
        allow_edit_without_isolation: bool = False,
        init_timeout: float = 120.0,
        call_timeout: float = 120.0,
    ) -> None:
        self.toolchain = toolchain
        self.candidate = cfg.check_candidate(candidate)
        self.candidate_id = cfg.candidate_id(self.candidate)
        self.paths = cfg.SerenaPaths(Path(state_root) / self.candidate_id)
        self.sandbox = sandbox
        self.protected_paths = tuple(protected_paths)
        self.redactor = redactor or Redactor([self.paths.state_dir, self.candidate, toolchain.root])
        self._factory = session_factory or _default_factory
        self._expected_hashes = expected_hashes
        self.allow_edit_without_isolation = allow_edit_without_isolation
        self.init_timeout = init_timeout
        self.call_timeout = call_timeout
        self.subset: SerenaEditSubset = derive_serena_edit_subset(toolchain.backend_report("serena"))
        self.comparison: CapabilityComparison | None = None
        self.warnings: list[str] = []
        self.isolation: IsolationMode = sandbox.mode
        self.guard = gd.CandidateGuard(self.candidate)
        self.verible_filelist: list[str] = []
        self._session: ToolCaller | None = None
        self._failure: SvwError | None = None
        self._lock = threading.RLock()

    # ------------------------------------------------------------------ lifecycle
    def _check_versions(self) -> None:
        tc = self.toolchain
        py = tc.serena_venv / "bin" / "python"
        try:
            p = subprocess.run(
                [str(py), "-I", "-c",
                 "import importlib.metadata as m;print(m.version('serena-agent'))"],
                capture_output=True, text=True, timeout=60, check=False,
                env={"PATH": "/usr/bin:/bin"},
            )
        except (OSError, subprocess.SubprocessError) as e:
            raise SvwError(ErrorCode.BACKEND_UNAVAILABLE, f"cannot query the Serena version: {e}",
                           {"backend": BACKEND, "reason": "version_probe_failed"}) from e
        if p.stdout.strip() != tc.serena_version:
            raise SvwError(
                ErrorCode.CAPABILITY_MISMATCH,
                "installed Serena version differs from toolchain.lock.json",
                {"backend": BACKEND, "reason": "version_mismatch", "expected": tc.serena_version,
                 "actual": p.stdout.strip() or self.redactor.text(p.stderr[-200:])},
            )
        if not tc.verible_ls.is_file() or sha256_file(tc.verible_ls) != tc.verible_ls_sha256:
            raise SvwError(
                ErrorCode.CAPABILITY_MISMATCH,
                "pinned verible-verilog-ls is missing or its hash differs from the lock",
                {"backend": BACKEND, "reason": "verible_mismatch"},
            )

    def _exempt_candidate(self) -> tuple[Path, ...]:
        """An edit candidate lives at ``<protected root>/.svweave/candidates/<txn>`` (plan 12.4):
        that exact directory may be bound read-write although it sits inside a protected root.
        Any other candidate inside a protected root stays refused by the sandbox."""
        for pr in self.protected_paths:
            allowed = Path(os.path.realpath(pr)) / ".svweave" / "candidates"
            if self.candidate.parent == allowed:
                return (self.candidate,)
        return ()

    def _spec(self, env: dict[str, str]) -> SandboxSpec:
        tc = self.toolchain
        ro: list[Path] = [
            Path(os.path.realpath(tc.serena_venv)),
            Path(os.path.realpath(tc.verible_ls.parent.parent)),
            self.paths.ctx_dir,  # context file: read-only for the backend
        ]
        src = tc.root / str(tc.component("serena").get("src_dir", ""))
        if src.is_dir() and src != tc.root:
            ro.append(Path(os.path.realpath(src)))
        ro.extend(cfg.python_binds(tc.serena_venv))
        spec = SandboxSpec(
            ro_paths=tuple(ro),
            rw_paths=(self.candidate, self.paths.work),  # host-only logs/ctx are NOT writable
            chdir=self.candidate,
            network=False,
            protected_paths=self.protected_paths,
            rw_exempt=(self.paths.work, *self._exempt_candidate()),
            env=env,
        )
        # only the links these very mounts do not already show (no second copy of the bind rules)
        return replace(spec, symlinks=tuple(cfg.python_links(tc.serena_venv, spec)))

    def start(self) -> CapabilityComparison:
        with self._lock:
            if self._session is not None:
                assert self.comparison is not None
                return self.comparison
            self._failure = None
            self._check_versions()
            self.paths.ensure()
            self.paths.prepare(self.candidate)
            self.verible_filelist = cfg.write_verible_filelist(self.candidate)
            env = self.paths.env(self.toolchain.verible_bin_dir, self.toolchain.serena_venv)
            found = cfg.which_in(env["PATH"], "verible-verilog-ls")
            if found != str(self.toolchain.verible_ls):
                raise SvwError(
                    ErrorCode.CAPABILITY_MISMATCH,
                    "pinned verible-verilog-ls is not first on the Serena PATH",
                    {"backend": BACKEND, "reason": "verible_not_first", "found": self.redactor.text(str(found))},
                )
            exe = os.path.realpath(self.toolchain.serena_exe)
            inner = [
                exe, "start-mcp-server", "--project", str(self.candidate),
                "--context", str(self.paths.context_file), "--transport", "stdio",
                "--enable-web-dashboard", "false", "--open-web-dashboard", "false",
                "--enable-gui-log-window", "false", "--log-level", "INFO", "--tool-timeout", "120",
            ]
            wrapped = self.sandbox.wrap(self._spec(env), inner)
            self.isolation = wrapped.isolation
            self.warnings = list(wrapped.warnings)
            launch = McpLaunch(
                backend=BACKEND, argv=wrapped.argv, env=wrapped.env,
                stderr_log=self.paths.stderr_log, cwd=None,
                expected_protocol=self.toolchain.pinned_protocol("serena"),
                init_timeout=self.init_timeout, call_timeout=self.call_timeout,
                redactor=self.redactor,
            )
            self._session = self._factory(launch)
            hashes = self._expected_hashes or self.toolchain.pinned_tool_hashes("serena")
            self.comparison = compare_capabilities(
                BACKEND, self._session.tools, hashes, SERENA_FEATURES, SERENA_CORE_FEATURES,
                forbidden=SERENA_FORBIDDEN,
            )
            self.guard.baseline()  # AFTER verible.filelist has been written
            return self.comparison

    def stop(self) -> None:
        with self._lock:
            s, self._session = self._session, None
            try:
                if s is not None:
                    s.stop()
            finally:
                raw = self.paths.stderr_log
                with contextlib.suppress(OSError, SvwError):
                    self.redactor.redact_file(raw, raw.with_name("serena.stderr.redacted.log"))

    def __enter__(self) -> SerenaAdapter:
        self.start()
        return self

    def __exit__(self, *exc: object) -> None:
        self.stop()

    # Narrow read-only views; the raw session is deliberately not exposed (allowlist bypass).
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
    def _assert_active(self, candidate: Path) -> None:
        """INT-03: the process is bound to ONE candidate; any other request is refused.

        Also re-validates the bound candidate on every call, so a ``.serena/`` directory (or a
        link at a generated path) planted by the backend while running is NOT swallowed.
        """
        req = Path(os.path.realpath(candidate))
        if cfg.candidate_id(req) != self.candidate_id:
            raise SvwError(
                ErrorCode.STALE_SNAPSHOT,
                "the active Serena project is not the requested candidate; refusing to mix projects",
                {"backend": BACKEND, "reason": "active_project_mismatch",
                 "active_candidate_id": self.candidate_id,
                 "requested_candidate_id": cfg.candidate_id(req)},
            )
        cfg.check_candidate(self.candidate)

    def _require_running(self) -> ToolCaller:
        if self._session is None:
            raise SvwError(ErrorCode.BACKEND_UNAVAILABLE, "Serena is not running",
                           {"backend": BACKEND, "reason": "not_running"})
        if self._failure is not None:
            raise self._failure
        if not self._session.alive:
            violation = getattr(self._session, "violation", None)
            if (violation or {}).get("kind") in SIZE_VIOLATIONS and hasattr(self._session, "violation_error"):
                # the transport already killed the group; reap now (tokens, detached daemons) and
                # report the real reason, not a generic protocol error
                err = self._session.violation_error()
                with contextlib.suppress(Exception):
                    self._session.stop()
                err.details["backend"] = BACKEND
                self._failure = err
                raise self._failure
            self._failure = SvwError(
                ErrorCode.BACKEND_UNAVAILABLE,
                f"Serena violated the stdio protocol ({violation.get('kind')})" if violation
                else "Serena process is gone",
                {"backend": BACKEND, "reason": "protocol_error" if violation else "process_exited",
                 "protocol_error": violation or {},
                 "stderr_tail": self.redactor.tail_file(self.paths.stderr_log, 2000)},
            )
            raise self._failure
        return self._session

    def _call(self, tool: str, args: dict[str, Any], feature: str) -> ToolResult:
        if tool not in SERENA_ALLOWLIST:
            raise SvwError(ErrorCode.POLICY_DENIED, f"Serena tool {tool!r} is not on the gateway allowlist",
                           {"backend": BACKEND, "tool": tool})
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
                ErrorCode.BACKEND_UNAVAILABLE, f"Serena tool {tool} reported an error",
                {"backend": BACKEND, "reason": "tool_error", "tool": tool,
                 "text": self.redactor.text(res.text[:500])},
            )
        return res

    def _file_bytes(self, relative_path: str) -> bytes:
        return gd.safe_candidate_file(self.candidate, relative_path).read_bytes()

    # ------------------------------------------------------------------ reads
    def _symbol_from(self, d: dict[str, Any]) -> SymbolRecord | None:
        rel = d.get("relative_path")
        loc = d.get("body_location") or {}
        if safeio.safe_rel_path(rel) is None or not isinstance(loc, dict) or "start_line" not in loc:
            return None  # Serena-returned paths are untrusted: absolute / '..' results are dropped
        assert isinstance(rel, str)
        start, end = _line_no(loc.get("start_line")), _line_no(loc.get("end_line"))
        if start is None or end is None:
            raise _bad_output(self.redactor, "symbol body_location is not a pair of line numbers", loc)
        rec = SymbolRecord(
            name_path=str(d.get("name_path", "")),
            kind=str(d.get("kind", "")),
            relative_path=rel,
            start_line=start,
            end_line=end,
            body=d.get("body") if isinstance(d.get("body"), str) else None,
        )
        try:
            data = self._file_bytes(rel)
            if rec.body is not None:
                span = offsets.locate_body(data, rec.start_line, rec.end_line, rec.body)
                if span is not None:
                    rec.byte_start, rec.byte_end = span
                    rec.span_precision = "exact"
            if rec.span_precision != "exact":
                rec.byte_start, rec.byte_end = offsets.line_window(data, rec.start_line, rec.end_line)
                rec.span_precision = "line"
        except (SvwError, ValueError):
            rec.span_precision = "none"
        return rec

    def find_symbol(
        self,
        candidate: Path,
        name_path: str,
        relative_path: str | None = None,
        *,
        include_body: bool = False,
        depth: int = 0,
    ) -> list[SymbolRecord]:
        with self._lock:
            self._assert_active(candidate)
            args: dict[str, Any] = {
                "name_path_pattern": name_path, "include_body": include_body, "depth": depth,
            }
            if relative_path is not None:
                gd.safe_candidate_file(self.candidate, relative_path)
                args["relative_path"] = relative_path
            res = self._call("find_symbol", args, "find_symbol")
            payload = res.json()
            if not isinstance(payload, list):
                raise SvwError(ErrorCode.BACKEND_UNAVAILABLE, "Serena find_symbol returned unexpected output",
                               {"backend": BACKEND, "reason": "bad_output",
                                "sample": self.redactor.text(res.text[:200])})
            out = [r for d in payload if isinstance(d, dict) and (r := self._symbol_from(d)) is not None]
            return out

    def find_referencing_symbols(
        self, candidate: Path, name_path: str, relative_path: str
    ) -> list[ReferenceRecord]:
        with self._lock:
            self._assert_active(candidate)
            gd.safe_candidate_file(self.candidate, relative_path)
            res = self._call(
                "find_referencing_symbols",
                {"name_path": name_path, "relative_path": relative_path}, "references",
            )
            payload = res.json()
            if not isinstance(payload, dict):
                raise SvwError(ErrorCode.BACKEND_UNAVAILABLE, "Serena references returned unexpected output",
                               {"backend": BACKEND, "reason": "bad_output",
                                "sample": self.redactor.text(res.text[:200])})
            out: list[ReferenceRecord] = []
            for rel, groups in payload.items():
                if not isinstance(groups, dict) or safeio.safe_rel_path(rel) is None:
                    continue
                for kind, items in groups.items():
                    if not isinstance(items, list):
                        raise _bad_output(self.redactor, "references group is not a list", items)
                    for it in items:
                        if not isinstance(it, dict):
                            raise _bad_output(self.redactor, "reference entry is not an object", it)
                        loc = it.get("body_location") or {}
                        sl = _line_no(loc.get("start_line", 0)) if isinstance(loc, dict) else None
                        el = _line_no(loc.get("end_line", 0)) if isinstance(loc, dict) else None
                        if sl is None or el is None:
                            raise _bad_output(self.redactor, "reference body_location is malformed", loc)
                        out.append(ReferenceRecord(
                            relative_path=str(rel), kind_group=str(kind),
                            name_path=str(it.get("name_path", "")),
                            start_line=sl, end_line=el,
                            content_around_reference=str(it.get("content_around_reference", "")),
                        ))
            return out

    def get_symbols_overview(self, candidate: Path, relative_path: str, depth: int = 1) -> Any:
        with self._lock:
            self._assert_active(candidate)
            gd.safe_candidate_file(self.candidate, relative_path)
            res = self._call(
                "get_symbols_overview", {"relative_path": relative_path, "depth": depth}, "overview"
            )
            return res.json()

    # ------------------------------------------------------------------ edit planning / editing
    @staticmethod
    def _name_matches(symbol_name_path: str, pattern: str) -> bool:
        if pattern.startswith("/"):
            return symbol_name_path == pattern[1:]
        return symbol_name_path == pattern or symbol_name_path.endswith("/" + pattern)

    def plan_symbol_edit(
        self, candidate: Path, name_path: str, relative_path: str
    ) -> SymbolEditPlan:
        """Decide HOW a symbol replacement may be done (ADR-012). Never picks among ambiguous
        matches: >1 match raises ``AMBIGUOUS_SYMBOL`` with the scoped candidates."""
        with self._lock:
            found = [
                s for s in self.find_symbol(candidate, name_path, relative_path, include_body=True)
                if s.kind != "File" and self._name_matches(s.name_path, name_path)
                and s.relative_path == relative_path
            ]
            if len(found) > 1:
                raise SvwError(
                    ErrorCode.AMBIGUOUS_SYMBOL,
                    f"{len(found)} symbols match {name_path!r}; refusing to pick one",
                    {"candidates": [{"name_path": s.name_path, "kind": s.kind,
                                     "start_line": s.start_line} for s in found]},
                )
            if not found:
                return SymbolEditPlan(
                    "exact_range",
                    "Serena/Verible cannot locate this symbol (tasks and constructors are not "
                    "exposed by Verible); use an explicit exact-range patch",
                )
            sym = found[0]
            if sym.kind not in self.subset.kinds:
                return SymbolEditPlan(
                    "exact_range",
                    f"symbol kind {sym.kind!r} is outside the qualified Serena edit subset "
                    f"{sorted(self.subset.kinds)}", sym, found,
                )
            if sym.span_precision != "exact":
                raise SvwError(
                    ErrorCode.EDIT_CONFLICT,
                    "Serena's view of the symbol does not match the candidate file bytes",
                    {"reason": "serena_stale_view", "name_path": sym.name_path},
                )
            return SymbolEditPlan("serena_symbol", "unique SystemVerilog function", sym, found)

    def replace_symbol_body(
        self,
        candidate: Path,
        name_path: str,
        relative_path: str,
        new_body: str,
        *,
        expected_body_sha256: str | None = None,
    ) -> gd.EditOutcome:
        """Serena semantic edit of a unique function body, with post-edit surprise detection."""
        with self._lock:
            if self.isolation is not IsolationMode.BWRAP and not self.allow_edit_without_isolation:
                raise SvwError(
                    ErrorCode.POLICY_DENIED,
                    "symbol edits require process isolation; the backend runs in degraded isolation",
                    {"reason": "isolation_unavailable", "isolation": self.isolation.value},
                )
            plan = self.plan_symbol_edit(candidate, name_path, relative_path)
            if plan.edit_path != "serena_symbol" or plan.symbol is None:
                raise SvwError(
                    ErrorCode.UNSUPPORTED_OPTION,
                    "symbol is not editable through Serena; use the exact_range path",
                    {"reason": "not_serena_editable", "detail": plan.reason, "edit_path": plan.edit_path},
                )
            sym = plan.symbol
            assert sym.body is not None and sym.byte_start is not None and sym.byte_end is not None
            if expected_body_sha256 is not None and (
                hashlib.sha256(sym.body.encode()).hexdigest() != expected_body_sha256
            ):
                raise SvwError(ErrorCode.EDIT_CONFLICT, "symbol body differs from the expected hash",
                               {"reason": "expected_body_mismatch", "name_path": sym.name_path})
            self.guard.baseline()
            before = self._file_bytes(relative_path)
            res = self._call(
                "replace_symbol_body",
                {"name_path": name_path, "relative_path": relative_path, "body": new_body},
                "symbol_edit",
            )
            if res.text.strip() != "OK":
                raise SvwError(ErrorCode.BACKEND_UNAVAILABLE, "Serena replace_symbol_body did not confirm",
                               {"backend": BACKEND, "reason": "edit_not_confirmed",
                                "text": self.redactor.text(res.text[:300])})
            changes = self.guard.changed()
            after = self._file_bytes(relative_path)
            region = gd.changed_region(before, after)
            within = region is not None and (
                sym.byte_start <= region[0][0] and region[0][1] <= sym.byte_end
            )
            unexpected = tuple(f for f in changes.files if f != relative_path)
            return gd.EditOutcome(
                edit_path="serena_symbol",
                relative_path=relative_path,
                changed_files=changes.files,
                unexpected_files=unexpected,
                old_span=region[0] if region else None,
                new_span=region[1] if region else None,
                within_target=bool(within),
                counted_as_serena_semantic_edit=True,
                target={"name_path": sym.name_path, "kind": sym.kind,
                        "byte_start": sym.byte_start, "byte_end": sym.byte_end},
            )

    def changed_files(self) -> gd.ChangeSet:
        """Files changed in the candidate since the last baseline (EDT-06 hook)."""
        return self.guard.changed()

    def rebaseline(self) -> None:
        self.guard.baseline()

    # ------------------------------------------------------------------ status
    def describe(self) -> dict[str, Any]:
        s = self._session
        return {
            "backend": BACKEND,
            "alive": self.alive,
            "candidate_id": self.candidate_id,
            "isolation": self.isolation.value,
            "warnings": list(self.warnings),
            "protocol_version": s.init.protocol_version if s and s.init else None,
            "features": dict(self.comparison.features) if self.comparison else {},
            "edit_subset": sorted(self.subset.kinds),
            "failure": self._failure.message if self._failure else None,
        }
