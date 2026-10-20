from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

import pytest
from click.testing import CliRunner

from svweave.application import doctor as doctor_mod
from svweave.application.doctor import CheckResult, DoctorContext, register_check
from svweave.application.templates import manifest_text
from svweave.cli import cli
from svweave.config.loader import load_manifest
from svweave.domain.envelope import Envelope
from tests.conftest import POLICY, Ws, profile


def run(root: Path, *args: str, **kw: Any) -> tuple[int, dict[str, Any]]:
    res = CliRunner().invoke(cli, ["--root", str(root), "--compact", *args], **kw)
    assert res.stdout.strip(), res.output
    data = json.loads(res.stdout)
    Envelope.model_validate(data)  # every stdout payload is a valid envelope
    return res.exit_code, data


def snapshot_tree(root: Path) -> dict[str, bytes]:
    return {
        str(p.relative_to(root)): p.read_bytes()
        for p in sorted(root.rglob("*"))
        if p.is_file() and ".svweave/snapshots" not in p.as_posix()
    }


# --------------------------------------------------------------------------- init
def test_init_creates_files_and_never_overwrites(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    proj = tmp_path / "My Project"
    proj.mkdir()
    code, out = run(proj, "init")
    assert code == 0 and out["outcome"] == "OK"
    assert set(out["data"]["created"]) == {
        ".svweave/",
        ".svweave/manifests/",
        ".svweave/snapshots/",
        "svweave.yaml",
    }
    assert out["data"]["skipped_existing"] == []
    pol_path = Path(out["data"]["trusted_policy"]["path"])
    assert out["data"]["trusted_policy"]["created"] is True
    assert not (proj / ".svweave" / "policy.json").exists()  # never inside the repo tree
    assert proj.resolve() not in pol_path.resolve().parents
    m = load_manifest(proj / "svweave.yaml")
    assert m.project.id == "My-Project" and m.editing.enabled is False
    pol = json.loads(pol_path.read_text())
    assert pol["allowed_roots"] == [str(proj.resolve())]  # absolute
    assert pol["network_enabled"] is False and pol["editing_enabled"] is False
    assert pol["approval_required"] is True
    # user edits survive a second init
    (proj / "svweave.yaml").write_text(manifest_text("x") + "# mine\n")
    pol_path.write_text(json.dumps({**POLICY, "allowed_roots": ["/src"]}))
    before = snapshot_tree(proj)
    code, out = run(proj, "init")
    assert code == 0 and out["data"]["created"] == []
    assert out["data"]["skipped_existing"] == ["svweave.yaml"]
    assert out["data"]["trusted_policy"]["created"] is False  # reported, never adopted/overwritten
    assert json.loads(pol_path.read_text())["allowed_roots"] == ["/src"]
    assert snapshot_tree(proj) == before
    # never touches global MCP config, git hooks, or anything outside the project
    assert list(home.iterdir()) == []
    assert not (proj / ".git").exists() and not (proj / ".mcp.json").exists()
    assert sorted(p.name for p in tmp_path.iterdir()) == ["My Project", "home", "xdg"]


def test_init_template_is_schema_valid_for_odd_dir_names(tmp_path: Path) -> None:
    for name in ("a b", "---", "über"):
        d = tmp_path / name
        d.mkdir()
        run(d, "init")
        assert load_manifest(d / "svweave.yaml").project.id


# --------------------------------------------------------------------------- doctor
def test_doctor_ok_after_init_and_exit_codes_for_failures(tmp_path: Path) -> None:
    proj = tmp_path / "p"
    proj.mkdir()
    run(proj, "init")
    code, out = run(proj, "doctor")
    assert code == 0 and out["outcome"] == "OK"
    names = {c["name"]: c["status"] for c in out["data"]["checks"]}
    assert names["manifest"] == names["policy"] == names["policy_tightening"] == "PASS"
    assert names["local_config"] == "SKIP" and names["root_mapping:project"] == "PASS"
    assert out["analysis_coverage"]["scope"] == "config_and_policy"
    # invalid config -> 2
    (proj / "svweave.yaml").write_text("schema_version: '1.0'\nbogus: 1\n")
    code, out = run(proj, "doctor")
    assert code == 2 and out["error"]["code"] == "INVALID_CONFIG"
    assert out["analysis_coverage"]["completeness"] == "BLOCKED"
    assert next(c for c in out["data"]["checks"] if c["name"] == "manifest")["status"] == "FAIL"


def test_doctor_missing_policy_and_policy_denied_and_unmapped_root(ws: Ws) -> None:
    ws.config({"p": profile()}, editing={"enabled": True})
    code, out = run(ws.root, "doctor")
    assert code == 6 and out["error"]["code"] == "POLICY_DENIED"
    assert out["error"]["details"]["check"] == "policy_tightening"
    ws.config({"p": profile()}, source_roots=[{"id": "project", "path": "."}, {"id": "uvm"}])
    code, out = run(ws.root, "doctor")
    assert code == 0 and any("uvm" in w for w in out["warnings"])
    ws.local([{"id": "uvm", "path": "does_not_exist"}])
    code, out = run(ws.root, "doctor")
    assert any("does not exist" in w for w in out["warnings"])
    (ws.root / "uvm_dir").mkdir()
    ws.local([{"id": "uvm", "path": "uvm_dir"}])
    code, out = run(ws.root, "doctor")
    # the client-boundary notice (M3) is always present and is the only warning
    assert [w for w in out["warnings"] if not w.startswith("client_boundary:")] == [] and code == 0
    ws.policy_path.unlink()
    code, out = run(ws.root, "doctor")
    assert (
        code == 2
        and next(c for c in out["data"]["checks"] if c["name"] == "policy")["status"] == "FAIL"
    )
    ws.write("svweave.local.yaml", "schema_version: '1.0'\nroot_mapping: []\n")
    code, out = run(ws.root, "doctor")
    assert next(c for c in out["data"]["checks"] if c["name"] == "local_config")["status"] == "FAIL"


def test_doctor_checks_are_pluggable(ws: Ws) -> None:
    @register_check
    def backend_stub(dc: DoctorContext) -> list[CheckResult]:
        return [CheckResult(name="backend:stub", status="WARN", message="not installed")]

    try:
        code, out = run(ws.root, "doctor")
        assert code == 0
        assert any(c["name"] == "backend:stub" for c in out["data"]["checks"])
        assert "backend:stub: not installed" in out["warnings"]
    finally:
        doctor_mod.CHECKS.remove(backend_stub)


# --------------------------------------------------------------------------- manifest / snapshot / gc
def test_manifest_resolve_success_and_exit_codes(ws: Ws) -> None:
    ws.config(
        {
            "ok": profile(source_inputs=[{"kind": "filelist", "path": "lists/main.f"}]),
            "blocked": profile(source_inputs=[{"kind": "filelist", "path": "lists/nope.f"}]),
        }
    )
    code, out = run(ws.root, "manifest", "resolve", "--profile", "ok")
    assert code == 0 and out["profile"] == "ok" and out["workspace_id"] == "t"
    cid = out["data"]["config_id"]
    assert (ws.root / out["data"]["manifest_path"]).name == f"{cid}.json"
    assert out["data"]["sources"] == 3 and out["data"]["ignored_options"][0]["option"] == "-l"
    assert out["analysis_coverage"]["completeness"] == "COMPLETE"
    assert "No semantic analysis" in out["analysis_coverage"]["limitations"][0]
    code, out = run(ws.root, "manifest", "resolve")  # default profile
    assert code == 0 and out["profile"] == "ok"
    code, out = run(ws.root, "manifest", "resolve", "--profile", "blocked")
    assert code == 3 and out["outcome"] == "BLOCKED" and out["error"]["code"] == "MISSING_INPUT"
    assert out["error"]["details"]["issues"][0]["reason"] == "filelist_missing"
    code, out = run(ws.root, "manifest", "resolve", "--profile", "ghost")
    assert code == 2 and out["error"]["code"] == "INVALID_CONFIG"


def test_manifest_resolve_exit_codes_unsupported_and_policy(neg: Ws, ws: Ws) -> None:
    neg.config(
        {"p": profile(source_inputs=[{"kind": "filelist", "path": "lists/unsupported_plus.f"}])}
    )
    code, out = run(neg.root, "manifest", "resolve", "--profile", "p")
    assert code == 3 and out["error"]["code"] == "UNSUPPORTED_OPTION"
    neg.config({"p": profile(source_inputs=[{"kind": "filelist", "path": "lists/escape.f"}])})
    code, out = run(neg.root, "manifest", "resolve", "--profile", "p")
    assert code == 6 and out["error"]["code"] == "PATH_NOT_ALLOWED"
    ws.write("lists/gen.f", "rtl/a.sv\n")
    ws.config(
        {
            "p": profile(
                source_inputs=[{"kind": "filelist", "path": "lists/gen.f"}],
                generated_inputs=[{"path": "rtl/a.sv"}],
            )
        }
    )
    code, out = run(ws.root, "manifest", "resolve", "--profile", "p")
    assert code == 0 and "incomplete provenance" in out["warnings"][0]


def test_snapshot_create_check_and_stale_exit_5(ws: Ws) -> None:
    ws.config(
        {
            "p": profile(
                source_inputs=[{"kind": "file", "path": "rtl/a.sv"}],
                include_dirs=["inc_a", "inc_b"],
            )
        }
    )
    code, out = run(ws.root, "snapshot", "create", "--profile", "p")
    assert code == 0 and out["freshness"] == "CURRENT" and len(out["snapshot_id"]) == 64
    sid = out["snapshot_id"]
    assert out["data"]["snapshot_dir"] == f".svweave/snapshots/{sid}"
    assert out["data"]["lookup_dirs"] == [
        {"dir": "rtl", "kind": "include", "entries": 2},
        {"dir": "inc_a", "kind": "include", "entries": 1},
        {"dir": "inc_b", "kind": "include", "entries": 1},
    ]
    assert (ws.root / ".svweave" / "svweave.db").is_file()
    code, out = run(ws.root, "snapshot", "check", "--snapshot", sid)
    assert code == 0 and out["freshness"] == "CURRENT" and out["data"]["changed"] == []
    ws.write("inc_a/common.svh", "`define A 1\n")  # SNP-02 through the CLI
    code, out = run(ws.root, "snapshot", "check", "--snapshot", sid)
    assert code == 5 and out["freshness"] == "STALE" and out["error"] is None
    assert {"path": "inc_a/common.svh", "change": "lookup_added"} in out["data"]["changed"]
    code, out = run(ws.root, "snapshot", "create", "--profile", "p")
    assert code == 0 and out["snapshot_id"] != sid
    code, out = run(ws.root, "snapshot", "check", "--snapshot", "xyz")
    assert code == 2
    code, out = run(ws.root, "snapshot", "check", "--snapshot", "0" * 64)
    assert code == 3 and out["error"]["details"]["reason"] == "snapshot_missing"


def test_snapshot_create_workspace_unstable_exit_4(ws: Ws, monkeypatch: pytest.MonkeyPatch) -> None:
    from svweave.domain.errors import ErrorCode, SvwError

    def boom(*a: Any, **k: Any) -> None:
        raise SvwError(ErrorCode.WORKSPACE_UNSTABLE, "changed during capture")

    monkeypatch.setattr("svweave.application.use_cases.capture_snapshot", boom)
    ws.config({"p": profile(source_inputs=[{"kind": "file", "path": "rtl/a.sv"}])})
    code, out = run(ws.root, "snapshot", "create", "--profile", "p")
    assert code == 5 and out["error"]["code"] == "WORKSPACE_UNSTABLE"


def test_cache_gc_dry_run_default_and_execute(ws: Ws) -> None:
    ws.config({"p": profile(source_inputs=[{"kind": "file", "path": "rtl/a.sv"}])})
    ids = []
    for i in range(5):
        (ws.root / "rtl" / "a.sv").write_text(f"module a; // {i}\nendmodule\n")
        ids.append(run(ws.root, "snapshot", "create", "--profile", "p")[1]["snapshot_id"])
    code, out = run(ws.root, "cache", "gc", "--dry-run")
    assert code == 0 and out["data"]["dry_run"] is True
    assert out["data"]["delete"] == [ids[1], ids[0]] and out["data"]["deleted"] == []
    code, out = run(ws.root, "cache", "gc")  # default is dry-run
    assert out["data"]["dry_run"] is True
    assert all((ws.root / ".svweave" / "snapshots" / i).is_dir() for i in ids)
    code, out = run(ws.root, "cache", "gc", "--no-dry-run")
    assert out["data"]["deleted"] == [ids[1], ids[0]]
    assert not (ws.root / ".svweave" / "snapshots" / ids[0]).exists()


def test_commands_need_policy_and_usage_errors(tmp_path: Path, ws: Ws) -> None:
    proj = tmp_path / "bare"
    proj.mkdir()
    (proj / "svweave.yaml").write_text(manifest_text("bare"))
    code, out = run(proj, "manifest", "resolve")
    assert code == 2 and "svweave init" in out["error"]["message"]
    res = CliRunner().invoke(cli, ["--root", str(ws.root), "bogus-command"])
    assert res.exit_code == 2  # click usage error -> schema/usage exit code
    res = CliRunner().invoke(cli, ["--root", str(ws.root), "snapshot", "check"])
    assert res.exit_code == 2
    res = CliRunner().invoke(cli, ["--help"])
    assert res.exit_code == 0 and "init" in res.output


def test_explicit_policy_and_manifest_paths(ws: Ws, tmp_path: Path) -> None:
    pol = tmp_path / "trusted" / "policy.json"
    pol.parent.mkdir()
    pol.write_text(json.dumps({**POLICY, "allowed_roots": [str(ws.root.resolve())]}))
    ws.policy_path.unlink()
    res = CliRunner().invoke(
        cli,
        [
            "--root",
            str(ws.root),
            "--manifest",
            str(ws.root / "svweave.yaml"),
            "--policy",
            str(pol),
            "--local",
            str(ws.root / "svweave.local.yaml"),
            "--request-id",
            "req-fixed",
            "doctor",
        ],
    )
    out = json.loads(res.stdout)
    assert res.exit_code == 0 and out["request_id"] == "req-fixed"
    assert "\n  " in res.stdout  # pretty by default


def test_schema_emit_and_check(tmp_path: Path) -> None:
    out_dir = tmp_path / "sch"
    code, out = run(tmp_path, "schema", "emit", "--out", str(out_dir))
    assert code == 0 and len(out["data"]["written"]) == 6 + 24  # models + evidence + edit + toolchain + MCP tool schemas
    code, out = run(tmp_path, "schema", "emit", "--out", str(out_dir), "--check")
    assert code == 0 and out["data"]["out_of_sync"] == []
    (out_dir / "local.schema.json").write_text("{}")
    code, out = run(tmp_path, "schema", "emit", "--out", str(out_dir), "--check")
    assert code == 2 and out["data"]["out_of_sync"] == ["local.schema.json"]
    code, out = run(tmp_path, "schema", "emit", "--out", "rel_schemas")  # relative to --root
    assert code == 0 and (tmp_path / "rel_schemas" / "manifest.schema.json").is_file()


def test_main_entrypoint_runs(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from svweave import cli as cli_mod

    proj = tmp_path / "proj"
    proj.mkdir()
    monkeypatch.setattr("sys.argv", ["svweave", "--root", str(proj), "init"])
    with pytest.raises(SystemExit) as ei:
        cli_mod.main()
    assert ei.value.code == 0
    assert os.path.isfile(proj / "svweave.yaml")
