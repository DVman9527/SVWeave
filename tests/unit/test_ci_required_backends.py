"""HDL-CI-02: real pytest subprocesses prove required skips cannot exit green."""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import unittest
import xml.etree.ElementTree as ET
from pathlib import Path

from scripts.qualify import parse_junit, suite_result_errors

ROOT = Path(__file__).resolve().parents[2]


class RequiredBackendsTests(unittest.TestCase):
    def run_case(self, body: str, required: bool) -> tuple[int, str, ET.Element]:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "pytest.ini").write_text("[pytest]\n")
            (root / "test_required.py").write_text("import pytest\n" + body)
            env = {**os.environ, "PYTHONPATH": str(ROOT), "PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1"}
            env["SVWEAVE_REQUIRE_BACKENDS"] = "1" if required else "0"
            env.pop("PYTEST_ADDOPTS", None)
            result = subprocess.run(
                [sys.executable, "-m", "pytest", "-p", "tests.required_backends", "-q",
                 "-c", str(root / "pytest.ini"), "--junitxml=result.xml", "test_required.py"],
                cwd=root, env=env, capture_output=True, text=True, timeout=30, check=False,
            )
            self.assertTrue((root / "result.xml").is_file(), result.stdout + result.stderr)
            report = ET.parse(root / "result.xml").getroot()
            gate_errors = suite_result_errors({"rc": result.returncode,
                                              **parse_junit(root / "result.xml")})
            if result.returncode == 0 and not list(report.iter("skipped")):
                self.assertEqual(gate_errors, [], result.stdout + result.stderr)
            else:
                self.assertTrue(gate_errors, result.stdout + result.stderr)
            return result.returncode, result.stdout + result.stderr, report

    def _assert_missing_uvm_skipif(self, required: bool) -> None:
        body = ('from pathlib import Path\n'
                '@pytest.mark.skipif(not Path("absent_uvm/uvm_pkg.sv").is_file(), '
                'reason="pinned UVM library missing")\n'
                'def test_uvm():\n    raise AssertionError("must not execute without UVM")\n')
        rc, output, report = self.run_case(body, required)
        self.assertEqual(rc, 1 if required else 0, output)
        self.assertEqual(len(list(report.iter("skipped"))), 0 if required else 1)
        if required:
            self.assertIn("required qualification test did not execute", output)
            self.assertTrue(list(report.iter("error")) or list(report.iter("failure")))

    def test_HDL_CI_02_nonrequired_missing_uvm_is_skipped(self) -> None:
        self._assert_missing_uvm_skipif(False)

    def test_HDL_CI_02_required_missing_uvm_is_failure(self) -> None:
        self._assert_missing_uvm_skipif(True)

    def _assert_required_skip_fails(self, body: str) -> None:
        rc, output, report = self.run_case(body, True)
        self.assertNotEqual(rc, 0, output)
        self.assertEqual(list(report.iter("skipped")), [])

    def test_HDL_CI_02_dynamic_skip_is_required_failure(self) -> None:
        self._assert_required_skip_fails('def test_missing_backend():\n    pytest.skip("backend missing")\n')

    def test_HDL_CI_02_xfail_is_required_failure(self) -> None:
        self._assert_required_skip_fails('@pytest.mark.xfail(reason="not yet qualified")\n'
                                         'def test_pending():\n    assert False\n')

    def test_HDL_CI_02_collection_skip_is_required_failure(self) -> None:
        self._assert_required_skip_fails('pytest.skip("missing backend at collection", allow_module_level=True)\n')

    def test_HDL_CI_02_required_executed_pass_stays_pass(self) -> None:
        rc, output, report = self.run_case('def test_ready():\n    assert True\n', True)
        self.assertEqual(rc, 0, output)
        self.assertEqual(len(list(report.iter("testcase"))), 1)
        self.assertEqual(list(report.iter("skipped")), [])

    def _run_named_unittest_cases(self, outcome: str) -> tuple[int, str, ET.Element]:
        # Plain methods, not unittest.subTest: every matrix case has its own
        # collection identity and JUnit node, using unmodified pinned pytest.
        return self.run_case(
            'import unittest\n'
            'class TestMatrix(unittest.TestCase):\n'
            '    def test_parent(self):\n        self.assertTrue(True)\n'
            '    def test_case_a(self):\n        self.assertEqual(1, 1)\n'
            f'    def test_case_b(self):\n        {outcome}\n', True,
        )

    def test_HDL_CI_03_named_unittest_matrix_has_exact_junit_evidence(self) -> None:
        rc, output, report = self._run_named_unittest_cases("self.assertEqual(2, 2)")
        self.assertEqual(rc, 0, output)
        cases = list(report.iter("testcase"))
        self.assertEqual({case.get("name") for case in cases},
                         {"test_parent", "test_case_a", "test_case_b"})
        self.assertEqual(len(cases), 3)
        suite = report.find("testsuite")
        self.assertIsNotNone(suite)
        assert suite is not None
        self.assertEqual(suite.get("tests"), "3")
        self.assertEqual(suite.get("failures"), "0")
        self.assertEqual(suite.get("errors"), "0")
        self.assertEqual(suite.get("skipped"), "0")

    def _assert_named_case_blocks(self, outcome: str) -> None:
        rc, output, report = self._run_named_unittest_cases(outcome)
        self.assertNotEqual(rc, 0, output)
        failed_names = {case.get("name") for case in report.iter("testcase")
                        if case.find("failure") is not None or case.find("error") is not None}
        self.assertEqual(failed_names, {"test_case_b"})
        self.assertEqual(list(report.iter("skipped")), [])

    def test_HDL_CI_03_named_unittest_failed_case_blocks(self) -> None:
        self._assert_named_case_blocks("self.assertEqual(1, 2)")

    def test_HDL_CI_03_named_unittest_skipped_case_blocks(self) -> None:
        self._assert_named_case_blocks('self.skipTest("not executed")')

    def test_HDL_CI_03_named_unittest_xfailed_case_blocks(self) -> None:
        self._assert_named_case_blocks('pytest.xfail("not qualified")')

    def test_HDL_CI_02_gate_is_loaded_by_the_repository_root_conftest(self) -> None:
        self.assertIn('pytest_plugins = ["tests.required_backends"]',
                      (ROOT / "tests/conftest.py").read_text())
