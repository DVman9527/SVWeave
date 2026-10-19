"""Helpers: a stub COMPLETE build row (no pyslang) so evidence/report/status can be unit tested."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from svweave.application.build import DB_FILENAME
from svweave.application.options import WorkspaceOptions
from svweave.application.use_cases import create_snapshot
from svweave.storage.db import Database
from tests.conftest import Ws


def stub_build(
    ws: Ws, tag: str = "x", completeness: str = "COMPLETE"
) -> tuple[WorkspaceOptions, str, str]:
    opts = WorkspaceOptions(root=ws.root)
    snap = create_snapshot(opts, None, "req-fx")
    sid = snap.snapshot_id
    assert sid
    bid = hashlib.sha256(f"{sid}:{tag}".encode()).hexdigest()
    ctx = opts.load()
    # real identities: freshness now also compares config_id / toolchain_id with the workspace
    from svweave.application.toolchain import compute_toolchain
    from svweave.manifest.resolver import resolve_profile

    cfg_id = resolve_profile(ctx, "p").config_id
    tc_id = compute_toolchain(ctx.project_root)[0]
    with Database(ctx.state_dir / DB_FILENAME) as db:
        db.stage_build(
            {
                "build_id": bid,
                "workspace_id": ctx.workspace_id,
                "profile": "p",
                "snapshot_id": sid,
                "config_id": cfg_id,
                "toolchain_id": tc_id,
                "semantic_schema_version": "1",
                "completeness": completeness,
                "diagnostic_outcome": "NO_ERRORS",
            }
        )
    return opts, bid, sid


def write_artifact(ws: Ws, rel: str = "out/run.log", text: str = "log\n") -> tuple[str, str]:
    p = ws.write(rel, text)
    return rel, hashlib.sha256(p.read_bytes()).hexdigest()


def record(**over: Any) -> dict[str, Any]:
    r: dict[str, Any] = {
        "schema_version": "1.0",
        "kind": "simulation",
        "producer": "external_runner",
        "trust_class": "EXTERNAL_DECLARED",
        "profile": "p",
        "snapshot_id": None,
        "build_id": None,
        "test_name": "t0",
        "seed": "1",
        "run_id": "r1",
        "tool_version": "v1",
        "result": "PASS",
        "artifacts": [],
    }
    r.update(over)
    return r


def write_manifest(dirpath: Path, data: Any, name: str = "ev.json") -> Path:
    p = dirpath / name
    p.write_text(json.dumps(data))
    return p
