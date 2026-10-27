"""Test-only Git isolation retains real Git and repository-local filter visibility."""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

from svweave.application.diff_impact import _git


def test_SEC_02_git_fixture_ignores_host_filters_but_retains_repository_filters(
    tmp_path: Path, isolated_git: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    home = tmp_path / "home"
    home.mkdir()
    global_config = home / ".gitconfig"
    system_config = tmp_path / "gitconfig-system"
    global_config.write_text('[filter "global"]\n clean = forbidden-global\n')
    system_config.write_text('[filter "system"]\n process = forbidden-system\n')
    env = {**os.environ, "HOME": str(home), "GIT_CONFIG_SYSTEM": str(system_config),
           "GIT_CONFIG_GLOBAL": str(global_config), "GIT_CONFIG_NOSYSTEM": "0"}
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run([str(isolated_git), "-C", str(repo), "init", "-q"], env=env, check=True)
    args = ["-C", str(repo), "config", "--get-regexp", r"^filter\..*\.(clean|process)$"]
    # Control: the real executable, without our launcher, sees both injected host configs.
    real_git = shutil.which("git", path=os.pathsep.join(os.environ["PATH"].split(os.pathsep)[1:]))
    assert real_git is not None
    control = subprocess.run([real_git, *args], env=env, check=True, capture_output=True, text=True)
    assert "filter.global.clean" in control.stdout and "filter.system.process" in control.stdout
    isolated = subprocess.run([str(isolated_git), *args], env=env, capture_output=True, text=True)
    assert isolated.returncode == 1 and not isolated.stdout
    subprocess.run([str(isolated_git), "-C", str(repo), "config", "filter.evil.clean", "forbidden-local"], env=env, check=True)
    monkeypatch.setenv("HOME", str(home))
    # Exercise production's subprocess environment allowlist, not a fake Git adapter.
    assert _git(repo, args[2:]).decode().strip() == "filter.evil.clean forbidden-local"
    assert global_config.read_text() == '[filter "global"]\n clean = forbidden-global\n'
    assert system_config.read_text() == '[filter "system"]\n process = forbidden-system\n'


def test_SEC_02_isolated_git_still_refuses_repository_filter_before_execution(
    tmp_path: Path, isolated_git: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from contextlib import nullcontext
    from types import SimpleNamespace

    from svweave.application import diff_impact
    from svweave.application.options import WorkspaceOptions
    from svweave.domain.errors import ErrorCode, SvwError

    repo = tmp_path / "project"
    repo.mkdir()

    def git(*args: str) -> None:
        subprocess.run([str(isolated_git), "-C", str(repo), *args], check=True, capture_output=True)

    git("init", "-q")
    source = repo / "a.sv"
    source.write_text("module a; endmodule\n")
    git("add", "a.sv")
    git("-c", "user.name=test", "-c", "user.email=test@example.invalid", "commit", "-qm", "base")
    marker = repo / "filter-ran"
    git("config", "filter.evil.clean", "touch filter-ran; cat")
    (repo / ".gitattributes").write_text("*.sv filter=evil\n")
    source.write_text("module a; wire b; endmodule\n")
    # Unit-only session: denial must occur before querying any semantic/build rows.
    context = SimpleNamespace(project_root=repo, path_policy=SimpleNamespace(check=lambda *_: ("", repo)))
    monkeypatch.setattr(diff_impact, "_session", lambda *_: nullcontext(SimpleNamespace(ctx=context)))
    with pytest.raises(SvwError) as raised:
        diff_impact.diff_impact(WorkspaceOptions(root=repo), "unused-build", "request")
    assert raised.value.code is ErrorCode.POLICY_DENIED
    assert raised.value.details["reason"] == "git_filters_configured"
    assert raised.value.details["filters"] == ["filter.evil.clean"]
    assert not marker.exists()
