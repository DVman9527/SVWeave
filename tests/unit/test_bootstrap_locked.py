"""scripts/bootstrap_locked.py: behaviour that needs neither the network nor the pinned toolchain."""

from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path
from types import ModuleType

import pytest

REPO = Path(__file__).resolve().parents[2]


def load() -> ModuleType:
    spec = importlib.util.spec_from_file_location("bootstrap_locked", REPO / "scripts/bootstrap_locked.py")
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_resolve_uv_fails_before_anything_is_downloaded(tmp_path: Path) -> None:
    bs = load()
    with pytest.raises(SystemExit) as e:
        bs.resolve_uv(str(tmp_path / "no-such-uv"))
    assert "uv not found" in str(e.value) and "Nothing was downloaded" in str(e.value)


def test_resolve_uv_accepts_an_explicit_executable(tmp_path: Path) -> None:
    bs = load()
    fake = tmp_path / "uv"
    fake.write_text("#!/bin/sh\n")
    fake.chmod(0o755)
    assert bs.resolve_uv(str(fake)) == str(fake)


@pytest.mark.parametrize("kind", ["directory", "not-executable", "dev-null"])
def test_resolve_uv_rejects_paths_that_cannot_run(tmp_path: Path, kind: str) -> None:
    """An existing path is not enough: a directory or a non-executable file would only fail after the
    downloads had started."""
    bs = load()
    if kind == "directory":
        cand = tmp_path
    elif kind == "dev-null":
        cand = Path("/dev/null")
    else:
        cand = tmp_path / "uv"
        cand.write_text("x")
        cand.chmod(0o644)
    with pytest.raises(SystemExit) as e:
        bs.resolve_uv(str(cand))
    assert "Nothing was downloaded" in str(e.value)


def test_serena_clone_failure_is_explained_without_putting_the_path_in_the_error(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    bs = load()
    missing = tmp_path / "not-a-checkout"
    c = {"commit": "0" * 40, "source_url": "https://example.invalid/serena"}
    with pytest.raises(bs.LockError) as e:
        bs._checkout_serena(c, tmp_path / "src", missing)
    assert "--serena-source" in str(e.value) and "visible to this process" in str(e.value)
    assert str(missing) not in str(e.value)  # the error ends up in the JSON report
    assert str(missing) in capsys.readouterr().err  # the user still sees which path failed


def _bare_venv(tmp_path: Path) -> str:
    venv = tmp_path / "venv"
    subprocess.run([sys.executable, "-m", "venv", "--without-pip", str(venv)], check=True)
    return str(venv / "bin/python")


def test_link_self_makes_svweave_importable_in_a_bare_venv(tmp_path: Path) -> None:
    bs = load()
    py = _bare_venv(tmp_path)
    report: dict = {"components": {}, "errors": []}
    bs.link_self(py, report)
    assert report["errors"] == [] and report["components"]["svweave"] == {"installed": "linked-src"}
    assert subprocess.run([py, "-I", "-c", "import svweave"], capture_output=True).returncode == 0
    script = Path(py).parent / "svweave"
    assert script.is_file() and script.stat().st_mode & 0o111
    assert script.read_text().startswith(f"#!{py}\n")
    # a second run leaves an existing launcher alone
    script.write_text("#!custom\n")
    bs.link_self(py, {"components": {}, "errors": []})
    assert script.read_text() == "#!custom\n"


def test_link_self_is_a_noop_without_a_source_tree(tmp_path: Path) -> None:
    """The DEP-01 clean room holds scripts/ only: nothing to link, and that is not an error."""
    bs = load()
    bs.REPO = tmp_path
    report: dict = {"components": {}, "errors": []}
    bs.link_self(_bare_venv(tmp_path), report)
    assert report["errors"] == [] and report["components"]["svweave"]["installed"] is None
