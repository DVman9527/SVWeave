"""GLB-01..07: shared launcher selection is never authorization (plan 7.3, 14.2, 15.1)."""

from __future__ import annotations

import json
import shlex
import sys
import threading
import tomllib
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

import pytest
from click.testing import CliRunner

from svweave.application.options import WorkspaceOptions
from svweave.application.registration import registration_preview
from svweave.application.session import Session
from svweave.cli import cli
from svweave.config.discovery import discover_workspace
from svweave.domain.errors import ErrorCode, SvwError
from svweave.launcher import launcher
from tests.conftest import Ws, profile

REPO = Path(__file__).resolve().parents[2]


def project(path: Path, *, trusted: bool = True) -> Ws:
    ws = Ws(path)
    ws.config({"p": profile()})
    if trusted:
        ws.policy()
    return ws


def test_GLB_01_two_projects_nested_discovery_and_no_selection(tmp_path: Path) -> None:
    a, b = project(tmp_path / "a"), project(tmp_path / "b")
    nested = a.root / "rtl" / "deep"
    nested.mkdir(parents=True)
    assert discover_workspace(cwd=nested, environ={}) == a.root
    assert discover_workspace(cwd=b.root, environ={}) == b.root
    with pytest.raises(SvwError, match="no workspace selected"):
        discover_workspace(cwd=tmp_path, environ={})
    # Discovery/selection alone never creates derived state.
    assert not (a.root / ".svweave").exists()
    assert not (b.root / ".svweave").exists()


def test_GLB_01_precedence_and_conflicting_hints(tmp_path: Path) -> None:
    a, b = project(tmp_path / "a"), project(tmp_path / "b")
    env = {"CLAUDE_PROJECT_DIR": str(a.root)}
    with pytest.raises(SvwError, match="ambiguous workspace"):
        discover_workspace(cwd=b.root, environ=env)
    assert discover_workspace(cwd=tmp_path, environ=env) == a.root
    assert discover_workspace(repo=b.root, cwd=tmp_path, environ=env) == b.root
    assert discover_workspace(
        cwd=b.root, environ={**env, "SVWEAVE_PROJECT_ROOT": str(b.root)}
    ) == b.root
    with pytest.raises(SvwError, match="conflicting explicit"):
        discover_workspace(repo=a.root, workspace=b.root, environ={})


@pytest.mark.parametrize("variable", ["SVWEAVE_PROJECT_ROOT", "CLAUDE_PROJECT_DIR"])
@pytest.mark.parametrize("value", ["", "relative", "/definitely/not/a/svweave/project"])
def test_GLB_01_bad_explicit_environment_never_falls_back(
    ws: Ws, variable: str, value: str
) -> None:
    with pytest.raises(SvwError):
        discover_workspace(cwd=ws.root, environ={variable: value})


def test_GLB_01_explicit_path_must_contain_manifest_not_parent(ws: Ws) -> None:
    sub = ws.root / "child"
    sub.mkdir()
    with pytest.raises(SvwError, match="no svweave.yaml"):
        discover_workspace(repo=sub, cwd=ws.root, environ={})
    with pytest.raises(SvwError, match="no fallback"):
        discover_workspace(repo=ws.root / "missing", cwd=ws.root, environ={})


@pytest.mark.parametrize("marker", ["directory", "file", "broken_symlink"])
def test_GLB_02_nested_checkout_worktree_boundary(ws: Ws, marker: str) -> None:
    nested = ws.root / "other"
    nested.mkdir()
    git = nested / ".git"
    if marker == "directory":
        git.mkdir()
    elif marker == "file":
        git.write_text("gitdir: /do/not/read")
    else:
        git.symlink_to("missing")
    with pytest.raises(SvwError, match="no workspace selected"):
        discover_workspace(cwd=nested, environ={})
    # A manifest in that checkout is selected, never the parent.
    project(nested, trusted=False)
    assert discover_workspace(cwd=nested, environ={}) == nested
    with pytest.raises(SvwError, match="trusted policy not found"):
        WorkspaceOptions(discover_workspace(cwd=nested, environ={})).load()


@pytest.mark.parametrize("kind", ["broken_symlink", "symlink", "directory", "invalid_yaml"])
def test_GLB_02_nearer_manifest_never_skipped(ws: Ws, kind: str) -> None:
    nested = ws.root / "other"
    nested.mkdir()
    manifest = nested / "svweave.yaml"
    if kind == "broken_symlink":
        manifest.symlink_to("absent")
    elif kind == "symlink":
        manifest.symlink_to(ws.root / "svweave.yaml")
    elif kind == "directory":
        manifest.mkdir()
    else:
        manifest.write_text("invalid: [yaml")
    with pytest.raises(SvwError):
        WorkspaceOptions(discover_workspace(cwd=nested, environ={})).load()


def test_GLB_02_symlink_selection_canonicalized_once(tmp_path: Path) -> None:
    a, b = project(tmp_path / "a"), project(tmp_path / "b")
    link = tmp_path / "current"
    link.symlink_to(a.root, target_is_directory=True)
    opts = WorkspaceOptions(discover_workspace(repo=link, environ={}))
    link.unlink()
    link.symlink_to(b.root, target_is_directory=True)
    assert opts.root == a.root
    assert opts.load().project_root == a.root
    loop = tmp_path / "loop"
    loop.symlink_to(loop)
    with pytest.raises(SvwError):
        discover_workspace(repo=loop, environ={})


def test_GLB_03_untrusted_launcher_fails_before_server_and_no_stdout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ws = project(tmp_path / "untrusted", trusted=False)
    calls: list[WorkspaceOptions] = []
    monkeypatch.setattr("svweave.server.serve_stdio", lambda opts, **kw: calls.append(opts))
    result = CliRunner().invoke(launcher, ["--repo", str(ws.root)])
    assert result.exit_code != 0 and result.stdout == ""
    assert "trusted policy not found" in result.stderr
    assert calls == [] and not (ws.root / ".svweave").exists()
    assert not ws.policy_path.exists()


def test_GLB_03_policy_cannot_be_supplied_by_project_or_widened(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ws = project(tmp_path / "untrusted")
    original = ws.policy_path.read_bytes()
    policy = json.loads(original)
    policy["allowed_roots"] = [str(tmp_path / "elsewhere")]
    ws.policy_path.write_text(json.dumps(policy))
    monkeypatch.setattr("svweave.server.serve_stdio", lambda *a, **kw: pytest.fail("served"))
    result = CliRunner().invoke(launcher, ["--repo", str(ws.root)])
    assert result.exit_code == 6 and result.stdout == ""
    assert json.loads(ws.policy_path.read_bytes())["allowed_roots"] == policy["allowed_roots"]
    assert not (ws.root / ".svweave").exists()


def test_GLB_04_concurrent_sessions_same_project_id_stay_bound(tmp_path: Path) -> None:
    a, b = project(tmp_path / "a"), project(tmp_path / "b")
    cwd = Path.cwd()
    barrier = threading.Barrier(2)

    def bound(ws: Ws) -> tuple[Path, Path, bytes, int]:
        root = discover_workspace(cwd=ws.root, environ={})
        session = Session(WorkspaceOptions(root), use_graph=False)
        try:
            barrier.wait(timeout=10)
            ctx = session.opts.load()
            key = session.cursor_key()
            for _ in range(8):
                assert session.opts.load().project_root == root
            assert not session.registry_created
            return ctx.state_dir, ctx.trusted_dir, key, id(session.jobs)
        finally:
            session.close()

    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(pool.map(bound, [a, b]))
    assert a.ctx().workspace_id == b.ctx().workspace_id  # ids need not be globally unique
    assert all(outcomes[0][i] != outcomes[1][i] for i in range(4))
    assert Path.cwd() == cwd


def test_GLB_04_policy_rechecked_after_binding(ws: Ws) -> None:
    opts = WorkspaceOptions(discover_workspace(repo=ws.root, environ={}))
    opts.load()
    ws.policy_path.unlink()
    with pytest.raises(SvwError, match="trusted policy not found"):
        opts.load()


def test_GLB_05_launcher_no_repo_uses_environment_and_execution_flags(
    ws: Ws, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: list[tuple[WorkspaceOptions, dict[str, Any]]] = []
    monkeypatch.setenv("SVWEAVE_PROJECT_ROOT", str(ws.root))
    monkeypatch.setattr("svweave.server.serve_stdio", lambda opts, **kw: seen.append((opts, kw)))
    result = CliRunner().invoke(launcher, ["--no-graph", "--execution-provider", "local"])
    assert result.exit_code == 0 and result.stdout == ""
    assert seen[0][0].root == ws.root
    assert seen[0][0].execution_overrides == {"provider": "local"}
    assert seen[0][1] == {"use_graph": False}


def test_GLB_05_serve_discovery_opt_in_preserves_explicit_cli(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    a, b = project(tmp_path / "a"), project(tmp_path / "b")
    seen: list[WorkspaceOptions] = []
    monkeypatch.setenv("SVWEAVE_PROJECT_ROOT", str(b.root))
    monkeypatch.setattr("svweave.server.serve_stdio", lambda opts, **kw: seen.append(opts))
    runner = CliRunner()
    for args in (["--root", str(a.root), "serve"],
                 ["--root", str(a.root), "serve", "--discover-workspace"],
                 ["serve", "--workspace", str(a.root), "--discover-workspace"]):
        assert runner.invoke(cli, args).exit_code == 0
        assert seen[-1].root == a.root
    assert runner.invoke(cli, ["serve", "--discover-workspace"]).exit_code == 0
    assert seen[-1].root == b.root


def executable(tmp_path: Path, name: str = "svweave-mcp") -> Path:
    script = tmp_path / name
    script.write_text("#!/bin/sh\nexit 99\n")
    script.chmod(0o755)
    return script


@pytest.mark.parametrize("client", ["claude", "codex"])
def test_GLB_06_registration_quotes_paths_and_does_not_apply(tmp_path: Path, client: str) -> None:
    script = executable(tmp_path, "SV Weave ' ; $() \" & 中文")
    out = registration_preview(client, executable=script, toolchain_root=REPO)
    assert out["applied"] is False and out["server_name"] == "svweave"
    assert shlex.split(out["registration_shell"]) == out["registration_argv"]
    assert out["command"] == str(script)
    assert "--repo" not in out["args"] and "--workspace" not in out["args"]
    if client == "claude":
        server = json.loads(out["client_config"])["mcpServers"]["svweave"]
        assert "--scope user" in out["registration_shell"]
    else:
        server = tomllib.loads(out["client_config"])["mcp_servers"]["svweave"]
        assert server["env_vars"] == ["SVWEAVE_PROJECT_ROOT"]
    assert server["command"] == out["command"] and server["args"] == out["args"]
    assert sorted(p.name for p in tmp_path.iterdir()) == [script.name, "xdg"]


def test_GLB_06_registration_executable_default_does_not_resolve_python_symlink(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    venv_bin = tmp_path / "venv" / "bin"
    venv_bin.mkdir(parents=True)
    py = venv_bin / "python"
    py.symlink_to(sys.executable)
    script = executable(venv_bin)
    monkeypatch.setattr(sys, "executable", str(py))
    out = registration_preview("codex", toolchain_root=REPO)
    assert out["command"] == str(script)


def test_GLB_06_registration_rejects_controls_relative_missing_and_bad_toolchain(tmp_path: Path) -> None:
    script = executable(tmp_path)
    for bad in (tmp_path / "absent", Path("svweave-mcp"), executable(tmp_path, "bad\nname")):
        with pytest.raises(SvwError):
            registration_preview("claude", executable=bad, toolchain_root=REPO)
    for bad in (tmp_path / "missing-toolchain", Path(".")):
        with pytest.raises(SvwError):
            registration_preview("claude", executable=script, toolchain_root=bad)
    with pytest.raises(SvwError, match="controls"):
        registration_preview("claude", executable=script, toolchain_root=REPO,
                             execution_profile="evil\nprofile")


def test_GLB_07_registration_cli_no_workspace_required(tmp_path: Path) -> None:
    script = executable(tmp_path)
    result = CliRunner().invoke(cli, ["--root", str(tmp_path), "mcp-config", "--client", "codex",
                                     "--executable", str(script), "--toolchain-root", str(REPO)])
    assert result.exit_code == 0, result.output
    data = json.loads(result.stdout)["data"]
    assert data["applied"] is False and data["server_name"] == "svweave"
    assert not (tmp_path / "svweave.yaml").exists()
    assert not (tmp_path / ".svweave").exists()


def test_GLB_07_launcher_no_selection_clean_stderr(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("SVWEAVE_PROJECT_ROOT", raising=False)
    monkeypatch.delenv("CLAUDE_PROJECT_DIR", raising=False)
    result = CliRunner().invoke(launcher, [])
    assert result.exit_code != 0 and result.stdout == ""
    assert "no workspace selected" in result.stderr
    assert "Traceback" not in result.stderr


def test_GLB_07_client_root_arguments_cannot_be_mcp_selection(ws: Ws) -> None:
    # Frozen MCP v1 schemas still reject arbitrary roots, and startup gains no client-root API.
    from svweave.gateway.tools import TOOLS

    for args in ({"root": str(ws.root)}, {"workspace": str(ws.root)}, {"repo": str(ws.root)}):
        with pytest.raises(SvwError) as exc:
            TOOLS["svw_status"].validate(args)
        assert exc.value.code is ErrorCode.INVALID_CONFIG


def test_GLB_02_manifest_permission_error_is_clean_failure(
    ws: Ws, monkeypatch: pytest.MonkeyPatch
) -> None:
    original = Path.is_symlink

    def denied(path: Path) -> bool:
        if path == ws.root / "svweave.yaml":
            raise PermissionError("injected inaccessible manifest")
        return original(path)

    monkeypatch.setattr(Path, "is_symlink", denied)
    result = CliRunner().invoke(launcher, ["--repo", str(ws.root)])
    assert result.exit_code == 6 and result.stdout == ""
    assert "cannot inspect workspace manifest" in result.stderr
    assert "Traceback" not in result.stderr


def test_GLB_05_discovery_rejects_manifest_selector_conflict(tmp_path: Path) -> None:
    a, b = project(tmp_path / "a"), project(tmp_path / "b")
    result = CliRunner().invoke(cli, ["--root", str(a.root), "--manifest",
                                     str(b.root / "svweave.yaml"), "serve", "--discover-workspace"])
    assert result.exit_code == 2 and result.stdout == ""
    assert "conflicts with discovered workspace" in result.stderr


@pytest.mark.parametrize("value", ["", ".", "relative/toolchain"])
def test_GLB_06_shared_toolchain_environment_cannot_be_cwd_relative(
    ws: Ws, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, value: str
) -> None:
    monkeypatch.setenv("SVWEAVE_TOOLCHAIN_ROOT", value)
    with pytest.raises(SvwError, match="must be absolute"):
        registration_preview("codex", executable=executable(tmp_path))
    result = CliRunner().invoke(launcher, ["--repo", str(ws.root)])
    assert result.exit_code == 2 and result.stdout == ""
    assert "must be absolute" in result.stderr


def test_GLB_06_bad_explicit_toolchain_no_fallback(ws: Ws, tmp_path: Path) -> None:
    loop = tmp_path / "toolchain-loop"
    loop.symlink_to(loop)
    result = CliRunner().invoke(launcher, ["--repo", str(ws.root), "--toolchain-root", str(loop)])
    assert result.exit_code == 2 and result.stdout == ""
    assert "invalid shared toolchain root" in result.stderr


def test_GLB_04_bound_canonical_directory_cannot_be_retargeted(tmp_path: Path) -> None:
    a, b = project(tmp_path / "a"), project(tmp_path / "b")
    root = discover_workspace(repo=a.root, environ={})
    opts = WorkspaceOptions(root=root, bound_manifest_dir=root)
    assert opts.load().project_root == a.root
    a.root.rename(tmp_path / "old-a")
    a.root.symlink_to(b.root, target_is_directory=True)
    with pytest.raises(SvwError, match="bound workspace path changed"):
        opts.load()
    assert opts.workspace_id() is None
    assert not (b.root / ".svweave").exists()
    assert not (b.ctx().trusted_dir / "cursor.key").exists()


def test_GLB_04_bound_manifest_cannot_become_a_symlink(tmp_path: Path) -> None:
    a, b = project(tmp_path / "a"), project(tmp_path / "b")
    opts = WorkspaceOptions(root=a.root, bound_manifest_dir=a.root)
    (a.root / "svweave.yaml").unlink()
    (a.root / "svweave.yaml").symlink_to(b.root / "svweave.yaml")
    with pytest.raises(SvwError, match="bound workspace path changed"):
        opts.load()
    assert opts.workspace_id() is None


def test_GLB_08_workspace_binding_mutation_has_precise_anchor_and_pure_tests() -> None:
    from scripts.mutation_suite import MUTATIONS

    assert {m.mid for m in MUTATIONS} == {f"M{i:02d}" for i in range(1, 28)}
    mutation = next(m for m in MUTATIONS if m.mid == "M27")
    assert len(mutation.patches) == 1
    patch = mutation.patches[0]
    assert (REPO / patch.path).read_text().count(patch.old) == 1
    assert patch.new == "        if True:  # SVW_MUTATION:M27\n"
    assert mutation.tests == [
        "tests/unit/test_global_launcher.py::test_GLB_04_bound_canonical_directory_cannot_be_retargeted",
        "tests/unit/test_global_launcher.py::test_GLB_04_bound_manifest_cannot_become_a_symlink",
        "tests/unit/test_global_launcher.py::test_GLB_04_rejected_binding_does_not_write_retargeted_event_log",
    ]



def test_GLB_04_rejected_binding_does_not_write_retargeted_event_log(tmp_path: Path) -> None:
    from svweave.server import make_gateway

    a, b = project(tmp_path / "a"), project(tmp_path / "b")
    gateway = make_gateway(WorkspaceOptions(root=a.root, bound_manifest_dir=a.root), use_graph=False)
    try:
        assert gateway.events is not None
        rel_log = gateway.events.path.relative_to(a.root)
        target_log = b.root / rel_log
        target_log.parent.mkdir(parents=True)
        target_log.write_bytes(b"sentinel\n")
        a.root.rename(tmp_path / "old-a")
        a.root.symlink_to(b.root, target_is_directory=True)
        result = gateway.call("svw_job", {"job_id": "00000000-0000-0000-0000-000000000001"})
        assert result.is_error
        assert result.envelope.error is not None
        assert result.envelope.error.code == "POLICY_DENIED"
        assert target_log.read_bytes() == b"sentinel\n"
        assert not (b.root / ".svweave" / "svweave.db").exists()
    finally:
        gateway.session.close()
