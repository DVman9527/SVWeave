"""Fix round B (job envelopes, schema, directory signatures, texts, policy)."""

from __future__ import annotations

import json
import os
import signal
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import jsonschema
import pytest

from svweave.application import services
from svweave.application.semantic_common import _dir_sigs
from svweave.config.schemas import check_schemas
from svweave.domain.states import Completeness
from tests.conftest import Ws, profile

REPO = Path(__file__).resolve().parents[2]


def _sess() -> Any:
    return SimpleNamespace(opts=SimpleNamespace(load=lambda: SimpleNamespace(workspace_id="w")))


def _job(life: str, **over: Any) -> Any:
    data = {"job_id": "j", "lifecycle": life, "build_id": None, "snapshot_id": None, "profile": "p"}
    data.update(over)
    return services._job_envelope(_sess(), "r", data)


def test_B3_job_envelope_reflects_the_true_lifecycle_and_build_result() -> None:
    run = _job("RUNNING")
    assert run.outcome == "OK" and run.analysis_coverage is None  # no analysis result yet
    assert any("RUNNING" in w for w in run.warnings)
    partial = _job(
        "SUCCEEDED",
        build_id="b" * 64,
        result={
            "outcome": "PARTIAL",
            "completeness": "PARTIAL",
            "freshness": "CURRENT",
            "error": {"code": "PARTIAL_ANALYSIS", "message": "not complete", "details": {}},
        },
    )
    assert partial.outcome == "PARTIAL" and partial.analysis_coverage is not None
    assert partial.analysis_coverage.completeness is Completeness.PARTIAL
    assert partial.error is not None and partial.error.code == "PARTIAL_ANALYSIS"
    assert partial.freshness == "CURRENT"
    ok = _job("SUCCEEDED", result={"outcome": "OK", "completeness": "COMPLETE"})
    assert ok.outcome == "OK" and ok.analysis_coverage is not None
    assert ok.analysis_coverage.completeness is Completeness.COMPLETE
    failed = _job("FAILED", error="BACKEND_UNAVAILABLE: worker died")
    assert failed.outcome == "ERROR" and failed.error is not None
    assert failed.error.code == "BACKEND_UNAVAILABLE"
    assert _job("TIMED_OUT").error.code == "BUILD_TIMEOUT"  # type: ignore[union-attr]
    canc = _job("CANCELLED")
    assert canc.outcome == "BLOCKED" and canc.analysis_coverage is not None
    assert canc.analysis_coverage.completeness is Completeness.BLOCKED


def test_B14_toolchain_schema_in_sync_and_validates_the_lock() -> None:
    assert check_schemas(REPO / "schemas") == []
    schema = json.loads((REPO / "schemas" / "toolchain.schema.json").read_text())
    lock = json.loads((REPO / "toolchain.lock.json").read_text())
    jsonschema.validate(lock, schema)
    bad = json.loads(json.dumps(lock))
    del bad["components"]["pyslang"]["record_sha256"]
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(bad, schema)
    bad2 = json.loads(json.dumps(lock))
    bad2["components"]["pyslang"]["wheel_sha256"] = "xyz"
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(bad2, schema)


def test_B7_dir_sigs_survive_symlink_cycles_and_respect_exclusions(ws: Ws) -> None:
    inc = ws.root / "inc"
    inc.mkdir()
    (inc / "a.svh").write_text("//\n")
    for n in ("l1", "l2", "l3"):  # cycles: each link points back at the directory itself
        os.symlink(".", inc / n)
    (inc / "out").mkdir()  # excluded by the root rules (**/out/**)
    (inc / "out" / "x.svh").write_text("//\n")
    ctx = ws.ctx()

    def on_alarm(*_: object) -> None:
        raise TimeoutError("directory signature walk did not terminate (symlink cycle)")

    old = signal.signal(signal.SIGALRM, on_alarm)
    signal.alarm(10)
    try:
        sigs = _dir_sigs(ctx, str(inc), True)
    finally:
        signal.alarm(0)
        signal.signal(signal.SIGALRM, old)
    assert len(sigs) == 1  # inc only: self-aliases are the same real directory, out/ is excluded


def test_B12_report_only_policy_is_no_longer_accepted(ws: Ws) -> None:
    from pydantic import ValidationError

    from svweave.config.models import Profile

    with pytest.raises(ValidationError):
        Profile.model_validate(
            profile(source_inputs=[{"kind": "file", "path": "a.sv"}], diagnostic_policy="report_only")
        )


def test_B13_user_facing_text_matches_the_real_state() -> None:
    from svweave import server

    assert "Editing is not available" not in server.INSTRUCTIONS
    assert "disabled" in server.INSTRUCTIONS
    assert "NOT_IMPLEMENTED (M4)" not in (REPO / "src/svweave/application/report.py").read_text()
    _ = sys


def test_A6_generated_role_wins_when_a_generated_input_is_also_a_source(ws: Ws) -> None:
    from svweave.manifest.resolver import resolve_profile
    from svweave.snapshot.capture import build_spec

    ws.write("gen/regs.sv", "module regs; endmodule\n")
    ws.config(
        {
            "p": profile(
                source_inputs=[{"kind": "file", "path": "gen/regs.sv"}],
                top_modules=["regs"],
                generated_inputs=[{"path": "gen/regs.sv"}],
            )
        }
    )
    ctx = ws.ctx()
    spec = build_spec(ctx, resolve_profile(ctx, "p", {}))
    roles = [e.role for e in spec.explicit if e.logical == "gen/regs.sv"]
    assert roles == ["generated"]  # once, and as generated


def test_B_degraded_worker_isolation_reaches_the_envelope() -> None:
    from svweave.adapters.slang.runner import WorkerOutcome
    from svweave.application.build import _surface_isolation
    from svweave.domain.envelope import Envelope, Outcome

    env = Envelope(request_id="r", outcome=Outcome.OK)
    out = WorkerOutcome(
        kind="ok", isolation="degraded_no_isolation", isolation_warnings=("no bwrap",)
    )
    _surface_isolation(env, out)
    assert env.data["worker_isolation"] == "degraded_no_isolation"
    assert any("no bwrap" in w for w in env.warnings)
    clean = Envelope(request_id="r", outcome=Outcome.OK)
    _surface_isolation(clean, WorkerOutcome(kind="ok"))
    assert clean.warnings == []
