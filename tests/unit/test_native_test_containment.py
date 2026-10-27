"""HDL-R10 / SEC-05: pure regressions for mandatory real-test launch boundaries.

Only stdlib and pure production sandbox construction are used. These are unit checks, not
substitutes for the real native integration / negative-control cases they protect.
"""
from __future__ import annotations

import ast
import hashlib
import importlib.metadata
import json
import os
import subprocess
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

from svweave.domain.errors import ErrorCode, SvwError  # noqa: E402
from svweave.domain.states import BackendAvailability  # noqa: E402
from svweave.orchestration.health import BackendHealth  # noqa: E402
from svweave.security.sandbox import (  # noqa: E402
    HostCapability,
    IsolationMode,
    SandboxRunner,
    SandboxSpec,
)


def function(path: str, name: str, **bindings):
    """Exercise the actual function without importing native/dependency-heavy test modules."""
    tree = ast.parse((ROOT / path).read_text())
    node = next(node for node in ast.walk(tree) if isinstance(node, ast.FunctionDef) and node.name == name)
    module = ast.Module(body=[ast.ImportFrom(module="__future__", names=[ast.alias(name="annotations")], level=0), node], type_ignores=[])
    env = {"__file__": str(ROOT / path), "Path": Path, "json": json, "os": os,
           "sys": sys, "hashlib": hashlib, **bindings}
    exec(compile(ast.fix_missing_locations(module), str(ROOT / path), "exec"), env)
    return env[name]


class NativeTestContainmentTests(unittest.TestCase):
    def test_raw_dto_requires_explicit_mandatory_worker_and_preserves_payload(self):
        payload = {"status": {"compiled": False}, "driver_messages": ["bad flag"], "stats": {}}
        run = Mock(return_value=types.SimpleNamespace(
            isolation="bwrap", ok=True, exit_code=0, stdout=json.dumps(payload).encode(),
            message="", stderr_tail="",
        ))
        raw = function("tests/integration/helpers.py", "raw_dto", run_worker=run, IsolationMode=IsolationMode)
        request = object()
        self.assertEqual(raw(request), payload)
        run.assert_called_once_with(request, timeout_s=180, memory_mib=4096, allow_degraded=False)

    def test_raw_dto_rejects_successful_but_degraded_worker(self):
        run = Mock(return_value=types.SimpleNamespace(
            isolation="degraded_no_isolation", ok=True, exit_code=0, stdout=b"{}",
            message="", stderr_tail="",
        ))
        raw = function("tests/integration/helpers.py", "raw_dto", run_worker=run, IsolationMode=IsolationMode)
        with self.assertRaises(AssertionError):
            raw(object())

    def test_native_check_has_narrow_inputs_and_checks_outer_evidence(self):
        evidence = {"safety": {"network_private": True, "pid_private": True,
                               "source_readonly": True, "secret_absent": True},
                    "result": {"native": "real-result-placeholder"}}
        boundary = types.SimpleNamespace(probe_spec=Mock(return_value="spec"), run=Mock(
            return_value=subprocess.CompletedProcess([], 0, json.dumps(evidence), "")))
        run = function("tests/integration/helpers.py", "native_check", boundary=boundary)
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, {"SVW_TEST_NATIVE_SECRET": "synthetic"}):
            source = Path(tmp) / "source"
            source.mkdir()
            (source / "t.sv").write_text("module t; endmodule")
            self.assertEqual(run("degraded_slang", source), evidence["result"])
            spec = boundary.probe_spec.call_args.kwargs
            self.assertEqual(spec["sources"], (source, ROOT / "tests/integration/native_checks.py", ROOT / "src"))
            self.assertEqual(spec["writable"], ())
            self.assertNotIn(ROOT, spec["sources"])
            self.assertEqual(boundary.run.call_args.args[0], "spec")
            self.assertIn("-I", boundary.run.call_args.args[1])

    def test_native_check_fails_closed_when_outer_boundary_denies_launch(self):
        boundary = types.SimpleNamespace(probe_spec=Mock(return_value="spec"), run=Mock(
            side_effect=SvwError(ErrorCode.BACKEND_UNAVAILABLE, "no sandbox")))
        run = function("tests/integration/helpers.py", "native_check", boundary=boundary)
        with (
            tempfile.TemporaryDirectory() as tmp,
            patch.dict(os.environ, {"SVW_TEST_NATIVE_SECRET": "synthetic"}),
            self.assertRaises(SvwError),
        ):
            run("degraded_cbm", Path(tmp))
        boundary.run.assert_called_once()

    def test_native_check_rejects_missing_private_namespace_evidence(self):
        evidence = {"safety": {"network_private": False, "pid_private": True,
                               "source_readonly": True, "secret_absent": True}, "result": {}}
        boundary = types.SimpleNamespace(probe_spec=Mock(return_value="spec"), run=Mock(
            return_value=subprocess.CompletedProcess([], 0, json.dumps(evidence), "")))
        run = function("tests/integration/helpers.py", "native_check", boundary=boundary)
        with (
            tempfile.TemporaryDirectory() as tmp,
            patch.dict(os.environ, {"SVW_TEST_NATIVE_SECRET": "synthetic"}),
            self.assertRaises(AssertionError),
        ):
            run("degraded_cbm", Path(tmp))

    def test_native_check_detects_source_mutation(self):
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, {"SVW_TEST_NATIVE_SECRET": "synthetic"}):
            source = Path(tmp)
            file = source / "t.sv"
            file.write_text("original")
            def changed(*args, **kwargs):
                file.write_text("mutated")
                return subprocess.CompletedProcess([], 1, "", "")
            boundary = types.SimpleNamespace(probe_spec=Mock(return_value="spec"), run=Mock(side_effect=changed))
            run = function("tests/integration/helpers.py", "native_check", boundary=boundary)
            with self.assertRaisesRegex(AssertionError, "changed its read-only source"):
                run("degraded_slang", source)

    def test_native_dispatch_requires_safety_before_loading_a_backend(self):
        safety = Mock(side_effect=AssertionError("same host namespace"))
        backend = Mock()
        main = function("tests/integration/native_checks.py", "main", safety=safety, degraded_cbm=backend)
        with (
            patch.object(sys, "argv", ["check", "degraded_cbm", "/source", "net", "pid", "/repo", "/state"]),
            self.assertRaisesRegex(AssertionError, "same host namespace"),
        ):
            main()
        backend.assert_not_called()

    def test_inherited_degraded_opt_out_is_rejected_before_fixture_hooks(self):
        class UsageError(Exception):
            pass
        configure = function("tests/conftest.py", "pytest_configure", pytest=types.SimpleNamespace(UsageError=UsageError))
        with (
            patch.dict(os.environ, {"SVWEAVE_ALLOW_DEGRADED_WORKER": "1"}),
            self.assertRaisesRegex(UsageError, "forbidden"),
        ):
            configure()
        # pytest_configure runs before collection/setup, unlike an autouse function fixture.
        tree = ast.parse((ROOT / "tests/conftest.py").read_text())
        hook = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "pytest_configure")
        self.assertEqual(hook.decorator_list, [])

    def test_normal_test_environment_remains_allowed(self):
        configure = function("tests/conftest.py", "pytest_configure", pytest=types.SimpleNamespace(UsageError=RuntimeError))
        with patch.dict(os.environ, {}, clear=True):
            configure()

    def test_pytest_rejects_inherited_opt_out_before_session_fixture_runs(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            marker = root / "fixture-started"
            (root / "test_order.py").write_text(
                "import pytest\nfrom pathlib import Path\n"
                "@pytest.fixture(scope='session', autouse=True)\n"
                f"def native_fixture():\n    Path({str(marker)!r}).write_text('unexpected')\n"
                "def test_fixture_order():\n    pass\n"
            )
            env = {**os.environ, "SVWEAVE_ALLOW_DEGRADED_WORKER": "1",
                   "PYTHONPATH": os.pathsep.join((str(ROOT), str(ROOT / "src"))),
                   "PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1"}
            result = subprocess.run(
                [sys.executable, "-m", "pytest", "-p", "tests.conftest", "-q", "test_order.py"],
                cwd=root, env=env, capture_output=True, text=True, timeout=30, check=False,
            )
            self.assertEqual(result.returncode, 4, result.stdout + result.stderr)
            self.assertIn("SVWEAVE_ALLOW_DEGRADED_WORKER=1 is forbidden", result.stderr)
            self.assertFalse(marker.exists(), "session fixture executed before isolation was enforced")

    def test_native_imports_are_only_in_guarded_child_not_pytest_cases(self):
        violations = []
        for suite in ("integration", "fault_injection", "acceptance", "unit", "contract"):
            for path in (ROOT / "tests" / suite).glob("test_*.py"):
                for node in ast.walk(ast.parse(path.read_text())):
                    if isinstance(node, ast.Import):
                        imports = [a.name for a in node.names]
                    elif isinstance(node, ast.ImportFrom):
                        imports = [node.module or ""]
                    else:
                        continue
                    if any(name == "pyslang" or name.startswith("pyslang.") or name == "svweave.adapters.slang.worker" for name in imports):
                        violations.append(f"{path.relative_to(ROOT)}:{node.lineno}")
        self.assertEqual(violations, [])


class NativeRegistryProbeTests(unittest.TestCase):
    def probe(self, available=True):
        cap = HostCapability(available, "/usr/bin/bwrap", "test", None if available else "denied")
        registry = types.SimpleNamespace(
            toolchain=types.SimpleNamespace(pyslang_version="12.0.0"),
            sandbox=SandboxRunner(allow_degraded=True, capability=cap),
            redactor=types.SimpleNamespace(text=lambda text: text), _finish_probe=lambda h: h,
        )
        def spec(req):
            return SandboxSpec(ro_paths=(Path(req.snapshot_root),), chdir=Path(req.snapshot_root), env={"PATH": "/usr/bin:/bin"})
        method = function(
            "src/svweave/orchestration/registry.py", "probe_pyslang",
            BackendHealth=BackendHealth, BackendAvailability=BackendAvailability, ErrorCode=ErrorCode,
            SvwError=SvwError, PYSLANG="pyslang", PYSLANG_PROBE_TOP="svw_probe_top",
            PYSLANG_PROBE_SV="module svw_probe_top; endmodule", PYSLANG_PROBE_SCRIPT="native probe sentinel",
            importlib=importlib, tempfile=tempfile, subprocess=subprocess,
            SandboxRunner=SandboxRunner, WorkerRequest=types.SimpleNamespace, worker_sandbox_spec=spec,
        )
        return lambda: method(registry)

    def test_registry_degraded_opt_in_cannot_bypass_probe_sandbox(self):
        probe = self.probe(available=False)
        with patch.object(importlib.metadata, "version", return_value="12.0.0"), patch("svweave.security.sandbox.subprocess.run") as spawn:
            health = probe()
        spawn.assert_not_called()
        self.assertEqual(health.status, BackendAvailability.UNAVAILABLE)
        self.assertEqual(health.error_code, "BACKEND_UNAVAILABLE")
        self.assertFalse(health.probed)

    def test_registry_compile_uses_readonly_netless_clean_environment(self):
        probe = self.probe()
        output = json.dumps({"ok": True, "errors": 0, "tops": ["svw_probe_top"], "version": "12.0.0"})
        with patch.object(importlib.metadata, "version", return_value="12.0.0"), patch(
            "svweave.security.sandbox.subprocess.run", return_value=subprocess.CompletedProcess([], 0, output, "")
        ) as spawn:
            health = probe()
        self.assertEqual(health.status, BackendAvailability.READY)
        self.assertTrue(health.probed)
        argv = spawn.call_args.args[0]
        self.assertIn("--unshare-net", argv)
        self.assertIn("--unshare-pid", argv)
        self.assertIn("--ro-bind", argv)
        self.assertNotIn("--bind", argv)
        self.assertIn("native probe sentinel", argv)
        self.assertEqual(spawn.call_args.kwargs["env"], {"PATH": "/usr/bin:/bin"})

    def test_registry_nonzero_native_exit_cannot_qualify_success_json(self):
        probe = self.probe()
        output = json.dumps({"ok": True, "errors": 0, "tops": ["svw_probe_top"], "version": "12.0.0"})
        with patch.object(importlib.metadata, "version", return_value="12.0.0"), patch(
            "svweave.security.sandbox.subprocess.run", return_value=subprocess.CompletedProcess([], -11, output, "crashed after output")
        ):
            health = probe()
        self.assertEqual(health.status, BackendAvailability.UNAVAILABLE)
        self.assertFalse(health.probed)
        self.assertEqual(health.error_code, "BACKEND_UNAVAILABLE")
        self.assertTrue(any("crashed" in reason for reason in health.reasons))


if __name__ == "__main__":
    unittest.main()
