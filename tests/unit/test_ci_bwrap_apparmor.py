"""SEC-05: fail closed before changing anything outside the authorized hosted CI."""

from __future__ import annotations

import importlib.util
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location("ci_bwrap", ROOT / "scripts/ci_prepare_bwrap_apparmor.py")
assert SPEC is not None and SPEC.loader is not None
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


class OfficialBwrapPolicyTests(unittest.TestCase):
    def test_SEC_05_refuses_non_hosted_environment_before_any_commands(self) -> None:
        with patch.dict("os.environ", {}, clear=True), patch.object(MODULE, "run") as run:
            with self.assertRaisesRegex(RuntimeError, "GitHub-hosted"):
                MODULE.main()
            run.assert_not_called()

    def test_SEC_05_exact_reviewed_policy_required(self) -> None:
        MODULE.validate_policy(MODULE.EXPECTED_POLICY)
        MODULE.validate_policy("# harmless comment\n" + MODULE.EXPECTED_POLICY)
        for old, new in [
            ("audit deny capability,", ""),
            ("allow px /** -> bwrap//&unpriv_bwrap,", "allow ix /**,"),
            ("/usr/bin/bwrap", "/usr/bin/*"),
            ("abi/4.0", "abi/5.0"),
        ]:
            with self.subTest(old=old), self.assertRaises(RuntimeError):
                MODULE.validate_policy(MODULE.EXPECTED_POLICY.replace(old, new))

    def test_SEC_05_only_official_ubuntu_archive_uri(self) -> None:
        for host in ("archive.ubuntu.com", "azure.archive.ubuntu.com", "security.ubuntu.com"):
            MODULE.validate_download_uri(f"'https://{host}/ubuntu/pool/main/a/apparmor/p.deb' p.deb 12 hash")
        for uri in ("https://evil.example/ubuntu/pool/p.deb",
                    "https://archive.ubuntu.com.evil.example/ubuntu/pool/p.deb",
                    "file:///tmp/p.deb", "https://archive.ubuntu.com/other/p.deb"):
            with self.subTest(uri=uri), self.assertRaises(RuntimeError):
                MODULE.validate_download_uri(f"'{uri}' p.deb 12 hash")
        with self.assertRaises(RuntimeError):
            MODULE.validate_download_uri("")

    def test_SEC_05_existing_policy_overrides_and_loaded_profiles_stop_setup(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            MODULE.check_conflicts(root, "unrelated (enforce)\n")
            for name in ("bwrap", "unpriv_bwrap", "bwrap//&unpriv_bwrap"):
                with self.subTest(name=name), self.assertRaises(RuntimeError):
                    MODULE.check_conflicts(root, f"{name} (enforce)\n")
            for name in (MODULE.POLICY_NAME, "local/bwrap-userns-restrict", "local/unpriv_bwrap"):
                path = root / name
                path.parent.mkdir(exist_ok=True)
                path.symlink_to(root / "absent")
                with self.subTest(name=name), self.assertRaises(RuntimeError):
                    MODULE.check_conflicts(root, "")
                path.unlink()
            (root / "custom").write_text("profile custom /usr/bin/bwrap {}")
            with self.assertRaisesRegex(RuntimeError, "attachment"):
                MODULE.check_conflicts(root, "")

    def test_SEC_05_only_add_profile_and_preserve_global_policy(self) -> None:
        script = (ROOT / "scripts/ci_prepare_bwrap_apparmor.py").read_text()
        self.assertIn('str(parser), "-a", str(target)', script)
        self.assertIn('"cp", "--no-clobber"', script)
        self.assertNotIn('"-r", str(target)', script)
        self.assertNotIn("sysctl -w", script)
        self.assertNotIn("allow_degraded", script)
