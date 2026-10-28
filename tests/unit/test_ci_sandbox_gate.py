"""SEC-05: diagnostic collection must never turn a failed mandatory probe into PASS."""

from __future__ import annotations

import os
import shlex
import subprocess
import sys
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
        self.assertNotIn("if", steps[host])
        self.assertNotIn("if", steps[boundary])
        self.assertEqual(steps[probe]["if"], "${{ !inputs.cleanroom_only }}")
        self.assertEqual(job["env"]["SVWEAVE_REQUIRE_BACKENDS"], "1")
        self.assertIn("not full qualification", job["name"])
        self.assertIn("Record exact validation identity", names)
        native = str(workflow["jobs"]["native-verilator"])
        self.assertNotIn("scripts/probe_backends.py", native)
        self.assertIn("SVWEAVE_REQUIRE_BACKENDS", native)
        self.assertIn("--junitxml=", native)

    def test_HDL_CI_01_cleanroom_diagnostic_is_opt_in_and_fail_closed(self) -> None:
        workflow = yaml.safe_load((ROOT / ".github/workflows/ci.yml").read_text())
        # PyYAML's YAML 1.1 loader interprets the unquoted workflow key `on` as True.
        events = workflow.get("on", workflow.get(True))
        option = events["workflow_dispatch"]["inputs"]["cleanroom_only"]
        self.assertEqual(option["type"], "boolean")
        self.assertIs(option["default"], False)
        steps = workflow["jobs"]["real-backends"]["steps"]
        names = [step.get("name") for step in steps]
        boundary = names.index("Required native probe isolation regressions (fail closed)")
        diagnostic = names.index("DEP-01 R2 offline-install and isolated-backend validation (not qualification)")
        self.assertLess(boundary, diagnostic)
        self.assertEqual(steps[diagnostic]["if"], "${{ inputs.cleanroom_only }}")
        command = steps[diagnostic]["run"]
        self.assertIn("tests/integration/test_dep01_cleanroom.py", command)
        self.assertIn("--junitxml=.svweave/ci/junit_dep01_r2.xml", command)
        self.assertIn('probe_status=$?', command)
        self.assertIn('exit "$probe_status"', command)
        self.assertIn('"$probe_started_at" "$probe_finished_at"', command)
        self.assertIn("controller-sandbox-kernel.log", command)
        self.assertNotIn("continue-on-error", steps[diagnostic])
        identity = steps[names.index("Record exact validation identity")]["run"]
        self.assertIn("DEP-01-R2 host-controller validation only; not qualification", identity)

    def test_HDL_CI_01_full_backend_mode_is_unchanged_unless_partial_mode_selected(self) -> None:
        workflow = yaml.safe_load((ROOT / ".github/workflows/ci.yml").read_text())
        steps = workflow["jobs"]["real-backends"]["steps"]
        full_only = {
            "Backend contract probe", "Integration, fault injection and acceptance (skips are failures)",
            "Critical mutation suite (plan 20.5)",
            "Small-scale benchmark (smoke only; not the plan 22.3 reference run)",
        }
        found = [step for step in steps if step.get("name") in full_only]
        self.assertEqual(len(found), len(full_only))
        for step in found:
            self.assertEqual(step["if"], "${{ !inputs.cleanroom_only }}")
            self.assertNotIn("continue-on-error", step)
        self.assertNotIn("cleanroom_only", str(workflow["jobs"]["native-verilator"]))
        self.assertNotIn("remaining_regressions", str(workflow["jobs"]["native-verilator"]))

    def _assert_remaining_mode_validation(self, real: bool, remaining: bool, diagnostic: bool, expected: int) -> None:
        workflow = yaml.safe_load((ROOT / ".github/workflows/ci.yml").read_text())
        command = workflow["jobs"]["real-backends"]["steps"][0]["run"]
        for key, value in (("real_backends", real), ("remaining_regressions", remaining),
                           ("cleanroom_only", diagnostic)):
            command = command.replace("${{ inputs." + key + " }}", str(value).lower())
        result = subprocess.run(["bash", "--noprofile", "--norc", "-eo", "pipefail", "-c", command],
                                capture_output=True, text=True, check=False, timeout=10)
        self.assertEqual(result.returncode, expected, result.stderr)

    def test_HDL_CI_01_retired_remaining_mode_rejects_even_explicit_selection(self) -> None:
        self._assert_remaining_mode_validation(True, True, False, 2)

    def test_HDL_CI_01_retired_R1_mode_cannot_exclude_revised_DEP_01(self) -> None:
        workflow = yaml.safe_load((ROOT / ".github/workflows/ci.yml").read_text())
        job = workflow["jobs"]["real-backends"]
        self.assertEqual(job["steps"][0]["name"], "Validate bounded regression mode")
        self.assertIn("retired: R1-only exclusion", job["steps"][0]["run"])
        self.assertNotIn("--deselect", str(job))
        self.assertNotIn("tests.remaining_regressions", str(job))
        self.assertNotIn("remaining-regressions-scope.json", str(job))
        self.assertIn('"dep01_contract_revision": "DEP-01-R2-host-controller"', str(job))

    def test_HDL_CI_01_remaining_mode_rejects_missing_real_backend_authorization(self) -> None:
        self._assert_remaining_mode_validation(False, True, False, 2)

    def test_HDL_CI_01_remaining_mode_rejects_cleanroom_conflict(self) -> None:
        self._assert_remaining_mode_validation(True, True, True, 2)

    def test_HDL_CI_01_remaining_mode_preserves_default_full_selection(self) -> None:
        self._assert_remaining_mode_validation(True, False, False, 0)

    def test_HDL_CI_01_remaining_mode_preserves_explicit_cleanroom_selection(self) -> None:
        self._assert_remaining_mode_validation(True, False, True, 0)

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

    def _assert_cleanroom_status_survives_diagnostics(
        self, probe_status: int, diagnostic_status: int, report_mode: str = "valid",
    ) -> None:
        workflow = yaml.safe_load((ROOT / ".github/workflows/ci.yml").read_text())
        command = next(step["run"] for step in workflow["jobs"]["real-backends"]["steps"]
                       if step.get("name") == "DEP-01 R2 offline-install and isolated-backend validation (not qualification)")
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / ".venv/bin").mkdir(parents=True)
            (root / "scripts").mkdir()
            # Execute the real workflow shell/interval parser; replace pytest and diagnostics only.
            # No backend, bwrap, host audit log or privileged command is executed.
            python = root / ".venv/bin/python"
            python.write_text(f"""#!{sys.executable}
import json, os, sys
from datetime import UTC, datetime
from pathlib import Path
if sys.argv[1:3] == ['-m', 'pytest']:
    stamp = datetime.now(UTC).strftime('%Y-%m-%d %H:%M:%S.%f UTC')
    start = '2000-01-01 00:00:00.000000 UTC' if {report_mode!r} == 'stale' else stamp
    report = {{'started_at_utc': start, 'finished_at_utc': start}}
    if {report_mode!r} == 'delimiters':
        report = {{key: value.replace(' ', '\\t\\n', 1) for key, value in report.items()}}
    if {report_mode!r} != 'missing':
        path = Path('.svweave/ci/dep01-r2/controller-sandbox-preflight.json')
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text('not-json' if {report_mode!r} == 'malformed' else json.dumps(report))
        Path('expected-bounds').write_text(start + '\\n' + start + '\\n')
    sys.exit({probe_status})
os.execv(sys.executable, [sys.executable, *sys.argv[1:]])
""")
            python.chmod(0o700)
            (root / "scripts/ci_sandbox_diagnostics.sh").write_text(
                '#!/bin/bash\nprintf "%s\\n%s\\n" "$1" "$2" > diagnostics-called\n'
                f'echo "bounded audit evidence"\nexit {diagnostic_status}\n'
            )
            result = subprocess.run(
                ["bash", "--noprofile", "--norc", "-eo", "pipefail", "-c", command],
                cwd=root, capture_output=True, text=True, check=False, timeout=10,
            )
            self.assertEqual(result.returncode, probe_status, result.stderr)
            called = root / "diagnostics-called"
            self.assertEqual(called.exists(), probe_status != 0)
            if called.exists():
                bounds = called.read_text()
                self.assertEqual(len(bounds.splitlines()), 2)
                if report_mode in ("valid", "delimiters"):
                    self.assertEqual(bounds, (root / "expected-bounds").read_text())
                else:
                    self.assertNotIn("2000-01-01", bounds)
                    for value in bounds.splitlines():
                        self.assertRegex(value, r"\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}\.\d{6} UTC")
                log = root / ".svweave/ci/dep01-r2/controller-sandbox-kernel.log"
                self.assertIn("bounded audit evidence", log.read_text())

    def test_SEC_05_cleanroom_success_does_not_collect_failure_diagnostics(self) -> None:
        self._assert_cleanroom_status_survives_diagnostics(0, 1)

    def test_SEC_05_cleanroom_failure_with_successful_diagnostics_stays_failed(self) -> None:
        self._assert_cleanroom_status_survives_diagnostics(7, 0)

    def test_SEC_05_cleanroom_failure_with_failed_diagnostics_stays_failed(self) -> None:
        self._assert_cleanroom_status_survives_diagnostics(7, 1)

    def test_SEC_05_cleanroom_failure_with_timed_out_diagnostics_stays_failed(self) -> None:
        self._assert_cleanroom_status_survives_diagnostics(7, 124)

    def test_SEC_05_cleanroom_missing_interval_uses_failed_test_window(self) -> None:
        self._assert_cleanroom_status_survives_diagnostics(7, 0, "missing")

    def test_SEC_05_cleanroom_malformed_interval_uses_failed_test_window(self) -> None:
        self._assert_cleanroom_status_survives_diagnostics(7, 0, "malformed")

    def test_SEC_05_cleanroom_stale_interval_uses_failed_test_window(self) -> None:
        self._assert_cleanroom_status_survives_diagnostics(7, 0, "stale")

    def test_SEC_05_cleanroom_interval_delimiters_cannot_change_shell_arguments(self) -> None:
        self._assert_cleanroom_status_survives_diagnostics(7, 0, "delimiters")

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
        self.assertIn("probe_finished_at=${2:-", script)
        argv = shlex.split(script[script.index("sudo -n journalctl"):].replace("\\\n", " "))
        self.assertEqual(argv[:4], ["sudo", "-n", "journalctl", "-k"])
        self.assertNotIn("--kernel", argv)
        self.assertEqual(argv[argv.index("--since") + 1], "$probe_started_at")
        self.assertEqual(argv[argv.index("--until") + 1], "$probe_finished_at")
        self.assertIn("--lines=100", argv)
        self.assertIn("--grep=apparmor|audit|DENIED|bwrap|bubblewrap|seccomp", argv)
