"""DEP-01-R2 pure contract checks; stdlib fake controllers are NOT backend evidence."""
from __future__ import annotations

import ast
import contextlib
import json
import os
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))
import _cleanroom_controller as controller  # noqa: E402


class CleanroomContractTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.room = Path(self.tmp.name)
        (self.room / ".venv/bin").mkdir(parents=True)
        (self.room / ".venv/bin/python").symlink_to(sys.executable)
        (self.room / "scripts").mkdir()
        self.script = self.room / "scripts/probe_backends.py"

    def run_fake_controller(self, code, timeout=10):
        self.script.write_text(code)
        return controller.run_controller(controller.probe_argv(self.room), self.room, timeout)

    def test_DEP_01_R2_controller_receives_only_explicit_environment(self):
        with patch.dict(os.environ, {"SYNTHETIC_SECRET": "must-not-leak", "SVWEAVE_ALLOW_DEGRADED_WORKER": "1",
                                     "PYTHONPATH": "/not/a/runtime", "HOME": "/not/the/cleanroom"}):
            result = self.run_fake_controller("import json,os;print(json.dumps(dict(os.environ)))")
        self.assertEqual(result.returncode, 0)
        env = json.loads(result.stdout)
        # Python may add LC_CTYPE when coercing the C locale; it cannot inherit any host secret.
        self.assertLessEqual(set(env), set(controller.controller_env(self.room)) | {"LC_CTYPE"})
        self.assertEqual(env["SVWEAVE_REQUIRE_BACKENDS"], "1")
        self.assertEqual(env["HOME"], str(self.room / ".svweave/controller-home"))
        self.assertEqual(env["PATH"], "/usr/bin:/bin")
        self.assertNotIn("SYNTHETIC_SECRET", env)
        self.assertNotIn("SVWEAVE_ALLOW_DEGRADED_WORKER", env)

    def test_DEP_01_R2_arbitrary_native_command_is_rejected_before_launch(self):
        with patch.object(subprocess, "Popen") as launch, self.assertRaises(ValueError):
            controller.run_controller(["/usr/bin/slang", "source.sv"], self.room, 10)
        launch.assert_not_called()

    def test_DEP_01_R2_controller_cannot_add_backend_selection_or_alternate_output(self):
        with patch.object(subprocess, "Popen") as launch, self.assertRaises(ValueError):
            controller.run_controller(controller.probe_argv(self.room) + ["--only", "pyslang"], self.room, 10)
        launch.assert_not_called()

    def test_DEP_01_R2_preflight_uses_same_sanitized_controller_boundary(self):
        import _cleanroom_preflight as preflight
        result = subprocess.CompletedProcess([], 0, '{"capability":{"available":true}}', "")
        with patch.object(controller, "run_controller", return_value=result) as launch:
            report = preflight.collect_sandbox_preflight(self.room / ".venv/bin/python", self.room, launch)
        self.assertTrue(report["ready"])
        self.assertEqual(launch.call_args.args[0],
                         [str(self.room / ".venv/bin/python"), "-I", "-c", preflight.PREFLIGHT_CODE])

    def test_DEP_01_R2_timeout_kills_controller_process_group(self):
        with patch.object(os, "killpg", wraps=os.killpg) as kill, self.assertRaises(subprocess.TimeoutExpired):
            self.run_fake_controller("import time;time.sleep(60)", timeout=0.1)
        self.assertTrue(kill.called)
        self.assertEqual(kill.call_args.args[1], signal.SIGKILL)

    def test_DEP_01_R2_stdout_overflow_is_failure_and_kills_group(self):
        with patch.object(os, "killpg", wraps=os.killpg) as kill, self.assertRaisesRegex(RuntimeError, "4 MiB"):
            self.run_fake_controller("import sys;sys.stdout.write('x' * (5 * 1024 * 1024))")
        self.assertTrue(kill.called)

    def test_DEP_01_R2_stderr_overflow_is_failure_and_kills_group(self):
        with patch.object(os, "killpg", wraps=os.killpg) as kill, self.assertRaisesRegex(RuntimeError, "4 MiB"):
            self.run_fake_controller("import sys;sys.stderr.write('x' * (5 * 1024 * 1024))")
        self.assertTrue(kill.called)

    def assert_detached_pipe_is_bounded(self, parent_waits):
        code = ("import subprocess,sys,time\nfrom pathlib import Path\n"
                "p=subprocess.Popen([sys.executable,'-I','-c','import time;time.sleep(60)'],"
                "start_new_session=True)\nPath('detached.pid').write_text(str(p.pid))\n"
                + ("time.sleep(60)\n" if parent_waits else ""))
        started = time.monotonic()
        try:
            with self.assertRaises(subprocess.TimeoutExpired if parent_waits else RuntimeError):
                self.run_fake_controller(code, timeout=0.3 if parent_waits else 5)
            self.assertLess(time.monotonic() - started, 3)
        finally:
            # This deliberately escaped stdlib fake is not a native backend. Real native
            # descendants are confined in mandatory bwrap PID namespaces with parent death.
            pidfile = self.room / "detached.pid"
            if pidfile.exists():
                with contextlib.suppress(ProcessLookupError):
                    os.kill(int(pidfile.read_text()), signal.SIGKILL)

    def test_DEP_01_R2_detached_pipe_cannot_hang_normal_controller_exit(self):
        self.assert_detached_pipe_is_bounded(False)

    def test_DEP_01_R2_detached_pipe_cannot_hang_controller_timeout(self):
        self.assert_detached_pipe_is_bounded(True)

    def valid_report(self):
        return {"isolation": "mandatory_bwrap", "sandbox_host_capability": {"available": True},
                "required_backend_errors": [], "backends": {
                    "pyslang": {"source_manifest_sha256": {"before": "hash", "after": "hash"}},
                    "slang_cli": {"source_manifest_sha256": {"before": "hash", "after": "hash"}},
                    "cbm": {"snapshot_unchanged_by_probe": True},
                    "serena": {"candidate_unchanged_by_probe": True}}}

    def test_DEP_01_R2_valid_probe_requires_success_and_all_immutable_backends(self):
        controller.validate_probe_evidence(self.valid_report(), 0)

    def test_DEP_01_R2_current_ci_identity_must_match_contract(self):
        identity = {"source_identity": {"git_head": "a" * 40, "src_tree_sha256": "b" * 64},
                    "lock_sha256": "c" * 64, "workflow_sha256": "d" * 64,
                    "revision": controller.CONTRACT_REVISION,
                    "dep01_contract_revision": controller.CONTRACT_REVISION}
        controller.validate_contract_identity(identity, dict(identity))

    def test_DEP_01_R2_stale_ci_identity_is_rejected(self):
        contract = {"source_identity": {"git_head": "a" * 40},
                    "lock_sha256": "c" * 64, "workflow_sha256": "d" * 64,
                    "revision": controller.CONTRACT_REVISION,
                    "dep01_contract_revision": controller.CONTRACT_REVISION}
        with self.assertRaises(ValueError):
            controller.validate_contract_identity(contract, {**contract, "workflow_sha256": "e" * 64})

    def test_DEP_01_R2_incomplete_ci_identity_is_rejected(self):
        with self.assertRaises(ValueError):
            controller.validate_contract_identity({}, {})

    def test_DEP_01_R2_wrong_revision_identity_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "revision"):
            controller.validate_contract_identity({"revision": controller.CONTRACT_REVISION},
                                                  {"dep01_contract_revision": "DEP-01-original"})

    def test_DEP_01_R2_nonzero_probe_cannot_pass_with_matching_summary(self):
        with self.assertRaises(ValueError):
            controller.validate_probe_evidence(self.valid_report(), 1)

    def test_DEP_01_R2_degraded_isolation_is_rejected(self):
        report = self.valid_report()
        report["isolation"] = "degraded_no_isolation"
        with self.assertRaises(ValueError):
            controller.validate_probe_evidence(report, 0)

    def test_DEP_01_R2_unavailable_host_is_rejected(self):
        report = self.valid_report()
        report["sandbox_host_capability"]["available"] = False
        with self.assertRaises(ValueError):
            controller.validate_probe_evidence(report, 0)

    def test_DEP_01_R2_required_backend_error_is_rejected(self):
        report = self.valid_report()
        report["required_backend_errors"] = ["unknown degraded reason"]
        with self.assertRaises(ValueError):
            controller.validate_probe_evidence(report, 0)

    def test_DEP_01_R2_original_source_manifest_mismatch_is_rejected(self):
        report = self.valid_report()
        report["backends"]["pyslang"]["source_manifest_sha256"]["after"] = "changed"
        with self.assertRaises(ValueError):
            controller.validate_probe_evidence(report, 0)

    def test_DEP_01_R2_mcp_source_mutation_is_rejected(self):
        report = self.valid_report()
        report["backends"]["cbm"]["snapshot_unchanged_by_probe"] = False
        with self.assertRaises(ValueError):
            controller.validate_probe_evidence(report, 0)

    def test_DEP_01_R2_offline_install_and_metadata_verification_stay_netless(self):
        source = (ROOT / "tests/integration/test_dep01_cleanroom.py").read_text()
        tree = ast.parse(source)
        function = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "_cleanroom")
        calls = [n for n in ast.walk(function) if isinstance(n, ast.Call)]
        self.assertFalse(any(isinstance(n.func, ast.Attribute) and isinstance(n.func.value, ast.Name)
                             and n.func.value.id == "subprocess" for n in calls))
        self.assertEqual(sum(isinstance(n.func, ast.Name) and n.func.id == "run_netless" for n in calls), 4)
        self.assertEqual(sum(isinstance(n.func, ast.Name) and n.func.id == "run_controller" for n in calls), 1)
        self.assertIn('assert result.returncode == 0', source)
        self.assertIn('assert ver.returncode == 0', source)
        self.assertIn('assert boot.returncode == 0', source)
        self.assertIn('controller_network_isolated": False', source)
        self.assertIn('original_contract_reclassified": False', source)
        self.assertIn('"evidence" / "dep01-r2"', source)
        self.assertNotIn('old["probe"]', source)
        self.assertIn('elif os.environ.get("GITHUB_ACTIONS") == "true":', source)
        self.assertIn('raise ValueError("R2 CI validation requires current identity.json")', source)
