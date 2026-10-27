"""HDL-R10 / SEC-05: pure launch-boundary and probe evidence regressions.

No HDL parser, backend binary, bwrap, installation or host security setting is executed.
Only the bounded subprocess-supervisor tests run harmless stdlib Python children, with a
mocked wrapper; these are not native sandbox/qualification evidence.
"""
from __future__ import annotations

import ast
import importlib.util
import json
import os
import runpy
import subprocess
import sys
import tempfile
import types
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts/spikes"))
sys.path.insert(0, str(ROOT / "src"))
import _sandbox as boundary  # noqa: E402

from svweave.domain.errors import SvwError  # noqa: E402
from svweave.security.sandbox import (  # noqa: E402
    HostCapability,
    IsolationMode,
    SandboxRunner,
    WrappedCommand,
)


def load_script(name: str, path: str):
    spec = importlib.util.spec_from_file_location(name, ROOT / path)
    module = importlib.util.module_from_spec(spec)
    # The tests don't use anyio/MCP. Keep these checks runnable with only stdlib installed.
    with patch.dict(sys.modules, {"anyio": types.ModuleType("anyio")}):
        spec.loader.exec_module(module)
    return module


class ProbeBoundaryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.source = self.root / "source"
        self.state = self.root / "state"
        self.tool = self.root / "tool"
        self.source.mkdir()
        self.state.mkdir()
        self.tool.write_text("never executed")
        self.spec = boundary.probe_spec(sources=(self.source,), tools=(self.tool,),
                                        writable=(self.state,), cwd=self.source)

    def runner(self, available=True):
        return SandboxRunner(capability=HostCapability(available, "/usr/bin/bwrap", "test",
                                                        None if available else "test denied"))

    def test_HDL_CI_04_real_wrap_has_read_only_inputs_scoped_state_and_no_network(self):
        with patch.object(boundary, "SandboxRunner", return_value=self.runner()) as factory:
            result = boundary.wrap(self.spec, [str(self.tool), "--probe"])
        factory.assert_called_once_with(allow_degraded=False)
        self.assertIn("--unshare-net", result.argv)
        self.assertIn("--unshare-pid", result.argv)
        for path in (self.source, self.tool):
            i = result.argv.index(str(path))
            self.assertEqual(result.argv[i - 1], "--ro-bind")
        self.assertEqual(result.argv[result.argv.index(str(self.state)) - 1], "--bind")
        self.assertNotIn(str(ROOT), result.argv)
        self.assertNotIn(str(Path.home()), result.argv)
        self.assertEqual(result.argv[-2:], [str(self.tool), "--probe"])
        self.assertIn("-i", result.argv)

    def test_HDL_CI_04_missing_sandbox_never_starts_a_process(self):
        with (
            patch.object(boundary, 'SandboxRunner', return_value=self.runner(False)),
            patch.object(boundary.subprocess, 'Popen') as start,
            self.assertRaises(SvwError),
        ):
            boundary.run(self.spec, [str(self.tool)])
        start.assert_not_called()

    def test_HDL_CI_04_degraded_environment_flag_cannot_enable_fallback(self):
        with (
            patch.dict(os.environ, {'SVWEAVE_ALLOW_DEGRADED_WORKER': '1'}),
            patch.object(boundary, 'SandboxRunner', return_value=self.runner(False)),
            self.assertRaises(SvwError),
        ):
            boundary.wrap(self.spec, [str(self.tool)])

    def test_HDL_CI_04_network_enabled_spec_is_rejected(self):
        with self.assertRaises(SvwError):
            boundary.wrap(replace(self.spec, network=True), [str(self.tool)])

    def test_HDL_CI_04_full_etc_spec_is_rejected(self):
        with self.assertRaises(SvwError):
            boundary.wrap(replace(self.spec, etc="full"), [str(self.tool)])

    def test_HDL_CI_04_secret_env_key_is_rejected(self):
        with self.assertRaises(SvwError):
            boundary.probe_spec(env={"AWS_SECRET_ACCESS_KEY": "not-a-real-secret"})

    def test_HDL_CI_04_source_and_state_overlap_is_rejected(self):
        with self.assertRaises(SvwError):
            boundary.probe_spec(sources=(self.source,), writable=(self.source / "nested",))

    def test_HDL_CI_04_tool_and_state_overlap_is_rejected(self):
        with self.assertRaises(SvwError):
            boundary.probe_spec(tools=(self.tool,), writable=(self.root,))

    def test_HDL_CI_04_original_repo_cannot_be_writable(self):
        with self.assertRaises(SvwError):
            boundary.probe_spec(writable=(ROOT / "fixtures",))

    def test_HDL_CI_04_broad_runtime_fallback_is_rejected(self):
        venv = self.root / "venv"
        with (
            patch.object(boundary, 'python_binds', return_value=[venv]),
            self.assertRaises(SvwError),
        ):
            boundary.probe_spec(python_venvs=(venv,))

    def test_HDL_CI_04_runtime_symlink_topology_is_narrow_and_complete(self):
        runtime = self.root / "runtime"
        (runtime / "bin").mkdir(parents=True)
        (runtime / "lib/python3.12").mkdir(parents=True)
        (runtime / "bin/python3.12").write_text("synthetic interpreter; never execute")
        (runtime / "bin/python3").symlink_to("python3.12")
        (runtime / "bin/python").symlink_to("python3")
        spec = boundary.probe_spec(python_venvs=(runtime,))
        self.assertNotIn(runtime, spec.ro_paths)
        self.assertIn(runtime / "bin/python3.12", spec.ro_paths)
        self.assertIn(("python3", runtime / "bin/python"), spec.symlinks)
        self.assertIn(("python3.12", runtime / "bin/python3"), spec.symlinks)

    def test_HDL_CI_04_existing_serena_runtime_api_is_same_function(self):
        # Execute only the stdlib config module, without the package's optional MCP __init__.
        config = runpy.run_path(str(ROOT / "src/svweave/adapters/serena/config.py"))
        from svweave.security import python_runtime
        self.assertIs(config["python_binds"], python_runtime.python_binds)
        self.assertIs(config["python_links"], python_runtime.python_links)
        self.assertIs(config["python_prefix_to_bind"], python_runtime.python_prefix_to_bind)

    def test_HDL_CI_04_serena_editable_source_is_explicit_and_readonly(self):
        toolchain = self.root / "toolchain"
        venv = toolchain / "serena-venv"
        (venv / "bin").mkdir(parents=True)
        (venv / "bin/python").symlink_to("/usr/bin/python3")
        context = self.root / "context.yml"
        spec = boundary.serena_spec(toolchain, self.state, self.source, context, boundary.BASE_ENV)
        self.assertIn(toolchain / "serena-src", spec.ro_paths)
        self.assertIn(venv, spec.ro_paths)
        self.assertNotIn(toolchain, spec.ro_paths)
        self.assertIn(self.source, spec.ro_paths)
        self.assertEqual(spec.rw_paths, (self.state / "work",))

    def fake_wrap(self, argv):
        # Unit test only: bypass bwrap for a harmless stdlib supervision payload, never HDL.
        return patch.object(boundary, "wrap", return_value=WrappedCommand(argv, IsolationMode.BWRAP))

    def test_HDL_CI_04_failed_metadata_is_not_accepted(self):
        with (
            patch.object(boundary, 'run', return_value=subprocess.CompletedProcess([], 1, 'fake version', '')),
            self.assertRaises(SvwError),
        ):
            boundary.checked_text(self.spec, [str(self.tool)])

    def test_HDL_CI_04_empty_metadata_is_not_accepted(self):
        with (
            patch.object(boundary, 'run', return_value=subprocess.CompletedProcess([], 0, '', '')),
            self.assertRaises(SvwError),
        ):
            boundary.checked_text(self.spec, [str(self.tool)])

    def test_HDL_CI_04_subprocess_environment_has_no_inherited_secret(self):
        code = "import json,os;print(json.dumps(dict(os.environ)))"
        with self.fake_wrap([sys.executable, "-I", "-c", code]), \
                patch.dict(os.environ, {"SVW_TEST_SECRET": "must-not-leak"}):
            result = boundary.run(self.spec, ["unit-only"])
        env = json.loads(result.stdout)
        self.assertNotIn("SVW_TEST_SECRET", env)
        self.assertNotIn("HOME", env)
        self.assertEqual(env["PATH"], "/usr/bin:/bin")

    def test_HDL_CI_04_stdout_overflow_fails_not_truncates(self):
        with (
            self.fake_wrap([sys.executable, '-I', '-c', "print('x'*10000)"]),
            self.assertRaises(SvwError) as error,
        ):
            boundary.run(self.spec, ["unit-only"], max_stdout_bytes=128)
        self.assertEqual(error.exception.code.value, "OUTPUT_LIMIT")

    def test_HDL_CI_04_stderr_overflow_fails_not_truncates(self):
        with (
            self.fake_wrap([sys.executable, '-I', '-c', "import sys;print('x'*10000,file=sys.stderr)"]),
            self.assertRaises(SvwError),
        ):
            boundary.run(self.spec, ["unit-only"], max_stderr_bytes=128)

    def test_HDL_CI_04_timeout_reaps_descendant_process_group(self):
        pidfile = self.root / "child.pid"
        code = ("import subprocess,sys,time;from pathlib import Path;"
                "p=subprocess.Popen([sys.executable,'-I','-c','import time;time.sleep(60)']);"
                "Path(sys.argv[1]).write_text(str(p.pid));time.sleep(60)")
        with (
            self.fake_wrap([sys.executable, '-I', '-c', code, str(pidfile)]),
            self.assertRaises(subprocess.TimeoutExpired),
        ):
            boundary.run(self.spec, ["unit-only"], timeout=0.5)
        self.assertTrue(pidfile.exists())
        status = Path(f"/proc/{pidfile.read_text()}/stat")
        self.assertTrue(not status.exists() or status.read_text().rsplit(")", 1)[1].split()[0] == "Z")


class ProbeEvidenceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.probe = load_script("qualification_probe", "scripts/probe_backends.py")

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        (self.root / "toolchain/slang").mkdir(parents=True)
        (self.root / "toolchain/slang/slang").write_text("fake unit-test binary; never execute")
        (self.root / "probe").mkdir()
        self.addCleanup(patch.stopall)
        patch.object(self.probe, "TOOLCHAIN", self.root / "toolchain").start()
        patch.object(self.probe, "PROBE", self.root / "probe").start()
        patch.object(self.probe, "checked_text", return_value="test version").start()

    def cli_run(self, spec, argv, **kwargs):
        if "--all-deps" in argv:
            out = Path(argv[argv.index("--all-deps") + 1])
            out.write_text("fixtures/demo/include/demo_defs.svh\n")
            self.assertEqual(spec.rw_paths, (out.parent,))
            self.assertEqual(spec.ro_paths[0], ROOT / "fixtures/demo")
            return subprocess.CompletedProcess(argv, 0, "Build succeeded: 0 errors", "")
        return subprocess.CompletedProcess(argv, 0, "slang version test", "")

    def test_HDL_CI_04_cli_positive_keeps_exact_real_checks(self):
        with patch.object(self.probe, "run", side_effect=self.cli_run) as run:
            result = self.probe.probe_slang_cli()
        self.assertEqual(result["status"], "READY")
        self.assertEqual(run.call_count, 1)
        self.assertIn("--single-unit", result["argv"])
        self.assertIn("--ast-json-source-info", result["argv"])
        self.assertTrue(all(c["pass"] for c in result["checks"]))

    def test_HDL_CI_04_changed_source_cannot_be_ready(self):
        with patch.object(self.probe, "run", side_effect=self.cli_run), \
                patch.object(self.probe, "tree_manifest", side_effect=[{"f": "old"}, {"f": "changed"}]):
            result = self.probe.probe_slang_cli()
        self.assertNotEqual(result["status"], "READY")
        self.assertFalse(result["checks"][-1]["pass"])

    def test_HDL_CI_04_cli_missing_new_output_cannot_reuse_stale_success(self):
        with patch.object(self.probe, "run", side_effect=self.cli_run):
            old = self.probe.probe_slang_cli()
        with patch.object(self.probe, "run", return_value=subprocess.CompletedProcess([], 0, "0 errors", "")):
            new = self.probe.probe_slang_cli()
        self.assertNotEqual(new["status"], "READY")
        self.assertNotEqual(old["argv"], new["argv"])

    def test_HDL_CI_04_cli_dependency_overflow_cannot_be_ready(self):
        def oversized(spec, argv, **kwargs):
            result = self.cli_run(spec, argv, **kwargs)
            if "--all-deps" in argv:
                Path(argv[argv.index("--all-deps") + 1]).write_bytes(b"x" * (1024 * 1024 + 1))
            return result
        with (
            patch.object(self.probe, 'run', side_effect=oversized),
            self.assertRaisesRegex(ValueError, 'exceeds'),
        ):
            self.probe.probe_slang_cli()

    def test_HDL_CI_04_cli_dependency_symlink_is_rejected(self):
        def symlinked(spec, argv, **kwargs):
            result = self.cli_run(spec, argv, **kwargs)
            if "--all-deps" in argv:
                path = Path(argv[argv.index("--all-deps") + 1])
                path.unlink()
                path.symlink_to(ROOT / "README.md")
            return result
        with (
            patch.object(self.probe, 'run', side_effect=symlinked),
            self.assertRaises(SvwError),
        ):
            self.probe.probe_slang_cli()

    def test_HDL_CI_04_pyslang_sandbox_denial_is_unavailable(self):
        with patch.object(self.probe, "checked_text", side_effect=RuntimeError("isolation denied")):
            result = self.probe.probe_pyslang()
        self.assertEqual(result["status"], "UNAVAILABLE")
        self.assertIn("isolation denied", result["error"])

    def test_HDL_CI_04_cbm_original_state_is_rejected_before_host_mutation(self):
        cbm = load_script("probe_cbm_unit", "scripts/spikes/cbm_spike.py")
        with (
            patch.object(Path, 'mkdir') as mkdir,
            patch.object(Path, 'chmod') as chmod,
            patch.object(boundary, 'run') as run,
            self.assertRaises(SvwError),
        ):
            cbm.configure_state(self.root / "toolchain/slang/slang", boundary.BASE_ENV, ROOT / "fixtures")
        mkdir.assert_not_called()
        chmod.assert_not_called()
        run.assert_not_called()

    def test_HDL_CI_04_cbm_config_all_commands_use_the_same_isolated_spec(self):
        cbm = load_script("probe_cbm_unit", "scripts/spikes/cbm_spike.py")
        state = self.root / "cbm-state"
        with patch.object(boundary, "run", return_value=subprocess.CompletedProcess([], 0, "safe config", "")) as run:
            result = cbm.configure_state(self.root / "toolchain/slang/slang", boundary.BASE_ENV, state)
        self.assertEqual(run.call_count, 5)
        self.assertEqual(result["config_list"], "safe config")
        self.assertTrue(all(call.args[0].rw_paths == (state,) for call in run.call_args_list))
        self.assertTrue(all(call.args[0].tmp_dirs == ("/tmp/cbm-rt",) for call in run.call_args_list))

    def test_HDL_CI_04_cbm_failed_safe_setting_stops_before_server_launch(self):
        cbm = load_script("probe_cbm_unit", "scripts/spikes/cbm_spike.py")
        with (
            patch.object(boundary, 'run', return_value=subprocess.CompletedProcess([], 1, '', 'denied')) as run,
            self.assertRaisesRegex(RuntimeError, 'safe config'),
        ):
            cbm.configure_state(self.root / "toolchain/slang/slang", boundary.BASE_ENV, self.root / "cbm-state")
        self.assertEqual(run.call_count, 1)

    def test_HDL_CI_04_sandbox_denial_reason_is_preserved_in_evidence(self):
        from svweave.domain.errors import ErrorCode
        error = SvwError(ErrorCode.BACKEND_UNAVAILABLE, "mandatory isolation unavailable",
                         {"isolation": "UNAVAILABLE", "reason": "nested user namespace denied"})
        with patch.object(self.probe, "checked_text", side_effect=error):
            result = self.probe.probe_pyslang()
        self.assertEqual(result["status"], "UNAVAILABLE")
        self.assertEqual(result["error_details"]["reason"], "nested user namespace denied")

    def test_HDL_CI_04_grouped_sandbox_denial_reason_is_preserved(self):
        from svweave.domain.errors import ErrorCode
        error = SvwError(ErrorCode.BACKEND_UNAVAILABLE, "mandatory isolation unavailable",
                         {"reason": "native host refusal"})
        result = self.probe.failure_record(ExceptionGroup("transport", [error]))
        self.assertEqual(result["causes"][0]["error_details"]["reason"], "native host refusal")

    def test_HDL_CI_04_nested_preflight_preserves_denial_and_fails_closed(self):
        from _cleanroom_preflight import collect_nested_preflight
        observation = {"capability": {"available": False, "reason": "nested namespace denied"}}
        result = subprocess.CompletedProcess([], 1, json.dumps(observation), "")
        with patch.object(subprocess, "run", return_value=result) as transport:
            report = collect_nested_preflight(self.root / "python", self.root, transport)
        self.assertFalse(report["ready"])
        self.assertEqual(report["observation"]["capability"]["reason"], "nested namespace denied")
        argv, cwd, timeout = transport.call_args.args
        self.assertEqual(argv[1:3], ["-I", "-c"])
        self.assertIn("probe_host(refresh=True)", argv[3])
        self.assertEqual((cwd, timeout), (self.root, 60))

    def test_HDL_CI_04_nested_preflight_requires_real_success_fields(self):
        from _cleanroom_preflight import collect_nested_preflight
        observation = {"capability": {"available": True, "reason": None}}
        result = subprocess.CompletedProcess([], 0, json.dumps(observation), "")
        with patch.object(subprocess, "run", return_value=result) as transport:
            report = collect_nested_preflight(self.root / "python", self.root, transport)
        self.assertTrue(report["ready"])

    def test_HDL_CI_04_nested_preflight_malformed_output_is_not_ready(self):
        from _cleanroom_preflight import collect_nested_preflight
        result = subprocess.CompletedProcess([], 0, "not-json", "")
        with patch.object(subprocess, "run", return_value=result) as transport:
            report = collect_nested_preflight(self.root / "python", self.root, transport)
        self.assertFalse(report["ready"])
        self.assertIn("parse_error", report)

    def test_HDL_CI_04_qualification_scripts_have_no_raw_subprocess_launches(self):
        for name in ("scripts/probe_backends.py", "scripts/spikes/pyslang_spike.py",
                     "scripts/spikes/cbm_spike.py", "scripts/spikes/serena_spike.py"):
            tree = ast.parse((ROOT / name).read_text())
            launches = [node for node in ast.walk(tree) if isinstance(node, ast.Call)
                        and isinstance(node.func, ast.Attribute)
                        and isinstance(node.func.value, ast.Name)
                        and node.func.value.id == "subprocess"
                        and node.func.attr in {"run", "Popen", "call", "check_call", "check_output"}]
            self.assertEqual(launches, [], name)

    def test_HDL_CI_04_mcp_requires_explicit_sandbox_and_no_default_environment(self):
        source = (ROOT / "scripts/spikes/_mcp_common.py").read_text()
        tree = ast.parse(source)
        session = next(n for n in tree.body if isinstance(n, ast.AsyncFunctionDef) and n.name == "mcp_session")
        self.assertEqual(session.args.kwonlyargs[0].arg, "sandbox")
        self.assertIsNone(session.args.kw_defaults[0])
        self.assertIn("wrapped = wrap(sandbox, [command, *args])", source)
        self.assertIn("env=LAUNCH_ENV", source)
        self.assertIn("inherit_default_environment=False", source)


if __name__ == "__main__":
    unittest.main()
