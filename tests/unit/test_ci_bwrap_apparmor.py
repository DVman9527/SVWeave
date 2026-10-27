"""SEC-05: fail closed before changing anything outside the authorized hosted CI."""

from __future__ import annotations

import importlib.util
import io
import stat
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import mock_open, patch

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
                    "file:///tmp/p.deb", "https://archive.ubuntu.com/other/p.deb",
                    "https://archive.ubuntu.com/ubuntu/pool/p.deb;extra"):
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

    def test_SEC_05_rejected_uri_diagnostic_omits_credentials_query_and_fragment(self) -> None:
        with self.assertRaises(RuntimeError) as raised:
            MODULE.validate_download_uri(
                "'https://user:password@evil.example/p.deb?token=secret#private' p.deb 12 hash"
            )
        message = str(raised.exception)
        self.assertIn('"host": "evil.example"', message)
        self.assertIn('"path": "/p.deb"', message)
        for secret in ("user", "password", "token", "secret", "private"):
            self.assertNotIn(secret, message)

    def test_SEC_05_mirror_evidence_is_read_only_and_omits_uri_secrets(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "apt-mirrors.txt"
            content = "https://user:password@archive.ubuntu.com/ubuntu?token=secret#private\tpriority:1\n"
            path.write_text(content)
            output = io.StringIO()
            with patch.object(MODULE, "Path", return_value=path), patch("sys.stdout", output):
                MODULE.describe_runner_mirror_list()
            self.assertEqual(path.read_text(), content)
            self.assertIn('"host": "archive.ubuntu.com"', output.getvalue())
            for secret in ("password", "token", "secret", "private"):
                self.assertNotIn(secret, output.getvalue())

    def test_SEC_05_only_fixed_mirror_file_and_apparmor_package(self) -> None:
        uri = "mirror+file:/etc/apt/apt-mirrors.txt/pool/main/a/apparmor/apparmor-profiles_4.0.1-0ubuntu1_all.deb"
        with patch.object(MODULE, "validate_runner_mirror_file") as validate:
            MODULE.validate_download_uri(f"'{uri}' package.deb 12 hash")
            validate.assert_called_once_with()
        for invalid in (
            uri.replace("/etc/apt/", "/tmp/"), uri.replace("/pool/", "/../pool/"),
            uri.replace("/pool/", "/%2e%2e/pool/"), uri + "?token=x", uri + "#x",
            uri.replace("mirror+file:", "mirror+file://evil"),
            uri.replace("apparmor-profiles_", "other_"), uri.replace(".deb", ".deb/other"),
        ):
            with self.subTest(uri=invalid), patch.object(MODULE, "validate_runner_mirror_file") as validate:
                with self.assertRaises(RuntimeError):
                    MODULE.validate_download_uri(f"'{invalid}' package.deb 12 hash")
                validate.assert_not_called()

    def test_SEC_05_every_mirror_origin_and_metadata_validated(self) -> None:
        valid = "http://azure.archive.ubuntu.com/ubuntu/\tpriority:1\nhttps://archive.ubuntu.com/ubuntu/\tpriority:2\nhttps://security.ubuntu.com/ubuntu/\tpriority:3\n"
        MODULE.validate_mirror_entries(valid)
        for invalid in (
            valid + "https://evil.example/ubuntu/\n", "", "# only comment\n",
            valid.replace("priority:1", "unknown:1"),
            valid.replace("/ubuntu/", "/ubuntu/../"),
            valid.replace("/ubuntu/", "/ubuntu/%2e%2e/"),
            valid.replace("azure.archive.ubuntu.com", "user:password@azure.archive.ubuntu.com"),
            valid.replace("azure.archive.ubuntu.com", "azure.archive.ubuntu.com:8080"),
            valid.replace("/ubuntu/", "/ubuntu/?token=x"),
            valid.replace("/ubuntu/", "/ubuntu/;extra"),
            valid.replace("https://", "mirror+https://"),
        ):
            with self.subTest(content=invalid), self.assertRaises(RuntimeError):
                MODULE.validate_mirror_entries(invalid)

    def test_SEC_05_mirror_file_ownership_permissions_and_symlink_guards(self) -> None:
        directory = SimpleNamespace(st_mode=stat.S_IFDIR | 0o755, st_uid=0, st_size=0)
        regular = SimpleNamespace(st_mode=stat.S_IFREG | 0o644, st_uid=0, st_size=50)
        with patch.object(MODULE.Path, "lstat", side_effect=[directory, directory, regular]), \
                patch.object(MODULE.Path, "open", mock_open(read_data="https://archive.ubuntu.com/ubuntu/\n")):
            MODULE.validate_runner_mirror_file()
        for bad in (
            SimpleNamespace(st_mode=stat.S_IFLNK | 0o777, st_uid=0, st_size=50),
            SimpleNamespace(st_mode=stat.S_IFREG | 0o664, st_uid=0, st_size=50),
            SimpleNamespace(st_mode=stat.S_IFREG | 0o644, st_uid=1000, st_size=50),
            SimpleNamespace(st_mode=stat.S_IFREG | 0o644, st_uid=0, st_size=4097),
        ):
            with patch.object(MODULE.Path, "lstat", side_effect=[directory, directory, bad]), \
                    patch.object(MODULE.Path, "open") as opened:
                with self.assertRaises(RuntimeError):
                    MODULE.validate_runner_mirror_file()
                opened.assert_not_called()
        with patch.object(MODULE.Path, "lstat", return_value=SimpleNamespace(
            st_mode=stat.S_IFDIR | 0o777, st_uid=0, st_size=0,
        )), self.assertRaises(RuntimeError):
            MODULE.validate_runner_mirror_file()
