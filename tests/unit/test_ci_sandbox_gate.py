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
    def test_SEC_05_probe_status_survives_best_effort_diagnostics(self) -> None:
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
        for probe_status, diagnostic_status in [(0, 0), (7, 0), (7, 1), (7, 124)]:
            with (
                self.subTest(probe_status=probe_status, diagnostic_status=diagnostic_status),
                tempfile.TemporaryDirectory() as directory,
            ):
                root = Path(directory)
                (root / ".venv/bin").mkdir(parents=True)
                (root / "scripts").mkdir()
                # Pure shell-control test: no apt, sandbox, sudo, or host diagnostics run.
                for name, body in {
                    "sudo": "exit 0",
                    ".venv/bin/python": f"cat >/dev/null\nexit {probe_status}",
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
                self.assertEqual(result.returncode, probe_status, result.stderr)
                called = root / "diagnostics-called"
                self.assertEqual(called.exists(), probe_status != 0)
                if called.exists():
                    self.assertRegex(called.read_text(), r"\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}\.\d{6} UTC")

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
