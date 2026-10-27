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


MIRROR_URI = 'mirror+file:/etc/apt/apt-mirrors.txt/pool/main/a/apparmor/apparmor-profiles_4.0.1-0ubuntu1_all.deb'
MIRROR_ENTRIES = (
    "http://azure.archive.ubuntu.com/ubuntu/\tpriority:1\n"
    "https://archive.ubuntu.com/ubuntu/\tpriority:2\n"
    "https://security.ubuntu.com/ubuntu/\tpriority:3\n"
)


class OfficialBwrapPolicyTests(unittest.TestCase):
    def test_SEC_05_refuses_non_hosted_environment_before_any_commands(self) -> None:
        with patch.dict("os.environ", {}, clear=True), patch.object(MODULE, "run") as run:
            with self.assertRaisesRegex(RuntimeError, "GitHub-hosted"):
                MODULE.main()
            run.assert_not_called()

    def test_SEC_05_exact_reviewed_policy_required(self) -> None:
        MODULE.validate_policy(MODULE.EXPECTED_POLICY)
        MODULE.validate_policy("# harmless comment\n" + MODULE.EXPECTED_POLICY)

    def _assert_policy_rejected(self, old: str, new: str) -> None:
        with self.assertRaises(RuntimeError):
            MODULE.validate_policy(MODULE.EXPECTED_POLICY.replace(old, new))

    def test_SEC_05_policy_rejects_missing_capability_deny(self) -> None:
        self._assert_policy_rejected('audit deny capability,', '')

    def test_SEC_05_policy_rejects_inherited_exec(self) -> None:
        self._assert_policy_rejected('allow px /** -> bwrap//&unpriv_bwrap,', 'allow ix /**,')

    def test_SEC_05_policy_rejects_wildcard_attachment(self) -> None:
        self._assert_policy_rejected('/usr/bin/bwrap', '/usr/bin/*')

    def test_SEC_05_policy_rejects_unreviewed_abi(self) -> None:
        self._assert_policy_rejected('abi/4.0', 'abi/5.0')

    def test_SEC_05_only_official_ubuntu_archive_uri(self) -> None:
        for host in ("archive.ubuntu.com", "azure.archive.ubuntu.com", "security.ubuntu.com"):
            MODULE.validate_download_uri(f"'https://{host}/ubuntu/pool/main/a/apparmor/p.deb' p.deb 12 hash")

    def _assert_download_uri_rejected(self, uri: str) -> None:
        with self.assertRaises(RuntimeError):
            MODULE.validate_download_uri(f"'{uri}' p.deb 12 hash")

    def test_SEC_05_empty_download_uri_rejected(self) -> None:
        with self.assertRaises(RuntimeError):
            MODULE.validate_download_uri("")

    def test_SEC_05_archive_rejects_unofficial_origin(self) -> None:
        self._assert_download_uri_rejected('https://evil.example/ubuntu/pool/p.deb')

    def test_SEC_05_archive_rejects_misleading_hostname(self) -> None:
        self._assert_download_uri_rejected('https://archive.ubuntu.com.evil.example/ubuntu/pool/p.deb')

    def test_SEC_05_archive_rejects_file_uri(self) -> None:
        self._assert_download_uri_rejected('file:///tmp/p.deb')

    def test_SEC_05_archive_rejects_non_ubuntu_path(self) -> None:
        self._assert_download_uri_rejected('https://archive.ubuntu.com/other/p.deb')

    def test_SEC_05_archive_rejects_semicolon_suffix(self) -> None:
        self._assert_download_uri_rejected('https://archive.ubuntu.com/ubuntu/pool/p.deb;extra')

    def test_SEC_05_unrelated_policy_has_no_conflict(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            MODULE.check_conflicts(Path(directory), "unrelated (enforce)\n")

    def _assert_loaded_profile_rejected(self, name: str) -> None:
        with tempfile.TemporaryDirectory() as directory, self.assertRaises(RuntimeError):
            MODULE.check_conflicts(Path(directory), f"{name} (enforce)\n")

    def test_SEC_05_loaded_profile_rejected_bwrap(self) -> None:
        self._assert_loaded_profile_rejected('bwrap')

    def test_SEC_05_loaded_profile_rejected_unpriv_bwrap(self) -> None:
        self._assert_loaded_profile_rejected('unpriv_bwrap')

    def test_SEC_05_loaded_profile_rejected_stacked(self) -> None:
        self._assert_loaded_profile_rejected('bwrap//&unpriv_bwrap')

    def _assert_existing_override_rejected(self, name: str) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / name
            path.parent.mkdir(exist_ok=True)
            path.symlink_to(root / "absent")
            with self.assertRaises(RuntimeError):
                MODULE.check_conflicts(root, "")

    def test_SEC_05_existing_override_rejected_official_policy(self) -> None:
        self._assert_existing_override_rejected(MODULE.POLICY_NAME)

    def test_SEC_05_existing_override_rejected_local_bwrap(self) -> None:
        self._assert_existing_override_rejected('local/bwrap-userns-restrict')

    def test_SEC_05_existing_override_rejected_local_unpriv(self) -> None:
        self._assert_existing_override_rejected('local/unpriv_bwrap')

    def test_SEC_05_existing_bwrap_attachment_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
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
        with patch.object(MODULE, "validate_runner_mirror_file") as validate:
            MODULE.validate_download_uri(f"'{MIRROR_URI}' package.deb 12 hash")
            validate.assert_called_once_with()

    def _assert_mirror_uri_rejected(self, uri: str) -> None:
        with patch.object(MODULE, "validate_runner_mirror_file") as validate:
            with self.assertRaises(RuntimeError):
                MODULE.validate_download_uri(f"'{uri}' package.deb 12 hash")
            validate.assert_not_called()

    def test_SEC_05_mirror_uri_rejects_wrong_directory(self) -> None:
        self._assert_mirror_uri_rejected(MIRROR_URI.replace("/etc/apt/", "/tmp/"))

    def test_SEC_05_mirror_uri_rejects_traversal(self) -> None:
        self._assert_mirror_uri_rejected(MIRROR_URI.replace("/pool/", "/../pool/"))

    def test_SEC_05_mirror_uri_rejects_encoded_traversal(self) -> None:
        self._assert_mirror_uri_rejected(MIRROR_URI.replace("/pool/", "/%2e%2e/pool/"))

    def test_SEC_05_mirror_uri_rejects_query(self) -> None:
        self._assert_mirror_uri_rejected(MIRROR_URI + "?token=x")

    def test_SEC_05_mirror_uri_rejects_fragment(self) -> None:
        self._assert_mirror_uri_rejected(MIRROR_URI + "#x")

    def test_SEC_05_mirror_uri_rejects_authority(self) -> None:
        self._assert_mirror_uri_rejected(MIRROR_URI.replace("mirror+file:", "mirror+file://evil"))

    def test_SEC_05_mirror_uri_rejects_other_package(self) -> None:
        self._assert_mirror_uri_rejected(MIRROR_URI.replace("apparmor-profiles_", "other_"))

    def test_SEC_05_mirror_uri_rejects_trailing_path(self) -> None:
        self._assert_mirror_uri_rejected(MIRROR_URI.replace(".deb", ".deb/other"))

    def test_SEC_05_every_mirror_origin_and_metadata_validated(self) -> None:
        MODULE.validate_mirror_entries(MIRROR_ENTRIES)

    def _assert_mirror_entries_rejected(self, content: str) -> None:
        with self.assertRaises(RuntimeError):
            MODULE.validate_mirror_entries(content)

    def test_SEC_05_mirror_entries_reject_mixed_origins(self) -> None:
        self._assert_mirror_entries_rejected(MIRROR_ENTRIES + "https://evil.example/ubuntu/\n")

    def test_SEC_05_mirror_entries_reject_empty(self) -> None:
        self._assert_mirror_entries_rejected("")

    def test_SEC_05_mirror_entries_reject_comments_only(self) -> None:
        self._assert_mirror_entries_rejected("# only comment\n")

    def test_SEC_05_mirror_entries_reject_unknown_metadata(self) -> None:
        self._assert_mirror_entries_rejected(MIRROR_ENTRIES.replace("priority:1", "unknown:1"))

    def test_SEC_05_mirror_entries_reject_traversal(self) -> None:
        self._assert_mirror_entries_rejected(MIRROR_ENTRIES.replace("/ubuntu/", "/ubuntu/../"))

    def test_SEC_05_mirror_entries_reject_encoded_traversal(self) -> None:
        self._assert_mirror_entries_rejected(MIRROR_ENTRIES.replace("/ubuntu/", "/ubuntu/%2e%2e/"))

    def test_SEC_05_mirror_entries_reject_credentials(self) -> None:
        self._assert_mirror_entries_rejected(MIRROR_ENTRIES.replace("azure.archive.ubuntu.com", "user:password@azure.archive.ubuntu.com"))

    def test_SEC_05_mirror_entries_reject_nonstandard_port(self) -> None:
        self._assert_mirror_entries_rejected(MIRROR_ENTRIES.replace("azure.archive.ubuntu.com", "azure.archive.ubuntu.com:8080"))

    def test_SEC_05_mirror_entries_reject_query(self) -> None:
        self._assert_mirror_entries_rejected(MIRROR_ENTRIES.replace("/ubuntu/", "/ubuntu/?token=x"))

    def test_SEC_05_mirror_entries_reject_semicolon_suffix(self) -> None:
        self._assert_mirror_entries_rejected(MIRROR_ENTRIES.replace("/ubuntu/", "/ubuntu/;extra"))

    def test_SEC_05_mirror_entries_reject_nested_scheme(self) -> None:
        self._assert_mirror_entries_rejected(MIRROR_ENTRIES.replace("https://", "mirror+https://"))

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
