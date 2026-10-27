"""HDL-VLT guard tests only. Mocks here never count as native Verilator evidence."""

from __future__ import annotations

import contextlib
import io
import json
import os
import tempfile
import unittest
import xml.etree.ElementTree as ET
from pathlib import Path
from unittest.mock import patch

from scripts import bootstrap_verilator as bootstrap
from scripts import verify_verilator_native as native

from svweave.security.sandbox import HostCapability, SandboxRunner


class NativeVerilatorGuards(unittest.TestCase):
    def test_HDL_VLT_commands_are_static_and_explicit_language(self) -> None:
        for case in native.CASES:
            for mode in ("lint", "json"):
                command = native.static_command(Path("/tool"), Path("/src/test.v"), Path("/out"), case, mode)
                self.assertEqual(command[command.index("--language") + 1], case["language"])
                self.assertNotIn("--binary", command)
                self.assertNotIn("--build", command)
                self.assertNotIn("--cc", command)
                self.assertNotIn("-Wno-fatal", command)
                self.assertIn("--lint-only" if mode == "lint" else "--json-only", command)
        with self.assertRaises(AssertionError):
            native.static_command(Path("/tool"), Path("/src/a.v"), Path("/out"), native.CASES[0], "simulation")

    def test_HDL_VLT_runtime_binds_readonly_sources_no_network(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            root, sources, out = (base / name for name in ("tool", "sources", "output"))
            for directory in (root, sources, out):
                directory.mkdir()
            spec = native.native_spec(root, sources, out)
            runner = SandboxRunner(capability=HostCapability(True, "/usr/bin/bwrap", "test", None))
            argv = runner.wrap(spec, ["/tool/bin/verilator", "--version"]).argv
            self.assertIn("--unshare-net", argv)
            self.assertEqual(spec.ro_paths, (root, sources))
            self.assertEqual(spec.rw_paths, (out,))
            self.assertEqual(spec.env["HOME"], "/tmp")
            self.assertNotIn("GITHUB_TOKEN", spec.env)
            self.assertFalse(runner.allow_degraded)

    def test_HDL_VLT_workflow_evidence_subdirectory_is_only_write_exemption(self) -> None:
        out = native.REPO / "evidence/verilator-native/HDL-VLT-01"
        sources = native.REPO / "evidence/verilator-native/sources"
        spec = native.native_spec(Path("/tmp/verilator"), sources, out)
        runner = SandboxRunner(capability=HostCapability(True, "/usr/bin/bwrap", "test", None))
        argv = runner.wrap(spec, ["/tmp/verilator/bin/verilator", "--version"]).argv
        self.assertEqual(spec.rw_exempt, (out,))
        self.assertEqual(argv.count("--bind"), 1)
        index = argv.index("--bind")
        self.assertEqual(argv[index + 1:index + 3], [str(out), str(out)])

    def test_HDL_VLT_rejection_requires_specific_error_not_arbitrary_nonzero(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            stdout, stderr = base / "stdout", base / "stderr"
            stdout.write_text("")
            result = {"returncode": 1, "stdout": str(stdout), "stderr": str(stderr)}
            case = native.CASES[2]
            for bad in ("bwrap: permission denied", "%Warning: missing_unit", "%Error: unrelated",
                        "%Error: missing_unit\n%Error: Verilator internal fault, sorry."):
                stderr.write_text(bad)
                with self.assertRaises(AssertionError):
                    native.verify_exit(result, case)
            stderr.write_text("%Error: Cannot find file containing module: missing_unit")
            native.verify_exit(result, case)
            for code in (0, -9, 137):
                with self.assertRaises(AssertionError):
                    native.verify_exit({**result, "returncode": code}, case)

    def test_HDL_VLT_valid_source_must_not_hide_compiler_failure(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            stdout, stderr = base / "stdout", base / "stderr"
            stdout.write_text("")
            stderr.write_text("%Error: bad source")
            with self.assertRaises(AssertionError):
                native.verify_exit({"returncode": 0, "stdout": str(stdout), "stderr": str(stderr)}, native.CASES[0])

    def test_HDL_VLT_junit_has_no_skip_or_aggregate_false_pass(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "junit.xml"
            for report in (
                {"status": "BLOCKED", "cases": [], "error": "sandbox unavailable"},
                {"status": "FAIL", "cases": [{"id": "HDL-VLT-01", "status": "PASS"}], "error": "source changed"},
            ):
                native.write_junit(report, path)
                root = ET.parse(path).getroot()
                self.assertEqual(root.attrib["skipped"], "0")
                self.assertEqual(root.attrib["failures"], "1")
                self.assertIsNotNone(root.find("testcase/failure"))

    def test_HDL_VLT_missing_isolation_never_starts_native(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / "evidence"
            cap = HostCapability(False, "/usr/bin/bwrap", "test", "recorded denial")
            args = ["verify", "--verilator-root", "/missing", "--output", str(output)]
            with patch("sys.argv", args), patch.object(native, "probe_host", return_value=cap), \
                    patch.object(native, "invoke") as invoke, contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(native.main(), 1)
                invoke.assert_not_called()
            report = json.loads((output / "report.json").read_text())
            self.assertEqual(report["status"], "BLOCKED")
            self.assertEqual(report["cases"], [])
            self.assertEqual(report["simulation"], "NOT_RUN")

    def test_HDL_VLT_bootstrap_refuses_local_install_without_network_or_probe(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            args = ["bootstrap", "--destination", str(Path(temporary) / "tool"), "--output", str(Path(temporary) / "evidence")]
            with patch("sys.argv", args), patch.dict(os.environ, {}, clear=True), \
                    patch.object(bootstrap, "probe_host") as probe, \
                    patch.object(bootstrap.subprocess, "run") as run, contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(bootstrap.main(), 1)
                probe.assert_not_called()
                run.assert_not_called()


if __name__ == "__main__":
    unittest.main()
