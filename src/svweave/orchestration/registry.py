"""Backend registry: lifecycle, health, capability comparison (plan 6.1, 7.4, tests INT-04, DEP-02).

Health rules:
* ``READY`` only after the probe ran a real fixture query successfully with matching capabilities
  and real isolation;
* ``DEGRADED`` with explicit reasons when something works but not everything (extraction gaps,
  a disabled feature after a tool-schema change, isolation fallback);
* ``UNAVAILABLE`` when the backend cannot be used (spawn/protocol failure, capability mismatch of
  a core feature, process died).
Runtime failures reported through :meth:`note_failure` downgrade health immediately.
"""

from __future__ import annotations

import contextlib
import importlib.metadata
import json
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from svweave.adapters.codebase_memory import CbmAdapter
from svweave.adapters.codebase_memory.adapter import BACKEND as CBM_BACKEND
from svweave.adapters.serena import SerenaAdapter
from svweave.adapters.serena.adapter import BACKEND as SERENA_BACKEND
from svweave.domain.errors import ErrorCode, SvwError
from svweave.domain.states import BackendAvailability
from svweave.orchestration.capability import CapabilityComparison
from svweave.orchestration.health import BackendHealth, unknown
from svweave.orchestration.toolchain import Toolchain
from svweave.security.redaction import Redactor
from svweave.security.sandbox import IsolationMode, SandboxRunner

PYSLANG = "pyslang"
CBM = CBM_BACKEND
SERENA = SERENA_BACKEND
BACKEND_NAMES = (PYSLANG, CBM, SERENA)

#: Seconds before a backend marked UNAVAILABLE by a runtime failure is retried.
RETRY_AFTER_S = 30.0

PYSLANG_PROBE_TOP = "svw_probe_top"
PYSLANG_PROBE_SV = (
    f"module {PYSLANG_PROBE_TOP}(input logic a, output logic b);\n  assign b = a;\nendmodule\n"
)
PYSLANG_PROBE_SCRIPT = """
import json, shlex, sys, pyslang
from pyslang import driver as drv
d = drv.Driver(); d.addStandardArgs()
line = "slang " + shlex.quote(sys.argv[1]) + " --top svw_probe_top"
ok = bool(d.parseCommandLine(line, drv.CommandLineOptions()) and d.processOptions() and d.parseAllSources())
comp = d.createCompilation()
errors = sum(1 for x in comp.getAllDiagnostics() if x.isError())
tops = [i.name for i in comp.getRoot().topInstances]
print(json.dumps({"ok": ok, "errors": errors, "tops": tops, "version": pyslang.__version__}))
"""


def _health_from_comparison(
    h: BackendHealth, cmp: CapabilityComparison, core_name: str
) -> None:
    h.features = dict(cmp.features)
    if not cmp.core_ok or cmp.forbidden_present:
        h.status = BackendAvailability.UNAVAILABLE
        h.error_code = ErrorCode.CAPABILITY_MISMATCH.value
        h.reasons.append(f"CAPABILITY_MISMATCH: core feature {core_name} disabled: " + "; ".join(cmp.reasons))
    elif not cmp.ok:
        h.status = max_degraded(h.status)
        h.error_code = ErrorCode.CAPABILITY_MISMATCH.value
        h.reasons.append(
            "CAPABILITY_MISMATCH: features disabled "
            f"{sorted(f for f, ok in cmp.features.items() if not ok)}: " + "; ".join(cmp.reasons)
        )


def max_degraded(s: BackendAvailability) -> BackendAvailability:
    return s if s is BackendAvailability.UNAVAILABLE else BackendAvailability.DEGRADED


class BackendRegistry:
    def __init__(
        self,
        toolchain: Toolchain,
        backends_root: Path,
        snapshots_root: Path,
        *,
        sandbox: SandboxRunner | None = None,
        allow_degraded_isolation: bool = False,
        protected_paths: tuple[Path, ...] = (),
        fixtures_dir: Path | None = None,
        redactor: Redactor | None = None,
    ) -> None:
        self.toolchain = toolchain
        self.backends_root = Path(backends_root)
        self.snapshots_root = Path(snapshots_root)
        self.sandbox = sandbox or SandboxRunner(allow_degraded=allow_degraded_isolation)
        self.protected_paths = protected_paths
        self.fixtures_dir = fixtures_dir or toolchain.fixtures_dir
        self.redactor = redactor or Redactor([self.backends_root, self.snapshots_root, toolchain.root])
        self._health: dict[str, BackendHealth] = {n: unknown(n) for n in BACKEND_NAMES}
        self._failed_at: dict[str, float] = {}
        self._cbm: CbmAdapter | None = None
        self._serena: SerenaAdapter | None = None
        self._lock = threading.RLock()
        self._serena_lock = threading.Lock()  # one Serena instance at a time (plan 7.1)

    @classmethod
    def for_workspace(
        cls, ws: Any, toolchain: Toolchain | None = None, **kw: Any
    ) -> BackendRegistry:
        """Registry for a loaded ``WorkspaceContext``: the workspace's ORIGINAL source roots are
        wired in as ``protected_paths`` so no backend bind can ever make them writable."""
        if ws.execution.provider != "local":
            raise SvwError(
                ErrorCode.BACKEND_UNAVAILABLE,
                "remote graph/Serena MCP execution is not qualified (P1)",
                {"reason": "srun_mcp_not_supported", "submitted": False},
            )
        protected = tuple(Path(r.abs) for r in ws.roots if r.abs is not None)
        return cls(
            toolchain or Toolchain.discover(),
            ws.state_dir / "backends",
            ws.state_dir / "snapshots",
            protected_paths=protected,
            **kw,
        )

    # ------------------------------------------------------------------ health access
    def health(self, name: str) -> BackendHealth:
        with self._lock:
            return self._health[name]

    def all_health(self) -> dict[str, BackendHealth]:
        with self._lock:
            return dict(self._health)

    def set_health(self, h: BackendHealth) -> None:
        with self._lock:
            self._health[h.name] = h

    def note_failure(self, name: str, err: SvwError) -> BackendHealth:
        """Runtime failure: downgrade health immediately (INT-04)."""
        with self._lock:
            h = self._health.get(name) or unknown(name)
            reason = str(err.details.get("reason") or err.code.value)
            h = BackendHealth(
                name=name,
                status=BackendAvailability.UNAVAILABLE,
                reasons=[f"{err.code.value}: {err.message} ({reason})"],
                limitations=h.limitations,
                version=h.version,
                protocol_version=h.protocol_version,
                isolation=h.isolation,
                features=h.features,
                probed=False,
                error_code=err.code.value,
            )
            self._health[name] = h
            self._failed_at[name] = time.monotonic()
            return h

    def usable(self, name: str) -> bool:
        """May a query try this backend now? UNAVAILABLE backends are retried after a cooldown."""
        with self._lock:
            h = self._health[name]
            if h.status is not BackendAvailability.UNAVAILABLE:
                return True
            if h.probed is False and name not in self._failed_at:
                return True  # never tried: let the first real use decide
            return time.monotonic() - self._failed_at.get(name, 0.0) >= RETRY_AFTER_S

    # ------------------------------------------------------------------ adapters
    def cbm(self, snapshot_root: Path | None = None) -> CbmAdapter:
        """Started cbm adapter (lazily launched); only ``snapshot_root`` is mounted into it."""
        with self._lock:
            if self._cbm is not None and self._cbm.alive:
                return self._cbm
            if self._cbm is not None:
                # The backend died under us: record it (health -> UNAVAILABLE, cooldown) and do
                # NOT silently restart inside the same query (INT-04).
                self._cbm.stop()
                self._cbm = None
                err = SvwError(
                    ErrorCode.BACKEND_UNAVAILABLE, "codebase-memory-mcp process died",
                    {"backend": CBM, "reason": "process_exited"},
                )
                self.note_failure(CBM, err)
                raise err
            ad = CbmAdapter(
                self.toolchain, self.backends_root / "cbm", self.snapshots_root, self.sandbox,
                protected_paths=self.protected_paths, redactor=self.redactor,
            )
            try:
                cmp = ad.start(snapshot_root)
            except BaseException as e:
                # a half-started backend (process, runtime dir) must not outlive the failure
                with contextlib.suppress(Exception):
                    ad.stop()
                if isinstance(e, SvwError):
                    self.note_failure(CBM, e)
                raise
            self._cbm = ad
            h = self._health[CBM]
            if h.status is BackendAvailability.UNAVAILABLE and not h.probed:
                h = BackendHealth(name=CBM, status=BackendAvailability.DEGRADED,
                                  reasons=["started; no fixture probe has run yet"])
                self._health[CBM] = h
            h.features = dict(cmp.features)
            h.isolation = ad.isolation.value
            self._failed_at.pop(CBM, None)
            return ad

    def serena_for(self, candidate: Path) -> SerenaAdapter:
        """Serena instance for ``candidate``; any instance for another candidate is stopped first."""
        with self._lock:
            from svweave.adapters.serena import config as scfg

            if self._serena is not None:
                same = self._serena.candidate_id == scfg.candidate_id(candidate)
                if same and self._serena.alive:
                    return self._serena
                died = same
                self._serena.stop()
                self._serena = None
                if died:
                    err = SvwError(
                        ErrorCode.BACKEND_UNAVAILABLE, "Serena process died",
                        {"backend": SERENA, "reason": "process_exited"},
                    )
                    self.note_failure(SERENA, err)
                    raise err
            ad = SerenaAdapter(
                self.toolchain, self.backends_root / "serena", candidate, self.sandbox,
                protected_paths=self.protected_paths, redactor=self.redactor,
            )
            try:
                ad.start()
            except BaseException as e:
                with contextlib.suppress(Exception):  # never two Serena processes (plan 6.3)
                    ad.stop()
                if isinstance(e, SvwError):
                    self.note_failure(SERENA, e)
                raise
            self._serena = ad
            self._failed_at.pop(SERENA, None)
            return ad

    def stop_serena(self) -> None:
        """Tear down only the Serena instance (edit prepare); other backends keep running."""
        with self._lock:
            ad, self._serena = self._serena, None
        if ad is not None:
            ad.stop()

    def shutdown(self) -> None:
        with self._lock:
            for ad in (self._cbm, self._serena):
                if ad is not None:
                    ad.stop()
            self._cbm = None
            self._serena = None

    def __enter__(self) -> BackendRegistry:
        return self

    def __exit__(self, *exc: object) -> None:
        self.shutdown()

    # ------------------------------------------------------------------ probes
    def probe_pyslang(self) -> BackendHealth:
        """READY needs a REAL compile of a tiny inline module (0 errors, top instance found)."""
        h = BackendHealth(name=PYSLANG, status=BackendAvailability.READY)
        want = self.toolchain.pyslang_version
        try:
            h.version = importlib.metadata.version("pyslang")
        except importlib.metadata.PackageNotFoundError:
            h.status = BackendAvailability.UNAVAILABLE
            h.reasons.append("pyslang is not installed")
            h.error_code = ErrorCode.BACKEND_UNAVAILABLE.value
            return self._finish_probe(h)
        result: dict[str, Any] = {}
        try:
            with tempfile.TemporaryDirectory(prefix="svwps-") as tmp:
                src = Path(tmp) / "probe.sv"
                src.write_text(PYSLANG_PROBE_SV, encoding="utf-8")
                p = subprocess.run(
                    [sys.executable, "-I", "-c", PYSLANG_PROBE_SCRIPT, str(src)],
                    capture_output=True, text=True, timeout=120, check=False,
                )
            result = json.loads(p.stdout.strip().splitlines()[-1]) if p.stdout.strip() else {}
            if p.returncode != 0 and not result:
                h.reasons.append("pyslang compile probe crashed: " + self.redactor.text(p.stderr[-200:]))
        except (OSError, subprocess.SubprocessError, ValueError) as e:
            h.reasons.append(f"pyslang compile probe failed: {type(e).__name__}")
        imported = str(result.get("version", ""))
        h.details = {"locked": want, "imported": imported, "compile": result}
        if not result:
            h.status = BackendAvailability.UNAVAILABLE
            h.error_code = ErrorCode.BACKEND_UNAVAILABLE.value
            h.reasons.append("pyslang cannot import or compile")
        elif imported != want or h.version != want:
            h.status = BackendAvailability.UNAVAILABLE
            h.error_code = ErrorCode.CAPABILITY_MISMATCH.value
            h.reasons.append(f"pyslang {imported or h.version} differs from the locked {want}")
        elif not result.get("ok") or result.get("errors") != 0 or PYSLANG_PROBE_TOP not in result.get("tops", []):
            h.status = BackendAvailability.UNAVAILABLE
            h.error_code = ErrorCode.BACKEND_UNAVAILABLE.value
            h.reasons.append(
                f"fixture compile failed: errors={result.get('errors')}, tops={result.get('tops')}"
            )
        else:
            h.probed = True  # a real compile of the pinned binding succeeded
        return self._finish_probe(h)

    def _finish_probe(self, h: BackendHealth) -> BackendHealth:
        """Store the probe result; a failed probe also starts the retry cooldown."""
        self.set_health(h)
        if h.status is BackendAvailability.UNAVAILABLE:
            self._failed_at[h.name] = time.monotonic()
        else:
            self._failed_at.pop(h.name, None)
        return h

    def _probe_fixture_copy(self, dest: Path) -> list[str]:
        missing = [d for d in ("demo", "uvm") if not (self.fixtures_dir / d).is_dir()]
        if missing:
            raise SvwError(ErrorCode.MISSING_INPUT, "probe fixtures are missing",
                           {"missing": missing, "fixtures_dir": self.redactor.text(str(self.fixtures_dir))})
        if dest.exists():
            shutil.rmtree(dest)
        for d in ("demo", "uvm"):
            shutil.copytree(self.fixtures_dir / d, dest / d, symlinks=True)
        return ["demo", "uvm"]

    def probe_cbm(self) -> BackendHealth:
        """Launch cbm on a COPY of the fixtures in a probe-only state and query it for real."""
        from svweave.adapters.codebase_memory import normalize as nz

        with self._lock:
            if self._cbm is not None:  # one heavy process at a time
                self._cbm.stop()
                self._cbm = None
            h = BackendHealth(name=CBM, status=BackendAvailability.READY)
            probe_root = self.backends_root / "probe"
            snaps = probe_root / "cbm-snapshots"
            sid = "probe-fixture"
            ad: CbmAdapter | None = None
            try:
                self._probe_fixture_copy(snaps / sid)
                ad = CbmAdapter(self.toolchain, probe_root / "cbm-state", snaps, self.sandbox,
                                redactor=self.redactor)
                cmp = ad.start(snaps / sid)
                h.version = ad.init_info.server_version if ad.init_info else None
                h.protocol_version = ad.init_info.protocol_version if ad.init_info else None
                h.isolation = ad.isolation.value
                _health_from_comparison(h, cmp, "index/search")
                if h.status is not BackendAvailability.UNAVAILABLE:
                    m = ad.ensure_indexed(sid, snaps / sid)
                    found: dict[str, bool] = {}
                    for name in ("demo_lane", "demo_top"):
                        r = ad.search(sid, name)
                        found[name] = any(
                            c.name == name and c.file.endswith(f"{name}.sv") for c in r.candidates
                        )
                    h.details = {"nodes": m.nodes, "edges": m.edges, "fixture_hits": found,
                                 "parse_unusable": list(m.parse_unusable),
                                 "parse_partial": [p for p, _ in m.parse_partial]}
                    if not all(found.values()):
                        h.status = max_degraded(h.status)
                        h.reasons.append(f"fixture query failed: {sorted(k for k, v in found.items() if not v)}")
                    h.probed = all(found.values())
                    h.limitations = list(nz.STATIC_LIMITATIONS)
                    if m.parse_unusable or m.parse_partial:
                        h.status = max_degraded(h.status)
                        h.reasons.append(
                            "SystemVerilog extraction gaps on the probe fixture: "
                            f"parse_unusable={list(m.parse_unusable)}, "
                            f"parse_partial={[p for p, _ in m.parse_partial]}"
                        )
            except SvwError as e:
                h.status = BackendAvailability.UNAVAILABLE
                h.error_code = e.code.value
                h.reasons.append(f"{e.code.value}: {e.message} ({e.details.get('reason', '')})")
                h.probed = False
            finally:
                if ad is not None:
                    ad.stop()
            if h.isolation == IsolationMode.DEGRADED_NO_ISOLATION.value:
                h.status = max_degraded(h.status)
                h.reasons.append("DEGRADED ISOLATION: running without a bwrap sandbox")
            return self._finish_probe(h)

    def probe_serena(self) -> BackendHealth:
        with self._lock:
            if self._serena is not None:
                self._serena.stop()
                self._serena = None
            h = BackendHealth(name=SERENA, status=BackendAvailability.READY)
            cand = self.backends_root / "probe" / "serena-candidate"
            ad: SerenaAdapter | None = None
            try:
                self._probe_fixture_copy(cand)
                ad = SerenaAdapter(self.toolchain, self.backends_root / "probe" / "serena-state", cand,
                                   self.sandbox, redactor=self.redactor)
                cmp = ad.start()
                h.version = self.toolchain.serena_version
                h.protocol_version = ad.init_info.protocol_version if ad.init_info else None
                h.isolation = ad.isolation.value
                _health_from_comparison(h, cmp, "find_symbol")
                if h.status is not BackendAvailability.UNAVAILABLE:
                    uvm = "uvm/svw_demo_uvm_pkg.sv"
                    lane = ad.find_symbol(cand, "demo_lane", "demo/rtl/demo_lane.sv")
                    refs = ad.find_referencing_symbols(cand, "demo_lane", "demo/rtl/demo_lane.sv")
                    fn_plan = ad.plan_symbol_edit(cand, "svw_env/build_phase", uvm)
                    task_plan = ad.plan_symbol_edit(cand, "svw_driver/run_phase", uvm)
                    checks = {
                        "find_symbol demo_lane": bool(lane),
                        "references demo_lane -> demo_top": any(r.relative_path.endswith("demo_top.sv") for r in refs),
                        "function svw_env/build_phase editable via Serena": fn_plan.edit_path == "serena_symbol",
                    }
                    h.details = {"checks": checks, "task_route": task_plan.edit_path}
                    failed = sorted(k for k, v in checks.items() if not v)
                    if failed:
                        h.status = max_degraded(h.status)
                        h.reasons.append(f"fixture checks failed: {failed}")
                    h.probed = not failed
                    if task_plan.edit_path == "exact_range":
                        h.limitations.append(
                            "Verible does not expose SystemVerilog task symbols or 'function new': "
                            "such edits use the gateway exact_range path, not Serena (ADR-012)"
                        )
                    h.limitations.append(
                        f"Serena symbol edit qualified for kinds {sorted(ad.subset.kinds)} only"
                    )
                if ad.changed_files():
                    h.status = max_degraded(h.status)
                    h.reasons.append("probe modified its candidate unexpectedly")
            except SvwError as e:
                h.status = BackendAvailability.UNAVAILABLE
                h.error_code = e.code.value
                h.reasons.append(f"{e.code.value}: {e.message} ({e.details.get('reason', '')})")
                h.probed = False
            finally:
                if ad is not None:
                    ad.stop()
            if h.isolation == IsolationMode.DEGRADED_NO_ISOLATION.value:
                h.status = max_degraded(h.status)
                h.reasons.append("DEGRADED ISOLATION: running without a bwrap sandbox; editing disabled")
            return self._finish_probe(h)

    def probe_all(self) -> dict[str, BackendHealth]:
        """Sequential on purpose: one heavy backend process at a time (host RAM)."""
        self.probe_pyslang()
        self.probe_cbm()
        self.probe_serena()
        return self.all_health()

    def probe(self, name: str) -> BackendHealth:
        fns: dict[str, Callable[[], BackendHealth]] = {
            PYSLANG: self.probe_pyslang, CBM: self.probe_cbm, SERENA: self.probe_serena,
        }
        return fns[name]()
