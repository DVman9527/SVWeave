"""Workspace for the plan-21 demo fixtures (fixtures/demo), mirroring svweave.yaml rtl_demo."""

from __future__ import annotations

import shutil
from pathlib import Path
from typing import Any

from tests.conftest import FIXTURES, Ws, profile


def demo_ws(tmp: Path, lanes: int = 2, data_w: int = 32, *, editing: bool = False, **prof: Any) -> Ws:
    root = tmp / "ws"
    shutil.copytree(FIXTURES / "demo", root / "fixtures" / "demo")
    w = Ws(root)
    base: dict[str, Any] = {
        "source_inputs": [
            {"kind": "filelist", "path": "fixtures/demo/filelists/rtl.f", "path_mode": "cwd"}
        ],
        "top_modules": ["demo_top"],
        "defines": [{"name": "SVW_DEMO", "value": "1"}],
        "include_dirs": ["fixtures/demo/include"],
        "top_parameters": [
            {"top": "demo_top", "name": "DATA_W", "value": str(data_w)},
            {"top": "demo_top", "name": "LANES", "value": str(lanes)},
        ],
    }
    base.update(prof)
    if not editing:
        w.config({"p": profile(**base)})
        w.policy()
        return w
    # editing enabled (test approval token allowed): the demo sources are the writable root
    w.config(
        {"p": profile(**base)},
        source_roots=[
            {"id": "project", "path": ".", "include": ["fixtures/**"],
             "exclude": ["**/.svweave/**"], "writable": False},
            {"id": "demo", "path": "fixtures/demo", "include": ["**"], "exclude": [],
             "writable": True},  # a writable root must be covered by policy.writable_paths
        ],
        editing={"enabled": True, "allowed_paths": ["fixtures/demo/**"], "max_files_per_patch": 5,
                 "max_changed_lines": 60, "require_human_approval": True,
                 "validate_profiles": ["p"]},
    )
    w.policy(editing_enabled=True, allow_test_approvals=True, writable_paths=["fixtures/demo/**"])
    return w
