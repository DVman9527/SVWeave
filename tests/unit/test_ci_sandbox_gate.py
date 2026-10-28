"""SEC-05: diagnostic collection must never turn a failed mandatory probe into PASS."""

from __future__ import annotations

import json
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
        diagnostic = names.index("DEP-01 nested sandbox diagnostic (required; not qualification)")
        self.assertLess(boundary, diagnostic)
        self.assertEqual(steps[diagnostic]["if"], "${{ inputs.cleanroom_only }}")
        command = steps[diagnostic]["run"]
        self.assertIn("tests/integration/test_dep01_cleanroom.py", command)
        self.assertIn("--junitxml=.svweave/ci/junit_dep01_cleanroom.xml", command)
        self.assertIn('probe_status=$?', command)
        self.assertIn('exit "$probe_status"', command)
        self.assertIn('"$probe_started_at" "$probe_finished_at"', command)
        self.assertIn("nested-sandbox-kernel.log", command)
        self.assertNotIn("continue-on-error", steps[diagnostic])
        identity = steps[names.index("Record exact validation identity")]["run"]
        self.assertIn("cleanroom nested-isolation diagnostic only; not qualification", identity)

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
            expected = ("${{ !inputs.cleanroom_only && !inputs.remaining_regressions }}"
                        if step["name"].startswith("Integration,") else "${{ !inputs.cleanroom_only }}")
            self.assertEqual(step["if"], expected)
            self.assertNotIn("continue-on-error", step)
        self.assertNotIn("cleanroom_only", str(workflow["jobs"]["native-verilator"]))
        self.assertNotIn("remaining_regressions", str(workflow["jobs"]["native-verilator"]))

    def test_HDL_CI_01_remaining_regressions_is_explicit_and_defaults_off(self) -> None:
        workflow = yaml.safe_load((ROOT / ".github/workflows/ci.yml").read_text())
        events = workflow.get("on", workflow.get(True))
        option = events["workflow_dispatch"]["inputs"]["remaining_regressions"]
        self.assertEqual(option["type"], "boolean")
        self.assertIs(option["default"], False)
        job = workflow["jobs"]["real-backends"]
        self.assertIn("DEP-01 BLOCKED; not qualification", job["name"])
        self.assertIn("inputs.real_backends || inputs.remaining_regressions", job["if"])
        self.assertEqual(job["env"]["SVWEAVE_REQUIRE_BACKENDS"], "1")
        names = [step.get("name") for step in job["steps"]]
        self.assertEqual(names[0], "Validate bounded regression mode")
        ledger = names.index("Record required DEP-01 BLOCKED ledger (not a qualification report)")
        setup = names.index("Install package and the pinned toolchain (network only here, hashes verified)")
        boundary = names.index("Required native probe isolation regressions (fail closed)")
        remaining = names.index(
            "Remaining integration, fault injection and acceptance (DEP-01 BLOCKED; skips are failures)")
        self.assertLess(ledger, setup)
        self.assertLess(boundary, remaining)
        for step in job["steps"][:remaining + 1]:
            self.assertNotIn("continue-on-error", step)

    def test_HDL_CI_01_remaining_regressions_excludes_only_exact_blocked_node(self) -> None:
        workflow = yaml.safe_load((ROOT / ".github/workflows/ci.yml").read_text())
        steps = workflow["jobs"]["real-backends"]["steps"]
        full = next(step for step in steps if step.get("name", "").startswith("Integration,"))
        remaining = next(step for step in steps if step.get("name", "").startswith("Remaining integration,"))
        self.assertEqual(remaining["if"], "${{ inputs.remaining_regressions }}")
        node = ("tests/integration/test_dep01_cleanroom.py::"
                "test_DEP_01_offline_bootstrap_into_empty_dir_then_probe_all_three_backends")
        argv = shlex.split(remaining["run"])
        exclusion = f"--deselect={node}"
        self.assertEqual(argv.count(exclusion), 1)
        argv.remove(exclusion)
        plugin = argv.index("-p")
        self.assertEqual(argv[plugin:plugin + 2], ["-p", "tests.remaining_regressions"])
        del argv[plugin:plugin + 2]
        argv = [arg.replace("junit_remaining.xml", "junit_required.xml") for arg in argv]
        self.assertEqual(argv, shlex.split(full["run"]))
        self.assertNotIn("--deselect", full["run"])

    def _assert_remaining_mode_validation(self, real: bool, remaining: bool, diagnostic: bool, expected: int) -> None:
        workflow = yaml.safe_load((ROOT / ".github/workflows/ci.yml").read_text())
        command = workflow["jobs"]["real-backends"]["steps"][0]["run"]
        for key, value in (("real_backends", real), ("remaining_regressions", remaining),
                           ("cleanroom_only", diagnostic)):
            command = command.replace("${{ inputs." + key + " }}", str(value).lower())
        result = subprocess.run(["bash", "--noprofile", "--norc", "-eo", "pipefail", "-c", command],
                                capture_output=True, text=True, check=False, timeout=10)
        self.assertEqual(result.returncode, expected, result.stderr)

    def test_HDL_CI_01_remaining_mode_accepts_explicit_selection(self) -> None:
        self._assert_remaining_mode_validation(True, True, False, 0)

    def test_HDL_CI_01_remaining_mode_rejects_missing_real_backend_authorization(self) -> None:
        self._assert_remaining_mode_validation(False, True, False, 2)

    def test_HDL_CI_01_remaining_mode_rejects_cleanroom_conflict(self) -> None:
        self._assert_remaining_mode_validation(True, True, True, 2)

    def test_HDL_CI_01_remaining_mode_preserves_default_full_selection(self) -> None:
        self._assert_remaining_mode_validation(True, False, False, 0)

    def test_HDL_CI_01_remaining_mode_preserves_explicit_cleanroom_selection(self) -> None:
        self._assert_remaining_mode_validation(True, False, True, 0)

    def _run_remaining_ledger(self, identity: dict | None) -> tuple[subprocess.CompletedProcess[str], dict | None]:
        workflow = yaml.safe_load((ROOT / ".github/workflows/ci.yml").read_text())
        step = next(step for step in workflow["jobs"]["real-backends"]["steps"]
                    if step.get("name") == "Record required DEP-01 BLOCKED ledger (not a qualification report)")
        self.assertEqual(step["if"], "${{ inputs.remaining_regressions }}")
        code = step["run"].split("<<'PY_SCOPE'\n", 1)[1].rsplit("PY_SCOPE", 1)[0]
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            out = root / ".svweave/ci"
            out.mkdir(parents=True)
            if identity is not None:
                (out / "identity.json").write_text(json.dumps(identity))
            result = subprocess.run([sys.executable, "-I", "-c", code], cwd=root,
                                    capture_output=True, text=True, check=False, timeout=10)
            ledger = out / "remaining-regressions-scope.json"
            return result, json.loads(ledger.read_text()) if ledger.exists() else None

    def test_HDL_CI_01_remaining_ledger_retains_blocked_identity_without_qualification(self) -> None:
        identity = {"source_identity": {"git_head": "f" * 40, "src_tree_sha256": "e" * 64},
                    "lock_sha256": "d" * 64, "remaining_regressions_only": "true"}
        result, ledger = self._run_remaining_ledger(identity)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIsNotNone(ledger)
        self.assertEqual(ledger["identity"], identity)
        self.assertEqual(ledger["full_qualification_status"], "BLOCKED")
        self.assertIs(ledger["qualify_py_executed"], False)
        self.assertIs(ledger["release_qualified"], False)
        self.assertEqual(ledger["collection"], {"status": "NOT_RUN"})
        self.assertEqual(len(ledger["excluded_tests"]), 1)
        blocked = ledger["excluded_tests"][0]
        self.assertEqual(blocked["requirement"], "DEP-01")
        self.assertEqual(blocked["status"], "BLOCKED")
        self.assertIs(blocked["acceptance_unchanged"], True)
        self.assertIs(blocked["waived"], False)
        self.assertEqual(blocked["observed_run_id"], 40490935466)
        self.assertEqual(blocked["observed_source_sha"], "4a7a360b455a4dc4db5cffa2b711beb1baf5e266")
        workflow = yaml.safe_load((ROOT / ".github/workflows/ci.yml").read_text())
        remaining = next(step for step in workflow["jobs"]["real-backends"]["steps"]
                         if step.get("name", "").startswith("Remaining integration,"))
        self.assertIn("--deselect=" + blocked["nodeid"], shlex.split(remaining["run"]))

    def test_HDL_CI_01_remaining_ledger_missing_identity_fails_before_regressions(self) -> None:
        result, ledger = self._run_remaining_ledger(None)
        self.assertNotEqual(result.returncode, 0)
        self.assertIsNone(ledger)

    def _assert_remaining_collection(self, mode: str) -> None:
        from tests.remaining_regressions import BLOCKED_NODE, SUITES
        identity = {"source_identity": {"git_head": "f" * 40, "src_tree_sha256": "e" * 64},
                    "lock_sha256": "d" * 64, "workflow_sha256": "c" * 64,
                    "remaining_regressions_only": "true"}
        if mode == "empty_identity":
            identity["source_identity"]["git_head"] = ""
        _, ledger = self._run_remaining_ledger(identity)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "pytest.ini").write_text("[pytest]\n")
            for suite in SUITES:
                (root / suite).mkdir(parents=True)
            blocked_name = BLOCKED_NODE.split("::")[1]
            if mode == "missing":
                blocked_name = "test_renamed_dep01"
            (root / "tests/integration/test_dep01_cleanroom.py").write_text(
                f"def {blocked_name}():\n    raise AssertionError('blocked test must never run')\n"
                + (f"def {blocked_name}_extra():\n    assert True\n" if mode == "prefix" else ""))
            body = ("import pytest\nfrom pathlib import Path\n"
                    "def test_remaining():\n    Path('executed').touch()\n")
            body += "    pytest.skip('required case missing')\n" if mode == "skip" else "    assert True\n"
            (root / "tests/integration/test_remaining.py").write_text(body)
            out = root / ".svweave/ci"
            out.mkdir(parents=True)
            if mode == "waived":
                ledger["excluded_tests"][0]["waived"] = True
            (out / "remaining-regressions-scope.json").write_text(json.dumps(ledger))
            if mode == "stale_identity":
                identity["lock_sha256"] = "b" * 64
            if mode != "missing_identity":
                (out / "identity.json").write_text(json.dumps(identity))
            args = [sys.executable, "-m", "pytest", *SUITES, "-q", "-c", "pytest.ini",
                    "-p", "tests.remaining_regressions", "-p", "tests.required_backends",
                    "--deselect=" + BLOCKED_NODE]
            if mode == "extra_exclusion":
                args.append("--deselect=tests/integration/test_remaining.py::test_remaining")
            env = {**os.environ, "PYTHONPATH": str(ROOT), "PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1",
                   "SVWEAVE_REQUIRE_BACKENDS": "0" if mode == "nonrequired" else "1"}
            env.pop("PYTEST_ADDOPTS", None)
            result = subprocess.run(args, cwd=root, env=env, capture_output=True, text=True,
                                    check=False, timeout=30)
            self.assertEqual(result.returncode, 0 if mode == "valid" else 1 if mode == "skip" else 4,
                             result.stdout + result.stderr)
            updated = json.loads((out / "remaining-regressions-scope.json").read_text())
            self.assertEqual(updated["full_qualification_status"], "BLOCKED")
            self.assertIs(updated["release_qualified"], False)
            self.assertEqual((root / "executed").exists(), mode in ("valid", "skip"))
            if mode in ("valid", "skip"):
                self.assertEqual(updated["collection"], {
                    "status": "VALIDATED", "collected_count": 2, "selected_count": 1,
                    "blocked_count": 1, "blocked_nodeids": [BLOCKED_NODE],
                })
            if mode in ("missing", "prefix"):
                self.assertEqual(updated["collection"]["status"], "FAIL")

    def test_HDL_CI_01_remaining_collection_records_exactly_one_blocked_node(self) -> None:
        self._assert_remaining_collection("valid")

    def test_HDL_CI_01_remaining_collection_missing_blocked_node_fails_before_execution(self) -> None:
        self._assert_remaining_collection("missing")

    def test_HDL_CI_01_remaining_collection_extra_exclusion_fails_before_execution(self) -> None:
        self._assert_remaining_collection("extra_exclusion")

    def test_HDL_CI_01_remaining_collection_prefix_collision_fails_before_execution(self) -> None:
        self._assert_remaining_collection("prefix")

    def test_HDL_CI_01_remaining_collection_cannot_disable_required_backend_mode(self) -> None:
        self._assert_remaining_collection("nonrequired")

    def test_HDL_CI_01_remaining_collection_cannot_waive_blocked_requirement(self) -> None:
        self._assert_remaining_collection("waived")

    def test_HDL_CI_01_remaining_collection_rejects_stale_ledger_identity(self) -> None:
        self._assert_remaining_collection("stale_identity")

    def test_HDL_CI_01_remaining_collection_requires_current_identity_file(self) -> None:
        self._assert_remaining_collection("missing_identity")

    def test_HDL_CI_01_remaining_collection_requires_nonempty_identity_fields(self) -> None:
        self._assert_remaining_collection("empty_identity")

    def test_HDL_CI_01_remaining_collection_selected_skip_still_fails(self) -> None:
        self._assert_remaining_collection("skip")

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
                       if step.get("name") == "DEP-01 nested sandbox diagnostic (required; not qualification)")
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
        path = Path('.svweave/ci/dep01/nested-sandbox-preflight.json')
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
                log = root / ".svweave/ci/dep01/nested-sandbox-kernel.log"
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
