"""LIC-01: owner-approved first-party MIT does not waive third-party review."""
from __future__ import annotations

import hashlib
import json
import shutil
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))
import qualify  # noqa: E402


@pytest.fixture
def license_repo(tmp_path: Path) -> Path:
    for name in ("LICENSE", "pyproject.toml", "README.md", "README.zh-TW.md", "README.zh-CN.md"):
        shutil.copyfile(ROOT / name, tmp_path / name)
    return tmp_path


def test_LIC_01_owner_approved_mit_is_consistent() -> None:
    assert qualify.project_license_errors(ROOT) == []
    assert hashlib.sha256((ROOT / "LICENSE").read_bytes()).hexdigest() == qualify.MIT_LICENSE_SHA256


@pytest.mark.parametrize("name", ["LICENSE", "pyproject.toml", "README.md", "README.zh-TW.md", "README.zh-CN.md"])
def test_LIC_01_missing_license_document_fails(license_repo: Path, name: str) -> None:
    (license_repo / name).unlink()
    assert qualify.project_license_errors(license_repo)


def test_LIC_01_altered_grant_or_copyright_fails(license_repo: Path) -> None:
    (license_repo / "LICENSE").write_text("MIT License\nAll rights reserved.\n")
    assert qualify.project_license_errors(license_repo)


@pytest.mark.parametrize("replacement", ['license = "Apache-2.0"', 'license = {text = "MIT"}'])
def test_LIC_01_inconsistent_package_license_fails(license_repo: Path, replacement: str) -> None:
    p = license_repo / "pyproject.toml"
    p.write_text(p.read_text().replace('license = "MIT"', replacement))
    assert qualify.project_license_errors(license_repo)


def test_LIC_01_missing_packaged_license_fails(license_repo: Path) -> None:
    p = license_repo / "pyproject.toml"
    p.write_text(p.read_text().replace('license-files = ["LICENSE"]', 'license-files = []'))
    assert qualify.project_license_errors(license_repo)


@pytest.mark.parametrize("name", ["README.md", "README.zh-TW.md", "README.zh-CN.md"])
def test_LIC_01_stale_owner_tbd_fails(license_repo: Path, name: str) -> None:
    p = license_repo / name
    p.write_text(p.read_text() + "\nTBD by owner\n")
    assert qualify.project_license_errors(license_repo)


def test_LIC_01_third_party_review_stays_open() -> None:
    inventory = json.loads((ROOT / "license_manifest.json").read_text())
    assert inventory["legal_review_status"] == "NOT_DONE"
    assert inventory["qualification_state"] == "UNQUALIFIED"
    report = {"owner_items": [{"id": "license_review", "title": "third-party legal review",
                               "detail": "NOT_DONE", "state": "OPEN"}]}
    assert qualify.blockers(report) == [
        "OPEN owner item license_review: third-party legal review (NOT_DONE)"]


def test_LIC_01_qualification_uses_current_consistency_not_old_mit_prohibition(tmp_path: Path) -> None:
    junit = tmp_path / "test.xml"
    junit.write_text('<testsuite tests="1"><testcase classname="license" name="ok"/></testsuite>')
    result = {"rc": 0, **qualify.parse_junit(junit)}
    docs = {"schema_emit_check": {"rc": 0}, "doc_tests": result,
            "required_files_missing": [], "project_license_errors": []}
    report = {"tests": {"unit": result}, "docs": docs, "benchmark_ref": (None, None)}
    reasons = qualify.compute_gates(report)["G5"]["reasons"]
    assert not any("license" in reason.lower() for reason in reasons)
    # Other gates remain unmet; a consistent first-party license cannot qualify a release.
    assert qualify.compute_gates(report)["G5"]["status"] == "FAIL"
    docs["project_license_errors"] = ["LICENSE differs from the owner-confirmed MIT text/copyright"]
    assert docs["project_license_errors"][0] in qualify.compute_gates(report)["G5"]["reasons"]
    del docs["project_license_errors"]
    assert "first-party license consistency not checked" in qualify.compute_gates(report)["G5"]["reasons"]
