"""SEM/SEC/API preparation tests only. No native parser, EDA or simulator invoked."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from click.testing import CliRunner

from svweave.adapters.verilator.protected import prepare_protected_source
from svweave.application.options import WorkspaceOptions
from svweave.application.source_preparation import prepare_source
from svweave.cli import cli
from svweave.domain.errors import SvwError
from tests.conftest import FIXTURES, Ws

BEGIN = b"`pragma protect begin_protected\n"
END = b"`pragma protect end_protected\n"


def test_SEM_mixed_fixture_preserves_plaintext_and_unknown_boundary() -> None:
    source = (FIXTURES / "verilator/protected/mixed.sv").read_bytes()
    prepared, report = prepare_protected_source(source)
    gap = report["gaps"][0]
    assert prepared[: gap["start_byte"]] == source[: gap["start_byte"]]
    assert prepared[gap["end_byte"] :] == source[gap["end_byte"] :]
    assert b"encrypted_unknown u_unknown()" in prepared
    assert b"U1lOVEh" not in prepared
    assert report["missing_definitions"] == report["encrypted_interfaces"] == "UNKNOWN"
    assert gap["ports"] == "UNKNOWN"
    assert report["static_compile"] == report["simulation"] == "NOT_RUN"
    assert report["completeness"] == "PARTIAL"


def test_SEM_whole_file_is_not_silently_dropped() -> None:
    source = (FIXTURES / "verilator/protected/fully_encrypted.sv").read_bytes()
    prepared, report = prepare_protected_source(source)
    assert len(source) == len(prepared)
    assert not prepared.strip()
    assert report["skipped_region_count"] == 1


@pytest.mark.parametrize("newline", [b"\n", b"\r\n"])
@pytest.mark.parametrize("final_newline", [True, False])
def test_LOC_byte_and_line_mapping(newline: bytes, final_newline: bool) -> None:
    source = b"module m;\n" + BEGIN + b'opaque"/*\\\n' + END + b"endmodule\n"
    source = source.replace(b"\n", newline)
    if not final_newline:
        source = source.rstrip(b"\r\n")
    prepared, report = prepare_protected_source(source)
    assert len(source) == len(prepared)
    assert [i for i, c in enumerate(source) if c in (10, 13)] == [
        i for i, c in enumerate(prepared) if c in (10, 13)
    ]
    assert report["gaps"][0]["start_line"] == 2
    assert report["gaps"][0]["end_line"] == 4


@pytest.mark.parametrize(
    "source",
    [
        b"// `pragma protect begin_protected\nmodule m; endmodule\n",
        b"/*\n`pragma protect begin_protected\n*/\n",
        b'string s = "`pragma protect begin_protected";\n',
        b'string s = """\n`pragma protect begin_protected\n""";\n',
        b"wire \\`protected ;\n",
        b'string s = "escaped \\" `protected";\n',
    ],
)
def test_SEC_comments_strings_identifiers_are_not_directives(source: bytes) -> None:
    prepared, report = prepare_protected_source(source)
    assert prepared == source
    assert not report["gaps"]


@pytest.mark.parametrize(
    "source",
    [
        BEGIN,
        END,
        BEGIN + BEGIN + END,
        BEGIN + b"`endprotected\n",
        b"`protected\n" + END,
        b"`pragma protect begin\n",
        b"`pragma protect data_block\nopaque\n",
        b"module x; " + BEGIN + END,
        BEGIN + b"`pragma protect end_protected junk\n",
        BEGIN + b"`protected junk\n" + END,
        b"`define F " + BEGIN + END,
        b"`define F \\\n" + BEGIN + END,
        b"/*\n*/ " + BEGIN + END,
        b"`pragma protect begin_protected /*\n" + END,
        BEGIN + b"`pragma protect end_protected /*\n",
        b"`protected\n`endprotected /*\n",
        BEGIN + b"`pragma protect end\n" + END,
        b"`protected\n`pragma protect data_block `endprotected\nopaque\n`endprotected\n",
        b"\x00",
        BEGIN.replace(b"\n", b"\r") + END,
    ],
)
def test_SEC_ambiguous_or_malformed_fails_closed(source: bytes) -> None:
    with pytest.raises(SvwError):
        prepare_protected_source(source)


def test_SEM_legacy_multiple_regions_and_empty_source() -> None:
    source = b"`protected\nopaque\n`endprotected\nmodule m; endmodule\n" + BEGIN + END
    prepared, report = prepare_protected_source(source)
    assert b"module m; endmodule" in prepared
    assert report["skipped_region_count"] == 2
    assert prepare_protected_source(b"")[0] == b""


def test_API_prepare_is_derived_only_with_auditable_report(ws: Ws) -> None:
    path = ws.root / "encrypted.sv"
    source = BEGIN + b"opaque\n" + END
    path.write_bytes(source)
    env = prepare_source(WorkspaceOptions(ws.root), Path("encrypted.sv"), "r")
    assert path.read_bytes() == source
    assert env.outcome == "PARTIAL"
    assert env.snapshot_id is None and env.build_id is None
    assert (ws.root / env.data["prepared_source"]).read_bytes() == prepare_protected_source(source)[
        0
    ]
    report = json.loads((ws.root / env.data["report_path"]).read_bytes())
    assert report["gaps"] == env.data["gaps"]
    assert not (ws.root / ".svweave/svweave.db").exists()
    assert prepare_source(WorkspaceOptions(ws.root), Path("encrypted.sv"), "r").data == env.data


def test_API_invalid_source_does_not_publish(ws: Ws) -> None:
    ws.write("bad.sv", BEGIN.decode())
    with pytest.raises(SvwError):
        prepare_source(WorkspaceOptions(ws.root), Path("bad.sv"), "r")
    assert not (ws.root / ".svweave/supplements/prepared").exists()


def test_API_cli_requires_explicit_partial_acceptance(ws: Ws) -> None:
    ws.write("encrypted.sv", (BEGIN + END).decode())
    args = ["--root", str(ws.root), "supplement", "prepare-source", "--source", "encrypted.sv"]
    result = CliRunner().invoke(cli, args)
    assert result.exit_code == 3, result.output
    assert json.loads(result.output)["data"]["static_compile"] == "NOT_RUN"
    result = CliRunner().invoke(cli, [*args, "--allow-partial"])
    assert result.exit_code == 0, result.output


@pytest.mark.parametrize("kind", ["symlink", "traversal", "storage_symlink"])
def test_SEC_preparation_path_safety(ws: Ws, tmp_path: Path, kind: str) -> None:
    ws.write("source.sv", (BEGIN + END).decode())
    source = Path("source.sv")
    if kind == "symlink":
        (ws.root / "link.sv").symlink_to(ws.root / source)
        source = Path("link.sv")
    elif kind == "traversal":
        source = Path("x/../source.sv")
    else:
        (ws.root / ".svweave").mkdir(exist_ok=True)
        (ws.root / ".svweave/supplements").symlink_to(tmp_path, target_is_directory=True)
    with pytest.raises(SvwError):
        prepare_source(WorkspaceOptions(ws.root), source, "r")


def test_SEC_resource_limits(monkeypatch: pytest.MonkeyPatch) -> None:
    from svweave.adapters.verilator import protected

    monkeypatch.setattr(protected, "SOURCE_LIMIT", 4)
    with pytest.raises(SvwError, match="RESOURCE_LIMIT"):
        prepare_protected_source(b"12345")
    monkeypatch.setattr(protected, "SOURCE_LIMIT", 10000)
    monkeypatch.setattr(protected, "GAP_LIMIT", 1)
    with pytest.raises(SvwError, match="RESOURCE_LIMIT"):
        prepare_protected_source((BEGIN + END) * 2)


def test_SEM_comments_on_delimiters_and_conditional_branches() -> None:
    source = (
        b"`ifdef UNUSED\n/* comment */ `pragma protect begin_protected // comment\n"
        b"opaque\n`pragma protect end_protected /* closed */\n`endif\n"
    )
    prepared, report = prepare_protected_source(source)
    assert prepared.startswith(b"`ifdef UNUSED\n") and prepared.endswith(b"`endif\n")
    assert report["skipped_region_count"] == 1
