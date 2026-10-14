"""Fix round B1: one toolchain discovery path, extractor source identity, pyslang RECORD check."""

from __future__ import annotations

import base64
import hashlib
import shutil
from importlib import metadata
from pathlib import Path

import pytest

from svweave.application import toolchain as T
from svweave.domain.errors import ErrorCode, SvwError
from svweave.orchestration.toolchain import ENV_ROOT, Toolchain

REPO = Path(__file__).resolve().parents[2]


def _hostile(tmp_path: Path) -> Path:
    h = tmp_path / "hostile"
    (h / "sub").mkdir(parents=True)
    shutil.copy(REPO / "toolchain.lock.json", h / "toolchain.lock.json")
    shutil.copy(REPO / "capability_report.json", h / "capability_report.json")
    return h


def test_B1a_discovery_never_uses_cwd_or_repository(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    h = _hostile(tmp_path)
    monkeypatch.delenv(ENV_ROOT, raising=False)
    monkeypatch.chdir(h / "sub")
    assert Toolchain.discover().root == REPO
    assert T.find_lock(h) == REPO / "toolchain.lock.json"  # repository under analysis is ignored
    assert T.compute_toolchain(h)[2] == REPO / "toolchain.lock.json"


def test_B1a_env_root_is_honoured_by_every_consumer(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    h = _hostile(tmp_path)
    monkeypatch.setenv(ENV_ROOT, str(h))
    assert Toolchain.discover().root == h.resolve()
    assert T.find_lock() == h / "toolchain.lock.json"
    monkeypatch.setenv(ENV_ROOT, str(tmp_path / "empty"))
    with pytest.raises(SvwError) as ei:
        T.find_lock()
    assert ei.value.code is ErrorCode.CAPABILITY_MISMATCH


def test_B1b_extractor_source_change_changes_toolchain_id(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    import svweave.adapters.slang as pkg

    before = T.compute_toolchain(tmp_path)[0]
    copy = tmp_path / "slang"
    shutil.copytree(Path(pkg.__file__).parent, copy, ignore=shutil.ignore_patterns("__pycache__"))
    monkeypatch.setattr(T._slang_pkg, "__file__", str(copy / "__init__.py"))
    assert T.compute_toolchain(tmp_path)[0] == before  # identical sources -> identical identity
    (copy / "extract.py").write_text((copy / "extract.py").read_text() + "\n# behavioural change\n")
    assert T.compute_toolchain(tmp_path)[0] != before


def _fake_dist(tmp_path: Path, content: bytes = b"x = 1\n") -> tuple[metadata.Distribution, str]:
    sp = tmp_path / "sp"
    (sp / "pyslang").mkdir(parents=True)
    (sp / "pyslang" / "m.py").write_bytes(content)
    di = sp / "pyslang-12.0.0.dist-info"
    di.mkdir()
    h = base64.urlsafe_b64encode(hashlib.sha256(b"x = 1\n").digest()).rstrip(b"=").decode()
    rec = f"pyslang/m.py,sha256={h},{len(b'x = 1' + bytes([10]))}\npyslang-12.0.0.dist-info/RECORD,,\n"
    (di / "RECORD").write_text(rec)
    return metadata.PathDistribution(di), hashlib.sha256(rec.encode()).hexdigest()


def test_B1c_pyslang_content_verified_against_record_and_lock(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    dist, pin = _fake_dist(tmp_path)
    monkeypatch.setattr(T.metadata, "distribution", lambda name: dist)
    assert T.verify_pyslang_install(pin) == pin
    with pytest.raises(SvwError) as ei:  # lock pin differs from RECORD
        T.verify_pyslang_install("0" * 64)
    assert ei.value.code is ErrorCode.CAPABILITY_MISMATCH
    with pytest.raises(SvwError):  # lock without a pin
        T.verify_pyslang_install(None)
    (tmp_path / "sp" / "pyslang" / "m.py").write_bytes(b"x = 2\n")  # tampered file
    with pytest.raises(SvwError) as e2:
        T.verify_pyslang_install(pin)
    assert e2.value.code is ErrorCode.CAPABILITY_MISMATCH and "RECORD hash" in e2.value.message


def test_B1c_unlisted_file_in_package_is_rejected(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    dist, pin = _fake_dist(tmp_path)
    monkeypatch.setattr(T.metadata, "distribution", lambda name: dist)
    (tmp_path / "sp" / "pyslang" / "evil.py").write_text("import os\n")
    with pytest.raises(SvwError) as ei:
        T.verify_pyslang_install(pin)
    assert ei.value.code is ErrorCode.CAPABILITY_MISMATCH


def test_B1c_real_install_matches_lock() -> None:
    parts = T.compute_toolchain(REPO)[1]
    assert parts["pyslang"]["record_sha256"] and parts["extractor_source_sha256"]
