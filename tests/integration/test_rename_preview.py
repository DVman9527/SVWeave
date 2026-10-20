"""RNM-I: `svweave query rename-preview` on real builds, and its edit request through the real
controlled edit loop (prepare -> validate with slang -> approve -> commit)."""

from __future__ import annotations

import json
import stat
from pathlib import Path
from typing import Any

import pytest

from svweave.application import edit as E
from svweave.application.options import WorkspaceOptions
from tests.conftest import Ws, profile
from tests.integration.demo_support import demo_ws
from tests.integration.test_cli_semantic import run
from tests.unit._edit_fx import typed_confirm

pytestmark = pytest.mark.integration
LANE = "fixtures/demo/rtl/demo_lane.sv"
TOP = "fixtures/demo/rtl/demo_top.sv"


def built(tmp_path: Path, **kw: Any) -> tuple[Path, str]:
    w = demo_ws(tmp_path, **kw)
    code, b = run(w.root, "build", "--profile", "p")
    assert code == 0
    return w.root, str(b["build_id"])


def preview(root: Path, bid: str, *args: str) -> tuple[int, dict[str, Any]]:
    return run(root, "query", "rename-preview", "--build", bid, *args)


def apply_through_edit_loop(root: Path, request: dict[str, Any]) -> dict[str, Any]:
    """prepare (no Serena needed for exact_range) -> validate -> approve (test token) -> commit."""
    opts = WorkspaceOptions(root=root)
    prep = E.prepare(opts, "r-prep", request, use_serena=False)
    assert prep.error is None, prep.error
    tid = prep.data["transaction_id"]
    val = E.validate(opts, "r-val", transaction_id=tid)
    assert val.error is None, val.error
    h = val.data["patch_hash"]
    E.approve(opts, "r-appr", transaction_id=tid, patch_hash=h, confirm=typed_confirm())
    com = E.commit(opts, "r-com", transaction_id=tid, expected_patch_hash=h, request_key="commit-rename-1")
    assert com.error is None, com.error
    out: dict[str, Any] = com.data
    return out


def test_RNM_I01_port_rename_finds_every_site_and_survives_the_edit_loop(tmp_path: Path) -> None:
    root, bid = built(tmp_path, editing=True)
    code, env = preview(root, bid, "--name", "demo_lane.data_o", "--to", "data_out")
    assert code == 0, env.get("error")
    d = env["data"]
    assert d["ready_for_edit_request"] is True and d["blockers"] == []
    roles = sorted((s["role"], s["file"].rsplit("/", 1)[-1]) for s in d["sites"])
    assert roles == [
        ("declaration", "demo_lane.sv"),
        ("named_port_connection", "demo_top.sv"),  # the formal name of .data_o(...) at the instance
        ("reference", "demo_bind.sv"),  # a reference from the bind (SVA) file
        ("reference", "demo_lane.sv"),
    ]
    assert d["target"]["kind"] == "port" and len(d["target"]["same_entity"]) == 2
    assert all(s["end_byte"] - s["start_byte"] == len("data_o") for s in d["sites"])
    assert "data_out" in "".join(x["diff"] for x in d["diffs"]) and d["limits"]["within_limits"]
    req = d["edit_request"]
    assert len(req["edits"]) == 4 and req["base_build_id"] == bid
    before = {f: (root / f).read_bytes() for f in (LANE, TOP)}
    result = apply_through_edit_loop(root, req)  # slang validates baseline and candidate: NO_ERRORS
    assert result["state"] in ("APPLIED", "POST_APPLY_VERIFIED")
    lane, top = (root / LANE).read_text(), (root / TOP).read_text()
    assert "data_out" in lane and "data_o " not in lane.replace("data_out", "")  # declaration + use
    assert ".data_out" in top and (root / LANE).read_bytes() != before[LANE]
    code, b2 = run(root, "build", "--profile", "p")  # the renamed design still elaborates completely
    assert code == 0 and b2["data"]["completeness"] == "COMPLETE"
    assert b2["data"]["counts"]["instances"] == 5 and b2["data"]["counts"]["port_bindings"] == 26
    code, gone = preview(root, b2["build_id"], "--name", "demo_lane.data_o", "--to", "x_out")
    assert code == 3 and gone["error"]["code"] == "MISSING_INPUT"  # the old name no longer exists
    code, there = preview(root, b2["build_id"], "--name", "demo_lane.data_out", "--to", "data_o")
    assert code == 0 and there["data"]["summary"]["sites"] == 4  # and the new one has all 4 sites


def test_RNM_I02_module_rename_end_to_end(tmp_path: Path) -> None:
    root, bid = built(tmp_path, editing=True)
    code, env = preview(root, bid, "--name", "demo_lane", "--to", "demo_lane_v2")
    d = env["data"]
    assert code == 0 and d["ready_for_edit_request"] is True, d["blockers"]
    # the SVA `bind demo_lane ...` target is not a slang reference: found lexically, edited too
    assert sorted(s["role"] for s in d["sites"]) == ["bind_target", "declaration", "reference"]
    apply_through_edit_loop(root, d["edit_request"])
    code, b2 = run(root, "build", "--profile", "p")
    assert code == 0 and b2["data"]["counts"]["instances"] == 5
    assert "demo_lane_v2" in (root / TOP).read_text()


def test_RNM_I03_name_blockers_and_refusals(tmp_path: Path) -> None:
    root, bid = built(tmp_path)
    cases = {
        "wire": "keyword",
        "ready_i": "name_collision",  # another port of demo_lane
        "valid_i": "same_name",
    }
    for new, code_ in cases.items():
        code, env = preview(root, bid, "--name", "demo_lane.valid_i", "--to", new)
        assert code == 0 and env["data"]["ready_for_edit_request"] is False, new
        assert code_ in [b["code"] for b in env["data"]["blockers"]], new
        assert env["data"]["edit_request"] is None
    for bad in ("1abc", "a b", "a;b", "\\esc ", "", "x" * 300, "$x", "a-b"):
        code, env = preview(root, bid, "--name", "demo_lane.valid_i", "--to", bad)
        assert code == 2 and env["error"]["code"] == "INVALID_CONFIG", bad
    code, env = preview(root, bid, "--name", "valid_i", "--to", "q_i")
    assert code == 3 and env["error"]["code"] == "AMBIGUOUS_SYMBOL"
    code, env = preview(root, bid, "--name", "no_such", "--to", "x")
    assert code == 3 and env["error"]["code"] == "MISSING_INPUT"
    code, env = preview(root, bid, "--to", "x")  # no target
    assert code == 2


def test_RNM_I04_stale_workspace_blocks_the_edit_request_but_not_the_preview(tmp_path: Path) -> None:
    root, bid = built(tmp_path)
    p = root / LANE
    p.write_text(p.read_text() + "// touched\n")
    code, env = preview(root, bid, "--name", "demo_lane.data_o", "--to", "data_out", "--allow-stale")
    d = env["data"]
    assert code == 0 and d["ready_for_edit_request"] is False and d["edit_request"] is None
    assert "build_not_current" in [b["code"] for b in d["blockers"]]
    assert d["summary"]["sites"] == 4  # the preview itself is still complete for the snapshot


def lean_ws(tmp_path: Path, files: dict[str, str], top: str) -> tuple[Path, str]:
    w = Ws(tmp_path / "ws")
    for rel, text in files.items():
        w.write(rel, text)
    w.config({"p": profile(source_inputs=[{"kind": "file", "path": r} for r in files], top_modules=[top])})
    w.policy()
    code, b = run(w.root, "build", "--profile", "p")
    assert code == 0, b.get("error")
    return w.root, str(b["build_id"])


def test_RNM_I05_implicit_connections_wildcard_and_unconfirmed_occurrences(tmp_path: Path) -> None:
    src = """\
module leaf (input logic a, output logic y);  // a is the input
  assign y = a;
endmodule : leaf
module top (input logic a, output logic y1, y2, y3);
  logic w;
  leaf u1 (.a, .y(y1));
  leaf u2 (.*, .y(y2));
  leaf u3 (.a(a), .y(y3));
  string s = "a";
endmodule
"""
    root, bid = lean_ws(tmp_path, {"rtl/t.sv": src}, "top")
    code, env = preview(root, bid, "--name", "leaf.a", "--to", "in_a")
    d = env["data"]
    assert code == 0 and d["ready_for_edit_request"] is False and d["edit_request"] is None
    assert "implicit_port_connections" in [b["code"] for b in d["blockers"]]
    styles = sorted(x["style"] for x in d["implicit_connections"])
    assert styles == ["named_implicit", "wildcard"]
    roles = [s["role"] for s in d["sites"]]
    assert roles.count("named_port_connection") == 1  # only u3's .a(a) is located automatically
    cats = d["unconfirmed_summary"]["by_category"]
    assert cats.get("comment_or_string", 0) >= 1  # the comment and the string mention "a"
    code, env = preview(root, bid, "--name", "leaf", "--to", "leaf2")
    d = env["data"]
    assert code == 0 and d["ready_for_edit_request"] is True, d["blockers"]
    # the module name has three more spellings than slang's references: all are edit sites
    assert sorted(x["role"] for x in d["sites"]) == ["declaration", "end_label", "reference",
                                                      "reference", "reference"]
    end = next(x for x in d["sites"] if x["role"] == "end_label")
    assert end["line"] == 3  # `endmodule : leaf`
    cats = d["unconfirmed_summary"]["by_category"]
    assert all(k == "comment_or_string" for k, v in cats.items() if v)  # nothing real left over
    # a local (non-global) entity keeps its end label as an unconfirmed occurrence, never an edit
    code, env = preview(root, bid, "--name", "top.w", "--to", "w2")
    assert code == 0 and env["data"]["ready_for_edit_request"] is True


def test_RNM_I06_edit_limit_and_macro_body_sites(tmp_path: Path) -> None:
    many = "\n".join(f"  assign y{i} = s;" for i in range(60))
    outs = ", ".join(f"output logic y{i}" for i in range(60))
    src = f"module m (input logic s, {outs});\n{many}\nendmodule\n"
    root, bid = lean_ws(tmp_path / "a", {"rtl/m.sv": src}, "m")
    code, env = preview(root, bid, "--name", "m.s", "--to", "sig")
    d = env["data"]
    assert code == 0 and d["summary"]["sites"] >= 61 and d["ready_for_edit_request"] is False
    assert "exceeds_edit_limits" in [b["code"] for b in d["blockers"]]
    assert d["edit_request"] is None and d["limits"]["within_limits"] is False
    macro = "`define MKSIG logic mk_sig;\nmodule t;\n  `MKSIG\n  assign mk_sig = 1'b0;\nendmodule\n"
    root2, bid2 = lean_ws(tmp_path / "b", {"rtl/t.sv": macro}, "t")
    code, env = preview(root2, bid2, "--name", "t.mk_sig", "--to", "other_sig")
    d = env["data"]
    assert code == 0 and d["ready_for_edit_request"] is False
    assert any(x["reason"] == "spelled_in_macro_body" for x in d["skipped_sites"])
    assert "sites_not_editable" in [b["code"] for b in d["blockers"]]


def test_RNM_I07_write_spec_is_private_and_accepted_by_edit_prepare(tmp_path: Path) -> None:
    root, bid = built(tmp_path, editing=True)
    code, env = preview(root, bid, "--name", "demo_lane.valid_i", "--to", "valid_in",
                        "--write-spec", "rn1")
    assert code == 0 and env["data"]["spec_path"] == ".svweave/exports/rn1.json"
    f = root / env["data"]["spec_path"]
    assert stat.S_IMODE(f.stat().st_mode) == 0o600
    assert json.loads(f.read_text()) == env["data"]["edit_request"]
    code, prep = run(root, "edit", "prepare", "--spec", str(f))  # the existing CLI accepts it as is
    assert code == 0, prep.get("error")
    for bad in ("../x", "a/b", ".hid"):
        code, e = preview(root, bid, "--name", "demo_lane.valid_i", "--to", "valid_in", "--write-spec", bad)
        assert code == 2 and e["error"]["code"] == "INVALID_CONFIG", bad
    code, e = preview(root, bid, "--name", "demo_lane.valid_i", "--to", "wire", "--write-spec", "rn2")
    assert code == 0 and not (root / ".svweave/exports/rn2.json").exists()
    assert any("nothing written" in w for w in e["warnings"])


def test_RNM_I08_uses_of_a_global_name_outside_the_edit_set_block(tmp_path: Path) -> None:
    src = "module mm;\nendmodule\n`define MM mm\nmodule top;\n  `MM u1 ();\nendmodule\n"
    root, bid = lean_ws(tmp_path, {"rtl/t.sv": src}, "top")
    code, env = preview(root, bid, "--name", "mm", "--to", "nn")
    d = env["data"]
    codes = [b["code"] for b in d["blockers"]]
    assert code == 0 and d["ready_for_edit_request"] is False and d["edit_request"] is None
    assert "unconfirmed_uses_of_global_name" in codes or "sites_not_editable" in codes
    assert any(x["category"] == "other_use" for x in d["unconfirmed_occurrences"])


def test_RNM_I09_a_site_whose_bytes_are_not_the_old_name_is_skipped_never_edited(tmp_path: Path) -> None:
    import sqlite3

    root, bid = built(tmp_path)
    db = sqlite3.connect(root / ".svweave" / "svweave.db")
    row = db.execute(
        'SELECT r.reference_id, r.span_start FROM "references" r JOIN declarations d '
        "ON d.build_id=r.build_id AND d.declaration_id=r.target_id "
        "WHERE r.build_id=? AND r.file=? AND d.qualified_name='demo_lane.valid_i' "
        "ORDER BY r.span_start LIMIT 1",
        (bid, LANE),
    ).fetchone()
    assert row is not None  # the one use of `valid_i` inside demo_lane (assign valid_o = valid_i)
    db.execute(
        'UPDATE "references" SET span_start=span_start+1, span_end=span_end+1 '
        "WHERE build_id=? AND reference_id=?",
        (bid, row[0]),
    )
    db.commit()
    db.close()
    code, env = preview(root, bid, "--name", "demo_lane.valid_i", "--to", "valid_in")
    d = env["data"]
    assert code == 0 and d["ready_for_edit_request"] is False and d["edit_request"] is None
    bad = [x for x in d["skipped_sites"] if x["reason"] == "text_mismatch"]
    assert len(bad) == 1 and bad[0]["found"] != "valid_i" and bad[0]["start_byte"] == row[1] + 1
    assert "sites_not_editable" in [b["code"] for b in d["blockers"]]
    assert all(s["start_byte"] != row[1] + 1 for s in d["sites"])  # never in the edit set


IFDEF = """\
module m (input logic old, output logic y, output logic z);
  assign y = old;
`ifdef FEATURE
  assign z = old;
`else
  assign z = 1'b0;
`endif
endmodule
module other (input logic old, output logic q);
  assign q = old;
endmodule
"""
IFDEF_CONTROL = IFDEF.replace("`ifdef FEATURE\n  assign z = old;\n`else\n  assign z = 1'b0;\n`endif\n",
                              "  assign z = old;\n")


def test_RNM_I10_a_use_in_an_inactive_branch_of_the_declaring_scope_blocks(tmp_path: Path) -> None:
    root, bid = lean_ws(tmp_path / "a", {"rtl/m.sv": IFDEF}, "m")
    code, env = preview(root, bid, "--name", "m.old", "--to", "fresh")
    d = env["data"]
    codes = [b["code"] for b in d["blockers"]]
    assert code == 0 and d["ready_for_edit_request"] is False and d["edit_request"] is None
    assert "unresolved_uses_in_owner_scope" in codes
    blocker = next(b for b in d["blockers"] if b["code"] == "unresolved_uses_in_owner_scope")
    assert [e["line"] for e in blocker["examples"]] == [4]  # the `assign z = old;` in `ifdef FEATURE
    assert sorted(s["role"] for s in d["sites"]) == ["declaration", "reference"]  # the live use only
    # precision: the SAME name in another module's scope is not a reason to block
    other = d["unconfirmed_summary"]["by_category"].get("other_use", 0)
    assert other >= 2  # the inactive use AND other.old are both listed ...
    assert len(blocker["examples"]) == 1  # ... but only the one inside m blocks
    # control: without the inactive branch the same rename is ready
    root2, bid2 = lean_ws(tmp_path / "b", {"rtl/m.sv": IFDEF_CONTROL}, "m")
    code, env2 = preview(root2, bid2, "--name", "m.old", "--to", "fresh")
    assert code == 0 and env2["data"]["ready_for_edit_request"] is True, env2["data"]["blockers"]


def test_RNM_I11_like_wildcards_in_names_do_not_match_other_parameters(tmp_path: Path) -> None:
    src = """\
module leaf #(parameter int a_b = 1, parameter int axb = 2) (input logic i, output logic o);
  assign o = i;
endmodule
module top (input logic i, output logic o);
  leaf #(.axb(5)) u1 (.i(i), .o(o));
endmodule
"""
    root, bid = lean_ws(tmp_path, {"rtl/p.sv": src}, "top")
    code, env = preview(root, bid, "--name", "leaf.a_b", "--to", "c_d")
    # a_b is never overridden: `_` must not match the x of `axb` (the unescaped LIKE warned here)
    assert code == 0 and "parameter_overrides_by_name" not in [
        w["code"] for w in env["data"]["warnings"]
    ]
    code, env = preview(root, bid, "--name", "leaf.axb", "--to", "c_d")
    warn = [w for w in env["data"]["warnings"] if w["code"] == "parameter_overrides_by_name"]
    assert code == 0 and len(warn) == 1 and warn[0]["instances"] == 1  # the real override is found
