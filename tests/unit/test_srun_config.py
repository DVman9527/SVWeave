"""SRUN-01/02/03/15/21/22: offline models are not cluster qualification."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from svweave.config.execution import (
    ExecutionPolicy,
    ExecutionResources,
    ExecutionSelection,
    SlurmSiteProfile,
    resolve_execution,
)
from svweave.config.models import LocalConfig, Policy
from svweave.domain.errors import ErrorCode, SvwError


def selection() -> ExecutionSelection:
    return ExecutionSelection(provider="srun", site_profile="reviewed", allocation_id="123")


def site(**changes: object) -> SlurmSiteProfile:
    data: dict[str, object] = dict(
        cluster="test",
        srun_path="/usr/bin/srun",
        scancel_path="/usr/bin/scancel",
        scontrol_path="/usr/bin/scontrol",
        python_path="/opt/runtime/bin/python",
        bootstrap_path="/opt/runtime/bootstrap.py",
        shared_root="/srv/svweave",
    )
    data.update(changes)
    return SlurmSiteProfile.model_validate(data)


def test_SRUN_01_old_configs_default_local() -> None:
    local = LocalConfig(schema_version="1.0")
    policy = Policy(schema_version="1.0", allowed_roots=["/project"])
    assert resolve_execution(local.execution, policy.execution).provider == "local"
    assert not policy.execution.scheduler_network_approved
    assert not policy.network_enabled


@pytest.mark.parametrize(
    "data",
    [
        {"provider": "srun"},
        {"provider": "srun", "site_profile": "reviewed"},
        {"provider": "local", "site_profile": "reviewed"},
        {"provider": "srun", "site_profile": "x\n--pty", "allocation_id": "1"},
        {"provider": "srun", "site_profile": "reviewed", "allocation_id": "1.batch"},
        {"provider": "srun", "site_profile": "reviewed", "allocation_id": "0"},
        {"provider": "srun", "site_profile": "reviewed", "allocation_id": "1", "extra_args": []},
    ],
)
def test_SRUN_03_selection_rejects_ambiguous_or_injected(data: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        ExecutionSelection.model_validate(data)


@pytest.mark.parametrize(
    "changes",
    [
        {"srun_path": "srun"},
        {"srun_path": "/bin/srun\x00"},
        {"srun_path": "/a/../srun"},
        {"account": "user --pty"},
        {"cluster": "a,b"},
        {"shared_root": "/"},
        {"extra_args": ["--pty"]},
    ],
)
def test_SRUN_03_profile_rejects_untrusted_argv(changes: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        site(**changes)


def test_SRUN_03_spaces_in_canonical_path_remain_literal() -> None:
    assert site(srun_path="/opt/slurm client/srun").srun_path == "/opt/slurm client/srun"


@pytest.mark.parametrize(
    "changes",
    [
        {"nodes": 2},
        {"tasks": 2},
        {"memory_mib": 0},
        {"cpus_per_task": "2"},
        {"walltime_seconds": -1},
    ],
)
def test_SRUN_03_resource_bounds(changes: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        ExecutionResources.model_validate(changes)


def test_SRUN_21_local_data_cannot_grant_authority() -> None:
    with pytest.raises(ValidationError):
        LocalConfig.model_validate(
            {
                "schema_version": "1.0",
                "execution": {"provider": "local", "scheduler_network_approved": True},
            }
        )
    with pytest.raises(SvwError) as err:
        resolve_execution(selection(), ExecutionPolicy()).require_srun_authorized()
    assert err.value.code == ErrorCode.POLICY_DENIED
    assert err.value.details["submitted"] is False


def test_SRUN_02_unqualified_site_never_launches_even_with_digest() -> None:
    policy = ExecutionPolicy(
        scheduler_network_approved=True,
        site_profiles={"reviewed": site(qualification_digest="a" * 64)},
    )
    with pytest.raises(SvwError) as err:
        resolve_execution(selection(), policy).require_srun_authorized()
    assert err.value.code == ErrorCode.BACKEND_UNAVAILABLE
    assert err.value.details["qualification"] == "BLOCKED"


def test_SRUN_15_fingerprint_binds_provider_profile_and_allocation() -> None:
    policy = ExecutionPolicy(site_profiles={"reviewed": site()})
    first = resolve_execution(selection(), policy)
    other = resolve_execution(
        ExecutionSelection(provider="srun", site_profile="reviewed", allocation_id="124"), policy
    )
    assert first.fingerprint != other.fingerprint
    assert first.fingerprint != resolve_execution(ExecutionSelection(), policy).fingerprint
    assert first.fingerprint == resolve_execution(selection(), policy).fingerprint


def test_SRUN_22_cli_preflight_and_priority(ws: object) -> None:
    import json
    from pathlib import Path
    from typing import cast

    import yaml
    from click.testing import CliRunner

    from svweave.cli import cli
    from tests.conftest import Ws, manifest_dict, profile

    workspace = cast(Ws, ws)
    workspace.write("svweave.yaml", yaml.safe_dump(manifest_dict({"p": profile()})))
    workspace.write(
        "svweave.local.yaml",
        yaml.safe_dump({"schema_version": "1.0", "execution": selection().model_dump()}),
    )
    root: Path = workspace.root
    runner = CliRunner()
    denied = runner.invoke(cli, ["--root", str(root), "execution-preflight"])
    assert json.loads(denied.output)["error"]["details"]["submitted"] is False
    local = runner.invoke(
        cli, ["--root", str(root), "--execution-provider", "local", "execution-preflight"]
    )
    assert local.exit_code == 0, local.output
    assert json.loads(local.output)["data"] == {
        "provider": "local",
        "execution_fingerprint": resolve_execution(
            ExecutionSelection(), ExecutionPolicy()
        ).fingerprint,
        "submitted": False,
        "remote_probe": "NOT_RUN",
    }


def test_SRUN_22_cli_override_inherits_other_selection_fields(ws: object) -> None:
    from typing import cast

    import yaml

    from svweave.application.options import WorkspaceOptions
    from tests.conftest import Ws, manifest_dict, profile

    workspace = cast(Ws, ws)
    workspace.write("svweave.yaml", yaml.safe_dump(manifest_dict({"p": profile()})))
    workspace.write(
        "svweave.local.yaml",
        yaml.safe_dump({"schema_version": "1.0", "execution": selection().model_dump()}),
    )
    ctx = WorkspaceOptions(root=workspace.root, execution_overrides={"allocation_id": "456"}).load()
    assert ctx.execution.selection.site_profile == "reviewed"
    assert ctx.execution.selection.allocation_id == "456"


def test_SRUN_22_exact_target_version_is_pinned() -> None:
    assert site().slurm_version == "22.05.9"
    with pytest.raises(ValidationError):
        site(slurm_version="25.05.0")


@pytest.mark.parametrize("cluster", ["all", "ALL", "All"])
def test_SRUN_03_rejects_all_clusters_selector(cluster: str) -> None:
    with pytest.raises(ValidationError):
        site(cluster=cluster)


@pytest.mark.parametrize("field", ["nodes", "tasks"])
@pytest.mark.parametrize("value", [True, False, 1.0, "1"])
def test_SRUN_03_node_task_literal_requires_exact_integer(field: str, value: object) -> None:
    with pytest.raises(ValidationError):
        ExecutionResources.model_validate({field: value})
    assert getattr(ExecutionResources.model_validate({field: 1}), field) == 1
