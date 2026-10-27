"""Regression tests for the independent M2 review (B1-B3, M1-M6, L1, surviving mutants).

Every test here fails against the code before the corresponding fix (the finding id is in the
test name)."""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

import pytest
from click.testing import CliRunner

from svweave.adapters.slang.protocol import WorkerRequest
from svweave.adapters.slang.runner import run_worker
from svweave.application import queries as Q
from svweave.application import toolchain
from svweave.application.build import ARTIFACTS
from svweave.cli import cli
from svweave.domain.cursor import MAX_RESPONSE_BYTES
from tests.conftest import Ws, profile
from tests.integration.helpers import (
    UVM_SRC,
    build,
    dbq,
    decl_by_qn,
    decl_rows,
    native_check,
    opts,
    raw_dto,
    ref_rows,
    uvm_ws,
)

pytestmark = pytest.mark.integration


def mk(tmp: Path, files: dict[str, str | bytes], top: str, **prof: Any) -> Ws:
    root = tmp / "ws"
    w = Ws(root)
    for name, text in files.items():
        p = root / name
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(text if isinstance(text, bytes) else text.encode())
    base: dict[str, Any] = {
        "source_inputs": [{"kind": "file", "path": n} for n in files if n.endswith(".sv")],
        "top_modules": [top],
    }
    base.update(prof)
    # only rtl/** is a source root when sources live in rtl/: editing svweave.yaml then cannot
    # change the snapshot (a config-only change)
    roots = (
        [{"id": "project", "path": ".", "include": ["rtl/**"], "exclude": []}]
        if any(n.startswith("rtl/") for n in files)
        else None
    )
    if roots:
        w.config({"p": profile(**base)}, source_roots=roots)
    else:
        w.config({"p": profile(**base)})
    w.policy()
    return w


ROOTS = [{"id": "project", "path": ".", "include": ["rtl/**"], "exclude": []}]


def cli_run(root: Path, *args: str) -> tuple[int, str]:
    res = CliRunner().invoke(cli, ["--root", str(root), *args])
    return res.exit_code, res.stdout


# ============================================================================== B1
EXT_LIB = """
module ext_cell(input a, output y);
  specparam tpd = 1;
  assign y = a;
  specify (a => y) = tpd; endspecify
endmodule
"""
PROJ = """
module top(input a, output y, output z);
  %s
endmodule
module proj_cell(input a, output y);
  specparam tq = 2;
  assign y = a;
  specify (a => y) = tq; endspecify
endmodule
"""


def ext_ws(tmp: Path, body: str, ext_first: bool, proj: str = PROJ) -> Ws:
    ext_dir = tmp / "extlib"
    ext_dir.mkdir(parents=True)
    (ext_dir / "lib.sv").write_text(EXT_LIB)
    root = tmp / "p2"
    w = Ws(root)
    w.write("top.sv", proj % body)
    srcs = [{"kind": "file", "path": "@ext/lib.sv"}, {"kind": "file", "path": "top.sv"}]
    if not ext_first:
        srcs.reverse()
    w.config(
        {"p": profile(source_inputs=srcs, top_modules=["top"])},
        source_roots=[
            {"id": "project", "path": ".", "include": ["**"], "exclude": ["**/.svweave/**"]},
            {"id": "ext", "include": ["**"], "exclude": []},
        ],
    )
    w.policy(allowed_roots=[str(root.resolve()), str(ext_dir.resolve())])
    w.local([{"id": "ext", "path": str(ext_dir)}])
    return w


@pytest.mark.parametrize("ext_first", [True, False])
@pytest.mark.parametrize("ext_instance_first", [True, False])
def test_B1_project_unsupported_occurrence_is_partial_whatever_the_instantiation_order(
    tmp_path: Path, ext_first: bool, ext_instance_first: bool
) -> None:
    ext_i = "ext_cell u_ext(.a(a), .y(y));"
    prj_i = "proj_cell u_p(.a(a), .y(z));"
    body = f"{ext_i}\n  {prj_i}" if ext_instance_first else f"{prj_i}\n  {ext_i}"
    w = ext_ws(tmp_path, body, ext_first)
    env = build(w)
    assert env.analysis_coverage is not None
    assert env.analysis_coverage.completeness == "PARTIAL", env.data["reasons"]
    assert env.data["published"] is False
    rows = dbq(w, "SELECT kind, count, in_scope, sample FROM unsupported_nodes")
    proj_rows = [r for r in rows if r["in_scope"] == 1]
    ext_rows = [r for r in rows if r["in_scope"] == 0]
    assert proj_rows and ext_rows  # both occurrences are kept, per scope
    assert all(json.loads(r["sample"])["file"] == "top.sv" for r in proj_rows)
    assert all(json.loads(r["sample"])["file"].startswith("@ext/") for r in ext_rows)
    assert any("project files" in x for x in env.data["reasons"])
    lim = " ".join(env.analysis_coverage.limitations)
    assert "external roots" in lim


def test_B1_external_only_unsupported_nodes_stay_a_limitation(tmp_path: Path) -> None:
    proj = PROJ.replace("specparam tq = 2;", "").replace("specify (a => y) = tq; endspecify", "")
    w = ext_ws(
        tmp_path, "ext_cell u_ext(.a(a), .y(y));\n  proj_cell u_p(.a(a), .y(z));", True, proj
    )
    env = build(w)
    assert env.analysis_coverage is not None
    assert env.analysis_coverage.completeness == "COMPLETE", env.data["reasons"]
    assert any("external roots" in x for x in env.analysis_coverage.limitations)
    assert {r["in_scope"] for r in dbq(w, "SELECT in_scope FROM unsupported_nodes")} == {0}


# ============================================================================== B2
MACRO_PORT = """`define SIG sa
module leaf(input logic [3:0] d, input logic [3:0] e, input logic [3:0] f);
endmodule
module top;
  logic [3:0] sa, sb;
  leaf u(.d(`SIG + 1), .e(sb & `SIG), .f(`SIG));
endmodule
"""


def test_B2_macro_started_or_ended_port_actuals_get_use_site_spans(tmp_path: Path) -> None:
    w = mk(tmp_path, {"t.sv": MACRO_PORT}, "top")
    env = build(w)
    bid = env.build_id
    assert bid
    data = (w.root / "t.sv").read_bytes()
    want = {"d": b"`SIG + 1", "e": b"sb & `SIG", "f": b"`SIG"}
    c = Q.connections(opts(w), bid, "top.u", "r")
    got = {i["formal_port"]: i for i in c.data["items"]}
    for port, text in want.items():
        a = got[port]["actual"]
        loc = a["location"]
        assert data[loc["start"] : loc["end"]] == text, (
            port,
            loc,
        )  # bytes at the span ARE the actual
        assert a["text"] == text.decode()
        assert loc["macro"]["name"] == "SIG" and loc["macro"]["use_site"] is True
        assert got[port]["supported"] is True
    assert got["e"]["actual"]["location"]["start"] == data.index(b"sb & `SIG")


# ============================================================================== B3
PASTE = "`define MK(n) logic r_``n;\r\nmodule top;\r\n\t`MK(a)\r\n\tlogic plain;\r\nendmodule\r\n"


def test_B3_token_pasted_name_span_is_marked_inexact_not_exact(tmp_path: Path) -> None:
    w = mk(tmp_path, {"t.sv": PASTE.encode()}, "top")
    env = build(w)
    bid = env.build_id
    assert bid
    rows = decl_rows(w, bid)
    data = (w.root / "t.sv").read_bytes()
    pasted = decl_by_qn(rows, "top.r_a")[0]
    loc = json.loads(pasted["loc"])
    assert data[loc["start"] : loc["end"]] != b"r_a"  # the span cannot be the name ...
    assert loc["exact"] is False and "token pasting" in loc["inexact_reason"]  # ... so it says so
    plain = json.loads(decl_by_qn(rows, "top.plain")[0]["loc"])
    assert "exact" not in plain  # ordinary names stay exact
    assert data[plain["start"] : plain["end"]] == b"plain"
    s = Q.symbol(opts(w), bid, "r", symbol_id=pasted["symbol_id"])
    assert s.data["symbol"]["location"]["exact"] is False


# ============================================================================== M1
RETVAR = """
module top;
  function automatic int f(int a);
    f = a + 1;
    if (f > 3) f = 3;
  endfunction
  class box;
    int v;
    function int get(); return this.v; endfunction
  endclass
  box b = new();
  int r;
  initial r = f(2) + b.get();
endmodule
"""


def test_M1_function_return_variable_and_this_are_resolved_references(tmp_path: Path) -> None:
    w = mk(tmp_path, {"t.sv": RETVAR}, "top")
    env = build(w)
    bid = env.build_id
    assert bid and env.analysis_coverage is not None
    assert env.analysis_coverage.completeness == "COMPLETE", env.data["reasons"]
    stats = json.loads(dbq(w, "SELECT stats FROM build_reports")[0]["stats"])
    assert stats["dropped_refs_project"] == 0
    rows = decl_rows(w, bid)
    data = (w.root / "t.sv").read_bytes()
    f = decl_by_qn(rows, "top.f", "function")[0]
    refs = [r for r in ref_rows(w, bid) if r["target_id"] == f["declaration_id"]]
    spans = sorted(data[r["span_start"] : r["span_end"]] for r in refs)
    assert [r["kind"] for r in refs].count("return_value") == 3  # f = ..., f > 3, f = 3
    assert [r["kind"] for r in refs].count("call") == 1 and set(spans) == {b"f"}
    box = decl_by_qn(rows, "top.box", "class")[0]
    this = [r for r in ref_rows(w, bid) if r["kind"] == "this"]
    assert this and all(r["target_id"] == box["declaration_id"] for r in this)


def test_M1_remaining_project_drops_make_the_build_partial_with_a_limitation(
    tmp_path: Path,
) -> None:
    sv = """
module top;
  int q[$] = {1, 2, 3};
  int r[$];
  initial r = q.find(x) with (x > 1);
endmodule
"""
    w = mk(tmp_path, {"t.sv": sv}, "top")
    env = build(w)
    assert env.analysis_coverage is not None
    stats = json.loads(dbq(w, "SELECT stats FROM build_reports")[0]["stats"])
    assert stats["dropped_refs_project"] >= 1
    assert env.analysis_coverage.completeness == "PARTIAL"
    assert any("reference occurrence(s) in project files" in x for x in env.data["reasons"])
    assert any("dropped in project files" in x for x in env.analysis_coverage.limitations)


@pytest.mark.skipif(not (UVM_SRC / "uvm_pkg.sv").is_file(), reason="pinned UVM library missing")
def test_M1_external_drops_are_counted_per_scope_and_do_not_block(tmp_path: Path) -> None:
    w = uvm_ws(tmp_path, ["svw_demo_uvm_pkg.sv", "svw_uvm_top.sv"], "svw_uvm_top")
    env = build(w)
    assert env.analysis_coverage is not None
    assert env.analysis_coverage.completeness == "COMPLETE", env.data["reasons"]
    stats = json.loads(dbq(w, "SELECT stats FROM build_reports")[0]["stats"])
    assert stats["dropped_refs_project"] == 0 and stats["dropped_refs_external"] > 100
    assert any("dropped in external files" in x for x in env.analysis_coverage.limitations)


# ============================================================================== M2
def test_M2_config_only_change_makes_the_old_build_stale_then_historical(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    sv = "module top; `ifdef USE_X logic x; `else logic y; `endif endmodule\n"
    w = mk(tmp_path, {"rtl/t.sv": sv}, "top")
    e1 = build(w)
    b1 = e1.build_id
    assert b1 and Q.hierarchy(opts(w), b1, "r").freshness == "CURRENT"
    # only svweave.yaml changes: same source bytes, same snapshot
    w.config(
        {
            "p": profile(
                source_inputs=[{"kind": "file", "path": "rtl/t.sv"}],
                top_modules=["top"],
                defines=[{"name": "USE_X"}],
            )
        },
        source_roots=ROOTS,
    )
    h = Q.hierarchy(opts(w), b1, "r")
    assert h.freshness == "STALE" and any("STALE" in x for x in h.warnings)
    e2 = build(w)
    assert e2.snapshot_id == e1.snapshot_id and e2.build_id != b1
    assert Q.hierarchy(opts(w), e2.build_id, "r").freshness == "CURRENT"  # type: ignore[arg-type]
    assert Q.hierarchy(opts(w), b1, "r").freshness == "HISTORICAL"  # superseded by a newer build
    # reverting the config makes the first build current again (it is not "superseded" any more)
    w.config(
        {"p": profile(source_inputs=[{"kind": "file", "path": "rtl/t.sv"}], top_modules=["top"])},
        source_roots=ROOTS,
    )
    assert Q.hierarchy(opts(w), b1, "r").freshness == "CURRENT"
    # a toolchain/extractor change also invalidates the build
    monkeypatch.setattr(toolchain, "EXTRACTOR_VERSION", "svweave-slang-extractor/other")
    assert Q.hierarchy(opts(w), b1, "r").freshness in ("STALE", "HISTORICAL")


# ============================================================================== M3
CASE = """
module leaf(input logic clk, input logic a);
  ap: assert property (@(posedge clk) a);
endmodule
module wrap(input logic clk, input logic a);
  leaf l(.clk(clk), .a(a));
endmodule
module top(input logic clk, input logic a);
  wrap u(.clk(clk), .a(a));
  wrap U(.clk(clk), .a(a));
endmodule
"""


def test_M3_assertion_scope_filter_is_case_sensitive_and_wildcard_free(tmp_path: Path) -> None:
    w = mk(tmp_path, {"t.sv": CASE}, "top")
    env = build(w)
    bid = env.build_id
    assert bid
    lower = Q.assertions(opts(w), bid, "r", scope="top.u", entry_type="elaborated")
    upper = Q.assertions(opts(w), bid, "r", scope="top.U", entry_type="elaborated")
    assert [i["scope"] for i in lower.data["items"]] == ["top.u.l"]
    assert [i["scope"] for i in upper.data["items"]] == ["top.U.l"]
    # sibling instance names that merely share a prefix do not match (the '.' boundary is exact)
    both = Q.assertions(opts(w), bid, "r", entry_type="elaborated")
    assert {i["scope"] for i in both.data["items"]} == {"top.u.l", "top.U.l"}


def test_M3_references_scope_filter_is_case_sensitive_and_wildcard_free(tmp_path: Path) -> None:
    files = {
        "tx.sv": "module m_leaf; endmodule\n",
        "t_.sv": "module top; int v; initial v = 1; m_leaf u(); endmodule\n",
    }
    w = mk(tmp_path, files, "top")
    env = build(w)
    bid = env.build_id
    assert bid
    sym = decl_by_qn(decl_rows(w, bid), "top.v", "variable")[0]["symbol_id"]
    n = lambda scope: Q.references(opts(w), bid, sym, "r", scope=scope).data["total_matches"]  # noqa: E731
    assert n("t_") == 1 and n(None) == 1
    assert n("T_") == 0  # LIKE would have matched
    assert n("tx") == 0 and n("t%") == 0 and n("t_.sv") == 1 and n("T_.sv") == 0
    assert n("t.") == 0  # '_' is not a wildcard


# ============================================================================== 15.4 exit codes
def test_exit_codes_for_stale_historical_and_partial_queries(tmp_path: Path) -> None:
    w = mk(tmp_path, {"t.sv": "module top; logic a; endmodule\n"}, "top")
    e1 = build(w)
    b1 = e1.build_id
    assert b1
    code, out = cli_run(w.root, "--compact", "query", "hierarchy", "--build", b1)
    assert code == 0 and json.loads(out)["freshness"] == "CURRENT"
    (w.root / "t.sv").write_text("module top; logic a, b; endmodule\n")
    code, out = cli_run(w.root, "--compact", "query", "hierarchy", "--build", b1)
    assert code == 5 and json.loads(out)["freshness"] == "STALE"  # state still in the result
    code, out = cli_run(w.root, "--compact", "query", "hierarchy", "--build", b1, "--allow-stale")
    assert code == 0 and json.loads(out)["freshness"] == "STALE"
    e2 = build(w)
    code, out = cli_run(w.root, "--compact", "query", "diagnostics", "--build", b1)
    assert code == 5 and json.loads(out)["freshness"] == "HISTORICAL"
    assert (
        cli_run(w.root, "--compact", "query", "diagnostics", "--build", b1, "--allow-stale")[0] == 0
    )
    assert cli_run(w.root, "--compact", "query", "diagnostics", "--build", e2.build_id)[0] == 0  # type: ignore[arg-type]
    # PARTIAL builds
    w2 = mk(tmp_path / "e", {"t.sv": "module top; logic [7:0 x; endmodule\n"}, "top")
    pb = build(w2).build_id
    assert pb
    code, out = cli_run(w2.root, "--compact", "query", "diagnostics", "--build", pb)
    assert code == 3 and json.loads(out)["analysis_coverage"]["completeness"] == "PARTIAL"
    code, out = cli_run(
        w2.root, "--compact", "query", "diagnostics", "--build", pb, "--allow-partial"
    )
    assert code == 0 and json.loads(out)["data"]["total_matches"] >= 1
    for cmd in (["hierarchy"], ["assertions"]):
        assert cli_run(w2.root, "--compact", "query", *cmd, "--build", pb)[0] == 3
        assert (
            cli_run(w2.root, "--compact", "query", *cmd, "--build", pb, "--allow-partial")[0] == 0
        )
    # config-only staleness reaches the CLI too
    w3 = mk(tmp_path / "c", {"rtl/t.sv": "module top; endmodule\n"}, "top")
    cb = build(w3).build_id
    assert cb
    w3.config(
        {
            "p": profile(
                source_inputs=[{"kind": "file", "path": "rtl/t.sv"}],
                top_modules=["top"],
                defines=[{"name": "X"}],
            )
        },
        source_roots=ROOTS,
    )
    assert cli_run(w3.root, "--compact", "query", "hierarchy", "--build", cb)[0] == 5


# ============================================================================== M4
@pytest.mark.skipif(not (UVM_SRC / "uvm_pkg.sv").is_file(), reason="pinned UVM library missing")
def test_M4_cli_pretty_output_respects_the_128kib_cap(tmp_path: Path) -> None:
    w = uvm_ws(tmp_path, ["svw_demo_uvm_pkg.sv", "svw_uvm_top.sv"], "svw_uvm_top")
    bid = build(w).build_id
    assert bid
    top = dbq(
        w,
        'SELECT target_id, COUNT(*) n FROM "references" WHERE build_id=? AND '
        "target_id IS NOT NULL GROUP BY target_id ORDER BY n DESC LIMIT 1",
        bid,
    )[0]
    sym = dbq(w, "SELECT symbol_id FROM declarations WHERE declaration_id=?", top["target_id"])[0]
    env = Q.references(opts(w), bid, sym["symbol_id"], "r", limit=500)
    compact = len(env.to_json(indent=None).encode())
    assert compact <= MAX_RESPONSE_BYTES < len(env.to_json(indent=2).encode())  # the trap
    code, out = cli_run(
        w.root,
        "query",
        "references",
        "--build",
        bid,
        "--symbol",
        sym["symbol_id"],
        "--limit",
        "500",
        "--allow-partial",
    )
    assert code == 0 and len(out.encode()) <= MAX_RESPONSE_BYTES
    doc = json.loads(out)
    assert doc["pagination"]["truncated"] and doc["pagination"]["next_cursor"]


# ============================================================================== M6
def run_files(w: Ws, job_id: str) -> dict[str, Any]:
    d = w.root / ".svweave" / "runs" / job_id
    return {p.name: p for p in d.iterdir()}


def test_M6_failed_and_aborted_jobs_still_leave_all_thirteen_artifacts(tmp_path: Path) -> None:
    expect = set(ARTIFACTS) | {"events.jsonl"}
    assert len(expect) == 13
    # (a) worker killed by timeout
    w = mk(tmp_path / "a", {"t.sv": "module top; endmodule\n"}, "top")
    import yaml

    cfgp = w.root / "svweave.yaml"
    d = yaml.safe_load(cfgp.read_text())
    d["analysis"] = {"max_build_seconds": 1}
    cfgp.write_text(yaml.safe_dump(d, sort_keys=False))
    env = build(w, test_fault="hang")
    files = run_files(w, env.data["job_id"])
    assert expect <= set(files)
    assert json.loads(files["resource_usage.json"].read_text())["wall_s"] > 0  # what ran is real
    na = json.loads(files["dependency_report.json"].read_text())
    assert na["status"] == "not_available" and "reason" in na  # what did not run says so
    assert json.loads(files["result.json"].read_text())["error"]["code"] == "BUILD_TIMEOUT"
    # (b) crash
    env = build(w, test_fault="abort")
    assert expect <= set(run_files(w, env.data["job_id"]))
    # (c) an exception before the worker even starts (explicit_groups with library inputs)
    grp = {"id": "g1", "sources": [{"kind": "file", "path": "t.sv"}], "library_dirs": ["."]}
    w2 = mk(
        tmp_path / "b",
        {"t.sv": "module top; endmodule\n"},
        "top",
        source_inputs=[],
        compilation_units={"mode": "explicit_groups", "groups": [grp]},
    )
    from svweave.domain.errors import SvwError

    with pytest.raises(SvwError):
        build(w2)
    (jobdir,) = (w2.root / ".svweave" / "runs").iterdir()
    assert expect <= {p.name for p in jobdir.iterdir()}
    res = json.loads((jobdir / "result.json").read_text())
    assert res["status"] == "aborted" and res["code"] == "CAPABILITY_MISMATCH"
    ev = [json.loads(x) for x in (jobdir / "events.jsonl").read_text().splitlines()]
    assert ev[-1]["event"] == "aborted"


# ============================================================================== surviving mutants
def test_ignored_severity_diagnostics_are_excluded_from_counts_and_outcome(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    sv = """
module top;
  logic [3:0] w;
  int i;
  logic [7:0] big;
  initial begin
    w = big;
    i = w + 1;
    if (i < big) w = 0;
    if (i) w = 1;
  end
endmodule
"""
    root = tmp_path / "raw"
    root.mkdir()
    (root / "t.sv").write_text(sv)
    monkeypatch.setenv("SVW_TEST_NATIVE_SECRET", "must-not-reach-native-check")
    premise = native_check("ignored_diagnostics", root)
    assert premise["ignored"] >= 3  # independent real premise: slang hands us default-off diagnostics
    r = raw_dto(WorkerRequest(snapshot_root=str(root), args=["--top", "top", "t.sv"]))
    assert r["diagnostics"] == []
    w = mk(tmp_path / "w", {"t.sv": sv}, "top")
    env = build(w)
    assert env.data["diagnostic_outcome"] == "NO_ERRORS" and env.data["counts"]["diagnostics"] == 0
    assert env.analysis_coverage is not None and env.analysis_coverage.completeness == "COMPLETE"
    assert dbq(w, "SELECT 1 FROM diagnostics") == []


def _sleepers() -> set[int]:
    """Host pids of the fault-injection ``sleep 3600``: the worker runs in its own PID namespace,
    so the pid it prints is not a host pid."""
    out: set[int] = set()
    for d in Path("/proc").iterdir():
        if d.name.isdigit():
            try:
                alive = (d / "stat").read_text().rsplit(")", 1)[1].split()[0] != "Z"
                if alive and (d / "cmdline").read_bytes() == b"sleep\x003600\x00":
                    out.add(int(d.name))
            except OSError:
                pass
    return out


def test_process_group_is_reaped_after_a_NORMAL_worker_exit(tmp_path: Path) -> None:
    (tmp_path / "t.sv").write_text("module top; endmodule\n")
    req = WorkerRequest(snapshot_root=str(tmp_path), args=["--top", "top", "t.sv"])
    before = _sleepers()
    t0 = time.monotonic()
    out = run_worker(req, timeout_s=60, memory_mib=4096, test_fault="grandchild")
    assert out.kind == "ok" and out.exit_code == 0  # a normal, successful exit ...
    assert time.monotonic() - t0 < 30
    assert "FAULT_CHILD_PID=" in out.stderr_tail
    for _ in range(40):
        if not (_sleepers() - before):
            break
        time.sleep(0.05)
    assert not (_sleepers() - before)  # ... still leaves no orphaned descendant
    assert json.loads(out.stdout)["status"]["compiled"] is True
