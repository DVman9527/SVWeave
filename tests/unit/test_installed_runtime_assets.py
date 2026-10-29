"""Plan 7.2/7.3 (DEP-01) and 8.1 (SEC-03): install-owned pins, not project-owned pins.

These are pure packaging/discovery tests, not backend qualification. A real artifact
check runs only when the project's exact build backend is already available offline.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tarfile
import tomllib
import zipfile
from importlib import metadata
from pathlib import Path

import pytest
import yaml
from scripts import ci_wheel_smoke

from svweave.application.toolchain import find_lock
from svweave.domain.errors import ErrorCode, SvwError
from svweave.orchestration import toolchain as T

REPO = Path(__file__).resolve().parents[2]
ASSETS = (
    "toolchain.lock.json",
    "capability_report.json",
    "license_manifest.json",
    "THIRD_PARTY_NOTICES.md",
    "fixtures/demo/filelists/rtl.f",
    "fixtures/demo/include/demo_defs.svh",
    "fixtures/demo/rtl/demo_lane.sv",
    "fixtures/demo/rtl/demo_top.sv",
    "fixtures/demo/sva/demo_bind.sv",
    "fixtures/demo/sva/stream_hold_sva.sv",
    "fixtures/uvm/svw_demo_uvm_pkg.sv",
    "fixtures/uvm/svw_uvm_cfgdb_pkg.sv",
    "fixtures/uvm/svw_uvm_ext_top.sv",
    "fixtures/uvm/svw_uvm_override_pkg.sv",
    "fixtures/uvm/svw_uvm_static_calls.sv",
    "fixtures/uvm/svw_uvm_top.sv",
)


def _wheel_asset_map() -> dict[str, str]:
    config = tomllib.loads((REPO / "pyproject.toml").read_text())
    return config["tool"]["hatch"]["build"]["targets"]["wheel"]["force-include"]


def _asset_files() -> list[Path]:
    return [REPO / name for name in ASSETS]


def _stage_assets(site: Path) -> Path:
    """Exercise the configured file mapping without claiming to have built a wheel."""
    for source, target in _wheel_asset_map().items():
        src, dst = REPO / source, site / target
        dst.parent.mkdir(parents=True, exist_ok=True)
        if src.is_dir():
            shutil.copytree(src, dst)
        else:
            shutil.copyfile(src, dst)
    return site / "svweave" / "_runtime"


def test_DEP_01_wheel_configuration_carries_authoritative_assets(tmp_path: Path) -> None:
    assert _wheel_asset_map() == {name: f"svweave/_runtime/{name}" for name in ASSETS}
    config = tomllib.loads((REPO / "pyproject.toml").read_text())
    assert config["project"]["scripts"]["svweave-mcp"] == "svweave.launcher:main"
    runtime = _stage_assets(tmp_path / "site-packages")
    for source in _asset_files():
        assert (runtime / source.relative_to(REPO)).read_bytes() == source.read_bytes()
    assert not (runtime / ".toolchain").exists()  # native tools are installed separately


def test_DEP_01_detached_package_finds_its_own_assets(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    site = tmp_path / "venv" / "lib" / "python3.12" / "site-packages"
    runtime = _stage_assets(site)
    monkeypatch.setattr(T, "__file__", str(site / "svweave" / "orchestration" / "toolchain.py"))
    monkeypatch.delenv(T.ENV_ROOT, raising=False)
    project = tmp_path / "unrelated-hdl-project"
    project.mkdir()
    (project / T.LOCK_NAME).write_text('{"hostile": true}')
    (project / T.REPORT_NAME).write_text('{"hostile": true}')
    monkeypatch.chdir(project)
    tc = T.Toolchain.discover()
    assert T.installed_root() == tc.root == runtime
    assert find_lock(project) == runtime / T.LOCK_NAME
    assert tc.pyslang_version == "12.0.0" and tc.serena_version == "1.7.0"
    assert (tc.fixtures_dir / "demo" / "rtl" / "demo_lane.sv").is_file()
    assert (tc.fixtures_dir / "uvm" / "svw_demo_uvm_pkg.sv").is_file()


def test_SEC_03_detached_package_never_adopts_ancestor_lock(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    # This ancestor was selected by the old parents[3] source-layout assumption.
    site = tmp_path / "python3.12" / "site-packages"
    monkeypatch.setattr(T, "__file__", str(site / "svweave" / "orchestration" / "toolchain.py"))
    monkeypatch.delenv(T.ENV_ROOT, raising=False)
    (tmp_path / "python3.12").mkdir()
    shutil.copyfile(REPO / T.LOCK_NAME, tmp_path / "python3.12" / T.LOCK_NAME)
    monkeypatch.chdir(tmp_path / "python3.12")
    assert T.locate_root() is None
    with pytest.raises(SvwError) as exc:
        T.Toolchain.discover()
    assert exc.value.code is ErrorCode.MISSING_INPUT


def test_DEP_01_source_install_remains_checkout_relative(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(T.ENV_ROOT, raising=False)
    assert T.installed_root() == REPO
    assert T.Toolchain.discover().root == REPO


def test_SEC_03_explicit_shared_install_overrides_bundled_assets_and_fails_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    site = tmp_path / "site-packages"
    _stage_assets(site)
    monkeypatch.setattr(T, "__file__", str(site / "svweave" / "orchestration" / "toolchain.py"))
    shared = tmp_path / "shared-install"
    shared.mkdir()
    for name in (T.LOCK_NAME, T.REPORT_NAME):
        shutil.copyfile(REPO / name, shared / name)
    monkeypatch.setenv(T.ENV_ROOT, str(shared))
    assert T.Toolchain.discover().root == shared
    assert find_lock() == shared / T.LOCK_NAME
    # Explicit selection still outranks the env, and neither may fall back on error.
    assert T.Toolchain.discover(REPO).root == REPO
    with pytest.raises(SvwError):
        T.Toolchain.discover(tmp_path / "missing")
    monkeypatch.setenv(T.ENV_ROOT, str(tmp_path / "missing"))
    with pytest.raises(SvwError):
        T.Toolchain.discover()
    with pytest.raises(SvwError):
        find_lock()


def _require_build_backend() -> None:
    required = os.environ.get("SVWEAVE_REQUIRE_PACKAGE_BUILD") == "1"
    try:
        version = metadata.version("hatchling")
    except metadata.PackageNotFoundError:
        if required:
            pytest.fail("required artifact build needs already-installed hatchling==1.29.0")
        pytest.skip("offline artifact build requires already-installed hatchling==1.29.0")
    if version != "1.29.0":
        if required:
            pytest.fail(f"required artifact build needs hatchling==1.29.0, found {version}")
        pytest.skip(f"offline artifact build requires hatchling==1.29.0, found {version}")


def test_DEP_01_required_artifact_mode_cannot_skip(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SVWEAVE_REQUIRE_PACKAGE_BUILD", "1")

    def missing(name: str) -> str:
        raise metadata.PackageNotFoundError(name)

    monkeypatch.setattr(metadata, "version", missing)
    with pytest.raises(pytest.fail.Exception, match="already-installed hatchling"):
        _require_build_backend()
    monkeypatch.setattr(metadata, "version", lambda name: "wrong-version")
    with pytest.raises(pytest.fail.Exception, match="found wrong-version"):
        _require_build_backend()


def test_DEP_01_smoke_refuses_unverified_installer_without_executing_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    for name in ("pyproject.toml", "toolchain.lock.json"):
        shutil.copyfile(REPO / name, tmp_path / name)
    monkeypatch.setattr(ci_wheel_smoke, "REPO", tmp_path)
    monkeypatch.setattr(metadata, "version", lambda name: "1.29.0")

    def forbidden(*args: object, **kwargs: object) -> str:
        pytest.fail("a command ran before installer preflight passed")

    monkeypatch.setattr(ci_wheel_smoke, "run", forbidden)
    with pytest.raises(RuntimeError, match="pinned uv is missing"):
        ci_wheel_smoke.smoke()


def test_DEP_01_ci_requires_real_artifacts_and_scopes_evidence() -> None:
    workflow = yaml.safe_load((REPO / ".github/workflows/ci.yml").read_text())
    steps = workflow["jobs"]["fast"]["steps"]
    smoke = next(step for step in steps if "scripts/ci_wheel_smoke.py" in step.get("run", ""))
    assert "hatchling==1.29.0" in smoke["run"]
    assert "SVWEAVE_REQUIRE_PACKAGE_BUILD=1" in smoke["run"]
    assert "tests/unit/test_installed_runtime_assets.py" in smoke["run"]
    assert not smoke.get("continue-on-error") and "|| true" not in smoke["run"]
    upload = next(step for step in steps if step.get("with", {}).get("name") == "svweave-wheel-smoke")
    assert upload["if"] == "always()"
    assert upload["with"]["path"] == ".svweave/ci/wheel-smoke.json"


def test_DEP_01_actual_wheel_contains_byte_identical_runtime_assets(tmp_path: Path) -> None:
    _require_build_backend()
    source = tmp_path / "source"
    source.mkdir()
    for name in ("pyproject.toml", "LICENSE", ".gitignore", *ASSETS):
        target = source / name
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(REPO / name, target)
    shutil.copytree(REPO / "src", source / "src", ignore=shutil.ignore_patterns("__pycache__"))
    private = source / "fixtures" / "demo" / ".svweave" / "private-source.sv"
    private.parent.mkdir()
    private.write_text("PRIVATE SOURCE MUST NOT BE PACKAGED")
    subprocess.run(
        [sys.executable, "-I", "-c", "from hatchling.build import build_wheel; "
         f"build_wheel({str(tmp_path)!r})"],
        cwd=source, check=True, capture_output=True, text=True,
    )
    [wheel] = tmp_path.glob("*.whl")
    with zipfile.ZipFile(wheel) as archive:
        assert not any(".svweave" in name or "private-source" in name for name in archive.namelist())
        for source in _asset_files():
            name = f"svweave/_runtime/{source.relative_to(REPO).as_posix()}"
            assert archive.read(name) == source.read_bytes()


def test_DEP_01_actual_sdist_retains_wheel_asset_sources(tmp_path: Path) -> None:
    _require_build_backend()
    subprocess.run(
        [sys.executable, "-I", "-c", "from hatchling.build import build_sdist; "
         f"build_sdist({str(tmp_path)!r})"],
        cwd=REPO, check=True, capture_output=True, text=True,
    )
    [sdist] = tmp_path.glob("*.tar.gz")
    with tarfile.open(sdist) as archive:
        for source in _asset_files():
            suffix = "/" + source.relative_to(REPO).as_posix()
            [member] = [m for m in archive.getmembers() if m.name.endswith(suffix)]
            data = archive.extractfile(member)
            assert data is not None and data.read() == source.read_bytes()
