"""Read-only recovery CLI contract; mocked service dispatch is not cluster evidence."""

from __future__ import annotations

import json

import pytest
import yaml
from click.testing import CliRunner

from svweave.application.options import WorkspaceOptions
from svweave.cli import cli
from svweave.config.execution import ResolvedExecution
from svweave.domain.envelope import Envelope, Outcome
from tests.conftest import Ws


@pytest.mark.parametrize("value", ["0", "1001", "-1", "nan", "1.0"])
def test_SRUN_22_recovery_limit_bounds(ws: Ws, value: str) -> None:
    result = CliRunner().invoke(
        cli, ["--root", str(ws.root), "execution-recovery", "--limit", value]
    )
    assert result.exit_code == 2
    assert "Invalid value" in result.output


@pytest.mark.parametrize(
    "args,expected", [([], 200), (["--limit", "1"], 1), (["--limit", "1000"], 1000)]
)
def test_SRUN_22_recovery_dispatches_bounded_request_under_srun(
    ws: Ws,
    monkeypatch: pytest.MonkeyPatch,
    args: list[str],
    expected: int,
) -> None:
    from svweave.application import execution_recovery

    ws.write(
        "svweave.local.yaml",
        yaml.safe_dump(
            {
                "schema_version": "1.0",
                "execution": {
                    "provider": "srun",
                    "site_profile": "unqualified",
                    "allocation_id": "123",
                },
            }
        ),
    )
    called: list[int] = []

    def no_launch_guard(self: ResolvedExecution) -> None:
        pytest.fail("read-only inspection must not require execution authorization")

    def inspect(opts: WorkspaceOptions, request_id: str, *, limit: int = 200) -> Envelope:
        assert opts.load().execution.provider == "srun"
        called.append(limit)
        return Envelope(
            request_id=request_id,
            outcome=Outcome.OK,
            data={"read_only": True, "submitted": False, "absence_proven": False},
        )

    monkeypatch.setattr(ResolvedExecution, "require_srun_authorized", no_launch_guard)
    monkeypatch.setattr(execution_recovery, "inspect_execution", inspect)
    result = CliRunner().invoke(cli, ["--root", str(ws.root), "execution-recovery", *args])
    assert result.exit_code == 0, result.output
    assert called == [expected]
    assert json.loads(result.output)["data"]["absence_proven"] is False


def test_SRUN_22_recovery_preserves_recovery_required_error(
    ws: Ws, monkeypatch: pytest.MonkeyPatch
) -> None:
    from svweave.application import execution_recovery
    from svweave.domain.errors import ErrorCode, SvwError

    def inspect(opts: WorkspaceOptions, request_id: str, *, limit: int = 200) -> Envelope:
        raise SvwError(
            ErrorCode.RECOVERY_REQUIRED, "record missing", {"read_only": True, "submitted": False}
        )

    monkeypatch.setattr(execution_recovery, "inspect_execution", inspect)
    result = CliRunner().invoke(cli, ["--root", str(ws.root), "execution-recovery"])
    assert result.exit_code != 0
    envelope = json.loads(result.output)
    assert envelope["error"]["code"] == "RECOVERY_REQUIRED"
    assert envelope["error"]["details"]["submitted"] is False


def test_SRUN_22_empty_recovery_is_read_only_not_remote_absence_proof(
    ws: Ws,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ws.write(
        "svweave.local.yaml",
        yaml.safe_dump(
            {
                "schema_version": "1.0",
                "execution": {
                    "provider": "srun",
                    "site_profile": "unqualified",
                    "allocation_id": "123",
                },
            }
        ),
    )

    def no_launch_guard(self: ResolvedExecution) -> None:
        pytest.fail("inspection must not invoke remote authorization")

    monkeypatch.setattr(ResolvedExecution, "require_srun_authorized", no_launch_guard)
    before = set(ws.root.rglob("*"))
    result = CliRunner().invoke(cli, ["--root", str(ws.root), "execution-recovery"])
    assert result.exit_code == 0, result.output
    data = json.loads(result.output)["data"]
    assert data["read_only"] is True
    assert data["submitted"] is False
    assert data["scheduler_queried"] is False
    assert data["remote_terminal_verified"] is False
    assert data["attempts"] == []
    assert any("not proof" in text for text in data["limitations"])
    assert set(ws.root.rglob("*")) == before
