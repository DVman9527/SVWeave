"""Phase-1 update equivalence on the real, mandatory-bwrap slang worker.

Requirement mapping: SNP-01/02/03 + plan 9.2/9.3 -> exact full-capture identity,
bytes and lookup inventories after small edits; plan 9.6 -> exact COMPLETE reuse,
whole-profile analysis otherwise, immutable historical queries and failed publish.
These are runtime integration checks, not synthetic speed or partial-elaboration claims.
"""

from __future__ import annotations

import hashlib
import json
import shutil
import stat
from pathlib import Path
from typing import Any

import pytest
from click.testing import CliRunner

from svweave.application import queries as Q
from svweave.application.update import run_update
from svweave.cli import cli
from svweave.domain.envelope import Envelope
from svweave.snapshot.capture import load_record
from svweave.snapshot.models import SnapshotRecord
from tests.conftest import Ws, profile
from tests.integration.helpers import build, dbq, opts

pytestmark = pytest.mark.integration

# All tables produced by semantic ingestion, plus the provenance/dependency report.
# Semantic identities are deliberately retained: identical snapshots/configs/toolchains MUST
# produce identical source, declaration, symbol, reference, instance and build IDs.
SEMANTIC_TABLES = (
    "declarations", "references", "instances", "port_bindings", "assertions",
    "diagnostics", "specializations", "inactive_regions", "coverage_gaps",
    "unsupported_nodes", "build_reports",
)
SOURCE_PATHS = ("rtl/incremental_pkg.sv", *[f"rtl/lane_{n}.sv" for n in range(6)], "rtl/top.sv")


def _workspace(tmp: Path) -> Ws:
    """One profile/top, a package, six modules, a loaded library and ordered include lookup."""
    w = Ws(tmp / "ws")
    w.write("rtl/incremental_pkg.sv", "package incremental_pkg;\n  parameter int BASE_WIDTH = 8;\nendpackage\n")
    for n in range(6):
        w.write(
            f"rtl/lane_{n}.sv",
            f"module lane_{n} #(parameter int W = 8, parameter int OFFSET = {n + 1})\n"
            "  (input logic [W-1:0] data_i, output logic [W-1:0] data_o);\n"
            "  assign data_o = data_i + W'(OFFSET);\nendmodule\n",
        )
    w.write(
        "lib/library_tail.sv",
        "module library_tail #(parameter int W = 8)\n"
        "  (input logic [W-1:0] data_i, output logic [W-1:0] data_o);\n"
        "  assign data_o = ~data_i;\nendmodule\n",
    )
    body = [
        '`include "width.svh"',
        "module incremental_top #(parameter int W = incremental_pkg::BASE_WIDTH + `EXTRA_WIDTH)",
        "  (input logic [W-1:0] data_i, output logic [W-1:0] data_o);",
        *[f"  logic [W-1:0] link_{n};" for n in range(6)],
    ]
    for n in range(6):
        actual = "data_i" if n == 0 else f"link_{n - 1}"
        body.append(f"  lane_{n} #(.W(W)) u{n} (.data_i({actual}), .data_o(link_{n}));")
    body += ["  library_tail #(.W(W)) tail (.data_i(link_5), .data_o(data_o));", "endmodule"]
    w.write("rtl/top.sv", "\n".join(body) + "\n")
    w.write("include/fallback/width.svh", "`define EXTRA_WIDTH 0\n")
    (w.root / "include" / "preferred").mkdir(parents=True)
    w.write("lists/files.f", "\n".join(SOURCE_PATHS) + "\n")
    w.config({"p": profile(
        source_inputs=[{"kind": "filelist", "path": "lists/files.f", "path_mode": "cwd"}],
        top_modules=["incremental_top"],
        include_dirs=["include/preferred", "include/fallback"],
        library_dirs=["lib"],
        library_extensions=[".sv"],
    )})
    w.policy()
    return w


def _snapshot(w: Ws, sid: str) -> tuple[SnapshotRecord, dict[str, bytes]]:
    root = w.root / ".svweave" / "snapshots" / sid
    rec = load_record(root)
    assert rec.snapshot_id == sid
    contents = {f.logical_path: (root / f.snapshot_path).read_bytes() for f in rec.files}
    assert len(contents) == len(rec.files)
    assert {p.relative_to(root / "files").as_posix() for p in (root / "files").rglob("*") if p.is_file()} == set(contents)
    for f in rec.files:
        assert hashlib.sha256(contents[f.logical_path]).hexdigest() == f.sha256
        assert len(contents[f.logical_path]) == f.size
        captured = root / f.snapshot_path
        captured_stat = captured.stat()
        assert not captured.is_symlink() and stat.S_ISREG(captured_stat.st_mode)
        assert captured_stat.st_nlink == 1 and not captured_stat.st_mode & 0o222
        original = Path(f.original_path).stat()
        assert (captured_stat.st_dev, captured_stat.st_ino) != (original.st_dev, original.st_ino)
    return rec, contents


def _relocated_record(rec: SnapshotRecord, w: Ws) -> dict[str, Any]:
    # Only capture time/attempts and host location vary across independent workspaces.
    # All identity-bearing fields, ordered inventories, roles and lookup metadata remain exact.
    result = rec.model_dump(mode="json", exclude={"created_at", "attempts", "workspace_id"})
    for item in result["files"]:
        item["original_path"] = Path(item["original_path"]).relative_to(w.root).as_posix()
    for item in result["lookup"]:
        item["original_dir"] = Path(item["original_dir"]).relative_to(w.root).as_posix()
    return result


def _semantic_rows(w: Ws, bid: str) -> dict[str, list[str]]:
    result = {}
    for table in SEMANTIC_TABLES:
        rows = dbq(w, f'SELECT * FROM "{table}" WHERE build_id=?', bid)
        # job_id identifies an execution, diagnostic_id is SQLite's global row counter, and
        # worker_wall_s is elapsed time. Do not remove any semantic IDs or provenance.
        ignored = {"job_id"} if table in {"diagnostics", "build_reports"} else set()
        if table == "diagnostics":
            ignored.add("diagnostic_id")
        if table == "build_reports":
            for row in rows:
                stats = json.loads(row["stats"])
                assert isinstance(stats["worker_wall_s"], (int, float)) and stats["worker_wall_s"] >= 0
                del stats["worker_wall_s"]
                row["stats"] = json.dumps(stats, sort_keys=True)
        result[table] = sorted(json.dumps(
            {k: v for k, v in row.items() if k not in ignored}, sort_keys=True,
        ) for row in rows)
    assert len(result["declarations"]) > 20 and len(result["references"]) > 10
    assert len(result["instances"]) == 8 and len(result["port_bindings"]) >= 14
    return result


def _complete(env: Envelope, *, ran_worker: bool = True) -> None:
    assert env.error is None and env.outcome == "OK", env.model_dump_json(indent=2)
    assert env.snapshot_id and env.build_id and env.analysis_coverage is not None
    assert env.analysis_coverage.completeness == "COMPLETE"
    assert env.data["diagnostic_outcome"] == "NO_ERRORS"
    assert env.data["published"] is True
    assert env.freshness == "CURRENT"
    if ran_worker:
        assert env.data["worker_isolation"] == "bwrap"
        assert env.data["reused"] is False and env.data["job_id"]


def _whole_profile_request(w: Ws, env: Envelope) -> None:
    run = w.root / env.data["run_dir"]
    request = json.loads((run / "request.json").read_text())["worker_request"]
    assert request["tops"] == ["incremental_top"]
    source_root = w.root / ".svweave" / "snapshots" / str(env.snapshot_id) / "files"
    assert set(str(source_root / p) for p in SOURCE_PATHS) <= set(request["args"])
    dependencies = json.loads((run / "dependency_report.json").read_text())
    assert dependencies["ok"] is True and dependencies["violations"] == []
    assert {*SOURCE_PATHS, "lib/library_tail.sv"} <= set(dependencies["files_read"])


def _full_oracle(tmp: Path, w: Ws, incremental: Envelope) -> None:
    """Fresh state uses legacy run_build/capture_snapshot, never the update path or cache."""
    full = Ws(tmp / "ws")
    shutil.copytree(w.root, full.root, ignore=shutil.ignore_patterns(".svweave"))
    full.policy()
    assert not (full.root / ".svweave").exists()
    expected = build(full)
    _complete(expected)
    _whole_profile_request(full, expected)
    assert expected.build_id == incremental.build_id
    assert expected.snapshot_id == incremental.snapshot_id
    assert expected.profile == incremental.profile == "p"
    assert expected.provenance == incremental.provenance
    assert expected.analysis_coverage == incremental.analysis_coverage
    assert expected.snapshot_id and expected.build_id
    current_record, current_bytes = _snapshot(w, expected.snapshot_id)
    full_record, full_bytes = _snapshot(full, expected.snapshot_id)
    assert _relocated_record(current_record, w) == _relocated_record(full_record, full)
    assert current_bytes == full_bytes
    assert _semantic_rows(w, expected.build_id) == _semantic_rows(full, expected.build_id)
    calls = (
        (Q.hierarchy, {}),
        (Q.connections, {"instance": "incremental_top.u0"}),
        (Q.diagnostics, {}),
    )
    for query, kw in calls:
        actual = query(opts(w), expected.build_id, request_id="equivalence", limit=500, **kw)
        oracle = query(opts(full), expected.build_id, request_id="equivalence", limit=500, **kw)
        assert not actual.pagination.truncated and not oracle.pagination.truncated
        assert actual.model_dump(exclude={"workspace_id"}) == oracle.model_dump(exclude={"workspace_id"})


def _run_files(w: Ws) -> dict[str, bytes]:
    root = w.root / ".svweave" / "runs"
    return {p.relative_to(root).as_posix(): p.read_bytes() for p in root.rglob("*") if p.is_file()}


def _top_width(w: Ws, bid: str) -> str:
    tree = Q.hierarchy(opts(w), bid, "width", top="incremental_top", depth=0)
    return {p["name"]: p["value"] for p in tree.data["items"][0]["parameters"]}["W"]


def test_SNP_01_02_03_update_matches_full_capture_and_real_semantics(tmp_path: Path) -> None:
    w = _workspace(tmp_path / "incremental")
    initial = build(w)
    _complete(initial)
    _whole_profile_request(w, initial)
    assert initial.build_id and initial.snapshot_id
    initial_record, initial_bytes = _snapshot(w, initial.snapshot_id)
    old_hierarchy = Q.hierarchy(opts(w), initial.build_id, "history").data
    old_rows = _semantic_rows(w, initial.build_id)
    initial_runs = _run_files(w)
    initial_jobs = dbq(w, "SELECT * FROM jobs")

    # The real CLI's no-op must preserve both semantic rows AND all historical run bytes.
    result = CliRunner().invoke(cli, ["--root", str(w.root), "--compact", "update", "--profile", "p", "--no-git"])
    assert result.exit_code == 0, result.output
    noop = Envelope.model_validate_json(result.stdout)
    _complete(noop, ran_worker=False)
    assert noop.build_id == initial.build_id and noop.snapshot_id == initial.snapshot_id
    assert noop.data["reused"] is True and noop.data["job_id"] is None
    assert noop.data["update"]["semantic_action"] == "exact_build_reuse"
    materialization = noop.data["update"]["materialization"]
    assert materialization["noop_snapshot_reused"] is True
    assert materialization["files_copied"] == materialization["files_cloned"] == 0
    assert noop.data["update"]["changes"]["total"] == 0
    assert dbq(w, "SELECT * FROM jobs") == initial_jobs and _run_files(w) == initial_runs
    assert _semantic_rows(w, initial.build_id) == old_rows
    _full_oracle(tmp_path / "full-noop", w, noop)

    # The shadowed-header edit also matters: no semantic change is required for an input to
    # invalidate a build. It makes the final deletion a new identity, not a cached reversion.
    edits = (
        ("one-source", "rtl/lane_0.sv", "modified", 8),
        ("included-macro", "include/fallback/width.svh", "modified", 12),
        ("lookup-add", "include/preferred/width.svh", "added", 16),
        ("shadowed-header", "include/fallback/width.svh", "modified", 16),
        ("lookup-delete", "include/preferred/width.svh", "removed", 14),
    )
    previous = noop
    for label, path, change, width in edits:
        source = w.root / path
        if label == "one-source":
            source.write_text(source.read_text().replace("OFFSET = 1", "OFFSET = 3"))
        elif change == "removed":
            source.unlink()
        else:
            extra = {"included-macro": 4, "lookup-add": 8, "shadowed-header": 6}[label]
            w.write(path, f"`define EXTRA_WIDTH {extra}\n")
        before_jobs = len(dbq(w, "SELECT * FROM jobs"))
        changed = run_update(opts(w), "p", f"update-{label}", git=False)
        _complete(changed)
        _whole_profile_request(w, changed)
        assert changed.build_id and changed.snapshot_id
        assert changed.build_id != previous.build_id and changed.snapshot_id != previous.snapshot_id, label
        report = changed.data["update"]
        assert report["baseline_snapshot_id"] == previous.snapshot_id, label
        assert report["semantic_action"] == "full_profile_analysis", label
        assert report["changes"]["files"] == [{"path": path, "change": change}], label
        assert report["changes"]["total"] == 1 and not report["changes"]["truncated"], label
        assert report["changes"]["config_changed"] is False, label
        assert report["changes"]["lookup_changed"] == (change in {"added", "removed"}), label
        assert len(dbq(w, "SELECT * FROM jobs")) == before_jobs + 1, label
        assert _top_width(w, changed.build_id) == str(width), label
        if label == "one-source":
            instance = Q.hierarchy(opts(w), changed.build_id, "offset", instance="incremental_top.u0", depth=0)
            assert {p["name"]: p["value"] for p in instance.data["items"][0]["parameters"]}["OFFSET"] == "3"
        _full_oracle(tmp_path / f"full-{label}", w, changed)
        historical = Q.hierarchy(opts(w), initial.build_id, "history")
        assert historical.freshness == "HISTORICAL" and historical.data == old_hierarchy
        assert _semantic_rows(w, initial.build_id) == old_rows
        # Old source bytes stay queryable even when a new snapshot has been materialized.
        historical_root = w.root / ".svweave" / "snapshots" / initial.snapshot_id
        assert {f.logical_path: (historical_root / f.snapshot_path).read_bytes() for f in initial_record.files} == initial_bytes
        assert dbq(w, "SELECT build_id FROM published_pointer") == [{"build_id": changed.build_id}]
        assert all(_run_files(w)[name] == data for name, data in initial_runs.items())
        previous = changed


def test_SNP_update_invalid_edit_never_replaces_the_usable_build(tmp_path: Path) -> None:
    w = _workspace(tmp_path)
    initial = build(w)
    _complete(initial)
    assert initial.build_id and initial.snapshot_id
    old_rows = _semantic_rows(w, initial.build_id)
    old_hierarchy = Q.hierarchy(opts(w), initial.build_id, "old").data
    pointer = dbq(w, "SELECT * FROM published_pointer")
    run_bytes = _run_files(w)
    source = w.root / "rtl" / "lane_0.sv"
    good = source.read_bytes()
    source.write_bytes(good.replace(b"assign data_o = data_i + W'(OFFSET);", b"assign data_o = ;"))
    invalid = run_update(opts(w), "p", "invalid-edit", git=False)
    assert invalid.outcome == "PARTIAL" and invalid.error is not None
    assert invalid.error.code == "PARTIAL_ANALYSIS"
    assert invalid.build_id and invalid.build_id != initial.build_id
    assert invalid.data["worker_isolation"] == "bwrap"
    assert invalid.data["update"]["semantic_action"] == "full_profile_analysis"
    assert invalid.data["published"] is False and invalid.data["diagnostic_outcome"] == "HAS_ERRORS"
    assert invalid.analysis_coverage is not None and invalid.analysis_coverage.completeness == "PARTIAL"
    assert dbq(w, "SELECT * FROM published_pointer") == pointer
    assert dbq(w, "SELECT build_id FROM builds WHERE completeness='COMPLETE'") == [{"build_id": initial.build_id}]
    assert any(d["is_error"] for d in Q.diagnostics(opts(w), invalid.build_id, "invalid").data["items"])
    old = Q.hierarchy(opts(w), initial.build_id, "old")
    assert old.freshness == "STALE" and old.data == old_hierarchy
    assert _semantic_rows(w, initial.build_id) == old_rows
    assert all(_run_files(w)[name] == data for name, data in run_bytes.items())
    snapshot = w.root / ".svweave" / "snapshots" / initial.snapshot_id / "files" / "rtl" / "lane_0.sv"
    assert snapshot.read_bytes() == good
