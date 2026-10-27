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

    def _assert_outcome_blocks(self, outcome: str, key: str) -> None:
        result = self.result('<testsuite><testcase classname="test_uvm" name="ok"/>'
                             f'<testcase classname="test_uvm" name="missing"><{outcome}/>'
                             '</testcase></testsuite>')
        self.assertEqual(result[key], 1)
        self.assertTrue(qualify.suite_result_errors(result))

    def test_HDL_CI_03_skipped_case_blocks(self) -> None:
        self._assert_outcome_blocks("skipped", "skipped")

    def test_HDL_CI_03_failed_case_blocks(self) -> None:
        self._assert_outcome_blocks("failure", "failed")

    def test_HDL_CI_03_error_case_blocks(self) -> None:
        self._assert_outcome_blocks("error", "errors")

    def test_HDL_CI_03_xfailed_case_blocks(self) -> None:
        self._assert_outcome_blocks('skipped type="pytest.xfail"', "skipped")

    def _assert_report_blocks(self, xml: str | None) -> None:
        self.assertTrue(qualify.suite_result_errors(self.result(xml)))

    def test_HDL_CI_03_missing_report_blocks(self) -> None:
        self._assert_report_blocks(None)

    def test_HDL_CI_03_empty_report_blocks(self) -> None:
        self._assert_report_blocks("")

    def test_HDL_CI_03_zero_case_report_blocks(self) -> None:
        self._assert_report_blocks("<testsuites/>")

    def test_HDL_CI_03_unknown_root_blocks(self) -> None:
        self._assert_report_blocks("<oops/>")

    def test_HDL_CI_03_malformed_report_blocks(self) -> None:
        self._assert_report_blocks("<testsuite>")

    def test_HDL_CI_03_inconsistent_suite_skip_count_blocks(self) -> None:
        self._assert_report_blocks('<testsuite tests="1" skipped="1">'
                                  '<testcase classname="test_uvm" name="ok"/></testsuite>')

    def test_HDL_CI_03_inconsistent_root_skip_count_blocks(self) -> None:
        self._assert_report_blocks('<testsuites skipped="1"><testsuite>'
                                  '<testcase classname="test_uvm" name="ok"/>'
                                  '</testsuite></testsuites>')

    def test_HDL_CI_03_invalid_count_blocks(self) -> None:
        self._assert_report_blocks('<testsuite tests="invalid">'
                                  '<testcase classname="test_uvm" name="ok"/></testsuite>')

    def test_HDL_CI_03_independently_named_parent_and_cases_pass(self) -> None:
        result = self.result('<testsuites tests="3" failures="0" errors="0" skipped="0">'
                             '<testsuite tests="3" failures="0" errors="0" skipped="0">'
                             '<testcase classname="test_uvm" name="test_parent"/>'
                             '<testcase classname="test_uvm" name="test_case_a"/>'
                             '<testcase classname="test_uvm" name="test_case_b"/>'
                             '</testsuite></testsuites>')
        self.assertEqual(qualify.suite_result_errors(result), [])
        self.assertEqual(result["passed"], 3)
        self.assertEqual(result["modules"]["test_uvm"]["passed"], 3)

    def test_HDL_CI_03_unserialized_subtest_count_still_blocks(self) -> None:
        result = self.result('<testsuite tests="3" failures="0" errors="0" skipped="0">'
                             '<testcase classname="test_uvm" name="test_parent"/></testsuite>')
        self.assertIn("count mismatch", result["invalid"])
        self.assertEqual(result["passed"], 0)
        self.assertTrue(qualify.suite_result_errors(result))

    def test_HDL_CI_03_duplicate_case_id_blocks(self) -> None:
        result = self.result('<testsuite tests="2">'
                             '<testcase classname="test_uvm" name="same"/>'
                             '<testcase classname="test_uvm" name="same"/></testsuite>')
        self.assertIn("duplicate", result["invalid"])
        self.assertTrue(qualify.suite_result_errors(result))

    def test_HDL_CI_03_duplicate_across_suites_blocks(self) -> None:
        self._assert_report_blocks('<testsuites><testsuite>'
                                  '<testcase classname="test_uvm" name="same"/></testsuite>'
                                  '<testsuite><testcase classname="test_uvm" name="same"/>'
                                  '</testsuite></testsuites>')

    def test_HDL_CI_03_same_case_name_in_distinct_modules_passes(self) -> None:
        result = self.result('<testsuite tests="2">'
                             '<testcase classname="test_uvm" name="same"/>'
                             '<testcase classname="test_sem" name="same"/></testsuite>')
        self.assertEqual(qualify.suite_result_errors(result), [])
        self.assertEqual(result["passed"], 2)

    def test_HDL_CI_03_missing_case_name_blocks(self) -> None:
        self._assert_report_blocks('<testsuite><testcase classname="test_uvm"/></testsuite>')

    def test_HDL_CI_03_missing_classname_blocks(self) -> None:
        self._assert_report_blocks('<testsuite><testcase name="case"/></testsuite>')

    def test_HDL_CI_03_empty_identity_blocks(self) -> None:
        self._assert_report_blocks('<testsuite><testcase classname=" " name=" "/></testsuite>')

    def test_HDL_CI_03_multiple_outcomes_block(self) -> None:
        self._assert_report_blocks('<testsuite><testcase classname="test_uvm" name="case">'
                                  '<failure/><skipped/></testcase></testsuite>')

    def test_HDL_CI_03_repeated_outcome_blocks(self) -> None:
        self._assert_report_blocks('<testsuite><testcase classname="test_uvm" name="case">'
                                  '<failure/><failure/></testcase></testsuite>')

    def test_HDL_CI_03_nonzero_exit_cannot_be_hidden_by_passing_modules(self) -> None:
        cases = ''.join(f'<testcase classname="{name}" name="ok"/>'
                        for name in qualify.GATE_MODULES["G2"])
        for rc, expected in ((0, "PASS"), (2, "FAIL")):
            result = self.result(f"<testsuite>{cases}</testsuite>", rc)
            report = {"tests": {"integration": result}, "benchmark_ref": (None, None)}
            self.assertEqual(qualify.compute_gates(report)["G2"]["status"], expected)
            self.assertTrue(qualify._mods_ok(report["tests"], ["test_uvm"]).startswith(expected))

    def _assert_probe_status_blocks(self, name: str, value: str | None) -> None:
        ready = {backend: "READY" for backend in ("pyslang", "slang_cli", "cbm", "serena")}
        self.assertTrue(required_backend_errors({**ready, name: value}))

    def test_HDL_CI_03_probe_ready_and_declared_subset(self) -> None:
        ready = {name: "READY" for name in ("pyslang", "slang_cli", "cbm", "serena")}
        self.assertEqual(required_backend_errors(ready), [])
        self.assertTrue(required_backend_errors({**ready, "pyslang": "DEGRADED"}))
        self.assertEqual(required_backend_errors({**ready, "serena": "DEGRADED"}), [])

    def test_HDL_CI_03_probe_pyslang_missing_blocks(self) -> None:
        self._assert_probe_status_blocks('pyslang', None)

    def test_HDL_CI_03_probe_pyslang_unavailable_blocks(self) -> None:
        self._assert_probe_status_blocks('pyslang', 'UNAVAILABLE')

    def test_HDL_CI_03_probe_pyslang_skipped_blocks(self) -> None:
        self._assert_probe_status_blocks('pyslang', 'SKIPPED')

    def test_HDL_CI_03_probe_pyslang_not_run_blocks(self) -> None:
        self._assert_probe_status_blocks('pyslang', 'NOT_RUN')

    def test_HDL_CI_03_probe_pyslang_unknown_blocks(self) -> None:
        self._assert_probe_status_blocks('pyslang', 'unknown')

    def test_HDL_CI_03_probe_slang_cli_missing_blocks(self) -> None:
        self._assert_probe_status_blocks('slang_cli', None)

    def test_HDL_CI_03_probe_slang_cli_unavailable_blocks(self) -> None:
        self._assert_probe_status_blocks('slang_cli', 'UNAVAILABLE')

    def test_HDL_CI_03_probe_slang_cli_skipped_blocks(self) -> None:
        self._assert_probe_status_blocks('slang_cli', 'SKIPPED')

    def test_HDL_CI_03_probe_slang_cli_not_run_blocks(self) -> None:
        self._assert_probe_status_blocks('slang_cli', 'NOT_RUN')

    def test_HDL_CI_03_probe_slang_cli_unknown_blocks(self) -> None:
        self._assert_probe_status_blocks('slang_cli', 'unknown')

    def test_HDL_CI_03_probe_cbm_missing_blocks(self) -> None:
        self._assert_probe_status_blocks('cbm', None)

    def test_HDL_CI_03_probe_cbm_unavailable_blocks(self) -> None:
        self._assert_probe_status_blocks('cbm', 'UNAVAILABLE')

    def test_HDL_CI_03_probe_cbm_skipped_blocks(self) -> None:
        self._assert_probe_status_blocks('cbm', 'SKIPPED')

    def test_HDL_CI_03_probe_cbm_not_run_blocks(self) -> None:
        self._assert_probe_status_blocks('cbm', 'NOT_RUN')

    def test_HDL_CI_03_probe_cbm_unknown_blocks(self) -> None:
        self._assert_probe_status_blocks('cbm', 'unknown')

    def test_HDL_CI_03_probe_serena_missing_blocks(self) -> None:
        self._assert_probe_status_blocks('serena', None)

    def test_HDL_CI_03_probe_serena_unavailable_blocks(self) -> None:
        self._assert_probe_status_blocks('serena', 'UNAVAILABLE')

    def test_HDL_CI_03_probe_serena_skipped_blocks(self) -> None:
        self._assert_probe_status_blocks('serena', 'SKIPPED')

    def test_HDL_CI_03_probe_serena_not_run_blocks(self) -> None:
        self._assert_probe_status_blocks('serena', 'NOT_RUN')

    def test_HDL_CI_03_probe_serena_unknown_blocks(self) -> None:
        self._assert_probe_status_blocks('serena', 'unknown')

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
