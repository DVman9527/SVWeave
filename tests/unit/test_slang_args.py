from __future__ import annotations

import shlex
from pathlib import Path
from typing import Any

import pytest

from svweave.adapters.slang.args import build_invocation
from svweave.domain.errors import ErrorCode, SvwError
from svweave.manifest.resolver import resolve_profile
from tests.conftest import Ws, profile


def nm_for(ws: Ws, **prof: Any) -> Any:
    ws.config({"p": profile(**prof)})
    return resolve_profile(ws.ctx(), "p", {})


def test_single_and_per_file_delegate_to_the_verified_argv(ws: Ws, tmp_path: Path) -> None:
    nm = nm_for(
        ws,
        source_inputs=[{"kind": "file", "path": "rtl/a.sv"}],
        top_modules=["a"],
        defines=[{"name": "X", "value": '"q r"'}, {"name": "Y"}],
        include_dirs=["inc_b"],
        top_parameters=[{"top": "a", "name": "W", "value": "8"}],
    )
    inv = build_invocation(nm, Path("/snap/files"), tmp_path / "g")
    assert inv.args == [
        "--single-unit",
        "-I",
        "/snap/files/inc_b",
        "-D",
        'X="q r"',
        "-D",
        "Y",
        "--top",
        "a",
        "-G",
        "a.W=8",
        "--libext",
        ".v",
        "--libext",
        ".sv",
        "/snap/files/rtl/a.sv",
    ]
    assert inv.group_files == []
    per = nm_for(
        ws,
        source_inputs=[{"kind": "file", "path": "rtl/a.sv"}],
        top_modules=["a"],
        compilation_units={"mode": "per_file"},
    )
    assert "--single-unit" not in build_invocation(per, Path("/s"), tmp_path / "g").args


def test_explicit_groups_write_one_command_file_per_group(ws: Ws, tmp_path: Path) -> None:
    grp = lambda gid, v: {  # noqa: E731
        "id": gid,
        "sources": [{"kind": "file", "path": "macros/use.sv"}],
        "include_dirs": ["inc_b"],
        "defines": [{"name": "MSG", "value": v}],
    }
    nm = nm_for(
        ws,
        source_inputs=[],
        top_modules=["a"],
        compilation_units={
            "mode": "explicit_groups",
            "groups": [grp("g1", '"a b"'), grp("g2", "7")],
        },
    )
    inv = build_invocation(nm, Path("/snap/files"), tmp_path / "run" / "groups")
    assert inv.args == [
        "--top",
        "a",
        "-C",
        str(tmp_path / "run/groups/cu_g1.f"),
        "-C",
        str(tmp_path / "run/groups/cu_g2.f"),
    ]
    assert [g.path for g in inv.group_files] == [
        str(tmp_path / "run/groups/cu_g1.f"),
        str(tmp_path / "run/groups/cu_g2.f"),
    ]
    t1 = (tmp_path / "run/groups/cu_g1.f").read_text()
    # absolute paths only (relative paths in a -C file resolve against the file itself), shell-quoted
    assert shlex.split(t1) == [
        "-I",
        "/snap/files/inc_b",
        "-D",
        'MSG="a b"',
        "/snap/files/macros/use.sv",
    ]
    assert shlex.split((tmp_path / "run/groups/cu_g2.f").read_text())[3] == "MSG=7"
    import hashlib

    assert inv.group_files[0].sha256 == hashlib.sha256(t1.encode()).hexdigest()


def test_group_with_library_inputs_and_conflicting_top_params_are_rejected(
    ws: Ws, tmp_path: Path
) -> None:
    g = {
        "id": "g1",
        "sources": [{"kind": "file", "path": "rtl/a.sv"}],
        "library_dirs": ["libs/lib_a"],
    }
    nm = nm_for(
        ws,
        source_inputs=[],
        top_modules=["a"],
        compilation_units={"mode": "explicit_groups", "groups": [g]},
    )
    with pytest.raises(SvwError) as ei:
        build_invocation(nm, Path("/s"), tmp_path / "g")
    assert ei.value.code == ErrorCode.CAPABILITY_MISMATCH
    g2 = {"id": "g1", "sources": [{"kind": "file", "path": "rtl/a.sv"}]}
    nm2 = nm_for(
        ws,
        source_inputs=[],
        top_modules=["a", "b"],
        top_parameters=[
            {"top": "a", "name": "W", "value": "1"},
            {"top": "b", "name": "W", "value": "2"},
        ],
        compilation_units={"mode": "explicit_groups", "groups": [g2]},
    )
    # B5: the top-scoped form expresses different values per top (no global -G NAME=VALUE)
    args2 = build_invocation(nm2, Path("/s"), tmp_path / "g").args
    assert [x for x in args2 if x in ("-G", "a.W=1", "b.W=2")] == ["-G", "a.W=1", "-G", "b.W=2"]
    assert "W=1" not in args2 and "W=2" not in args2
