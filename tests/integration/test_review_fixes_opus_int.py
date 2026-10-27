"""Regression tests for the Opus whole-repository review that need a real build or real git."""

from __future__ import annotations

import json
import os
import re
import subprocess
from pathlib import Path

import pytest
from click.testing import CliRunner

from svweave.application.options import WorkspaceOptions
from svweave.cli import cli
from tests.conftest import Ws, profile
from tests.integration.test_cli_semantic import run
from tests.integration.test_rename_preview import lean_ws, preview

pytestmark = pytest.mark.integration


def _git(root: Path, *a: str) -> str:
    env = {"PATH": "/usr/bin:/bin", "HOME": str(root), "GIT_CONFIG_NOSYSTEM": "1",
           "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t",
           "GIT_COMMITTER_EMAIL": "t@t"}
    return subprocess.run(["git", "-C", str(root), *a], check=True, capture_output=True, text=True,
                          env=env).stdout


# ----------------------------------------------------------------------------- snapshots
def test_OPI_B1_B5_a_planted_snapshot_is_replaced_and_metadata_is_this_profiles(tmp_path: Path) -> None:
    from svweave.manifest.resolver import resolve_profile
    from svweave.snapshot.capture import capture_snapshot, snapshots_dir

    w = Ws(tmp_path / "ws")
    w.write("rtl/a.sv", "module a; endmodule\n")
    src = [{"kind": "file", "path": "rtl/a.sv"}]
    w.config({"p": profile(source_inputs=src, top_modules=["a"]),
              "q": profile(source_inputs=src, top_modules=["a"], defines=[{"name": "X", "value": "1"}])})
    w.policy()
    ctx = w.ctx()
    rp = capture_snapshot(ctx, resolve_profile(ctx, "p", {}))
    f = snapshots_dir(ctx) / rp.snapshot_id / "files" / "rtl" / "a.sv"
    for d in (f.parent, f.parent.parent, f.parent.parent.parent):
        os.chmod(d, 0o755)
    os.chmod(f, 0o644)
    f.write_text("module EVIL; endmodule\n")  # as if a cloned repo shipped this .svweave/
    rq = capture_snapshot(ctx, resolve_profile(ctx, "q", {}))
    assert rq.snapshot_id == rp.snapshot_id  # same inputs, same id ...
    assert f.read_text() == "module a; endmodule\n"  # ... but the planted bytes were replaced
    assert rq.profile == "q"  # and the record is this call's, not p's


# ----------------------------------------------------------------------------- queries
def test_OPI_C1_an_ambiguous_symbol_answer_stays_under_the_response_cap(tmp_path: Path) -> None:
    mods = "".join(f"module m{i}(input logic clk); logic u; assign u = clk; endmodule\n" for i in range(300))
    inst = "".join(f"  m{i} u{i}(.clk(clk));\n" for i in range(300))
    root, bid = lean_ws(tmp_path, {"rtl/t.sv": mods + f"module top(input logic clk);\n{inst}endmodule\n"},
                        "top")
    r = CliRunner().invoke(cli, ["--root", str(root), "--compact", "query", "symbol", "--build", bid,
                                 "--name", "clk", "--limit", "500"])
    assert len(r.stdout.encode()) <= 128 * 1024, len(r.stdout.encode())  # was 179 KB
    env = json.loads(r.stdout)
    assert env["error"]["code"] == "AMBIGUOUS_SYMBOL" and env["pagination"]["truncated"] is True
    assert env["pagination"]["next_cursor"]  # the rest stays reachable


def test_OPI_C2_debug_bundle_works_on_a_build_with_diagnostics(tmp_path: Path) -> None:
    w = Ws(tmp_path / "ws")
    w.write("rtl/t.sv", "module t(output logic y); assign y = undeclared_sig; endmodule\n")
    w.config({"p": profile(source_inputs=[{"kind": "file", "path": "rtl/t.sv"}], top_modules=["t"])})
    w.policy()
    code, b = run(w.root, "build", "--profile", "p")
    assert code == 3 and b["data"]["counts"]["diagnostics"] >= 1
    code, env = run(w.root, "debug", "bundle", "--build", b["build_id"])
    assert env["error"] is None or env["error"]["code"] != "INTERNAL", env["error"]  # was KeyError


def test_OPI_C3_a_graph_cursor_restarts_the_text_fallback_at_zero(tmp_path: Path) -> None:
    from svweave.application import search as S
    from svweave.application.keys import cursor_key
    from svweave.snapshot.capture import snapshots_dir

    w = Ws(tmp_path / "ws")
    w.write("rtl/a.sv", "// needle first\nmodule a; endmodule\n")
    w.write("rtl/b.sv", "// needle second\nmodule b; endmodule\n")
    w.config({"p": profile(source_inputs=[{"kind": "file", "path": "rtl/a.sv"},
                                          {"kind": "file", "path": "rtl/b.sv"}], top_modules=["a"])})
    w.policy()
    code, b = run(w.root, "build", "--profile", "p")
    ctx = w.ctx()
    sid = b["snapshot_id"]
    key = cursor_key(ctx)
    cur = S.encode_cursor(sid, S.query_hash("needle", None), S.CBM_BACKEND, 1, key)
    env = S.svw_search(snapshot_id=sid, snapshot_root=snapshots_dir(ctx) / sid / "files",
                       query="needle", limit=1, cursor=cur, registry=None, cursor_key=key)
    first = env.data["items"][0]
    assert first["file"].endswith("a.sv")  # was skipped: the graph offset 1 was applied to text
    assert any("restarted as a text_fallback" in x for x in env.warnings)


# ----------------------------------------------------------------------------- rename preview
FN = """\
module top(input logic a, output logic y);
  function automatic logic foo(input logic x);
    logic tmp;
    tmp = x;
`ifdef NOPE
    tmp = tmp + 1;
`endif
    return tmp;
  endfunction : foo
  genvar i;
  for (i = 0; i < 1; i++) begin : g
    logic gtmp;
    assign gtmp = a;
`ifdef NOPE
    assign gtmp = gtmp;
`endif
  end
  assign y = foo(.x(a));
endmodule
"""


def test_OPI_F1_unresolved_uses_in_functions_generate_blocks_and_headers_block(tmp_path: Path) -> None:
    root, bid = lean_ws(tmp_path / "a", {"rtl/t.sv": FN}, "top")
    for name in ("top.foo.tmp", "top.g.gtmp"):
        code, env = preview(root, bid, "--name", name, "--to", "fresh")
        d = env["data"]
        assert code == 0 and d["ready_for_edit_request"] is False, (name, d["blockers"])
        assert "unresolved_uses_in_owner_scope" in [b["code"] for b in d["blockers"]], name
    inc = "module m(input logic old, output logic y);\n  `include \"body.svh\"\nendmodule\n"
    hdr = "assign y = old;\n`ifdef NOPE\nassign y = old;\n`endif\n"
    w = Ws(tmp_path / "b")
    w.write("rtl/m.sv", inc)
    w.write("rtl/body.svh", hdr)
    w.config({"p": profile(source_inputs=[{"kind": "file", "path": "rtl/m.sv"}], top_modules=["m"],
                           include_dirs=["rtl"])})
    w.policy()
    code, b = run(w.root, "build", "--profile", "p")
    code, env = preview(w.root, b["build_id"], "--name", "m.old", "--to", "fresh")
    codes = [x["code"] for x in env["data"]["blockers"]]
    assert env["data"]["ready_for_edit_request"] is False
    assert "unresolved_uses_in_included_files" in codes or "unresolved_uses_in_owner_scope" in codes


def test_OPI_F5_function_labels_named_arguments_and_parameter_overrides(tmp_path: Path) -> None:
    root, bid = lean_ws(tmp_path / "a", {"rtl/t.sv": FN.replace("`ifdef NOPE\n    tmp = tmp + 1;\n`endif\n", "")
                                         .replace("`ifdef NOPE\n    assign gtmp = gtmp;\n`endif\n", "")},
                        "top")
    code, env = preview(root, bid, "--name", "top.foo", "--to", "bar")
    d = env["data"]
    assert "end_label" in [s["role"] for s in d["sites"]], d["sites"]  # `endfunction : foo`
    code, env = preview(root, bid, "--name", "top.foo.x", "--to", "z")
    assert env["data"]["ready_for_edit_request"] is False  # foo(.x(a)) binds the argument by name
    assert "named_argument_bindings" in [b["code"] for b in env["data"]["blockers"]]
    src = ("module leaf #(parameter int W = 1)(input logic i, output logic o); assign o = i; endmodule\n"
           "module t(input logic i, output logic o); leaf #(.W(8)) u (.i(i), .o(o)); endmodule\n")
    r2, b2 = lean_ws(tmp_path / "b", {"rtl/p.sv": src}, "t")
    code, env = preview(r2, b2, "--name", "leaf.W", "--to", "WIDTH")
    assert env["data"]["ready_for_edit_request"] is False  # validation would fail on #(.W(8))
    assert "parameter_overrides_by_name" in [b["code"] for b in env["data"]["blockers"]]


def test_OPI_F7_a_port_picked_by_symbol_id_is_the_same_entity_as_by_name(tmp_path: Path) -> None:
    src = ("module leaf(input logic a, output logic y); assign y = a; endmodule\n"
           "module t(input logic a, output logic y); leaf u(.a(a), .y(y)); endmodule\n")
    root, bid = lean_ws(tmp_path, {"rtl/l.sv": src}, "t")
    _, by_name = preview(root, bid, "--name", "leaf.a", "--to", "b")
    sid = next(c for c in run(root, "query", "symbol", "--build", bid, "--name", "leaf.a")[1]["error"]
               ["details"]["candidates"] if c["kind"] == "port")["symbol_id"]
    _, by_sym = preview(root, bid, "--symbol", sid, "--to", "b")
    assert by_sym["data"]["summary"]["sites"] == by_name["data"]["summary"]["sites"] == 3
    assert by_sym["data"]["ready_for_edit_request"] is True, by_sym["data"]["blockers"]


def test_OPI_F9_a_truncated_occurrence_scan_blocks(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from svweave.application import rename as R

    src = "module t(input logic a, output logic y);\n" + "".join(
        f"  logic s{i} = a; // a\n" for i in range(20)) + "  assign y = a;\nendmodule\n"
    root, bid = lean_ws(tmp_path, {"rtl/t.sv": src}, "t")
    monkeypatch.setattr(R, "MAX_SCAN_MATCHES", 3)
    env = R.rename_preview(WorkspaceOptions(root=root), bid, "r", new_name="b", name="t.a")
    assert env.data["ready_for_edit_request"] is False
    assert "lexical_scan_truncated" in [b["code"] for b in env.data["blockers"]]


# ----------------------------------------------------------------------------- graph / impact
def test_OPI_F2_graph_never_connects_two_instances_of_one_module(tmp_path: Path) -> None:
    src = ("module prod(output logic o); assign o = 1'b1; endmodule\n"
           "module cons(input logic i); logic u; assign u = i; endmodule\n"
           "module mid; logic w; prod p(.o(w)); cons c(.i(w)); endmodule\n"
           "module top; mid u1(); mid u2(); endmodule\n")
    root, bid = lean_ws(tmp_path, {"rtl/g.sv": src}, "top")
    code, env = run(root, "graph", "export", "--build", bid)
    page = (root / env["data"]["path"]).read_text()
    data = json.loads(re.search(r'<script id="data" type="application/json">(.*?)</script>', page, re.S)
                      .group(1))
    path = {n["id"]: n["path"] for n in data["nodes"]}
    pairs = {(path[e["from"]], path[e["to"]]) for e in data["edges"]}
    assert ("top.u1.p", "top.u1.c") in pairs and ("top.u2.p", "top.u2.c") in pairs
    assert not any(a.split(".")[1] != b.split(".")[1] for a, b in pairs)  # never u1 <-> u2


def test_OPI_F6_F8_owner_through_generate_blocks_and_no_false_descendant_warning(tmp_path: Path) -> None:
    src = ("module leaf(input logic a, output logic y);\n  for (genvar i = 0; i < 1; i++) begin : g\n"
           "    logic [3:0] tmp;\n    assign tmp = {4{a}};\n  end\n  assign y = a;\nendmodule\n"
           "module top(input logic a, output logic y1, y2); leaf u1(.a(a), .y(y1)); "
           "leaf u2(.a(a), .y(y2)); endmodule\n")
    root, bid = lean_ws(tmp_path, {"rtl/l.sv": src}, "top")
    code, env = run(root, "query", "impact", "--build", bid, "--name", "leaf.g.tmp")
    assert env["data"]["owner"]["qualified_name"] == "leaf"  # was None: scope leaf.g is not a module
    code, env = run(root, "query", "impact", "--build", bid, "--instance", "top")
    assert not any(w.startswith("descendants") for w in env["warnings"]), env["warnings"]


# ----------------------------------------------------------------------------- diff impact
@pytest.mark.usefixtures("isolated_git")
def test_OPI_F4_git_filters_are_never_run_and_F10_the_diff_stays_in_the_project(tmp_path: Path) -> None:
    mono = tmp_path / "mono"
    w = Ws(mono / "proj")
    w.write("rtl/m.sv", "module m(input logic a, output logic y);\n  assign y = a;\nendmodule\n")
    w.config({"p": profile(source_inputs=[{"kind": "file", "path": "rtl/m.sv"}], top_modules=["m"])})
    w.policy()
    (mono / "other.txt").write_text("outside the project\n")
    (mono / ".gitignore").write_text(".svweave/\n")
    _git(mono, "init", "-q", "-b", "main")
    _git(mono, "add", "-A")
    _git(mono, "commit", "-q", "-m", "base")
    code, b = run(w.root, "build", "--profile", "p")
    (mono / "other.txt").write_text("changed outside\n")
    p = w.root / "rtl/m.sv"
    p.write_text(p.read_text().replace("assign y = a;", "assign y = ~a;"))
    code, env = run(w.root, "query", "diff-impact", "--build", b["build_id"])
    d = env["data"]
    assert code == 0 and [f["logical_path"] for f in d["files"]] == ["rtl/m.sv"]
    assert d["unmapped_files"] == []  # other.txt is outside the project: not even listed
    marker = tmp_path / "PWNED"
    (mono / ".gitattributes").write_text("*.sv filter=evil\n")
    _git(mono, "config", "filter.evil.clean", f"sh -c 'touch {marker}; cat'")
    code, env = run(w.root, "query", "diff-impact", "--build", b["build_id"])
    assert env["error"]["code"] == "POLICY_DENIED" and env["error"]["details"]["reason"] == "git_filters_configured"
    assert env["error"]["details"]["filters"] == ["filter.evil.clean"]
    assert not marker.exists()  # the repository's program was not run


# ----------------------------------------------------------------------------- semantic core
def test_OPI_S1_a_declared_stub_with_a_body_is_still_a_boundary(tmp_path: Path) -> None:
    w = Ws(tmp_path / "ws")
    w.write("rtl/t.sv", "module vendor_ip(input logic clk, output logic q); assign q = 1'b0; endmodule\n"
                        "module top(input logic clk, output logic q); vendor_ip u(.clk(clk), .q(q)); endmodule\n")
    w.config({"p": profile(source_inputs=[{"kind": "file", "path": "rtl/t.sv"}], top_modules=["top"])},
             analysis={"missing_input_policy": "block", "unsupported_option_policy": "block",
                       "stub_policy": "explicit_only", "max_build_seconds": 120,
                       "max_worker_memory_mib": 4096,
                       "stubs": [{"module": "vendor_ip", "original_ip": "acme_ip_v3",
                                  "model_sha256": "a" * 64, "replaced_scope": "top.u"}]})
    w.policy()
    code, b = run(w.root, "build", "--profile", "p")
    assert b["data"]["completeness"] == "PARTIAL" and b["data"]["published"] is False  # was COMPLETE
    assert any("stub" in r for r in b["data"]["reasons"])


def test_OPI_S3_a_second_identical_profile_is_published_and_labelled_as_itself(tmp_path: Path) -> None:
    w = Ws(tmp_path / "ws")
    w.write("rtl/a.sv", "module a; endmodule\n")
    prof = profile(source_inputs=[{"kind": "file", "path": "rtl/a.sv"}], top_modules=["a"])
    w.config({"p1": prof, "p2": prof})
    w.policy()
    code, b1 = run(w.root, "build", "--profile", "p1")
    code, b2 = run(w.root, "build", "--profile", "p2")
    assert b2["build_id"] == b1["build_id"] and b2["data"]["reused"] is True
    assert b2["profile"] == "p2" and b2["data"]["published"] is True  # was labelled p1, unpublished
    from svweave.storage.db import Database

    with Database(w.root / ".svweave" / "svweave.db") as db:
        assert db.published(b1["workspace_id"], "p2")["build_id"] == b1["build_id"]


def test_OPI_S4_macro_references_in_two_files_at_the_same_offset_are_both_kept(tmp_path: Path) -> None:
    files = {
        "rtl/p.sv": "package p; localparam int W=4; endpackage\n`define WV p::W\n",
        "rtl/a.sv": "module a; int x = `WV; endmodule\n",
        "rtl/b.sv": "module b; int x = `WV; endmodule\n",  # byte-for-byte the same offsets as a.sv
        "rtl/t.sv": "module t; a ua(); b ub(); endmodule\n",
    }
    root, bid = lean_ws(tmp_path, files, "t")
    code, s = run(root, "query", "symbol", "--build", bid, "--name", "p::W")
    sid = s["data"]["symbol"]["symbol_id"]
    code, r = run(root, "query", "references", "--build", bid, "--symbol", sid)
    files_seen = sorted({(i["context"] or {}).get("macro_expansion_file") or i["location"]["macro"]["expansion"]["file"]
                         if (i["location"] or {}).get("macro") else i["location"]["file"]
                         for i in r["data"]["items"]})
    assert r["data"]["total_matches"] >= 2, files_seen  # was 1: b.sv's use was deduplicated away
