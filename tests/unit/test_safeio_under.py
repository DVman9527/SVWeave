"""REV-S: reading a backend-writable tree follows no link in ANY component."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from svweave.domain.errors import ErrorCode, SvwError
from svweave.security import safeio


@pytest.fixture
def tree(tmp_path: Path) -> Path:
    base = tmp_path / "cand"
    (base / "rtl").mkdir(parents=True)
    (base / "rtl" / "a.sv").write_text("module a; endmodule\n")
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.sv").write_text("TOP SECRET\n")
    return base


def test_REV_S01_regular_file_is_read_and_a_missing_one_raises_filenotfound(tree: Path) -> None:
    assert safeio.read_regular_under(tree, "rtl/a.sv") == b"module a; endmodule\n"
    assert safeio.read_regular_under(tree, "rtl/a.sv", 6) == b"module"
    with pytest.raises(FileNotFoundError):
        safeio.read_regular_under(tree, "rtl/nope.sv")
    with pytest.raises(SvwError):  # a missing parent directory is refused like write_under does
        safeio.read_regular_under(tree, "nodir/a.sv")


def test_REV_S02_a_directory_symlink_in_the_middle_is_refused(tree: Path) -> None:
    os.symlink(tree.parent / "outside", tree / "linked")  # candidate/linked -> /outside
    # the old reader only refused a link at the FINAL component and followed this one:
    assert safeio.read_regular(tree / "linked" / "secret.sv") == b"TOP SECRET\n"
    with pytest.raises(SvwError) as e:
        safeio.read_regular_under(tree, "linked/secret.sv")
    assert e.value.code is ErrorCode.POLICY_DENIED


def test_REV_S03_final_symlink_unsafe_paths_and_non_regular_files_are_refused(tree: Path) -> None:
    os.symlink(tree.parent / "outside" / "secret.sv", tree / "rtl" / "l.sv")
    for rel in ("rtl/l.sv", "../outside/secret.sv", "/etc/passwd", "rtl/../rtl/a.sv", ""):
        with pytest.raises(SvwError) as e:
            safeio.read_regular_under(tree, rel)
        assert e.value.code is ErrorCode.POLICY_DENIED, rel
    os.mkfifo(tree / "rtl" / "pipe.sv")
    with pytest.raises(SvwError):
        safeio.read_regular_under(tree, "rtl/pipe.sv")  # not a regular file (and never blocks)
