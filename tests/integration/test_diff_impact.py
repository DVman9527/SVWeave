"""DIF-I: `svweave query diff-impact` against a real build and a real git repository."""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any

import pytest

from tests.integration.demo_support import demo_ws
from tests.integration.test_cli_semantic import run

# Exercise real Git and repository policy, not unrelated runner/user filters (e.g. Git LFS).
pytestmark = [pytest.mark.integration, pytest.mark.usefixtures("isolated_git")]

LANE = "fixtures/demo/rtl/demo_lane.sv"


def git(root: Path, *args: str) -> str:
    env = {"PATH": "/usr/bin:/bin", "HOME": str(root), "GIT_CONFIG_NOSYSTEM": "1",
           "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t",
           "GIT_COMMITTER_EMAIL": "t@t"}
    r = subprocess.run(["git", "-C", str(root), *args], capture_output=True, text=True, env=env, check=True)
    return r.stdout


def repo_build(tmp_path: Path) -> tuple[Path, str]:
    """Workspace that is also a git repo; the build is made from the committed (base) state."""
    w = demo_ws(tmp_path)
    (w.root / ".gitignore").write_text(".svweave/\n")
    git(w.root, "init", "-q", "-b", "main")
    git(w.root, "add", "-A")
    git(w.root, "commit", "-q", "-m", "base")
    code, b = run(w.root, "build", "--profile", "p")
    assert code == 0
    return w.root, str(b["build_id"])


def edit(root: Path, old: str, new: str) -> None:
    p = root / LANE
    p.write_text(p.read_text().replace(old, new, 1))


def diff_impact(root: Path, bid: str, *extra: str) -> tuple[int, dict[str, Any]]:
    return run(root, "query", "diff-impact", "--build", bid, "--depth", "2", *extra)


def test_DIF_I01_a_body_change_maps_to_its_module_instances_and_dependents(tmp_path: Path) -> None:
    root, bid = repo_build(tmp_path)
    edit(root, "assign data_o  = data_i;", "assign data_o  = ~data_i;")
    code, env = diff_impact(root, bid)
    assert code == 0 and env["outcome"] == "OK", env.get("error")
    d = env["data"]
    assert d["summary"]["files_changed"] == 1 and d["summary"]["files_aligned"] == 1
    assert d["files"][0]["aligned"] is True and d["files"][0]["logical_path"] == LANE
    assert [x["qualified_name"] for x in d["affected_definitions"]] == ["demo_lane"]
    dl = d["affected_definitions"][0]
    assert dl["instances"] == 2 and [x["qualified_name"] for x in dl["dependents"]] == ["demo_top"]
    assert d["summary"]["definitions_affected"] == 1 and d["summary"]["unattributed_ranges"] == 0
    assert all(x["owner_definition_id"] == dl["declaration_id"] for x in d["affected_declarations"])
    assert d["base"] == "HEAD" and d["head"] == "working tree" and "not a risk score" in d["note"]


def test_DIF_I02_a_port_change_lists_its_connection_sites(tmp_path: Path) -> None:
    root, bid = repo_build(tmp_path)
    edit(root, "input  logic              valid_i,", "input  logic              valid_in,")
    code, env = diff_impact(root, bid)
    d = env["data"]
    assert code == 0
    ports = [p for x in d["affected_definitions"] for p in x["port_changes"]]
    assert ports == [{"port": "demo_lane.valid_i", "connection_sites": 2}]
    kinds = {x["kind"] for x in d["affected_declarations"]}
    assert "port" in kinds and "net" not in kinds  # the implicit net twin is the same entity


def test_DIF_I03_unmapped_unattributed_and_no_change(tmp_path: Path) -> None:
    root, bid = repo_build(tmp_path)
    code, env = diff_impact(root, bid)
    assert code == 0 and env["data"]["summary"]["files_changed"] == 0
    assert env["data"]["affected_definitions"] == [] and env["data"]["truncated_lists"] == []
    (root / "README.md").write_text("docs\n")
    git(root, "add", "README.md")
    edit(root, "`default_nettype none", "`default_nettype none // c")  # outside any declaration
    code, env = diff_impact(root, bid)
    d = env["data"]
    assert code == 0
    assert [u["path"] for u in d["unmapped_files"]] == ["README.md"]
    assert d["unmapped_files"][0]["reason"] == "not_in_build_snapshot"
    assert d["summary"]["unattributed_ranges"] == 1 and d["unattributed"][0]["lines"] == [1, 1]
    assert d["affected_definitions"] == []
    assert any("not part of this build" in w for w in env["warnings"])


def test_DIF_I04_head_revision_and_alignment_with_a_later_build(tmp_path: Path) -> None:
    root, bid = repo_build(tmp_path)
    edit(root, "assign valid_o = valid_i;", "assign valid_o = ~valid_i;")
    git(root, "commit", "-q", "-am", "change")
    code, env = diff_impact(root, bid, "--base", "HEAD~1", "--head", "HEAD")
    assert code == 0 and env["data"]["files"][0]["aligned"] is True  # build == base version
    assert [x["qualified_name"] for x in env["data"]["affected_definitions"]] == ["demo_lane"]
    # a build made AFTER the change no longer equals the base version: flagged, never exact
    code, b2 = run(root, "build", "--profile", "p")
    code, env2 = diff_impact(root, b2["build_id"], "--base", "HEAD~1", "--head", "HEAD")
    f = env2["data"]["files"][0]
    assert code == 0 and f["aligned"] is False and "approximate" in f["note"]
    assert env2["data"]["summary"]["files_unaligned"] == 1
    assert any("approximate" in w for w in env2["warnings"])


def test_DIF_I05_refusals_and_git_errors(tmp_path: Path) -> None:
    root, bid = repo_build(tmp_path)
    for bad in (["--base", "--output=x"], ["--base", "a..b"], ["--head", "-x"], ["--depth", "0"],
                ["--limit", "9999"]):
        code, env = diff_impact(root, bid, *bad)
        assert code == 2 and env["error"]["code"] == "INVALID_CONFIG", bad
    code, env = diff_impact(root, bid, "--base", "no_such_rev")
    assert code == 3 and env["error"]["code"] == "MISSING_INPUT"
    assert env["error"]["details"]["reason"] == "git_failed"
    code, env = diff_impact(root, bid, "--repo", str(tmp_path.parent))  # outside the allowed roots
    assert code == 6 and env["error"]["code"] == "PATH_NOT_ALLOWED"
    code, env = run(root, "query", "diff-impact", "--build", "0" * 64)
    assert code == 3 and env["error"]["code"] == "MISSING_INPUT"


def test_DIF_I06_not_a_git_repository(tmp_path: Path) -> None:
    w = demo_ws(tmp_path)
    code, b = run(w.root, "build", "--profile", "p")
    code, env = diff_impact(w.root, str(b["build_id"]))
    assert code == 3 and env["error"]["code"] == "MISSING_INPUT"
    assert env["error"]["details"]["reason"] == "git_failed"
