"""HDL-CI-03: missing, skipped, failed and partial evidence never make a green gate."""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))
import qualify  # noqa: E402
from _required_backends import required_backend_errors  # noqa: E402


class QualificationGateTests(unittest.TestCase):
    def result(self, xml: str | None, rc: int = 0) -> dict:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "junit.xml"
            if xml is not None:
                path.write_text(xml)
            return {"rc": rc, **qualify.parse_junit(path)}

    def test_HDL_CI_03_zero_skip_valid_report_passes(self) -> None:
        result = self.result('<testsuites><testsuite tests="1" failures="0" errors="0" skipped="0">'
                             '<testcase classname="tests.integration.test_uvm" name="UVM_03"/>'
                             '</testsuite></testsuites>')
        self.assertEqual(qualify.suite_result_errors(result), [])
        self.assertEqual(result["modules"]["test_uvm"]["passed"], 1)

    def test_HDL_CI_03_skip_failure_and_error_each_block(self) -> None:
        for outcome, key in (("skipped", "skipped"), ("failure", "failed"), ("error", "errors")):
            with self.subTest(outcome=outcome):
                result = self.result('<testsuite><testcase classname="test_uvm" name="ok"/>'
                                     f'<testcase classname="test_uvm" name="missing"><{outcome}/>'
                                     '</testcase></testsuite>')
                self.assertEqual(result[key], 1)
                self.assertTrue(qualify.suite_result_errors(result))

    def test_HDL_CI_03_missing_empty_malformed_or_inconsistent_report_blocks(self) -> None:
        for xml in (None, "", "<testsuites/>", "<oops/>", "<testsuite>",
                    '<testsuite tests="1" skipped="1"><testcase name="ok"/></testsuite>',
                    '<testsuites skipped="1"><testsuite><testcase name="ok"/></testsuite></testsuites>',
                    '<testsuite tests="invalid"><testcase name="ok"/></testsuite>'):
            with self.subTest(xml=xml):
                self.assertTrue(qualify.suite_result_errors(self.result(xml)))

    def test_HDL_CI_03_nonzero_exit_cannot_be_hidden_by_passing_modules(self) -> None:
        cases = ''.join(f'<testcase classname="{name}" name="ok"/>'
                        for name in qualify.GATE_MODULES["G2"])
        for rc, expected in ((0, "PASS"), (2, "FAIL")):
            result = self.result(f"<testsuite>{cases}</testsuite>", rc)
            report = {"tests": {"integration": result}, "benchmark_ref": (None, None)}
            self.assertEqual(qualify.compute_gates(report)["G2"]["status"], expected)
            self.assertTrue(qualify._mods_ok(report["tests"], ["test_uvm"]).startswith(expected))

    def test_HDL_CI_03_probe_missing_unknown_or_failed_slang_uvm_blocks(self) -> None:
        ready = {name: "READY" for name in ("pyslang", "slang_cli", "cbm", "serena")}
        self.assertEqual(required_backend_errors(ready), [])
        for name in ready:
            for value in (None, "UNAVAILABLE", "SKIPPED", "NOT_RUN", "unknown"):
                summary = {**ready, name: value}
                with self.subTest(name=name, value=value):
                    self.assertTrue(required_backend_errors(summary))
        self.assertTrue(required_backend_errors({**ready, "pyslang": "DEGRADED"}))
        self.assertEqual(required_backend_errors({**ready, "serena": "DEGRADED"}), [])

    def test_HDL_CI_03_g0_checks_probe_exit_even_with_ready_status(self) -> None:
        cases = ''.join(f'<testcase classname="{name}" name="ok"/>'
                        for name in qualify.GATE_MODULES["G0"])
        report = {
            "tests": {"integration": self.result(f"<testsuite>{cases}</testsuite>")},
            "lock": {"rc": 0, "null_or_empty_fields": []},
            "probe": {"rc": 1, "summary": {name: "READY" for name in
                                          ("pyslang", "slang_cli", "cbm", "serena")}},
            "benchmark_ref": (None, None),
        }
        gate = qualify.compute_gates(report)["G0"]
        self.assertEqual(gate["status"], "FAIL")
        self.assertIn("backend probe exit 1", gate["reasons"])
