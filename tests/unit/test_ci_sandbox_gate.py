"""SEC-05: diagnostic collection must never turn a failed mandatory probe into PASS."""

from __future__ import annotations

import os
import shlex
import subprocess
import tempfile
import unittest
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]


class SandboxGateTests(unittest.TestCase):
    def test_HDL_CI_01_all_jobs_use_identical_mandatory_sandbox_gate(self) -> None:
        workflow = yaml.safe_load((ROOT / ".github/workflows/ci.yml").read_text())
        commands = []
        for name in ("fast", "real-backends", "native-verilator"):
            steps = workflow["jobs"][name]["steps"]
            command = next(step["run"] for step in steps
                           if step.get("name") == "Install and verify the mandatory worker sandbox")
            commands.append(command)
            self.assertIn("scripts/ci_prepare_bwrap_apparmor.py", command)
            self.assertNotIn("sysctl", command)
            self.assertNotIn("allow_degraded", command)
        self.assertEqual(len(set(commands)), 1)
        for job in workflow["jobs"].values():
            self.assertNotIn("apparmor_restrict_unprivileged_userns=0", str(job))

    def test_HDL_CI_01_native_isolation_gate_precedes_all_backend_probes(self) -> None:
        workflow = yaml.safe_load((ROOT / ".github/workflows/ci.yml").read_text())
        job = workflow["jobs"]["real-backends"]
        steps = job["steps"]
        names = [step.get("name") for step in steps]
        host = names.index("Install and verify the mandatory worker sandbox")
        boundary = names.index("Required native probe isolation regressions (fail closed)")
        probe = names.index("Backend contract probe")
        self.assertLess(host, boundary)
        self.assertLess(boundary, probe)
        self.assertIn("test_ci_probe_isolation.py", steps[boundary]["run"])
        self.assertIn("test_int_probe_isolation.py", steps[boundary]["run"])
        self.assertIn("--junitxml=.svweave/ci/junit_probe_isolation.xml", steps[boundary]["run"])
        self.assertNotIn("|| true", steps[boundary]["run"])
        self.assertNotIn("continue-on-error", job)
        for step in steps[host:probe + 1]:
            self.assertNotIn("continue-on-error", step)
            self.assertNotIn("if", step)  # default success-only chaining, never always() on probes
        self.assertEqual(job["env"]["SVWEAVE_REQUIRE_BACKENDS"], "1")
        self.assertIn("not full qualification", job["name"])
        self.assertIn("Record exact validation identity", names)
        native = str(workflow["jobs"]["native-verilator"])
        self.assertNotIn("scripts/probe_backends.py", native)
        self.assertIn("SVWEAVE_REQUIRE_BACKENDS", native)
        self.assertIn("--junitxml=", native)

    def test_HDL_CI_01_hidden_junit_and_identity_artifacts_are_explicitly_retained(self) -> None:
        workflow = yaml.safe_load((ROOT / ".github/workflows/ci.yml").read_text())
        for name in ("real-backends", "native-verilator"):
            upload = next(step for step in workflow["jobs"][name]["steps"]
                          if step.get("uses", "").startswith("actions/upload-artifact@"))
            self.assertEqual(upload["if"], "always()")
            self.assertIs(upload["with"]["include-hidden-files"], True)
            paths = upload["with"]["path"].splitlines()
            self.assertIn(".svweave/ci/", paths)
            self.assertNotIn(".svweave/", paths)
            self.assertEqual(upload["with"]["if-no-files-found"], "error")

    def _assert_probe_status_survives_diagnostics(
        self, probe_status: int, diagnostic_status: int, setup_status: int,
    ) -> None:
        workflow = yaml.safe_load((ROOT / ".github/workflows/ci.yml").read_text())
        step = next(
            step
            for step in workflow["jobs"]["fast"]["steps"]
            if step.get("name") == "Install and verify the mandatory worker sandbox"
        )
        command = step["run"]
        self.assertIn("capability = probe_host(refresh=True)", command)
        self.assertEqual(command.count("probe_host(refresh=True)"), 1)
        self.assertIn("date -u '+%Y-%m-%d %H:%M:%S.%6N UTC'", command)
        self.assertIn('timeout 60s bash scripts/ci_sandbox_diagnostics.sh "$probe_started_at"', command)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / ".venv/bin").mkdir(parents=True)
            (root / "scripts").mkdir()
            # Pure shell-control test: no apt, sandbox, sudo, or host diagnostics run.
            for name, body in {
                "sudo": "exit 0",
                ".venv/bin/python": (
                    'if [[ "$1" == scripts/ci_prepare_bwrap_apparmor.py ]]; then '
                    f'exit {setup_status}; fi\n'
                    f"cat >/dev/null\nexit {probe_status}"
                ),
                "scripts/ci_sandbox_diagnostics.sh": (
                    'printf "%s\\n" "$1" > diagnostics-called\n'
                    f"exit {diagnostic_status}"
                ),
            }.items():
                script = root / name
                script.write_text(f"#!/bin/bash\n{body}\n")
                script.chmod(0o700)
            result = subprocess.run(
                ["bash", "--noprofile", "--norc", "-eo", "pipefail", "-c", command],
                cwd=root,
                env={**os.environ, "PATH": f"{root}:{os.environ['PATH']}"},
                capture_output=True,
                text=True,
                check=False,
                timeout=10,
            )
            self.assertEqual(result.returncode, setup_status or probe_status, result.stderr)
            called = root / "diagnostics-called"
            self.assertEqual(called.exists(), not setup_status and probe_status != 0)
            if called.exists():
                self.assertRegex(called.read_text(), r"\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}\.\d{6} UTC")

    def test_SEC_05_passing_probe_preserves_status(self) -> None:
        self._assert_probe_status_survives_diagnostics(0, 0, 0)

    def test_SEC_05_failed_probe_successful_diagnostics_preserves_status(self) -> None:
        self._assert_probe_status_survives_diagnostics(7, 0, 0)

    def test_SEC_05_failed_probe_failed_diagnostics_preserves_status(self) -> None:
        self._assert_probe_status_survives_diagnostics(7, 1, 0)

    def test_SEC_05_failed_probe_timed_out_diagnostics_preserves_status(self) -> None:
        self._assert_probe_status_survives_diagnostics(7, 124, 0)

    def test_SEC_05_failed_setup_preserves_status(self) -> None:
        self._assert_probe_status_survives_diagnostics(0, 0, 9)

    def test_SEC_05_diagnostics_shell_syntax(self) -> None:
        result = subprocess.run(
            ["bash", "-n", str(ROOT / "scripts/ci_sandbox_diagnostics.sh")],
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_SEC_05_kernel_audit_read_is_supported_and_bounded(self) -> None:
        script = (ROOT / "scripts/ci_sandbox_diagnostics.sh").read_text()
        self.assertIn("date -u '+%Y-%m-%d %H:%M:%S.%6N UTC'", script)
        argv = shlex.split(script[script.index("sudo -n journalctl"):].replace("\\\n", " "))
        self.assertEqual(argv[:4], ["sudo", "-n", "journalctl", "-k"])
        self.assertNotIn("--kernel", argv)
        self.assertEqual(argv[argv.index("--since") + 1], "$probe_started_at")
        self.assertEqual(argv[argv.index("--until") + 1], "$probe_finished_at")
        self.assertIn("--lines=100", argv)
        self.assertIn("--grep=apparmor|audit|DENIED|bwrap|bubblewrap|seccomp", argv)
