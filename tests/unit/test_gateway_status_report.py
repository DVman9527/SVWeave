"""report (plan 28.2), debug bundle (OBS-01/23.3), doctor wiring, cursor key, status facts."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from svweave.application import evidence as E
from svweave.application import services
from svweave.application.build import DB_FILENAME
from svweave.application.debug_bundle import make_bundle
from svweave.application.doctor import run_doctor
from svweave.application.keys import cursor_key
from svweave.application.options import WorkspaceOptions
from svweave.application.report import build_report
from svweave.application.session import Session, load_probe, save_probe
from svweave.domain.errors import ErrorCode, SvwError
from svweave.domain.states import BackendAvailability
from svweave.orchestration.health import BackendHealth
from svweave.orchestration.registry import BACKEND_NAMES, BackendRegistry
from svweave.storage.db import Database
from tests.conftest import Ws
from tests.unit._gateway_fx import record, stub_build, write_manifest


# ------------------------------------------------------------------------------ report
def test_report_fills_template_from_real_data_and_marks_the_rest(ws: Ws) -> None:
    opts, bid, sid = stub_build(ws)
    sess = Session(opts, use_graph=False)
    rep = build_report(sess, "r", bid).data["report"]
    assert rep["gates"] == {g: "NOT_EVALUATED" for g in ("G0", "G1", "G2", "G3", "G4", "G5")}
    assert rep["eda_compile"] == rep["simulation"] == rep["formal"] == "NOT_RUN"
    assert rep["coverage"] == "NOT_RUN"
    assert rep["release_decision"] == "BLOCKED"
    assert rep["statement_branch_coverage"] == rep["benchmark_report"] == "NOT_EVALUATED"
    assert rep["real_integration"]["serena"]["status"] == "NOT_PROBED"
    assert rep["editing"]["client_boundary"]["qualified"] is False
    assert rep["editing"]["approval"].startswith("IMPLEMENTED")  # B13: editing exists
    assert rep["editing"]["enabled"] is False  # ... and is disabled by default
    assert "NOT_IMPLEMENTED" not in str(rep["editing"])
    assert isinstance(rep["semantic_deviations"], list) and "completeness_reasons" in rep
    assert rep["build"]["build_id"] == bid and rep["build"]["completeness"] == "COMPLETE"
    assert rep["toolchain_lock_hash"] and len(rep["toolchain_lock_hash"]) == 64
    assert rep["source_commit"] in ("NO_COMMIT",) or len(rep["source_commit"]) >= 7
    assert not sess.registry_created


def test_report_shows_last_probe_and_declared_evidence_separately(ws: Ws, tmp_path: Path) -> None:
    opts, bid, sid = stub_build(ws)
    ctx = opts.load()
    save_probe(
        ctx.trusted_dir,
        {
            "serena": {"status": "READY", "probed": True, "checked_at": "t", "reasons": []},
            "pyslang": {"status": "DEGRADED", "probed": True, "checked_at": "t", "reasons": ["x"]},
        },
    )
    link = {"build_id": bid, "snapshot_id": sid}
    E.import_manifest(
        opts,
        write_manifest(
            tmp_path,
            [
                record(**link, result="PASS", seed="1"),
                record(**link, result="FAIL", seed="2", run_id="r2"),
                record(
                    **link,
                    kind="eda_compile",
                    test_name=None,
                    seed=None,
                    result="BLOCKED",
                    run_id="c",
                ),
            ],
        ),
        "r",
    )
    rep = build_report(Session(opts, use_graph=False), "r", bid).data["report"]
    ri = rep["real_integration"]
    assert ri["serena"]["status"] == "READY" and ri["pyslang"]["status"] == "DEGRADED"
    assert ri["codebase-memory-mcp"]["status"] == "NOT_PROBED"
    assert rep["simulation"].startswith("EXTERNAL_DECLARED (FAIL=1,PASS=1")  # separate, not merged
    assert "not verified" in rep["simulation"]
    assert "BLOCKED=1" in rep["eda_compile"]
    assert rep["formal"] == "NOT_RUN"
    assert rep["release_decision"] == "BLOCKED"  # declared evidence never accepts a release


def test_report_unknown_build(ws: Ws) -> None:
    opts, _, _ = stub_build(ws)
    with pytest.raises(SvwError) as ei:
        build_report(Session(opts), "r", "0" * 64)
    assert ei.value.code is ErrorCode.MISSING_INPUT


# ------------------------------------------------------------------------------ bundle
def test_debug_bundle_has_hashes_no_source_and_no_host_paths(ws: Ws) -> None:
    marker = "TOP_SECRET_SOURCE_MARKER_7731"
    ws.write("rtl/a.sv", f"module a; // {marker}\nendmodule\n")
    opts, bid, sid = stub_build(ws, tag="bundle")
    out = tmp = ws.root / ".svweave" / "bundles"
    env = make_bundle(opts, bid, "r")
    bdir = ws.root / env.data["bundle_dir"]
    assert bdir.is_dir() and bdir.parent == tmp
    texts = {p.name: p.read_text() for p in bdir.iterdir()}
    assert {"bundle.json", "source_inventory.json", "diagnostics_summary.json"} <= set(texts)
    blob = "\n".join(texts.values())
    assert marker not in blob  # no source contents
    assert str(ws.root) not in blob and str(ws.root.resolve()) not in blob  # no host paths
    b = json.loads(texts["bundle.json"])
    assert b["contains_source_text"] is False and b["build"]["build_id"] == bid
    assert len(b["semantic_digest"]) == 64 and b["toolchain_lock_hash"]
    inv = json.loads(texts["source_inventory.json"])
    assert inv and all(len(x["sha256"]) == 64 for x in inv)
    import hashlib

    for name, digest in b["files"].items():
        if name != "bundle.json":
            assert hashlib.sha256((bdir / name).read_bytes()).hexdigest() == digest
    # deterministic: same build -> same digest and same inventory
    again = make_bundle(opts, bid, "r2")
    assert again.data["semantic_digest"] == env.data["semantic_digest"]
    del out


def test_debug_bundle_refuses_output_outside_policy_roots(ws: Ws, tmp_path_factory: Any) -> None:
    opts, bid, _ = stub_build(ws)
    with pytest.raises(SvwError) as ei:
        make_bundle(opts, bid, "r", Path("/tmp/svw-outside-bundle"))
    assert ei.value.code is ErrorCode.PATH_NOT_ALLOWED


# ------------------------------------------------------------------------------ cursor key
def test_cursor_key_lives_in_the_trusted_dir_not_the_repo(ws: Ws) -> None:
    ctx = ws.ctx()
    key = cursor_key(ctx)
    assert len(key) == 32 and cursor_key(ctx) == key
    p = ctx.trusted_dir / "cursor.key"
    assert p.is_file() and (p.stat().st_mode & 0o777) == 0o600
    assert not (ctx.state_dir / "cursor.key").exists()
    assert str(ctx.project_root) not in str(p)
    from svweave.application import queries

    assert queries._cursor_key(ctx) == key  # noqa: SLF001


# ------------------------------------------------------------------------------ status facts
def test_status_pending_edits_counts_open_transactions(ws: Ws) -> None:
    opts, bid, sid = stub_build(ws)
    ctx = opts.load()
    with Database(ctx.state_dir / DB_FILENAME) as db:
        for i, state in enumerate(
            ("PREPARED", "AWAITING_APPROVAL", "ABORTED", "POST_APPLY_VERIFIED")
        ):
            db.conn.execute(
                "INSERT INTO edit_transactions(transaction_id, base_snapshot_id, state) VALUES (?,?,?)",
                (f"t{i}", sid, state),
            )
    env = services.status(Session(opts, use_graph=False), "r")
    assert env.data["pending_edits"] == 2
    assert env.data["profiles"][0]["current_build"]["build_id"] == bid


def test_status_reports_last_probe_record_with_its_source(ws: Ws) -> None:
    opts, _, _ = stub_build(ws)
    save_probe(
        opts.load().trusted_dir,
        {
            "serena": {
                "status": "DEGRADED",
                "probed": True,
                "checked_at": "2026-01-01T00:00:00+00:00",
                "reasons": ["edit subset"],
            }
        },
    )
    env = services.status(Session(opts, use_graph=False), "r")
    s = env.data["backends"]["serena"]
    assert s["status"] == "DEGRADED" and s["source"] == "last_doctor_probe"
    assert env.data["backends"]["pyslang"]["status"] == "NOT_PROBED"


def test_status_with_failed_toolchain_probe_is_unavailable_not_ready(ws: Ws) -> None:
    opts, _, _ = stub_build(ws)

    def boom() -> BackendRegistry:
        raise SvwError(ErrorCode.MISSING_INPUT, "no toolchain")

    sess = Session(opts, use_graph=False, registry_factory=boom)
    env = services.status(sess, "r", probe=True)
    assert {b["status"] for b in env.data["backends"].values()} == {"UNAVAILABLE"}


# ------------------------------------------------------------------------------ doctor
def _health(name: str) -> BackendHealth:
    return BackendHealth(name=name, status=BackendAvailability.READY, probed=True, version="1")


def test_doctor_default_does_not_probe_and_prints_client_boundary(
    ws: Ws, monkeypatch: pytest.MonkeyPatch
) -> None:
    def forbidden(self: Any) -> Any:
        raise AssertionError("doctor must not probe backends without --probe")

    monkeypatch.setattr(BackendRegistry, "probe_all", forbidden)
    monkeypatch.setattr(BackendRegistry, "cbm", forbidden)
    monkeypatch.setattr(BackendRegistry, "serena_for", forbidden)
    env = run_doctor(WorkspaceOptions(root=ws.root), "r")
    checks = {c["name"]: c for c in env.data["checks"]}
    cb = checks["client_boundary"]
    assert cb["status"] == "WARN" and "client boundary not qualified" in cb["message"]
    for n in BACKEND_NAMES:
        assert checks[f"backend:{n}"]["status"] == "SKIP"  # never READY without a probe
    assert "toolchain_lock" in checks and "isolation:bwrap" in checks
    assert any("client boundary not qualified" in w for w in env.warnings)


def test_doctor_probe_uses_the_registry_once_stores_result_and_shuts_down(
    ws: Ws, monkeypatch: pytest.MonkeyPatch
) -> None:
    probed: list[int] = []
    closed: list[int] = []

    def fake_probe(self: BackendRegistry) -> dict[str, BackendHealth]:
        probed.append(1)
        for n in BACKEND_NAMES:
            self.set_health(_health(n))
        return self.all_health()

    real_shutdown = BackendRegistry.shutdown

    def counting_shutdown(self: BackendRegistry) -> None:
        closed.append(1)
        real_shutdown(self)

    monkeypatch.setattr(BackendRegistry, "probe_all", fake_probe)
    monkeypatch.setattr(BackendRegistry, "shutdown", counting_shutdown)
    opts = WorkspaceOptions(root=ws.root)
    env = run_doctor(opts, "r", probe=True)
    assert probed == [1] and closed  # every registry created was shut down
    checks = {c["name"]: c for c in env.data["checks"]}
    assert all(checks[f"backend:{n}"]["status"] == "PASS" for n in BACKEND_NAMES)
    stored = load_probe(opts.load().trusted_dir)
    assert stored is not None and stored["serena"]["status"] == "READY"
    # a later status call shows that record as "last_doctor_probe", not as live health
    st = services.status(Session(opts, use_graph=False), "r")
    assert st.data["backends"]["serena"]["source"] == "last_doctor_probe"
