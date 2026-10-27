"""Shared test helpers: build throw-away workspaces from fixtures or inline text."""

from __future__ import annotations

import json
import os
import shutil
from pathlib import Path
from typing import Any

import pytest
import yaml

from svweave.config.context import WorkspaceContext
from svweave.config.trusted import policy_path_for

pytest_plugins = ["tests.required_backends"]

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"


def pytest_configure() -> None:
    """An inherited worker opt-out must never silently weaken real qualification tests.

    Deliberate degraded-feature controls use explicit arguments inside mandatory outer isolation;
    this only rejects the host environment fallback, before any test or backend fixture runs.
    """
    if os.environ.get("SVWEAVE_ALLOW_DEGRADED_WORKER") == "1":
        raise pytest.UsageError(
            "SVWEAVE_ALLOW_DEGRADED_WORKER=1 is forbidden in the test runner; "
            "native tests require mandatory sandboxing"
        )


def profile(**over: Any) -> dict[str, Any]:
    p: dict[str, Any] = {
        "cwd": ".",
        "source_inputs": [
            {"kind": "filelist", "path": "lists/main.f", "dialect": "svweave-filelist-v1"}
        ],
        "compilation_units": {"mode": "single"},
        "top_modules": ["a"],
    }
    p.update(over)
    return p


def manifest_dict(profiles: dict[str, Any], **over: Any) -> dict[str, Any]:
    m: dict[str, Any] = {
        "schema_version": "1.0",
        "project": {"id": "t", "root": ".", "default_profile": next(iter(profiles))},
        "source_roots": [
            {"id": "project", "path": ".", "include": ["**"], "exclude": ["**/out/**"]}
        ],
        "profiles": profiles,
    }
    m.update(over)
    return m


POLICY: dict[str, Any] = {
    "schema_version": "1.0",
    "allowed_roots": ["/placeholder/abs/root"],
    "writable_paths": [],
    "approval_required": True,
    "network_enabled": False,
    "editing_enabled": False,
}


class Ws:
    def __init__(self, root: Path) -> None:
        self.root = root

    def write(self, rel: str, text: str = "module m; endmodule\n") -> Path:
        p = self.root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8")
        return p

    def config(self, profiles: dict[str, Any], **over: Any) -> None:
        self.write("svweave.yaml", yaml.safe_dump(manifest_dict(profiles, **over), sort_keys=False))

    @property
    def policy_path(self) -> Path:
        return policy_path_for(self.root / "svweave.yaml")

    def policy(self, **over: Any) -> None:
        """Write the TRUSTED policy (outside the repo tree, under XDG_CONFIG_HOME)."""
        d = {**POLICY, "allowed_roots": [str(self.root.resolve())], **over}
        self.policy_path.parent.mkdir(parents=True, exist_ok=True)
        self.policy_path.write_text(json.dumps(d))

    def local(self, mappings: list[dict[str, str]]) -> None:
        self.write(
            "svweave.local.yaml",
            yaml.safe_dump({"schema_version": "1.0", "root_mappings": mappings}),
        )

    def ctx(self) -> WorkspaceContext:
        return WorkspaceContext.load(self.root)


@pytest.fixture
def ws(tmp_path: Path) -> Ws:
    """Copy of fixtures/filelists/proj with a default config + trusted policy."""
    root = tmp_path / "ws"
    shutil.copytree(FIXTURES / "filelists" / "proj", root)
    w = Ws(root)
    w.config({"p": profile()})
    w.policy()
    return w


@pytest.fixture
def neg(tmp_path: Path) -> Ws:
    root = tmp_path / "neg"
    shutil.copytree(FIXTURES / "negative" / "proj_neg", root)
    w = Ws(root)
    w.config({"p": profile(source_inputs=[{"kind": "filelist", "path": "lists/missing_src.f"}])})
    w.policy()
    return w


@pytest.fixture(autouse=True)
def _isolated_config_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Trusted policies live under XDG_CONFIG_HOME; never touch the real one."""
    xdg = tmp_path / "xdg"
    xdg.mkdir()
    monkeypatch.setenv("XDG_CONFIG_HOME", str(xdg))
    return xdg


@pytest.fixture
def isolated_git(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Use real Git with only repository configuration; never modify runner/user config."""
    from tests.git_support import isolated_git_launcher

    launcher = isolated_git_launcher(tmp_path / "git-bin")
    monkeypatch.setenv("PATH", str(launcher.parent), prepend=os.pathsep)
    return launcher
