"""Pure controller safety tests: no native backend import/build/execution (SEC-05/G0)."""
from __future__ import annotations

import copy
import importlib.util
import io
import json
import tarfile
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
HERE = ROOT / "scripts/spikes/partial_semantic_g0"


def load(name):
    spec = importlib.util.spec_from_file_location(f"partial_g0_{name}", HERE / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


setup = load("setup")
controller = load("run")


def archive(members):
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as output:
        for name, kind in members:
            info = tarfile.TarInfo(name)
            info.type = kind
            info.linkname = "/etc/passwd" if kind == tarfile.SYMTYPE else ""
            output.addfile(info, io.BytesIO())
    return buffer.getvalue()


class PartialSemanticHarnessTests(unittest.TestCase):
    def test_SEC_05_isolation_failure_precedes_any_native_launch(self):
        with (patch.object(controller, "wrap", side_effect=RuntimeError("no bwrap")),
              patch.object(controller.subprocess, "Popen") as popen):
            with self.assertRaisesRegex(RuntimeError, "no bwrap"):
                controller.execute(None, ["native"], timeout=1, rss_limit=1024)
            popen.assert_not_called()

    def test_SEC_05_runtime_spec_read_only_no_secret_environment(self):
        spec = controller.probe_spec(sources=(HERE,), tools=(HERE / "run.py",))
        self.assertFalse(spec.network)
        self.assertFalse(spec.rw_paths)
        self.assertEqual(spec.etc, "minimal")
        self.assertEqual(set(spec.env), {"PATH", "LANG", "PYTHONDONTWRITEBYTECODE"})

    def test_SEC_05_source_tool_writable_overlap_refused(self):
        with (tempfile.TemporaryDirectory() as directory,
              self.assertRaises(controller.probe_spec.__globals__["SvwError"])):
            controller.probe_spec(sources=(directory,), writable=(directory,))

    def test_DEP_G0_archives_reject_links_traversal_devices_duplicates(self):
        for members in (
            [("root/escape", tarfile.SYMTYPE)],
            [("root/../escape", tarfile.REGTYPE)],
            [("/root/escape", tarfile.REGTYPE)],
            [("root/device", tarfile.CHRTYPE)],
            [("root/file", tarfile.REGTYPE), ("root/file", tarfile.REGTYPE)],
            [("root/file", tarfile.REGTYPE), ("other/file", tarfile.REGTYPE)],
        ):
            with (self.subTest(members=members), tempfile.TemporaryDirectory() as directory,
                  self.assertRaises(ValueError)):
                setup.extract(archive(members), Path(directory))

    def test_DEP_G0_regular_archive_extracts_only_below_destination(self):
        with tempfile.TemporaryDirectory() as directory:
            setup.extract(archive([("root/file", tarfile.REGTYPE)]), Path(directory))
            self.assertEqual((Path(directory) / "file").read_bytes(), b"")

    def test_DEP_G0_all_sources_are_checksum_pinned(self):
        sources = json.loads((HERE / "sources.json").read_text())
        self.assertEqual(set(sources), {"slang", "fmt", "boost_regex", "tomlplusplus"})
        for item in sources.values():
            self.assertRegex(item["sha256"], r"^[0-9a-f]{64}$")
            self.assertTrue(item["url"].startswith("https://codeload.github.com/"))

    def test_G0_sanitizers_offline_same_flags(self):
        flags = " ".join(controller.BUILD_FLAGS)
        for required in ("-fsanitize=address,undefined", "-fno-sanitize-recover=all",
                         "FETCHCONTENT_FULLY_DISCONNECTED=ON", "SLANG_USE_MIMALLOC=OFF"):
            self.assertIn(required, flags)
        self.assertNotIn("PCU_STOCK", flags)

    def test_G0_strict_native_evidence_rejects_missing_corrupted_unknown(self):
        a = {"ast": {"value": 0}, "diagnostics": [],
             "scope": "exact-three-module-constant-rhs-fixture-only"}
        b = {**a, "ast": {"value": 1}}
        good = {"status": "MICRO_PROTOTYPE_PASS", "general_g0": "NOT_ESTABLISHED",
                "generations": [a, b, a],
                "assertions": dict.fromkeys(controller.ASSERTIONS, True),
                "counters": {key: dict.fromkeys(controller.WORK, 1) for key in controller.COUNTERS}}
        for key in ("expressions", "conversions"):
            good["counters"]["changed_after_b"][key] = 2
            good["counters"]["changed_after_a"][key] = 3
        controller.validate_incremental(good, a, b)
        bads = [None, {}, {**good, "extra": True}, {**good, "status": "PASS"},
                {**good, "generations": [a, a, a]}, {**good, "assertions": {}},
                {**good, "counters": {}}, {**good, "general_g0": "PASS"}]
        for field, member, value in (("assertions", "stale_handle_rejected", False),
                                     ("counters", "control_after", dict.fromkeys(controller.WORK, 2)),
                                     ("counters", "changed_after_b", dict.fromkeys(controller.WORK, True))):
            bad = copy.deepcopy(good)
            bad[field][member] = value
            bads.append(bad)
        for bad in bads:
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                controller.validate_incremental(bad, a, b)
        for text in ('{"status":1,"status":2}', '{"x":NaN}', 'not JSON'):
            with self.subTest(text=text), self.assertRaises(ValueError):
                controller.strict_json(text)

    def test_G0_workflow_experimental_manual_default_off(self):
        text = (ROOT / ".github/workflows/ci.yml").read_text()
        field = text.split("      partial_semantic_g0:\n", 1)[1].split("      native_verilator:", 1)[0]
        self.assertIn("default: false", field)
        self.assertIn("github.event_name == 'workflow_dispatch' && inputs.partial_semantic_g0", text)
        self.assertIn("persist-credentials: false", text.split("  partial-semantic-g0:", 1)[1])


if __name__ == "__main__":
    unittest.main()
