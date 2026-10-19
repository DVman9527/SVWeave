from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

import jsonschema
import pytest
import yaml
from pydantic import BaseModel

from svweave.config.loader import (
    load_local_config,
    load_manifest,
    load_policy,
    policy_hash,
)
from svweave.config.models import LocalConfig, Manifest, Policy
from svweave.config.paths import PathPolicy, glob_match
from svweave.config.schemas import SCHEMA_MODELS, check_schemas, emit_schemas, schema_text
from svweave.domain.errors import ErrorCode, SvwError
from tests.conftest import FIXTURES, POLICY, Ws, manifest_dict, profile

REPO = Path(__file__).resolve().parents[2]


def _expect(code: ErrorCode, fn: Any, *a: Any, **k: Any) -> SvwError:
    with pytest.raises(SvwError) as ei:
        fn(*a, **k)
    assert ei.value.code == code, ei.value
    return ei.value


# ------------------------------------------------------------------ schema strictness
def test_API_01_unknown_field_rejected_with_location_and_line(ws: Ws) -> None:
    m = manifest_dict({"p": profile(incude_dirs=["x"])})  # typo
    ws.write("svweave.yaml", yaml.safe_dump(m, sort_keys=False))
    e = _expect(ErrorCode.INVALID_CONFIG, load_manifest, ws.root / "svweave.yaml")
    issue = e.details["issues"][0]
    assert issue["location"] == "profiles.p.incude_dirs"
    assert issue["type"] == "extra_forbidden"
    assert isinstance(issue["line"], int) and issue["line"] > 1
    assert "incude_dirs" in e.message and f"line {issue['line']}" in e.message


def test_API_01_wrong_types_and_bad_enum_rejected(ws: Ws) -> None:
    for bad in (
        profile(compilation_units={"mode": "multi"}),
        profile(defines=[{"name": "X", "value": 1}]),  # int where string expected
        profile(defines=[{"name": "1bad"}]),
        profile(source_inputs=[]),
        profile(library_extensions=["v"]),
        profile(top_modules=["bad name"]),
        profile(environment_allowlist=["1X"]),
    ):
        ws.write("svweave.yaml", yaml.safe_dump(manifest_dict({"p": bad})))
        _expect(ErrorCode.INVALID_CONFIG, load_manifest, ws.root / "svweave.yaml")


def test_API_01_top_level_and_cross_field_rules(ws: Ws) -> None:
    good = manifest_dict({"p": profile()})
    cases: list[dict[str, Any]] = [
        {**good, "network": True},  # repo config can't even express network
        {**good, "schema_version": "2.0"},
        {**good, "project": {**good["project"], "default_profile": "nope"}},
        {**good, "source_roots": []},
        {**good, "source_roots": [good["source_roots"][0]] * 2},
        {**good, "profiles": {}},
        {**good, "editing": {"enabled": False, "validate_profiles": ["nope"]}},
        {**good, "editing": {"allow_all": True}},
    ]
    for c in cases:
        ws.write("svweave.yaml", yaml.safe_dump(c))
        _expect(ErrorCode.INVALID_CONFIG, load_manifest, ws.root / "svweave.yaml")


def test_explicit_groups_rules(ws: Ws) -> None:
    grp = {"id": "g1", "sources": [{"kind": "file", "path": "rtl/a.sv"}]}
    ok = profile(source_inputs=[], compilation_units={"mode": "explicit_groups", "groups": [grp]})
    ws.write("svweave.yaml", yaml.safe_dump(manifest_dict({"p": ok})))
    assert (
        load_manifest(ws.root / "svweave.yaml").profiles["p"].compilation_units.groups[0].id == "g1"
    )
    bad_cases = [
        profile(source_inputs=[], compilation_units={"mode": "explicit_groups"}),  # no groups
        profile(compilation_units={"mode": "single", "groups": [grp]}),
        # explicit groups must not inherit profile-level defines
        profile(
            source_inputs=[],
            defines=[{"name": "X"}],
            compilation_units={"mode": "explicit_groups", "groups": [grp]},
        ),
        profile(
            source_inputs=[],
            compilation_units={"mode": "explicit_groups", "groups": [grp, grp]},
        ),
        profile(
            source_inputs=[],
            compilation_units={
                "mode": "explicit_groups",
                "groups": [{"id": "g", "sources": []}],
            },
        ),
    ]
    for bad in bad_cases:
        ws.write("svweave.yaml", yaml.safe_dump(manifest_dict({"p": bad})))
        _expect(ErrorCode.INVALID_CONFIG, load_manifest, ws.root / "svweave.yaml")


def test_yaml_errors_have_location_and_duplicate_keys_rejected(ws: Ws) -> None:
    ws.write("svweave.yaml", "schema_version: '1.0'\nproject: [unclosed\n")
    e = _expect(ErrorCode.INVALID_CONFIG, load_manifest, ws.root / "svweave.yaml")
    assert "line" in e.details
    ws.write("svweave.yaml", "schema_version: '1.0'\nschema_version: '1.0'\n")
    e = _expect(ErrorCode.INVALID_CONFIG, load_manifest, ws.root / "svweave.yaml")
    assert "duplicate key" in e.message
    ws.write("svweave.yaml", "- a\n- b\n")
    _expect(ErrorCode.INVALID_CONFIG, load_manifest, ws.root / "svweave.yaml")
    ws.write("svweave.yaml", "x: !!python/object/apply:os.system ['echo pwned']\n")
    _expect(ErrorCode.INVALID_CONFIG, load_manifest, ws.root / "svweave.yaml")
    _expect(ErrorCode.INVALID_CONFIG, load_manifest, ws.root / "absent.yaml")


def test_local_config_and_policy_strict(ws: Ws) -> None:
    ws.local([{"id": "uvm", "path": ".toolchain/uvm"}])
    assert load_local_config(ws.root / "svweave.local.yaml").root_mappings[0].id == "uvm"
    ws.write("svweave.local.yaml", "schema_version: '1.0'\nroot_mapping: []\n")  # typo
    _expect(ErrorCode.INVALID_CONFIG, load_local_config, ws.root / "svweave.local.yaml")
    ws.local([{"id": "u", "path": "a"}, {"id": "u", "path": "b"}])
    _expect(ErrorCode.INVALID_CONFIG, load_local_config, ws.root / "svweave.local.yaml")

    pp = ws.policy_path
    assert load_policy(pp).network_enabled is False
    for bad in ({**POLICY, "extra": 1}, {**POLICY, "allowed_roots": []}, [1], {"x": 1}):
        pp.write_text(json.dumps(bad))
        _expect(ErrorCode.INVALID_CONFIG, load_policy, pp)
    pp.write_text("{not json")
    e = _expect(ErrorCode.INVALID_CONFIG, load_policy, pp)
    assert "line" in e.details
    h1 = policy_hash(Policy.model_validate(POLICY))
    h2 = policy_hash(Policy.model_validate({**POLICY, "writable_paths": ["a/**"]}))
    assert h1 != h2


def test_oversized_config_rejected(ws: Ws) -> None:
    ws.write("svweave.yaml", "# " + "x" * (1024 * 1024 + 10) + "\n")
    _expect(ErrorCode.INVALID_CONFIG, load_manifest, ws.root / "svweave.yaml")


# ------------------------------------------------------------------ JSON schemas
def test_schemas_in_sync_with_models(tmp_path: Path) -> None:
    assert check_schemas(REPO / "schemas") == []
    out = emit_schemas(tmp_path / "s")
    assert sorted(p.name for p in out) == sorted(SCHEMA_MODELS)
    assert check_schemas(tmp_path / "s") == []
    (tmp_path / "s" / "policy.schema.json").write_text("{}")
    assert check_schemas(tmp_path / "s") == ["policy.schema.json"]
    assert sorted(check_schemas(tmp_path / "missing")) == sorted(SCHEMA_MODELS)


def test_API_01_json_schema_rejects_unknown_fields_and_accepts_example() -> None:
    schema = json.loads((REPO / "schemas" / "manifest.schema.json").read_text())
    jsonschema.Draft202012Validator.check_schema(schema)
    example = yaml.safe_load((REPO / "svweave.yaml").read_text())
    jsonschema.validate(example, schema)
    bad = json.loads(json.dumps(example))
    bad["profiles"]["rtl_demo"]["defnes"] = []
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(bad, schema)
    bad2 = json.loads(json.dumps(example))
    bad2["editing"]["network"] = True
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(bad2, schema)
    pol = json.loads((REPO / "schemas" / "policy.schema.json").read_text())
    jsonschema.validate(POLICY, pol)
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate({**POLICY, "x": 1}, pol)
    for model in SCHEMA_MODELS.values():
        assert isinstance(model, type) and issubclass(model, BaseModel)
        assert json.loads(schema_text(model))["additionalProperties"] is False


def test_example_svweave_yaml_at_repo_root_is_valid() -> None:
    m = load_manifest(REPO / "svweave.yaml")
    assert set(m.profiles) == {"rtl_demo", "rtl_demo_l4", "uvm_smoke"}
    p, p4 = m.profiles["rtl_demo"], m.profiles["rtl_demo_l4"]
    assert [(t.name, t.value) for t in p.top_parameters] == [("DATA_W", "32"), ("LANES", "2")]
    assert [(t.name, t.value) for t in p4.top_parameters] == [("DATA_W", "32"), ("LANES", "4")]
    assert p.source_inputs[0].path == "fixtures/demo/filelists/rtl.f"
    assert p.defines[0].name == "SVW_DEMO" and p.include_dirs == ["fixtures/demo/include"]
    uvm = [r for r in m.source_roots if r.id == "uvm"][0]
    assert uvm.path is None
    LocalConfig.model_validate(
        {
            "schema_version": "1.0",
            "root_mappings": [{"id": "uvm", "path": ".toolchain/uvm-core/src"}],
        }
    )
    Manifest.model_validate(yaml.safe_load((REPO / "svweave.yaml").read_text()))


# ------------------------------------------------------------------ trusted policy tightening
def _mk(ws: Ws, **editing: Any) -> None:
    ws.config({"p": profile()}, editing={"enabled": False, **editing})


def test_policy_tightening_editing_cannot_be_enabled_by_repo(ws: Ws) -> None:
    ws.config({"p": profile()}, editing={"enabled": True, "allowed_paths": []})
    e = _expect(ErrorCode.POLICY_DENIED, ws.ctx)
    assert e.details["field"] == "editing.enabled"


def test_policy_tightening_cannot_drop_approval(ws: Ws) -> None:
    _mk(ws, require_human_approval=False)
    e = _expect(ErrorCode.POLICY_DENIED, ws.ctx)
    assert e.details["field"] == "editing.require_human_approval"
    ws.policy(approval_required=False)  # policy itself relaxed -> repo may opt in/out
    assert ws.ctx().effective.approval_required is False


def test_policy_tightening_allowed_paths_must_be_covered(ws: Ws) -> None:
    ws.policy(editing_enabled=True, writable_paths=["rtl/**"])
    ws.config({"p": profile()}, editing={"enabled": True, "allowed_paths": ["rtl/**"]})
    eff = ws.ctx().effective
    assert eff.editing_enabled and eff.allowed_paths == ["rtl/**"]
    ws.config({"p": profile()}, editing={"enabled": True, "allowed_paths": ["rtl/sub/**"]})
    assert ws.ctx().effective.allowed_paths == ["rtl/sub/**"]  # narrower is fine
    ws.config({"p": profile()}, editing={"enabled": True, "allowed_paths": ["lists/**"]})
    _expect(ErrorCode.POLICY_DENIED, ws.ctx)


def test_policy_tightening_repo_can_only_intersect(ws: Ws) -> None:
    ws.policy(editing_enabled=True, writable_paths=["**"])
    _mk(ws)  # repo keeps editing disabled even though policy would allow
    eff = ws.ctx().effective
    assert not eff.editing_enabled and eff.allowed_paths == []
    assert eff.network_enabled is False and eff.approval_required is True


def test_policy_tightening_cannot_add_roots(ws: Ws, tmp_path: Path) -> None:
    outside = tmp_path / "elsewhere"
    outside.mkdir()
    ws.config(
        {"p": profile()},
        source_roots=[
            {"id": "project", "path": "."},
            {"id": "extra", "path": str(outside)},
        ],
    )
    _expect(ErrorCode.PATH_NOT_ALLOWED, ws.ctx)
    # local mapping pointing outside policy allowed_roots is refused too
    ws.config(
        {"p": profile()},
        source_roots=[{"id": "project", "path": "."}, {"id": "ext"}],
    )
    ws.local([{"id": "ext", "path": str(outside)}])
    _expect(ErrorCode.PATH_NOT_ALLOWED, ws.ctx)


def test_policy_tightening_writable_root_requires_policy(ws: Ws) -> None:
    ws.config({"p": profile()}, source_roots=[{"id": "project", "path": ".", "writable": True}])
    _expect(ErrorCode.POLICY_DENIED, ws.ctx)
    ws.policy(writable_paths=["**"])
    assert ws.ctx().roots[0].writable


def test_missing_policy_is_invalid_config(ws: Ws) -> None:
    ws.policy_path.unlink()
    _expect(ErrorCode.INVALID_CONFIG, ws.ctx)


# ------------------------------------------------------------------ SEC-01 path policy
def test_glob_match_semantics() -> None:
    assert glob_match("fixtures/**", "fixtures/a/b.sv")
    assert glob_match("**/out/**", "x/y/out/z.sv") and glob_match("**/out/**", "out/z.sv")
    assert not glob_match("*.sv", "a/b.sv") and glob_match("*.sv", "b.sv")
    assert glob_match("a?c", "abc") and not glob_match("a?c", "a/c")
    assert glob_match("**", "") and glob_match("**/*.sv", "a.sv")


def test_SEC_01_dotdot_absolute_and_symlink_escape(ws: Ws, tmp_path: Path) -> None:
    ctx = ws.ctx()
    secret = tmp_path / "secret.sv"
    secret.write_text("module secret; endmodule\n")
    for raw in (
        "../secret.sv",
        "rtl/../../secret.sv",
        str(secret),
        "/etc/passwd",
        "rtl/../../../../etc/passwd",
    ):
        e = _expect(ErrorCode.PATH_NOT_ALLOWED, ctx.resolve, raw, ctx.project_root)
        assert "module secret" not in e.message  # no content leakage
    # inner `..` that stays inside is fine and is normalised
    assert ctx.resolve("rtl/../rtl/a.sv", ctx.project_root).logical == "rtl/a.sv"
    # symlink file escape
    os.symlink(secret, ws.root / "rtl" / "evil.sv")
    _expect(ErrorCode.PATH_NOT_ALLOWED, ctx.resolve, "rtl/evil.sv", ctx.project_root)
    # symlinked directory escape (and '..' through it)
    os.symlink(tmp_path, ws.root / "linkdir")
    _expect(ErrorCode.PATH_NOT_ALLOWED, ctx.resolve, "linkdir/secret.sv", ctx.project_root)
    _expect(ErrorCode.PATH_NOT_ALLOWED, ctx.resolve, "linkdir", ctx.project_root, is_dir=True)
    # symlink that stays inside the trusted roots is allowed and the target is recorded
    os.symlink(ws.root / "rtl" / "a.sv", ws.root / "rtl" / "alias.sv")
    rp = ctx.resolve("rtl/alias.sv", ctx.project_root)
    assert rp.logical == "rtl/alias.sv" and rp.symlink_target == "rtl/a.sv"


def test_path_policy_primitive(tmp_path: Path) -> None:
    root = tmp_path / "r"
    root.mkdir()
    pp = PathPolicy([root])
    lex, real = pp.check("a/../b.sv", root)
    assert lex == root.resolve() / "b.sv" and real == lex
    _expect(ErrorCode.PATH_NOT_ALLOWED, pp.check, "../x", root)


def test_source_root_include_exclude_rules(ws: Ws) -> None:
    ws.config(
        {"p": profile()},
        source_roots=[
            {
                "id": "project",
                "path": ".",
                "include": ["rtl/**", "lists/**"],
                "exclude": ["rtl/with space/**"],
            }
        ],
    )
    ctx = ws.ctx()
    assert ctx.resolve("rtl/a.sv", ctx.project_root).logical == "rtl/a.sv"
    _expect(ErrorCode.PATH_NOT_ALLOWED, ctx.resolve, "rel.sv", ctx.project_root)  # not included
    _expect(ErrorCode.PATH_NOT_ALLOWED, ctx.resolve, "rtl/with space/sp.sv", ctx.project_root)
    assert ctx.allows_file(ctx.project_root / "rtl" / "a.sv")
    assert not ctx.allows_file(ctx.project_root / "rel.sv")
    assert not ctx.allows_file(ctx.project_root.parent / "x.sv")
    assert ctx.dir_excluded(ctx.project_root / "rtl" / "with space")
    assert not ctx.dir_excluded(ctx.project_root / "rtl")


def test_external_root_mapping_resolution(ws: Ws) -> None:
    ext = ws.root / "ext_uvm"
    (ext / "src").mkdir(parents=True)
    (ext / "src" / "pkg.sv").write_text("package p; endpackage\n")
    ws.config(
        {"p": profile()},
        source_roots=[{"id": "project", "path": ".", "exclude": ["ext_uvm/**"]}, {"id": "uvm"}],
    )
    ctx = ws.ctx()
    e = _expect(ErrorCode.MISSING_INPUT, ctx.resolve, "@uvm/src/pkg.sv", ctx.project_root)
    assert e.details["reason"] == "root_unmapped"
    ws.local([{"id": "uvm", "path": "ext_uvm"}])
    ctx = ws.ctx()
    rp = ctx.resolve("@uvm/src/pkg.sv", ctx.project_root)
    assert rp.logical == "@uvm/src/pkg.sv"
    assert ctx.to_abs(rp.logical) == (ext / "src" / "pkg.sv").resolve()
    assert ctx.logical_of(ext / "src" / "pkg.sv") == "@uvm/src/pkg.sv"
    _expect(ErrorCode.PATH_NOT_ALLOWED, ctx.root_abs, "nope")
    assert ctx.to_abs("rtl/a.sv") == ctx.project_root / "rtl" / "a.sv"


def test_profile_lookup(ws: Ws) -> None:
    ctx = ws.ctx()
    assert ctx.profile(None)[0] == "p"
    _expect(ErrorCode.INVALID_CONFIG, ctx.profile, "ghost")
    assert (FIXTURES / "filelists" / "proj").is_dir()


def test_allowed_paths_are_inert_while_editing_disabled(ws: Ws) -> None:
    ws.config({"p": profile()}, editing={"enabled": False, "allowed_paths": ["anything/**"]})
    eff = ws.ctx().effective
    assert not eff.editing_enabled and eff.allowed_paths == []
