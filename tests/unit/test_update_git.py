"""Phase-1 update Git advisory requirements → pure tests (no HDL/backend execution).

SNP-03/9.2: staged, unstaged, untracked/ignored and NUL-safe names; Git is never authority.
SEC-02/23.1: no executable filters/hooks/host config, no out-of-policy repository metadata.
API-01: unavailable Git/ref/HEAD, bounded wall time/output and explicit truncation.
"""

from __future__ import annotations

import os
import subprocess
import sys
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from svweave.application import update_git
from tests.conftest import Ws


@pytest.fixture
def repo(ws: Ws, isolated_git: Path) -> tuple[Ws, Callable[..., bytes]]:
    def git(*args: str) -> bytes:
        return subprocess.run(
            [
                str(isolated_git),
                "-C",
                str(ws.root),
                "-c",
                "core.hooksPath=/dev/null",
                "-c",
                "commit.gpgsign=false",
                *args,
            ],
            env={**update_git._environment(), "PATH": os.environ["PATH"]},
            check=True,
            capture_output=True,
        ).stdout

    git("init", "--template=", "-q")
    git("add", ".")
    git("-c", "user.name=test", "-c", "user.email=test@example.invalid", "commit", "-qm", "base")
    return ws, git


def _unavailable(report: dict[str, Any], reason: str) -> None:
    assert report["status"] == "UNAVAILABLE"
    assert report["reason"] == reason
    assert report["advisory_only"] is True
    assert report["counts"] is None  # Unavailable is NOT a zero-change result.
    assert report["paths"] == []


def test_SNP_03_update_git_staged_unstaged_untracked_renamed_and_nul_names(
    repo: tuple[Ws, Callable[..., bytes]],
) -> None:
    ws, git = repo
    ws.write("staged.sv")
    ws.write("old name\nwith\ttabs.sv")
    git("add", ".")
    git("-c", "user.name=test", "-c", "user.email=test@example.invalid", "commit", "-qm", "files")
    git("mv", "old name\nwith\ttabs.sv", "new name\nwith\ttabs.sv")
    ws.write("staged.sv", "module changed; endmodule\n")
    git("add", "staged.sv")
    ws.write("rtl/a.sv", "module a; wire x; endmodule\n")
    ws.write("new\nfile.sv")
    report = update_git.git_advisory(ws.ctx())
    assert report["status"] == "AVAILABLE"
    assert report["advisory_only"] is True
    changes = {(r["path"], r["status"], r["source"]) for r in report["paths"]}
    assert ("staged.sv", "M", "tracked") in changes
    assert ("rtl/a.sv", "M", "tracked") in changes
    assert ("old name\nwith\ttabs.sv", "D", "tracked") in changes
    assert ("new name\nwith\ttabs.sv", "A", "tracked") in changes
    assert ("new\nfile.sv", "?", "untracked") in changes
    assert not report["truncated"]


def test_SNP_03_update_git_staged_cancelled_by_worktree_is_not_hidden(
    repo: tuple[Ws, Callable[..., bytes]],
) -> None:
    ws, git = repo
    original = (ws.root / "rtl/a.sv").read_bytes()
    ws.write("rtl/a.sv", "module changed; endmodule\n")
    git("add", "rtl/a.sv")
    (ws.root / "rtl/a.sv").write_bytes(original)
    report = update_git.git_advisory(ws.ctx())
    assert report["status"] == "AVAILABLE"
    assert report["paths"] == [{"path": "rtl/a.sv", "status": "M", "source": "tracked"}]


def test_SNP_03_update_git_ignored_inputs_are_explicitly_not_authoritative(
    repo: tuple[Ws, Callable[..., bytes]],
) -> None:
    ws, git = repo
    ws.write(".gitignore", "ignored.svh\n")
    git("add", ".gitignore")
    git("-c", "user.name=test", "-c", "user.email=test@example.invalid", "commit", "-qm", "ignore")
    ws.write("ignored.svh", "`define VALUE 2\n")
    report = update_git.git_advisory(ws.ctx())
    assert report["status"] == "AVAILABLE"
    assert report["paths"] == [{"path": "ignored.svh", "status": "?", "source": "untracked"}]
    assert report["advisory_only"]
    assert any("Ignored files" in text for text in report["limitations"])
    assert any("ignore rules are not applied" in text for text in report["limitations"])
    assert any(
        "content verification remain authoritative" in text for text in report["limitations"]
    )


@pytest.mark.parametrize("driver", ["clean", "smudge", "process"])
def test_SEC_02_update_git_refuses_executable_filters_without_running(
    repo: tuple[Ws, Callable[..., bytes]],
    driver: str,
) -> None:
    ws, git = repo
    git("config", f"filter.evil.{driver}", "touch filter-ran; cat")
    ws.write(".gitattributes", "*.sv filter=evil\n")
    ws.write("rtl/a.sv", "module changed; endmodule\n")
    _unavailable(update_git.git_advisory(ws.ctx()), "git_filters_configured")
    assert not (ws.root / "filter-ran").exists()


def test_SEC_02_update_git_disables_hooks_fsmonitor_external_diff_and_textconv(
    repo: tuple[Ws, Callable[..., bytes]],
) -> None:
    ws, git = repo
    marker = ws.root / "executed"
    script = ws.root / ".git" / "evil"
    script.write_text(f"#!/bin/sh\ntouch '{marker}'\n")
    script.chmod(0o755)
    for hook in ("post-index-change", "post-checkout", "reference-transaction"):
        hook_path = script.parent / hook
        hook_path.write_bytes(script.read_bytes())
        hook_path.chmod(0o755)
    git("config", "core.fsmonitor", str(script))
    git("config", "core.hooksPath", str(script.parent))
    git("config", "diff.external", str(script))
    git("config", "diff.evil.command", str(script))
    git("config", "diff.evil.textconv", str(script))
    ws.write(".gitattributes", "*.sv diff=evil\n")
    ws.write("rtl/a.sv", "module changed; endmodule\n")
    report = update_git.git_advisory(ws.ctx())
    assert report["status"] == "AVAILABLE"
    assert not marker.exists()


def test_SEC_02_update_git_ignores_host_config_and_inherited_git_environment(
    repo: tuple[Ws, Callable[..., bytes]],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ws, _ = repo
    config = tmp_path / "host-config"
    contents = '[filter "evil"]\n clean = touch host-filter-ran; cat\n'
    config.write_text(contents)
    for name in ("GIT_CONFIG_SYSTEM", "GIT_CONFIG_GLOBAL"):
        monkeypatch.setenv(name, str(config))
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "0")
    monkeypatch.setenv("GIT_DIR", str(tmp_path / "elsewhere"))
    monkeypatch.setenv("GIT_WORK_TREE", str(tmp_path))
    monkeypatch.setenv("GIT_CONFIG_COUNT", "1")
    monkeypatch.setenv("GIT_CONFIG_KEY_0", "filter.evil.clean")
    monkeypatch.setenv("GIT_CONFIG_VALUE_0", "touch environment-filter-ran; cat")
    ws.write(".gitattributes", "*.sv filter=evil\n")
    ws.write("rtl/a.sv", "module changed; endmodule\n")
    assert update_git.git_advisory(ws.ctx())["status"] == "AVAILABLE"
    assert config.read_text() == contents
    assert not (ws.root / "host-filter-ran").exists()
    assert not (ws.root / "environment-filter-ran").exists()


def test_SEC_02_update_git_does_not_follow_config_include(
    repo: tuple[Ws, Callable[..., bytes]],
    tmp_path: Path,
) -> None:
    ws, git = repo
    outside = tmp_path / "outside-config"
    outside.write_text('[filter "evil"]\n clean = touch filter-ran; cat\n')
    git("config", "include.path", str(outside))
    _unavailable(update_git.git_advisory(ws.ctx()), "git_config_include_unsupported")
    assert not (ws.root / "filter-ran").exists()


def test_SEC_02_update_git_rejects_metadata_and_source_symlink_escapes(
    repo: tuple[Ws, Callable[..., bytes]],
    tmp_path: Path,
) -> None:
    ws, _ = repo
    outside = tmp_path / "outside.sv"
    outside.write_text("private content\n")
    source = ws.root / "rtl/a.sv"
    source.unlink()
    source.symlink_to(outside)
    _unavailable(update_git.git_advisory(ws.ctx()), "git_path_not_allowed")
    source.unlink()
    ws.write("rtl/a.sv")
    (ws.root / ".git" / "objects" / "external").symlink_to(tmp_path)
    _unavailable(update_git.git_advisory(ws.ctx()), "git_metadata_indirection_unsupported")


@pytest.mark.parametrize(
    "base", ["--help", "HEAD:path", "HEAD\n", "x..y", "HEAD^{tree}", "missing-branch"]
)
def test_API_01_update_git_invalid_base_is_unavailable_not_unchanged(
    repo: tuple[Ws, Callable[..., bytes]],
    base: str,
) -> None:
    ws, _ = repo
    _unavailable(update_git.git_advisory(ws.ctx(), base=base), "invalid_base")


def test_API_01_update_git_handles_non_repository_unborn_and_missing_git(
    ws: Ws,
    isolated_git: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _unavailable(update_git.git_advisory(ws.ctx()), "not_git_repository")
    subprocess.run([str(isolated_git), "-C", str(ws.root), "init", "--template=", "-q"], check=True)
    _unavailable(update_git.git_advisory(ws.ctx()), "head_unavailable")
    monkeypatch.setattr(update_git.shutil, "which", lambda *args, **kwargs: None)
    _unavailable(update_git.git_advisory(ws.ctx()), "git_missing")


def test_API_01_update_git_output_truncation_is_explicit(
    repo: tuple[Ws, Callable[..., bytes]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ws, _ = repo
    for i in range(5):
        ws.write(f"new{i}.sv")
    monkeypatch.setattr(update_git, "MAX_GIT_PATHS", 2)
    report = update_git.git_advisory(ws.ctx())
    assert report["status"] == "AVAILABLE" and report["truncated"]
    assert len(report["paths"]) == 2
    assert report["counts"] == {"tracked": 0, "untracked": 5, "records": 5}


@pytest.mark.parametrize(
    "script", ["import time; time.sleep(30)", "import os,time; os.write(2,b'x'); time.sleep(30)"]
)
def test_SEC_02_update_git_silent_and_stderr_only_children_have_wall_deadline(script: str) -> None:
    started = time.monotonic()
    budget = update_git._Budget(started + 0.15, 1024)
    with pytest.raises(update_git._Unavailable, match="git_timeout"):
        update_git._bounded_run([sys.executable, "-c", script], update_git._environment(), budget)
    assert time.monotonic() - started < 2.0


@pytest.mark.parametrize("fd", [1, 2])
def test_SEC_02_update_git_caps_stdout_and_stderr(fd: int) -> None:
    script = f"import os; os.write({fd}, b'x' * 65536)"
    budget = update_git._Budget(time.monotonic() + 3, 1024)
    with pytest.raises(update_git._Unavailable, match="git_output_limit"):
        update_git._bounded_run([sys.executable, "-c", script], update_git._environment(), budget)


def test_API_01_update_git_runner_failures_are_unavailable_advisories(
    repo: tuple[Ws, Callable[..., bytes]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ws, _ = repo
    monkeypatch.setattr(update_git, "GIT_TIMEOUT_S", 0)
    _unavailable(update_git.git_advisory(ws.ctx()), "git_timeout")
    monkeypatch.setattr(update_git, "GIT_TIMEOUT_S", 3)
    monkeypatch.setattr(update_git, "MAX_GIT_BYTES", 1)
    _unavailable(update_git.git_advisory(ws.ctx()), "git_output_limit")


def test_SEC_02_update_git_late_filter_and_include_configuration_is_never_consumed(
    repo: tuple[Ws, Callable[..., bytes]],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ws, _ = repo
    original = update_git._bounded_run
    changed = False

    def race(*args: Any, **kwargs: Any) -> tuple[int, bytes]:
        nonlocal changed
        result = original(*args, **kwargs)
        if not changed:
            changed = True
            with (ws.root / ".git/config").open("a") as config:
                config.write('[filter "late"]\n clean = touch late-filter-ran; cat\n')
                config.write(f"[include]\n path = {tmp_path / 'outside-config'}\n")
            ws.write(".gitattributes", "*.sv filter=late\n")
            (ws.root / ".git/objects/info/alternates").write_text(str(tmp_path / "outside") + "\n")
        return result

    monkeypatch.setattr(update_git, "_bounded_run", race)
    ws.write("rtl/a.sv", "module changed; endmodule\n")
    report = update_git.git_advisory(ws.ctx())
    assert report["status"] == "AVAILABLE", report
    assert not (ws.root / "late-filter-ran").exists()


def test_SEC_02_update_git_object_replacement_cannot_redirect_opened_object_inputs(
    repo: tuple[Ws, Callable[..., bytes]],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ws, _ = repo
    original = update_git._bounded_run
    changed = False
    outside = tmp_path / "outside-object"
    outside.write_bytes(b"not a git object: must never be consumed")

    def race(*args: Any, **kwargs: Any) -> tuple[int, bytes]:
        nonlocal changed
        if not changed:
            changed = True
            for obj in (ws.root / ".git/objects").glob("??/*"):
                obj.unlink()
                obj.symlink_to(outside)
        return original(*args, **kwargs)

    monkeypatch.setattr(update_git, "_bounded_run", race)
    ws.write("new.sv")
    report = update_git.git_advisory(ws.ctx())
    assert report["status"] == "AVAILABLE", report
    assert {r["path"] for r in report["paths"]} == {"new.sv"}
    assert outside.read_bytes() == b"not a git object: must never be consumed"


def test_SEC_02_update_git_private_worktree_never_reads_attributes_or_ignore_files(
    repo: tuple[Ws, Callable[..., bytes]],
) -> None:
    ws, _ = repo
    os.mkfifo(ws.root / ".gitattributes")
    os.mkfifo(ws.root / ".gitignore")
    started = time.monotonic()
    report = update_git.git_advisory(ws.ctx())
    assert report["status"] == "AVAILABLE", report
    assert time.monotonic() - started < 2
    assert {r["path"] for r in report["paths"]} == {".gitattributes", ".gitignore"}


@pytest.mark.parametrize(
    ("cap", "reason"),
    [
        ("MAX_OBJECT_FILES", "git_object_file_limit"),
        ("MAX_METADATA_ENTRIES", "git_metadata_limit"),
        ("MAX_WORKTREE_ENTRIES", "git_worktree_inventory_limit"),
        ("MAX_DIRECTORY_DEPTH", "git_metadata_limit"),
    ],
)
def test_API_01_update_git_bounds_object_fds_and_directory_inventories(
    repo: tuple[Ws, Callable[..., bytes]],
    monkeypatch: pytest.MonkeyPatch,
    cap: str,
    reason: str,
) -> None:
    ws, _ = repo
    monkeypatch.setattr(update_git, cap, 0)
    report = update_git.git_advisory(ws.ctx())
    _unavailable(report, reason)
    assert report["elapsed_s"] >= 0
    assert report["caps"]["wall_time_s"] == update_git.GIT_TIMEOUT_S


def test_API_01_update_git_non_utf8_names_are_explicitly_unavailable(
    repo: tuple[Ws, Callable[..., bytes]],
) -> None:
    ws, _ = repo
    fd = os.open(os.fsencode(ws.root) + b"/invalid-\xff.sv", os.O_CREAT | os.O_WRONLY, 0o600)
    os.close(fd)
    _unavailable(update_git.git_advisory(ws.ctx()), "git_filename_encoding_unsupported")


@pytest.mark.parametrize(
    "name", ["commondir", "objects/info/alternates", "objects/info/http-alternates"]
)
def test_SEC_02_update_git_refuses_repository_indirection(
    repo: tuple[Ws, Callable[..., bytes]],
    name: str,
) -> None:
    ws, _ = repo
    (ws.root / ".git" / name).write_text("/outside\n")
    _unavailable(update_git.git_advisory(ws.ctx()), "git_metadata_indirection_unsupported")


def test_API_01_update_git_does_not_discover_ancestor_repositories(
    repo: tuple[Ws, Callable[..., bytes]],
) -> None:
    ws, _ = repo
    child = Ws(ws.root / "child")
    child.root.mkdir()
    child.config(
        {
            "p": {
                "cwd": ".",
                "source_inputs": [{"kind": "file", "path": "a.sv"}],
                "compilation_units": {"mode": "single"},
                "top_modules": ["a"],
            }
        }
    )
    child.policy()
    child.write("a.sv")
    _unavailable(update_git.git_advisory(child.ctx()), "not_git_repository")


@pytest.mark.parametrize("target", ["HEAD", "refs/heads/main"])
def test_SEC_02_update_git_malformed_symbolic_refs_are_unavailable(
    repo: tuple[Ws, Callable[..., bytes]],
    tmp_path: Path,
    target: str,
) -> None:
    ws, _ = repo
    outside = tmp_path / "outside-ref"
    os.mkfifo(outside)
    if target != "HEAD":
        (ws.root / ".git/HEAD").write_text("ref: refs/heads/main\n")
    (ws.root / ".git" / target).write_text("ref: " + str(outside) + "\n")
    started = time.monotonic()
    _unavailable(update_git.git_advisory(ws.ctx()), "head_unavailable")
    assert time.monotonic() - started < 2


def test_SNP_03_update_git_opened_packed_objects_and_historical_base(
    repo: tuple[Ws, Callable[..., bytes]],
) -> None:
    ws, git = repo
    old = git("rev-parse", "HEAD").strip().decode()
    ws.write("committed.sv")
    git("add", "committed.sv")
    git("-c", "user.name=test", "-c", "user.email=test@example.invalid", "commit", "-qm", "second")
    git("-c", "pack.threads=1", "repack", "-ad")
    report = update_git.git_advisory(ws.ctx(), base="HEAD~1")
    assert report["status"] == "AVAILABLE", report
    assert report["base"] == old
    assert {r["path"] for r in report["paths"]} == {"committed.sv"}
    assert list((ws.root / ".git/objects/pack").glob("*.pack"))


def test_SNP_03_update_git_sha256_repository(ws: Ws, isolated_git: Path) -> None:
    def git(*args: str) -> bytes:
        return subprocess.run(
            [
                str(isolated_git),
                "-C",
                str(ws.root),
                "-c",
                "core.hooksPath=/dev/null",
                "-c",
                "commit.gpgsign=false",
                *args,
            ],
            check=True,
            capture_output=True,
            env=update_git._environment(),
        ).stdout

    git("init", "--object-format=sha256", "--template=", "-q")
    git("add", ".")
    git("-c", "user.name=test", "-c", "user.email=test@example.invalid", "commit", "-qm", "base")
    ws.write("new.sv")
    report = update_git.git_advisory(ws.ctx())
    assert report["status"] == "AVAILABLE", report
    assert len(report["head"]) == len(report["base"]) == 64
    assert report["paths"] == [{"path": "new.sv", "status": "?", "source": "untracked"}]


def test_SEC_02_update_git_deadline_reaps_descendant_holding_pipes() -> None:
    script = "import os,time\nif os.fork(): os._exit(0)\ntime.sleep(30)\n"
    started = time.monotonic()
    budget = update_git._Budget(started + 0.15, 1024)
    with pytest.raises(update_git._Unavailable, match="git_timeout"):
        update_git._bounded_run([sys.executable, "-c", script], update_git._environment(), budget)
    assert time.monotonic() - started < 2


def test_SEC_02_update_git_metadata_names_consume_budget_without_subprocess_output(ws: Ws) -> None:
    # No Git process or control-file content is read: retained path names alone hit the cap.
    refs = ws.root / ".git/refs/heads"
    refs.mkdir(parents=True)
    (refs / ("long-" + "x" * 180)).write_text("0" * 40 + "\n")
    budget = update_git._Budget(time.monotonic() + 3, 100)
    with pytest.raises(update_git._Unavailable, match="git_output_limit"):
        update_git._metadata(ws.ctx(), budget)


def test_SEC_02_update_git_untracked_names_consume_budget_without_subprocess_output(ws: Ws) -> None:
    tracked = {path.relative_to(ws.root).as_posix() for path in ws.root.rglob("*") if path.is_file()}
    path = "long-" + "x" * 180 + ".sv"
    ws.write(path)
    # Existing tracked files consume no retained-hint bytes. This one long name must be refused
    # before it is appended, independently of Git subprocess/control-file byte accounting.
    budget = update_git._Budget(time.monotonic() + 3, len(path.encode("utf-8")) - 1)
    with pytest.raises(update_git._Unavailable, match="git_output_limit"):
        update_git._untracked(ws.ctx(), tracked, budget)
