"""HDL-CI-02: real pytest subprocesses prove required skips cannot exit green."""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import unittest
import xml.etree.ElementTree as ET
from pathlib import Path

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
            return result.returncode, result.stdout + result.stderr, ET.parse(root / "result.xml").getroot()

    def test_HDL_CI_02_missing_uvm_skipif_is_failure_only_when_required(self) -> None:
        body = ('from pathlib import Path\n'
                '@pytest.mark.skipif(not Path("absent_uvm/uvm_pkg.sv").is_file(), '
                'reason="pinned UVM library missing")\n'
                'def test_uvm():\n    raise AssertionError("must not execute without UVM")\n')
        for required in (False, True):
            with self.subTest(required=required):
                rc, output, report = self.run_case(body, required)
                self.assertEqual(rc, 1 if required else 0, output)
                self.assertEqual(len(list(report.iter("skipped"))), 0 if required else 1)
                if required:
                    self.assertIn("required qualification test did not execute", output)
                    self.assertTrue(list(report.iter("error")) or list(report.iter("failure")))

    def test_HDL_CI_02_dynamic_xfail_and_collection_skips_are_required_failures(self) -> None:
        for body in (
            'def test_missing_backend():\n    pytest.skip("backend missing")\n',
            '@pytest.mark.xfail(reason="not yet qualified")\ndef test_pending():\n    assert False\n',
            'pytest.skip("missing backend at collection", allow_module_level=True)\n',
        ):
            with self.subTest(body=body):
                rc, output, report = self.run_case(body, True)
                self.assertNotEqual(rc, 0, output)
                self.assertEqual(list(report.iter("skipped")), [])

    def test_HDL_CI_02_required_executed_pass_stays_pass(self) -> None:
        rc, output, report = self.run_case('def test_ready():\n    assert True\n', True)
        self.assertEqual(rc, 0, output)
        self.assertEqual(len(list(report.iter("testcase"))), 1)
        self.assertEqual(list(report.iter("skipped")), [])

    def test_HDL_CI_02_gate_is_loaded_by_the_repository_root_conftest(self) -> None:
        self.assertIn('pytest_plugins = ["tests.required_backends"]',
                      (ROOT / "tests/conftest.py").read_text())
