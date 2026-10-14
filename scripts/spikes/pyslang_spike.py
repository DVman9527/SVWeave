#!/usr/bin/env python3
"""M0 pyslang capability spike (SVW-002).

Runs each build in a separate child process (``--one <build>``) so that peak RSS
and wall time are measured per build, mirroring the planned out-of-process HDL
worker (plan §7.1).  Results are printed as JSON and written to
``.svweave/spike/pyslang/result.json`` plus ``evidence/m0/pyslang_spike.json``.

No mocks: everything here is the real pinned pyslang binding.

Usage:
    .venv/bin/python scripts/spikes/pyslang_spike.py [--root DIR] [--uvm-root DIR]
        [--out PATH] [--evidence PATH] [--only build1,build2]
"""

from __future__ import annotations

import argparse
import json
import os
import resource
import shlex
import subprocess
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]


# --------------------------------------------------------------------------
# helpers that run inside the child process
# --------------------------------------------------------------------------

def _build(cmdline: list[str], cwd: Path):
    """Create a slang Driver from an argv list and return (driver, compilation).

    The driver owns the SourceManager; keep it alive as long as any AST object is used.
    """
    import pyslang
    from pyslang import driver as drv

    d = drv.Driver()
    d.addStandardArgs()
    # parseCommandLine takes ONE string that slang tokenizes itself; quote every arg.
    line = " ".join(shlex.quote(a) for a in ["slang", *cmdline])
    opts = drv.CommandLineOptions()
    ok_cmd = d.parseCommandLine(line, opts)
    ok_opt = d.processOptions()
    ok_parse = d.parseAllSources()
    comp = d.createCompilation()
    return d, comp, {"parseCommandLine": ok_cmd, "processOptions": ok_opt, "parseAllSources": ok_parse,
                     "pyslang_version": pyslang.__version__}


def _loc(sm, loc) -> dict:
    """Map a SourceLocation to file + byte offset (+ macro handling)."""
    if not loc:
        return {"valid": False}
    out = {
        "buffer_id": loc.buffer.id,
        "offset": loc.offset,
        "is_macro_loc": bool(sm.isMacroLoc(loc)),
    }
    floc = loc
    if sm.isMacroLoc(loc):
        out["macro_name"] = sm.getMacroName(loc)
        floc = sm.getFullyOriginalLoc(loc)
        exp = sm.getFullyExpandedLoc(loc)
        out["expansion_loc"] = {"file": sm.getFileName(exp), "offset": exp.offset,
                                "line": sm.getLineNumber(exp), "column": sm.getColumnNumber(exp)}
    out.update({
        "file": sm.getFileName(floc),
        "full_path": str(sm.getFullPath(floc.buffer)),
        "file_offset": floc.offset,
        "line": sm.getLineNumber(floc),
        "column": sm.getColumnNumber(floc),
    })
    return out


def _range(sm, rng) -> dict:
    s, e = rng.start, rng.end
    out = {"start": _loc(sm, s), "end_offset": e.offset}
    if s.buffer == e.buffer and sm.isFileLoc(s):
        text = sm.getSourceText(s.buffer)
        # getSourceText returns str; offsets are byte offsets -> slice on bytes
        b = text.encode("utf-8")
        out["text"] = b[s.offset:e.offset].decode("utf-8", "replace")
    return out


def _diags(d, comp, include_ignored: bool = False) -> list[dict]:
    sm = comp.sourceManager
    eng = d.diagEngine
    import pyslang
    res = []
    for dg in comp.getAllDiagnostics():
        sev = eng.getSeverity(dg.code, dg.location)
        # QUIRK: Compilation.getAllDiagnostics() also returns diagnostics whose effective
        # severity is Ignored (default-off warnings). Filter them like the CLI does.
        if sev == pyslang.DiagnosticSeverity.Ignored and not include_ignored:
            continue
        res.append({
            "code": str(dg.code),
            "severity": str(sev),
            "is_error": dg.isError(),
            "message": eng.formatMessage(dg),
            "location": _loc(sm, dg.location),
            "option_name": eng.getOptionName(dg.code),
        })
    return res


def _deps(d, comp) -> dict:
    """Actual files read: all SourceManager buffers that are backed by files."""
    import pyslang
    sm = comp.sourceManager
    files = []
    seen: set[str] = set()
    # QUIRK: every `include creates a NEW BufferID (one per inclusion), so the same header
    # appears many times in getAllBuffers(); dedupe on full path for the dependency set.
    for b in sm.getAllBuffers():
        kind = sm.getBufferKind(b)
        name = sm.getRawFileName(b)
        if kind == pyslang.BufferKind.Macro or name.startswith("<unnamed_buffer"):
            continue
        fp = str(sm.getFullPath(b))
        if fp in seen:
            continue
        seen.add(fp)
        inc_from = sm.getIncludedFrom(b)
        files.append({
            "buffer_id": b.id,
            "raw_name": name,
            "full_path": str(sm.getFullPath(b)),
            "kind": str(kind),
            "included_from": (None if not inc_from else
                              {"file": sm.getFileName(inc_from), "offset": inc_from.offset}),
        })
    include_directives = []
    for t in d.syntaxTrees:
        for inc in t.getIncludeDirectives():
            include_directives.append({"path": inc.path,
                                       "resolved_buffer": inc.buffer.id if inc.buffer else None,
                                       "directive_loc": _loc(sm, inc.syntax.sourceRange.start)})
    return {"files": files, "include_directives": include_directives,
            "total_buffers": len(sm.getAllBuffers()),
            "unnamed_buffers": sum(1 for b in sm.getAllBuffers()
                                   if sm.getRawFileName(b).startswith("<unnamed_buffer"))}


def _actual_text(e):
    from pyslang import ast
    if e is None:
        return None
    if e.kind == ast.ExpressionKind.Assignment and e.syntax is None:
        e = e.left
    return str(e.syntax).strip() if e.syntax is not None else None


def _collect(comp):
    from pyslang import ast
    insts, props, asserts = [], [], []

    def cb(n):
        if isinstance(n, ast.InstanceSymbol):
            insts.append(n)
        elif isinstance(n, ast.PropertySymbol):
            props.append(n)
        elif isinstance(n, ast.ConcurrentAssertionStatement):
            asserts.append(n)
        return ast.VisitAction.Advance

    comp.getRoot().visit(cb)
    return insts, props, asserts


def _bind_info(sm, inst) -> dict | None:
    """Return bind directive info if this instance was created by a bind directive."""
    from pyslang import syntax as syn
    s = inst.syntax
    node = s.parent if s is not None else None
    while node is not None:
        if node.kind == syn.SyntaxKind.BindDirective:
            return {"bind_directive": _range(sm, node.sourceRange),
                    "bind_target_text": str(node.target).strip() if hasattr(node, "target") else None}
        node = node.parent
    return None


def run_demo(lanes: int, root: Path) -> dict:
    from pyslang import ast
    args = ["-f", "fixtures/demo/filelists/rtl.f", "-I", "fixtures/demo/include", "-DSVW_DEMO=1",
            "--top", "demo_top", "-G", f"LANES={lanes}"]
    d, comp, st = _build(args, root)
    sm = comp.sourceManager
    insts, props, asserts = _collect(comp)
    lane_insts = [i for i in insts if i.definition.name == "demo_lane"]
    out: dict = {"argv": args, "cwd": str(root), "status": st,
                 "diagnostics": _diags(d, comp),
                 "diagnostics_including_ignored_count": len(_diags(d, comp, include_ignored=True)),
                 "top_instances": [t.hierarchicalPath for t in comp.getRoot().topInstances],
                 "demo_lane_instance_count": len(lane_insts),
                 "demo_lane_instances": [
                     {"path": i.hierarchicalPath,
                      "params": {p.name: str(p.value) for p in i.body.parameters if hasattr(p, "value")},
                      "loc": _loc(sm, i.location)} for i in lane_insts],
                 }

    # (b) port connections of g_lane[0].u_lane (lookup by hierarchical name)
    target = comp.getRoot().lookupName("demo_top.g_lane[0].u_lane")
    pcs = []
    for pc in target.portConnections:
        e = pc.expression
        refs = []
        if e is not None:
            def ecb(x):
                if isinstance(x, ast.NamedValueExpression):
                    refs.append({"symbol": x.symbol.hierarchicalPath, "range": _range(sm, x.sourceRange)})
            e.visit(ecb)
        pcs.append({
            "port": pc.port.name,
            "direction": str(pc.port.direction),
            "port_type": str(pc.port.type),
            "port_decl_loc": _loc(sm, pc.port.location),
            "expr_kind": str(e.kind) if e is not None else None,
            # For output ports slang wraps the actual in an Assignment expr; syntax still is the actual.
            # QUIRK: output-port actuals are AssignmentExpression(left=actual, right=EmptyArgument)
            # with expression.syntax == None; use .left.syntax for the text. expression.sourceRange
            # is valid in both cases and covers exactly the actual.
            "actual_syntax_text": _actual_text(e),
            "actual_range": _range(sm, e.sourceRange) if e is not None else None,
            "is_output_assignment_wrapper": bool(e is not None and e.kind == ast.ExpressionKind.Assignment),
            "expr_type": str(e.type) if e is not None else None,
            "referenced_symbols": refs,
        })
    out["port_connections_g_lane0_u_lane"] = {"lookup": "demo_top.g_lane[0].u_lane",
                                              "found": target.hierarchicalPath, "connections": pcs}

    # (c) bind + SVA
    sva_insts = [i for i in insts if i.definition.name == "stream_hold_sva"]
    out["bound_checkers"] = [{"path": i.hierarchicalPath, "decl_loc": _loc(sm, i.location),
                              "bind": _bind_info(sm, i)} for i in sva_insts]
    out["properties"] = [{"path": p.hierarchicalPath, "loc": _loc(sm, p.location)} for p in props]
    al = []
    for a in asserts:
        member = a.syntax.parent if a.syntax is not None else None
        label = None
        if member is not None and hasattr(member, "statement"):
            pass
        # Labeled assertion: label lives on the ConcurrentAssertionStatement syntax.
        st_syn = a.syntax
        if st_syn is not None and getattr(st_syn, "label", None) is not None:
            label = str(st_syn.label.name).strip()
        al.append({"kind": str(a.assertionKind), "label": label,
                   "syntax_kind": str(st_syn.kind) if st_syn else None,
                   "range": _range(sm, st_syn.sourceRange) if st_syn else None})
    out["assertions"] = al
    # Also lookup by name inside one bound checker body
    sva0 = comp.getRoot().lookupName("demo_top.g_lane[0].u_lane.u_stream_hold_sva")
    ap = sva0.body.find("ap_stall_holds") if sva0 is not None else None
    out["lookup_ap_stall_holds"] = (None if ap is None else
                                    {"kind": str(ap.kind), "path": ap.hierarchicalPath, "loc": _loc(sm, ap.location)})
    out["dependencies"] = _deps(d, comp)
    return out


def run_uvm(root: Path, uvm_src: Path, fixture_dir: Path) -> dict:
    from pyslang import ast
    args = [f"+incdir+{uvm_src}", str(uvm_src / "uvm_pkg.sv"),
            str(fixture_dir / "svw_demo_uvm_pkg.sv"), str(fixture_dir / "svw_uvm_top.sv"),
            "--top", "svw_uvm_top"]
    d, comp, st = _build(args, root)
    sm = comp.sourceManager
    out: dict = {"argv": args, "cwd": str(root), "status": st}
    diags = _diags(d, comp)
    out["diagnostics_count"] = len(diags)
    out["diagnostics_including_ignored_count"] = len(_diags(d, comp, include_ignored=True))
    out["diagnostics_errors"] = [x for x in diags if x["is_error"]]
    out["diagnostics_in_fixture"] = [x for x in diags if str(fixture_dir) in x["location"].get("full_path", "")]
    out["diagnostics_sample"] = diags[:5]

    pkg = comp.getPackage("svw_demo_uvm_pkg")
    methods = []
    for cls_name, meth in (("svw_driver", "run_phase"), ("svw_monitor", "run_phase"),
                           ("svw_env", "build_phase"), ("svw_smoke_test", "build_phase")):
        cls = pkg.find(cls_name)
        m = cls.find(meth)
        body_rng = m.syntax.sourceRange if m.syntax is not None else None
        methods.append({
            "qualified": f"svw_demo_uvm_pkg::{cls_name}::{meth}",
            "class_kind": str(cls.kind),
            "class_base": str(cls.baseClass) if hasattr(cls, "baseClass") else None,
            "symbol_kind": str(m.kind),
            "hierarchicalPath": m.hierarchicalPath,
            "lexicalPath": m.lexicalPath,
            "is_virtual": m.isVirtual,
            "overrides": (m.override.hierarchicalPath if m.override is not None else None),
            "name_loc": _loc(sm, m.location),
            "decl_range": _range(sm, body_rng) if body_rng is not None else None,
        })
    out["run_phase_methods"] = [x for x in methods if x["qualified"].endswith("::run_phase")]
    out["same_name_methods"] = methods

    # (g) references: NamedValue + Call targets inside the fixture package
    refs = []
    fixture_buffers = {b.id for b in sm.getAllBuffers() if str(fixture_dir) in str(sm.getFullPath(b))}

    def in_fixture(loc):
        # A macro-body token's *original* location is the `define site (e.g. in UVM's
        # uvm_message_defines.svh); its *expanded* location is the macro usage site.
        # Accept a reference if either lands in a fixture buffer.
        if not loc:
            return False
        if not sm.isMacroLoc(loc):
            return loc.buffer.id in fixture_buffers
        return (sm.getFullyOriginalLoc(loc).buffer.id in fixture_buffers
                or sm.getFullyExpandedLoc(loc).buffer.id in fixture_buffers)

    def rcb(n):
        if isinstance(n, ast.NamedValueExpression):
            if in_fixture(n.sourceRange.start):
                refs.append({"ref_kind": "NamedValue", "target": n.symbol.hierarchicalPath,
                             "target_kind": str(n.symbol.kind), "at": _loc(sm, n.sourceRange.start)})
        elif isinstance(n, ast.CallExpression):
            if in_fixture(n.sourceRange.start):
                if n.isSystemCall:
                    tgt = n.subroutineName
                else:
                    tgt = n.subroutine.hierarchicalPath
                refs.append({"ref_kind": "Call", "system": n.isSystemCall, "name": n.subroutineName,
                             "target": tgt, "at": _loc(sm, n.sourceRange.start)})
        return ast.VisitAction.Advance

    for cls_name in ("svw_driver", "svw_monitor", "svw_env", "svw_smoke_test"):
        pkg.find(cls_name).visit(rcb)
    comp.getRoot().lookupName("svw_uvm_top").visit(rcb)
    out["references_in_fixture"] = refs
    deps = _deps(d, comp)
    out["dependencies_summary"] = {"file_count": len(deps["files"]),
                                   "fixture_files": [f for f in deps["files"] if str(fixture_dir) in f["full_path"]],
                                   "first_uvm_files": [f["raw_name"] for f in deps["files"]][:8],
                                   "unnamed_buffers": deps["unnamed_buffers"],
                                   "total_buffers": deps["total_buffers"]}
    return out


def child(build: str, root: Path, uvm_src: Path, fixture_root: Path) -> dict:
    os.chdir(root)
    if build == "demo_L2":
        return run_demo(2, root)
    if build == "demo_L4":
        return run_demo(4, root)
    if build == "uvm":
        return run_uvm(root, uvm_src, fixture_root / "uvm")
    raise SystemExit(f"unknown build {build}")


# --------------------------------------------------------------------------
# parent orchestration
# --------------------------------------------------------------------------

def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", default=str(REPO), help="cwd for the build (fixture paths are relative)")
    ap.add_argument("--fixture-root", default=None, help="dir containing demo/ and uvm/ (default <root>/fixtures)")
    ap.add_argument("--uvm-src", default=str(REPO / ".toolchain/uvm-core/src"))
    ap.add_argument("--out", default=str(REPO / ".svweave/spike/pyslang/result.json"))
    ap.add_argument("--evidence", default=str(REPO / "evidence/m0/pyslang_spike.json"))
    ap.add_argument("--only", default="demo_L2,demo_L4,uvm")
    ap.add_argument("--one", default=None, help=argparse.SUPPRESS)
    ap.add_argument("--timeout", type=float, default=300)
    a = ap.parse_args()
    root = Path(a.root).resolve()
    fixture_root = Path(a.fixture_root).resolve() if a.fixture_root else root / "fixtures"
    uvm_src = Path(a.uvm_src).resolve()

    if a.one:
        res = child(a.one, root, uvm_src, fixture_root)
        sys.stdout.write(json.dumps(res, default=str))
        return 0

    results = {"tool": "pyslang_spike", "python": sys.version.split()[0], "builds": {}}
    for b in a.only.split(","):
        t0 = time.monotonic()
        before = resource.getrusage(resource.RUSAGE_CHILDREN).ru_maxrss
        cmd = [sys.executable, "-I", str(Path(__file__).resolve()), "--one", b, "--root", str(root),
               "--fixture-root", str(fixture_root), "--uvm-src", str(uvm_src)]
        try:
            p = subprocess.run(cmd, capture_output=True, text=True, timeout=a.timeout)
            wall = time.monotonic() - t0
            # ru_maxrss of RUSAGE_CHILDREN = max over all waited children (KiB on Linux)
            after = resource.getrusage(resource.RUSAGE_CHILDREN).ru_maxrss
            entry = {"returncode": p.returncode, "wall_s": round(wall, 3),
                     "peak_rss_kib_children_max": after,
                     "peak_rss_note": ("exact for this build" if after > before else
                                       "<= previous child max (ru_maxrss is a running max)"),
                     "stderr_tail": p.stderr[-2000:]}
            if p.returncode == 0:
                entry["result"] = json.loads(p.stdout)
            else:
                entry["stdout_tail"] = p.stdout[-2000:]
        except subprocess.TimeoutExpired as e:
            entry = {"returncode": None, "error": f"timeout after {a.timeout}s", "stderr_tail": str(e.stderr)[-2000:]}
        results["builds"][b] = entry

    # summary checks (facts, not judgements)
    s = {}
    bl = results["builds"]
    if "demo_L2" in bl and "result" in bl["demo_L2"]:
        r = bl["demo_L2"]["result"]
        s["demo_L2_lane_count"] = r["demo_lane_instance_count"]
        s["demo_L2_diag_count"] = len(r["diagnostics"])
        dconn = [c for c in r["port_connections_g_lane0_u_lane"]["connections"] if c["port"] == "data_i"]
        s["g_lane0_data_i_actual_text"] = dconn[0]["actual_syntax_text"] if dconn else None
        s["bound_checker_paths_L2"] = [x["path"] for x in r["bound_checkers"]]
    if "demo_L4" in bl and "result" in bl["demo_L4"]:
        r = bl["demo_L4"]["result"]
        s["demo_L4_lane_count"] = r["demo_lane_instance_count"]
        s["demo_L4_diag_count"] = len(r["diagnostics"])
        s["demo_L4_lane_paths"] = [x["path"] for x in r["demo_lane_instances"]]
    if "uvm" in bl and "result" in bl["uvm"]:
        r = bl["uvm"]["result"]
        s["uvm_error_count"] = len(r["diagnostics_errors"])
        s["uvm_diag_count"] = r["diagnostics_count"]
        s["run_phase"] = [{"q": m["qualified"], "hier": m["hierarchicalPath"],
                           "file": m["name_loc"]["file"], "offset": m["name_loc"]["file_offset"]}
                          for m in r["run_phase_methods"]]
    results["summary"] = s

    for path in (a.out, a.evidence):
        if path:
            Path(path).parent.mkdir(parents=True, exist_ok=True)
            Path(path).write_text(json.dumps(results, indent=2, default=str) + "\n")
    print(json.dumps(results["summary"], indent=2))
    ok = all(v.get("returncode") == 0 for v in bl.values())
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
