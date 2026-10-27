"""SEM/SEC/API: external inventory isolation, bounded IO and CLI retrieval (no backend)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from click.testing import CliRunner

from svweave.application import supplement
from svweave.application.options import WorkspaceOptions
from svweave.cli import cli
from svweave.domain.errors import ErrorCode, SvwError
from tests.conftest import FIXTURES, Ws


def artifacts(ws: Ws) -> tuple[Path, Path]:
    stem = "t_constraint_json_only"
    tree = ws.write("external/tree.json", (FIXTURES / "verilator" / f"{stem}.out").read_text())
    meta = ws.write(
        "external/meta.json",
        (FIXTURES / "verilator" / f"{stem}.synthetic.tree.meta.json").read_text(),
    )
    return tree, meta


def test_SEM_supplement_roundtrip_is_unbound_and_non_authoritative(ws: Ws) -> None:
    tree, meta = artifacts(ws)
    opts = WorkspaceOptions(ws.root)
    env = supplement.import_verilator(opts, tree, meta, "5.052", "r")
    sid = env.data["supplement_id"]
    assert env.snapshot_id is None and env.build_id is None
    assert env.provenance.evidence_kind == "EXTERNAL_DECLARED"
    assert env.analysis_coverage.completeness == "PARTIAL"
    assert not (ws.root / ".svweave/svweave.db").exists()
    assert supplement.import_verilator(opts, tree, meta, "5.052", "r").data["supplement_id"] == sid
    page = supplement.show_verilator(opts, sid, "r", limit=1)
    assert len(page.data["items"]) == 1
    assert page.data["next_offset"] == 1
    assert page.pagination.truncated
    item = page.data["items"][0]
    filtered = supplement.show_verilator(
        opts,
        sid,
        "r",
        name=item["name"],
        node_type=item["node_type"],
        source_file=item["source_file"],
    )
    assert item in filtered.data["items"]
    assert supplement.show_verilator(opts, sid, "r", name="does-not-exist").data["items"] == []
    assert supplement.show_verilator(opts, sid, "r", offset=10000).data["next_offset"] is None


def test_SEC_supplement_producer_filenames_never_opened(ws: Ws) -> None:
    tree, meta = artifacts(ws)
    metadata = json.loads(meta.read_text())
    for entry in metadata["files"].values():
        entry["filename"] = "/not-authorized/secret.sv"
    meta.write_text(json.dumps(metadata))
    env = supplement.import_verilator(WorkspaceOptions(ws.root), tree, meta, "5.052", "r")
    assert env.data["source_binding"] == "UNVERIFIED"


@pytest.mark.parametrize("kind", ["outside", "symlink", "parent_symlink", "traversal", "fifo"])
def test_SEC_supplement_unsafe_input(ws: Ws, tmp_path: Path, kind: str) -> None:
    tree, meta = artifacts(ws)
    if kind == "outside":
        tree = tmp_path / "outside.json"
        tree.write_text("{}")
    elif kind == "symlink":
        target = tree
        tree = ws.root / "link.json"
        tree.symlink_to(target)
    elif kind == "parent_symlink":
        (ws.root / "link").symlink_to(tree.parent, target_is_directory=True)
        tree = ws.root / "link/tree.json"
    elif kind == "fifo":
        import os

        tree = ws.root / "pipe"
        os.mkfifo(tree)
    else:
        tree = ws.root / "external/../external/tree.json"
    with pytest.raises(SvwError) as exc:
        supplement.import_verilator(WorkspaceOptions(ws.root), tree, meta, "5.052", "r")
    assert exc.value.code in {ErrorCode.PATH_NOT_ALLOWED, ErrorCode.POLICY_DENIED}


def test_SEC_supplement_bounded_read(ws: Ws, monkeypatch: pytest.MonkeyPatch) -> None:
    tree, meta = artifacts(ws)
    monkeypatch.setattr(supplement, "TREE_LIMIT", 10)
    with pytest.raises(SvwError, match="RESOURCE_LIMIT"):
        supplement.import_verilator(WorkspaceOptions(ws.root), tree, meta, "5.052", "r")


def test_SEC_supplement_storage_integrity_and_containment(ws: Ws, tmp_path: Path) -> None:
    tree, meta = artifacts(ws)
    opts = WorkspaceOptions(ws.root)
    sid = supplement.import_verilator(opts, tree, meta, "5.052", "r").data["supplement_id"]
    record = ws.root / supplement.STORE / sid / "record.json"
    record.write_text("{}")
    with pytest.raises(SvwError, match="hash mismatch"):
        supplement.show_verilator(opts, sid, "r")
    with pytest.raises(SvwError, match="INVALID_CONFIG"):
        supplement.show_verilator(opts, "../escape", "r")
    record.unlink()
    record.symlink_to(meta)
    with pytest.raises(SvwError, match="POLICY_DENIED"):
        supplement.show_verilator(opts, sid, "r")
    record.unlink()


def test_SEC_supplement_storage_parent_symlink(ws: Ws, tmp_path: Path) -> None:
    tree, meta = artifacts(ws)
    (ws.root / ".svweave").mkdir(exist_ok=True)
    (ws.root / ".svweave/supplements").symlink_to(tmp_path, target_is_directory=True)
    with pytest.raises(SvwError, match="POLICY_DENIED"):
        supplement.import_verilator(WorkspaceOptions(ws.root), tree, meta, "5.052", "r")
    assert not (tmp_path / "verilator").exists()


def test_API_supplement_cli_exit_and_query(ws: Ws) -> None:
    tree, meta = artifacts(ws)
    runner = CliRunner()
    args = ["--root", str(ws.root), "--compact", "supplement"]
    result = runner.invoke(
        cli,
        [*args, "verilator", "--tree", str(tree), "--meta", str(meta), "--tool-version", "5.052"],
    )
    assert result.exit_code == 0, result.output
    imported = json.loads(result.stdout)
    sid = imported["data"]["supplement_id"]
    for allow, code in [(False, 3), (True, 0)]:
        result = runner.invoke(
            cli, [*args, "show", "--supplement", sid, *(["--allow-partial"] if allow else [])]
        )
        assert result.exit_code == code, result.output
        assert json.loads(result.stdout)["snapshot_id"] is None
    result = runner.invoke(cli, [*args, "show", "--supplement", "../../escape"])
    assert result.exit_code == 2


def test_SEC_supplement_audit_artifact_tamper(ws: Ws) -> None:
    tree, meta = artifacts(ws)
    opts = WorkspaceOptions(ws.root)
    sid = supplement.import_verilator(opts, tree, meta, "5.052", "r").data["supplement_id"]
    (ws.root / supplement.STORE / sid / "tree.json").write_text("{}")
    with pytest.raises(SvwError, match="audit artifact hash mismatch"):
        supplement.show_verilator(opts, sid, "r")


def test_API_supplement_limits_response_bytes(ws: Ws, monkeypatch: pytest.MonkeyPatch) -> None:
    tree, meta = artifacts(ws)
    opts = WorkspaceOptions(ws.root)
    sid = supplement.import_verilator(opts, tree, meta, "5.052", "r").data["supplement_id"]
    first = supplement.show_verilator(opts, sid, "r", limit=1)
    monkeypatch.setattr(
        supplement, "MAX_RESPONSE_BYTES", len(first.to_json(indent=2).encode()) + 10
    )
    result = supplement.show_verilator(opts, sid, "r", limit=100)
    assert len(result.data["items"]) == 1
    assert result.data["next_offset"] == 1
    monkeypatch.setattr(supplement, "MAX_RESPONSE_BYTES", 1)
    with pytest.raises(SvwError, match="OUTPUT_LIMIT"):
        supplement.show_verilator(opts, sid, "r")


def test_SEC_supplement_forged_content_address_still_validates_schema(ws: Ws) -> None:
    import hashlib
    import shutil

    tree, meta = artifacts(ws)
    opts = WorkspaceOptions(ws.root)
    sid = supplement.import_verilator(opts, tree, meta, "5.052", "r").data["supplement_id"]
    stored = ws.root / supplement.STORE / sid
    for mutation in ["missing_version", "false_binding", "forged_items"]:
        record = json.loads((stored / "record.json").read_bytes())
        if mutation == "missing_version":
            del record["inventory"]["tool_version"]
        elif mutation == "false_binding":
            record["source_binding"] = "VERIFIED"
        else:
            record["inventory"]["items"] = [None]
        raw = json.dumps(record, sort_keys=True, separators=(",", ":")).encode()
        forged_id = hashlib.sha256(raw).hexdigest()
        target = ws.root / supplement.STORE / forged_id
        shutil.copytree(stored, target)
        (target / "record.json").write_bytes(raw)
        with pytest.raises(SvwError, match="INVALID_CONFIG"):
            supplement.show_verilator(opts, forged_id, "r")


def test_SEC_supplement_deep_stored_json(ws: Ws) -> None:
    import hashlib

    raw = b"[" * 2000 + b"0" + b"]" * 2000
    sid = hashlib.sha256(raw).hexdigest()
    target = ws.root / supplement.STORE / sid
    target.mkdir(parents=True)
    (target / "record.json").write_bytes(raw)
    with pytest.raises(SvwError, match="INVALID_CONFIG"):
        supplement.show_verilator(WorkspaceOptions(ws.root), sid, "r")
