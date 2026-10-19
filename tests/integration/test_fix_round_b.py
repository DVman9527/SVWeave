"""Fix round B on the real pyslang worker: snapshot completeness for nested includes (B6),
blackbox/protected-source gaps (B2), reuse publishing (B8), verified rerun (B4), top-scoped -G (B5),
redaction of the real blocked envelope (B10), semantic deviations (B11)."""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from svweave.application.build import run_build
from svweave.snapshot.capture import check_freshness
from tests.conftest import Ws, profile
from tests.integration.helpers import FIXTURES, LANG, build, dbq, lang_ws, opts

pytestmark = pytest.mark.integration


def _tiny(tmp_path: Path, files: dict[str, str], srcs: list[str], top: str, **prof: object) -> Ws:
    root = tmp_path / "ws"
    root.mkdir(parents=True)
    for rel, text in files.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text)
    w = Ws(root)
    base: dict[str, object] = {
        "source_inputs": [{"kind": "file", "path": s} for s in srcs],
        "top_modules": [top],
    }
    base.update(prof)
    w.config({"p": profile(**base)})
    w.policy()
    return w


def test_B6_nested_relative_include_is_captured_and_inventoried(tmp_path: Path) -> None:
    w = _tiny(
        tmp_path,
        {
            "rtl/top.sv": '`include "headers/defs.svh"\nmodule t; localparam int W = `WIDTH; endmodule\n',
            "rtl/headers/defs.svh": '`define WIDTH 8\n`include "more/deep.svh"\n',
            "rtl/headers/more/deep.svh": "`define DEEP 1\n",
        },
        ["rtl/top.sv"],
        "t",
    )
    env = build(w)
    assert env.outcome == "OK", env.model_dump_json()[:1500]
    assert env.analysis_coverage is not None and env.analysis_coverage.completeness == "COMPLETE"
    files = {r["logical_path"] for r in _snapshot_files(w, env.snapshot_id)}
    assert {"rtl/headers/defs.svh", "rtl/headers/more/deep.svh"} <= files
    # a new candidate header in the nested search space makes the snapshot STALE (negative lookup)
    (w.root / "rtl" / "headers" / "extra.svh").write_text("// new\n")
    rec = _record(w, env.snapshot_id)
    assert check_freshness(opts(w).load(), rec).freshness == "STALE"


def _record(w: Ws, sid: str | None):  # type: ignore[no-untyped-def]
    from svweave.snapshot.capture import load_record, snapshots_dir

    assert sid
    return load_record(snapshots_dir(opts(w).load()) / sid)


def _snapshot_files(w: Ws, sid: str | None) -> list[dict[str, str]]:
    return [{"logical_path": f.logical_path} for f in _record(w, sid).files]


def test_B2_encrypted_envelope_is_partial_not_complete(tmp_path: Path) -> None:
    w = lang_ws(tmp_path, "enc01_protected", "enc_top")
    env = build(w)
    assert env.analysis_coverage is not None
    assert env.analysis_coverage.completeness == "PARTIAL"
    assert env.data["diagnostic_outcome"] == "NO_ERRORS"  # warnings only: still PARTIAL by gap
    assert env.data["published"] is False
    kinds = {r["kind"] for r in dbq(w, "SELECT kind FROM coverage_gaps WHERE build_id=?", env.build_id)}
    assert {"protected_envelope", "empty_module_body"} <= kinds
    assert any("protected-source" in r for r in env.data["reasons"])


def test_B2_implicit_empty_module_is_partial_and_declared_stub_is_recorded(tmp_path: Path) -> None:
    files = {
        "ip.sv": "module vendor_ip(input logic clk, output logic q);\nendmodule\n",
        "top.sv": "module t(input logic clk, output logic q);\n  vendor_ip u(.clk(clk), .q(q));\nendmodule\n",
    }
    w = _tiny(tmp_path, files, ["ip.sv", "top.sv"], "t")
    env = build(w)
    assert env.analysis_coverage is not None and env.analysis_coverage.completeness == "PARTIAL"
    assert not env.data["published"]
    # a manifest stub record names what was replaced; still PARTIAL (plan 16.5), kind stub_boundary
    w2 = _tiny(tmp_path / "b", files, ["ip.sv", "top.sv"], "t")
    w2.config(
        {"p": profile(source_inputs=[{"kind": "file", "path": "ip.sv"}, {"kind": "file", "path": "top.sv"}], top_modules=["t"])},
        analysis={
            "stubs": [
                {
                    "module": "vendor_ip",
                    "original_ip": "vendor-ip-3.2",
                    "model_sha256": "a" * 64,
                    "replaced_scope": "vendor_ip body",
                }
            ]
        },
    )
    e2 = build(w2)
    assert e2.analysis_coverage is not None and e2.analysis_coverage.completeness == "PARTIAL"
    gaps = dbq(w2, "SELECT kind, detail FROM coverage_gaps WHERE build_id=?", e2.build_id)
    assert [g["kind"] for g in gaps] == ["stub_boundary"]
    assert "vendor-ip-3.2" in gaps[0]["detail"] and "a" * 64 in gaps[0]["detail"]
    assert any("declared stub for module vendor_ip" in d for d in e2.data["semantic_deviations"])


def test_B8_reused_complete_build_is_published_on_request(tmp_path: Path) -> None:
    w = _tiny(tmp_path, {"a.sv": "module a; endmodule\n"}, ["a.sv"], "a")
    e1 = build(w)
    assert e1.data["published"] is True
    (w.root / "a.sv").write_text("module a; wire x; endmodule\n")
    e2 = build(w)
    assert e2.build_id != e1.build_id
    (w.root / "a.sv").write_text("module a; endmodule\n")
    e3 = build(w)  # same inputs as e1 -> reused COMPLETE build, pointer must move back
    assert e3.build_id == e1.build_id and e3.data["reused"] is True
    ptr = dbq(w, "SELECT build_id FROM published_pointer")
    assert [r["build_id"] for r in ptr] == [e1.build_id]
    assert e3.data["published"] is True


def test_B4_verified_rerun_compares_content_not_counts(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import svweave.application.build as B
    from svweave.domain.errors import SvwError

    w = _tiny(tmp_path, {"a.sv": "module a; wire x; endmodule\n"}, ["a.sv"], "a")
    e1 = build(w)
    ok = build(w, reuse=False)  # honest rerun: identical
    assert ok.data["verified_rerun"] is True and ok.data["semantic_result_sha256"]
    real = B.ingest

    def tampered(*a, **k):  # type: ignore[no-untyped-def]
        ing = real(*a, **k)
        ing.rows["declarations"][0] = {**ing.rows["declarations"][0], "name": "renamed_by_nondeterminism"}
        return ing

    monkeypatch.setattr(B, "ingest", tampered)
    with pytest.raises(SvwError) as ei:
        run_build(opts(w), "p", "r2", reuse=False)
    assert "declarations" in ei.value.details["differences"]
    assert ei.value.details["stored_build_replaced"] is False
    assert dbq(w, "SELECT completeness FROM builds WHERE build_id=?", e1.build_id)[0]["completeness"] == "COMPLETE"


def test_B5_top_parameter_override_is_scoped_to_its_top(tmp_path: Path) -> None:
    files = {
        "t.sv": "module a #(parameter int W=1)(); endmodule\nmodule b #(parameter int W=1)(); endmodule\n"
    }
    w = _tiny(
        tmp_path,
        files,
        ["t.sv"],
        "a",
        top_modules=["a", "b"],
        top_parameters=[{"top": "a", "name": "W", "value": "5"}],
    )
    env = build(w)
    assert env.analysis_coverage is not None and env.analysis_coverage.completeness == "COMPLETE"
    rows = dbq(w, "SELECT definition, specialization FROM instances WHERE build_id=? AND is_top=1", env.build_id)
    spec = {r["definition"]: r["specialization"] for r in rows}
    assert spec["a"] == "W=5" and spec["b"] == "W=1"  # b is NOT overridden


def test_B10_real_blocked_envelope_is_redacted(tmp_path: Path) -> None:
    from svweave.gateway.tools import redact_envelope
    from svweave.security.redaction import Redactor

    # the sandboxed worker only sees what the sandbox exposes: a minimal /etc entry still reaches
    # the post-hoc dependency check (see test_api_semantic)
    visible = Path("/etc/nsswitch.conf")
    if not visible.is_file():
        pytest.skip("no /etc/nsswitch.conf on this host")
    w = lang_ws(tmp_path / "w", "sem01_scopes", "sem01_top")
    top = w.root / "top.sv"
    top.write_text(f'`include "{visible}"\n' + top.read_text())
    env = build(w)
    assert env.outcome == "BLOCKED", env.model_dump_json()[:3000]
    assert str(visible) in env.model_dump_json()  # the raw envelope does carry the host path
    red = redact_envelope(env, Redactor([w.root]))
    assert str(visible) not in red.model_dump_json()
    assert red.data["violations"][0]["reason"] == "read_outside_snapshot"
    deep = redact_envelope(env, Redactor([w.root]), deep=True)  # the MCP gateway's mode
    assert str(visible) not in deep.model_dump_json()
    assert all(str(visible) not in x for x in red.analysis_coverage.limitations)  # type: ignore[union-attr]


def test_B11_semantic_deviations_are_real_and_in_config_id(tmp_path: Path) -> None:
    w = _tiny(tmp_path, {"a.sv": "module a; endmodule\n"}, ["a.sv"], "a")
    env = build(w)
    devs = env.data["semantic_deviations"]
    assert any("__FILE__" in d for d in devs)
    assert env.data["reasons"] == []  # completeness reasons are a different field
    w.config(
        {"p": profile(source_inputs=[{"kind": "file", "path": "a.sv"}], top_modules=["a"],
                      language_options={"timescale": "1ns/1ps"})}
    )
    env2 = build(w)
    assert env2.build_id != env.build_id
    assert any("timescale=1ns/1ps" in d for d in env2.data["semantic_deviations"])


def test_B9_symbol_candidates_are_paginated_and_specializations_bounded(tmp_path: Path) -> None:
    from svweave.application import queries as Q

    pk = "".join(f"package p{i}; class c; endclass endpackage\n" for i in range(60))
    gen = "class g #(int N = 0); endclass\n"
    uses = "".join(f"  g#({i}) v{i};\n" for i in range(25))
    top = f"module t;\n{uses}endmodule\n"
    w = _tiny(tmp_path, {"a.sv": pk + gen + top}, ["a.sv"], "t")
    env = build(w)
    bid = env.build_id
    assert bid
    first = Q.symbol(opts(w), bid, "r", name="c")
    assert first.error is not None and first.error.code == "AMBIGUOUS_SYMBOL"
    assert first.data["candidate_count"] == 60 and len(first.data["candidates"]) == 50
    assert first.pagination.truncated is True and first.pagination.next_cursor
    second = Q.symbol(opts(w), bid, "r", name="c", cursor=first.pagination.next_cursor)
    assert len(second.data["candidates"]) == 10 and second.pagination.truncated is False
    seen = {c["qualified_name"] for c in first.data["candidates"] + second.data["candidates"]}
    assert len(seen) == 60  # nothing lost or doubled across pages
    g = Q.symbol(opts(w), bid, "r", name="g")
    cand = g.data["candidates"][0]
    assert cand["specialization_count"] == 25 and len(cand["specializations"]) == Q.MAX_SPECIALIZATIONS
    assert cand["specializations_truncated"] is True


_ = (FIXTURES, LANG, shutil)
