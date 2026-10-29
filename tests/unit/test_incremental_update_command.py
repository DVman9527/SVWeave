"""UPD-01..08: Phase-1 application contract, with a unit-only semantic worker boundary."""

from __future__ import annotations

import inspect
import json
from typing import Any

import pytest
from click.testing import CliRunner

from svweave.application import build, update
from svweave.application.options import WorkspaceOptions
from svweave.cli import cli
from svweave.domain.cursor import MAX_RESPONSE_BYTES
from svweave.domain.envelope import Envelope, Outcome
from svweave.domain.errors import ErrorCode, SvwError
from svweave.domain.ids import SEMANTIC_SCHEMA_VERSION, sha256_hex
from svweave.domain.states import JobLifecycle
from svweave.storage.db import Database
from svweave.storage.lock import WorkspaceBusy, build_lock, workspace_lock
from tests.conftest import Ws, profile


@pytest.fixture
def semantic_jobs(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    """Only the semantic-job boundary is mocked; snapshot, DB identities and cache are real."""
    calls: list[dict[str, Any]] = []

    def job(ctx, db, nm, rec, bid, tc_id, tc_parts, lock_path, run, rid, handle, test_fault, publish):
        calls.append({"build_id": bid, "snapshot_id": rec.snapshot_id, "profile": nm.profile})
        partial = test_fault == "partial"
        db.stage_build({
            "build_id": bid, "workspace_id": ctx.workspace_id, "profile": nm.profile,
            "snapshot_id": rec.snapshot_id, "config_id": nm.config_id,
            "toolchain_id": tc_id, "semantic_schema_version": SEMANTIC_SCHEMA_VERSION,
            "completeness": "PARTIAL" if partial else "COMPLETE",
            "diagnostic_outcome": "HAS_ERRORS" if partial else "NO_ERRORS",
        }, {"instances": [{"instance_id": "instance-" + bid, "build_id": bid, "path": "top"}]},
            publish=publish)
        db.finish_job(run.job_id, JobLifecycle.SUCCEEDED)
        return build._stored_envelope(ctx, db, bid, rid, reused=False, job_id=run.job_id)

    monkeypatch.setattr(build, "_run_job", job)
    return calls


def configure(ws: Ws) -> WorkspaceOptions:
    ws.config({"p": profile(source_inputs=[{"kind": "file", "path": "rtl/a.sv"}], include_dirs=["inc_b"])})
    return WorkspaceOptions(root=ws.root)


def test_UPD_01_noop_reuses_snapshot_and_exact_build_without_new_run(
    ws: Ws, semantic_jobs: list[dict[str, Any]],
) -> None:
    opts = configure(ws)
    first = update.run_update(opts, "p", "first", git=False)
    runs = set((ws.root / ".svweave/runs").iterdir())
    second = update.run_update(opts, "p", "second", git=False)
    report = second.data["update"]
    assert first.build_id == second.build_id and len(semantic_jobs) == 1
    assert second.data["reused"] and report["semantic_action"] == "exact_build_reuse"
    assert report["materialization"]["noop_snapshot_reused"]
    assert report["materialization"]["files_copied"] == report["materialization"]["files_cloned"] == 0
    assert report["changes"]["total"] == 0 and report["baseline_snapshot_id"] == first.snapshot_id
    assert set((ws.root / ".svweave/runs").iterdir()) == runs


def test_UPD_02_dirty_header_new_candidate_and_removal_require_whole_profile(
    ws: Ws, semantic_jobs: list[dict[str, Any]],
) -> None:
    opts = configure(ws)
    before = update.run_update(opts, "p", "before", git=False)
    historical = (ws.root / ".svweave/snapshots" / str(before.snapshot_id) / "files/inc_b/common.svh").read_bytes()
    ws.write("inc_b/common.svh", "`define CHANGED 2\n")
    ws.write("inc_b/untracked.svh", "`define NEW 1\n")
    (ws.root / "rtl/b.sv").unlink()
    after = update.run_update(opts, "p", "after", git=False)
    report = after.data["update"]
    assert len(semantic_jobs) == 2 and before.build_id != after.build_id
    assert report["semantic_action"] == "full_profile_analysis"
    assert report["changes"]["files"] == [
        {"path": "inc_b/common.svh", "change": "modified"},
        {"path": "inc_b/untracked.svh", "change": "added"},
        {"path": "rtl/b.sv", "change": "removed"},
    ]
    assert report["changes"]["lookup_changed"]
    assert (ws.root / ".svweave/snapshots" / str(before.snapshot_id) / "files/inc_b/common.svh").read_bytes() == historical
    with Database(ws.root / ".svweave/svweave.db") as db:
        assert db.conn.execute("SELECT COUNT(*) FROM instances WHERE build_id=?", (before.build_id,)).fetchone()[0] == 1
        assert db.published("t", "p")["build_id"] == after.build_id


def test_UPD_03_config_only_change_never_reuses_semantics(
    ws: Ws, semantic_jobs: list[dict[str, Any]],
) -> None:
    opts = configure(ws)
    before = update.run_update(opts, "p", "before", git=False)
    ws.config({"p": profile(source_inputs=[{"kind": "file", "path": "rtl/a.sv"}],
                            include_dirs=["inc_b"], defines=[{"name": "MODE", "value": "1"}])})
    after = update.run_update(opts, "p", "after", git=False)
    assert before.snapshot_id == after.snapshot_id and before.build_id != after.build_id
    assert len(semantic_jobs) == 2
    assert after.data["update"]["changes"]["config_changed"] is True
    assert after.data["update"]["materialization"]["noop_snapshot_reused"]


def test_UPD_03_toolchain_only_change_never_reuses_semantics(
    ws: Ws, semantic_jobs: list[dict[str, Any]], monkeypatch: pytest.MonkeyPatch,
) -> None:
    opts = configure(ws)
    before = update.run_update(opts, "p", "before", git=False)
    real = build.compute_toolchain
    monkeypatch.setattr(build, "compute_toolchain", lambda root: (sha256_hex("different"), *real(root)[1:]))
    after = update.run_update(opts, "p", "after", git=False)
    assert before.snapshot_id == after.snapshot_id and before.build_id != after.build_id
    assert len(semantic_jobs) == 2 and not after.data["reused"]


def test_UPD_04_baseline_is_profile_published_pointer_not_latest_snapshot(
    ws: Ws, semantic_jobs: list[dict[str, Any]],
) -> None:
    opts = configure(ws)
    before = update.run_update(opts, "p", "before", git=False)
    ws.write("inc_b/common.svh", "`define UNPUBLISHED 5\n")
    from svweave.application.use_cases import create_snapshot

    unpublished = create_snapshot(opts, "p", "snapshot-only")
    assert unpublished.snapshot_id != before.snapshot_id
    after = update.run_update(opts, "p", "after", git=False)
    assert after.data["update"]["baseline_snapshot_id"] == before.snapshot_id


def test_UPD_04_missing_baseline_metadata_falls_back_without_trusting_a_path(
    ws: Ws, semantic_jobs: list[dict[str, Any]],
) -> None:
    opts = configure(ws)
    before = update.run_update(opts, "p", "before", git=False)
    meta = ws.root / ".svweave/snapshots" / str(before.snapshot_id) / "snapshot.json"
    meta.parent.chmod(0o755)
    meta.unlink()
    meta.symlink_to(ws.root / "svweave.yaml")
    ws.write("inc_b/common.svh", "`define NEW_CONTENT 5\n")
    after = update.run_update(opts, "p", "after", git=False)
    assert after.data["update"]["baseline"]["status"] == "unavailable"
    assert after.data["update"]["baseline_snapshot_id"] is None
    assert len(semantic_jobs) == 2


def test_UPD_05_build_and_workspace_locks_protect_baseline_capture(
    ws: Ws, semantic_jobs: list[dict[str, Any]], monkeypatch: pytest.MonkeyPatch,
) -> None:
    opts = configure(ws)
    original = update.capture_snapshot_incremental

    def check_locks(ctx, nm, **kw):
        with pytest.raises(WorkspaceBusy), build_lock(ctx.state_dir):
            pass
        with pytest.raises(WorkspaceBusy), workspace_lock(ctx.state_dir, exclusive=True, blocking=False):
            pass
        return original(ctx, nm, **kw)

    monkeypatch.setattr(update, "capture_snapshot_incremental", check_locks)
    update.run_update(opts, "p", "first", git=False)
    with build_lock(ws.ctx().state_dir), pytest.raises(SvwError) as err:
        update.run_update(opts, "p", "busy", git=False)
    assert err.value.code is ErrorCode.RESOURCE_LIMIT
    assert len(semantic_jobs) == 1


def test_UPD_06_default_build_signature_and_capture_remain_unchanged(
    ws: Ws, semantic_jobs: list[dict[str, Any]], monkeypatch: pytest.MonkeyPatch,
) -> None:
    opts = configure(ws)
    calls = []
    original = build.capture_snapshot

    def capture(*args, **kwargs):
        calls.append(True)
        return original(*args, **kwargs)

    monkeypatch.setattr(build, "capture_snapshot", capture)
    monkeypatch.setattr(update, "capture_snapshot_incremental", lambda *_a, **_k: pytest.fail("opt-in only"))
    result = build.run_build(opts, "p", "ordinary")
    assert calls == [True] and "update" not in result.data
    assert list(inspect.signature(build.run_build).parameters) == [
        "opts", "profile", "request_id", "reuse", "handle", "on_job", "test_fault", "job_id",
        "expected_snapshot_id", "expected_execution_fingerprint",
    ]


def test_UPD_07_cli_options_are_thin_and_keep_failure_exit_policy(
    ws: Ws, monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = []

    def run(opts, profile, request_id, **kw):
        calls.append((profile, kw))
        return Envelope(request_id=request_id, outcome=Outcome.PARTIAL, data={"update": {}})

    monkeypatch.setattr(update, "run_update", run)
    result = CliRunner().invoke(cli, ["--root", str(ws.root), "--compact", "update", "--profile", "p", "--base", "HEAD~1", "--no-git"])
    assert result.exit_code == 3, result.output
    assert json.loads(result.stdout)["outcome"] == "PARTIAL"
    assert calls == [("p", {"base": "HEAD~1", "git": False})]


def test_UPD_08_inventory_response_cap_explicitly_marks_omissions() -> None:
    paths = [{"path": "x" * 2000, "change": "modified"} for _ in range(200)]
    report = {"changes": {"files": paths, "total": 200, "truncated": False},
              "git": {"paths": []}, "truncated": False}
    env = Envelope(request_id="bounded", outcome=Outcome.OK, data={"update": report})
    update._bound_report(env)
    assert len(env.to_json(indent=None).encode()) <= MAX_RESPONSE_BYTES
    assert env.pagination.truncated and report["changes"]["truncated"]
    assert report["changes"]["total"] == 200 and len(paths) < 200


def test_UPD_09_partial_result_does_not_replace_previous_published_build(
    ws: Ws, semantic_jobs: list[dict[str, Any]], monkeypatch: pytest.MonkeyPatch,
) -> None:
    opts = configure(ws)
    before = update.run_update(opts, "p", "before", git=False)
    original = build._run_job

    def partial(*args):
        argv = list(args)
        argv[-2] = "partial"
        return original(*argv)

    monkeypatch.setattr(build, "_run_job", partial)
    ws.write("rtl/a.sv", "module invalid(; endmodule\n")
    after = update.run_update(opts, "p", "partial", git=False)
    assert after.outcome is Outcome.PARTIAL and not after.data["published"]
    assert after.data["update"]["semantic_action"] == "full_profile_analysis"
    with Database(ws.root / ".svweave/svweave.db") as db:
        assert db.published("t", "p")["build_id"] == before.build_id
        assert db.conn.execute("SELECT completeness FROM builds WHERE build_id=?", (before.build_id,)).fetchone()[0] == "COMPLETE"
        assert db.conn.execute("SELECT completeness FROM builds WHERE build_id=?", (after.build_id,)).fetchone()[0] == "PARTIAL"


def test_UPD_10_capture_strategy_still_checks_expected_snapshot_before_semantics(
    ws: Ws, semantic_jobs: list[dict[str, Any]],
) -> None:
    opts = configure(ws)
    with pytest.raises(SvwError) as err:
        build._run_build_with_capture(
            opts, "p", "expected", expected_snapshot_id="f" * 64,
            capture=lambda ctx, nm: update.capture_snapshot_incremental(ctx, nm)[0],
        )
    assert err.value.code is ErrorCode.STALE_SNAPSHOT
    assert not semantic_jobs


def test_UPD_11_preworker_failure_keeps_stats_without_claiming_analysis_ran(
    ws: Ws, monkeypatch: pytest.MonkeyPatch,
) -> None:
    opts = configure(ws)

    def unavailable(*_args, **_kwargs):
        raise SvwError(ErrorCode.BACKEND_UNAVAILABLE, "unit injected preworker guard")

    monkeypatch.setattr(build, "_build_snapshot", unavailable)
    result = update.run_update(opts, "p", "blocked", git=False)
    report = result.data["update"]
    assert result.error.code == "BACKEND_UNAVAILABLE"
    assert result.build_id is None and result.snapshot_id is not None
    assert report["semantic_action"] == "full_profile_analysis_requested"
    assert report["semantic_status"] == "no_validated_result"
    assert report["materialization"]["files_hashed"] > 0
    assert not (ws.root / ".svweave/runs").exists()


def test_UPD_12_non_git_advisory_failure_does_not_prevent_authoritative_update(
    ws: Ws, semantic_jobs: list[dict[str, Any]],
) -> None:
    opts = configure(ws)
    first = update.run_update(opts, "p", "before")
    assert first.data["update"]["git"]["status"] == "UNAVAILABLE"
    # A relevant ignored/untracked input is included regardless of Git availability.
    ws.write(".gitignore", "inc_b/ignored.svh\n")
    ws.write("inc_b/ignored.svh", "`define IGNORED_BUT_RELEVANT 1\n")
    second = update.run_update(opts, "p", "after")
    assert second.data["update"]["git"]["advisory_only"]
    assert second.data["update"]["semantic_action"] == "full_profile_analysis"
    assert {"path": "inc_b/ignored.svh", "change": "added"} in second.data["update"]["changes"]["files"]
    assert len(semantic_jobs) == 2 and second.build_id != first.build_id
