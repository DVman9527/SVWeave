"""SRUN-20: cached registry and injected factories obey current execution selection.

All registries are in-memory fakes; no backend or scheduler process is started.
"""

from __future__ import annotations

from typing import cast

import pytest
import yaml

from svweave.application.options import WorkspaceOptions
from svweave.application.session import Session
from svweave.orchestration.registry import BackendRegistry
from tests.conftest import Ws


class FakeRegistry:
    def __init__(self) -> None:
        self.closed = 0

    def shutdown(self) -> None:
        self.closed += 1


def select(ws: Ws, provider: str) -> None:
    execution = {"provider": provider}
    if provider == "srun":
        execution.update(site_profile="reviewed", allocation_id="123")
    ws.write(
        "svweave.local.yaml", yaml.safe_dump({"schema_version": "1.0", "execution": execution})
    )


def test_SRUN_20_cached_local_registry_is_revoked_on_remote_selection(ws: Ws) -> None:
    registries: list[FakeRegistry] = []

    def factory() -> BackendRegistry:
        fake = FakeRegistry()
        registries.append(fake)
        return cast(BackendRegistry, fake)

    session = Session(WorkspaceOptions(root=ws.root), registry_factory=factory)
    try:
        first = session.registry
        assert first is registries[0]
        assert session.registry is first
        assert len(registries) == 1
        select(ws, "srun")
        assert session.registry is None
        assert registries[0].closed == 1
        assert not session.registry_created
        assert session.registry_error is not None
        assert session.registry_error.details["reason"] == "srun_mcp_not_supported"
        assert session.registry is None
        assert len(registries) == 1  # factory cannot bypass the remote provider denial
        select(ws, "local")
        assert session.registry is registries[1]
        assert session.registry_error is None
        assert registries[0].closed == 1
    finally:
        session.close()
    assert registries[-1].closed == 1


def test_SRUN_20_remote_error_clears_after_local_selection(ws: Ws) -> None:
    select(ws, "srun")
    fake = FakeRegistry()
    called = 0

    def factory() -> BackendRegistry:
        nonlocal called
        called += 1
        return cast(BackendRegistry, fake)

    session = Session(WorkspaceOptions(root=ws.root), registry_factory=factory)
    try:
        assert session.registry is None
        assert called == 0
        assert session.registry_error is not None
        select(ws, "local")
        assert session.registry is fake
        assert called == 1
        assert session.registry_error is None
    finally:
        session.close()


def test_SRUN_20_invalid_config_revokes_cached_registry_and_recovers(ws: Ws) -> None:
    registries: list[FakeRegistry] = []

    def factory() -> BackendRegistry:
        fake = FakeRegistry()
        registries.append(fake)
        return cast(BackendRegistry, fake)

    session = Session(WorkspaceOptions(root=ws.root), registry_factory=factory)
    try:
        assert session.registry is not None
        ws.write("svweave.local.yaml", "schema_version: '1.0'\nexecution: unknown\n")
        assert session.registry is None
        assert registries[0].closed == 1
        select(ws, "local")
        assert session.registry is registries[1]
        assert session.registry_error is None
    finally:
        session.close()


def test_SRUN_20_production_registry_factory_also_obeys_transition(
    ws: Ws, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = FakeRegistry()
    calls = 0

    def make(*args: object, **kwargs: object) -> BackendRegistry:
        nonlocal calls
        calls += 1
        return cast(BackendRegistry, fake)

    monkeypatch.setattr(BackendRegistry, "for_workspace", make)
    session = Session(WorkspaceOptions(root=ws.root))
    try:
        assert session.registry is fake
        select(ws, "srun")
        assert session.registry is None
        assert calls == 1
        assert fake.closed == 1
    finally:
        session.close()
