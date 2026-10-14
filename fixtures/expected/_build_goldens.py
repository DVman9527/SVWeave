#!/usr/bin/env python3
"""Writes fixtures/expected/*.json.

The EXPECTED CONTENT below is hand-written from reading the SystemVerilog fixtures (names,
qualified scopes, kinds, instance paths, counts, association styles).  The byte offsets are NOT
taken from SVWeave or pyslang: each span is located with ``bytes.find`` on the fixture file using
a hand-chosen unique context string (``ctx``) and the exact token text inside it (``text``).
This script imports nothing from svweave.  See README.md in this directory.

Usage: python3 fixtures/expected/_build_goldens.py            (rewrites the JSON files)
       python3 fixtures/expected/_build_goldens.py --check    (exit 1 if a JSON differs)
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
FIX = HERE.parent  # fixtures/
ROOTS = {"language": FIX / "language", "uvm": FIX / "uvm", "demo": FIX / "demo"}


def span(
    base: str, file: str, ctx: str, text: str | None = None, nth: int = 0, at: int | None = None
) -> dict[str, Any]:
    """Locate ``text`` inside the ``nth`` occurrence of ``ctx`` in ``<root>/<file>``."""
    root, _, rel = file.partition(":")
    path = ROOTS[root] / rel
    data = path.read_bytes()
    needle = ctx.encode("utf-8")
    pos = -1
    for _ in range(nth + 1):
        pos = data.find(needle, pos + 1)
        if pos < 0:
            raise SystemExit(f"{file}: context {ctx!r} (nth={nth}) not found")
    if nth == 0 and data.find(needle, pos + 1) >= 0:
        raise SystemExit(f"{file}: context {ctx!r} is not unique; give nth or a longer context")
    t = ctx if text is None else text
    off = ctx.index(t) if at is None else at
    tb = t.encode("utf-8")
    start = pos + len(ctx[:off].encode("utf-8"))
    assert data[start : start + len(tb)] == tb, (file, ctx, text)
    return {"file": rel, "ctx": ctx, "nth": nth, "text": t, "start": start, "end": start + len(tb)}


class T:
    """One golden file under construction (paths are relative to ``fixtures/<root>/<topic>``)."""

    def __init__(self, name: str, root: str, topic: str, **meta: Any) -> None:
        self.name, self.root, self.topic = name, root, topic
        self.doc: dict[str, Any] = {"topic": name, "fixture_dir": f"{root}/{topic}", **meta}

    def f(self, rel: str) -> str:
        return f"{self.root}:{self.topic}/{rel}" if self.topic else f"{self.root}:{rel}"

    def d(
        self, kind: str, qn: str, file: str, ctx: str, text: str | None = None, **kw: Any
    ) -> None:
        nth = kw.pop("nth", 0)
        at = kw.pop("at", None)
        sp = span(self.name, self.f(file), ctx, text or qn.split("::")[-1].split(".")[-1], nth, at)
        sp["file"] = file
        e = {"kind": kind, "qualified_name": qn, **sp, **kw}
        self.doc.setdefault("declarations", []).append(e)

    def r(
        self,
        kind: str,
        file: str,
        ctx: str,
        text: str,
        target: str | None,
        status: str = "resolved_static",
        **kw: Any,
    ) -> None:
        nth = kw.pop("nth", 0)
        at = kw.pop("at", None)
        sp = span(self.name, self.f(file), ctx, text, nth, at)
        sp["file"] = file
        e = {"kind": kind, "target": target, "status": status, **sp, **kw}
        self.doc.setdefault("references", []).append(e)

    def closed(self, decl_files: list[str], ref_files: list[str] | None = None) -> None:
        """Declare a CLOSED scope (plan 20.1): declarations located in ``decl_files`` (and
        references whose source occurrence is in ``ref_files``) must equal exactly the entries of
        this golden plus the hand-reviewed exclusions added with ``xd`` / ``xr``."""
        cs: dict[str, Any] = {"declaration_files": decl_files}
        if ref_files is not None:
            cs["reference_files"] = ref_files
        self.doc["closed_scope"] = cs

    def xd(self, kind: str, qn: str, file: str, ctx: str, text: str, reason: str, nth: int = 0,
           at: int | None = None) -> None:
        """Hand-reviewed exclusion: a declaration that exists in the source but is deliberately not
        a golden entry (e.g. implicit symbols); ``reason`` is mandatory."""
        sp = self.span(file, ctx, text, nth)
        if at is not None:
            sp = {**span(self.name, self.f(file), ctx, text, nth, at), "file": file}
        self.doc["closed_scope"].setdefault("exclude_declarations", []).append(
            {"kind": kind, "qualified_name": qn, **sp, "reason": reason})

    def xr(self, kind: str, file: str, ctx: str, text: str, reason: str, nth: int = 0) -> None:
        sp = self.span(file, ctx, text, nth)
        self.doc["closed_scope"].setdefault("exclude_references", []).append(
            {"kind": kind, **sp, "reason": reason})

    def add(self, key: str, value: Any) -> None:
        self.doc[key] = value

    def span(self, file: str, ctx: str, text: str | None = None, nth: int = 0) -> dict[str, Any]:
        sp = span(self.name, self.f(file), ctx, text, nth)
        sp["file"] = file
        return sp


GOLDENS: list[T] = []


def g(name: str, root: str, topic: str, **meta: Any) -> T:
    t = T(name, root, topic, **meta)
    GOLDENS.append(t)
    return t


# =============================================================================== SEM-01
t = g("sem01_scopes", "language", "sem01_scopes", top="sem01_top")
for pk, fld in (("pkg_a", "count"), ("pkg_b", "total")):
    f = f"{pk}.sv"
    t.d("package", pk, f, f"package {pk};", pk)
    t.d("class", f"{pk}::agent", f, "class agent;", "agent")
    t.d("field", f"{pk}::agent::{fld}", f, f"int {fld};", fld)
    t.d(
        "task", f"{pk}::agent::run_phase", f, "virtual task run_phase();", "run_phase", virtual=True
    )
    t.d("function", f"{pk}::agent::write", f, "function void write(int v);", "write", virtual=False)
    t.d("argument", f"{pk}::agent::write.v", f, "int v);", "v")
t.d("module", "sem01_top", "top.sv", "module sem01_top;", "sem01_top")
t.d("variable", "sem01_top.a", "top.sv", "pkg_a::agent a;", "a", at=13)
t.d("variable", "sem01_top.b", "top.sv", "pkg_b::agent b;", "b", at=13)
t.r("call", "top.sv", "a.write(1)", "write", "pkg_a::agent::write")
t.r("call", "top.sv", "b.write(2)", "write", "pkg_b::agent::write")
t.r("call", "top.sv", "a.run_phase()", "run_phase", "pkg_a::agent::run_phase", "dynamic_unresolved")
t.r("call", "top.sv", "b.run_phase()", "run_phase", "pkg_b::agent::run_phase", "dynamic_unresolved")
t.r("value", "pkg_a.sv", "count++", "count", "pkg_a::agent::count")
t.r("value", "pkg_a.sv", "count += v", "count", "pkg_a::agent::count")
t.r("value", "pkg_b.sv", "total--", "total", "pkg_b::agent::total")
t.r("value", "pkg_b.sv", "total -= v", "total", "pkg_b::agent::total")
t.r("type", "top.sv", "pkg_a::agent a;", "pkg_a::agent", "pkg_a::agent")
t.r("type", "top.sv", "pkg_b::agent b;", "pkg_b::agent", "pkg_b::agent")
t.add("counts", {"run_phase_declarations": 2, "write_declarations": 2, "instances": 1})

# =============================================================================== SEM-02
t = g("sem02_paramclass", "language", "sem02_paramclass", top="sem02_top")
t.d("package", "box_pkg", "box_pkg.sv", "package box_pkg;", "box_pkg")
t.d("class", "box_pkg::box", "box_pkg.sv", "class box #(", "box")
t.d("parameter", "box_pkg::box::W", "box_pkg.sv", "int W = 8", "W")
t.d("type_parameter", "box_pkg::box::T", "box_pkg.sv", "type T = logic", "T")
t.d("field", "box_pkg::box::data", "box_pkg.sv", "data;", "data")
t.d("field", "box_pkg::box::item", "box_pkg.sv", "T item;", "item")
t.d("function", "box_pkg::box::width", "box_pkg.sv", "function int width()", "width")
t.d("variable", "sem02_top.b8", "top.sv", "b8 = new()", "b8")
t.d("variable", "sem02_top.b16", "top.sv", "b16 = new()", "b16")
t.add(
    "specializations",
    [
        {
            "text": "box_pkg::box#(8,logic)",
            "class": "box_pkg::box",
            "parameters": {"W": "8", "T": "logic"},
        },
        {
            "text": "box_pkg::box#(16,int)",
            "class": "box_pkg::box",
            "parameters": {"W": "16", "T": "int"},
        },
    ],
)
t.r(
    "type",
    "top.sv",
    "box #(8) b8",
    "box #(8)",
    "box_pkg::box",
    specialization="box_pkg::box#(8,logic)",
)
t.r(
    "type",
    "top.sv",
    "box #(16, int) b16",
    "box #(16, int)",
    "box_pkg::box",
    specialization="box_pkg::box#(16,int)",
)
t.r(
    "call",
    "top.sv",
    "b8.width()",
    "width",
    "box_pkg::box::width",
    specialization_path="box_pkg::box#(8,logic)::width",
)
t.r(
    "call",
    "top.sv",
    "b16.width()",
    "width",
    "box_pkg::box::width",
    specialization_path="box_pkg::box#(16,int)::width",
)
t.add("counts", {"declarations_of_width": 1, "specializations": 2})

# =============================================================================== SEM-03 (demo)
t = g("demo_lanes", "demo", "", top="demo_top")
t.add(
    "variants",
    {
        "L2": {
            "LANES": 2,
            "DATA_W": 32,
            "lane_paths": ["demo_top.g_lane[0].u_lane", "demo_top.g_lane[1].u_lane"],
            "checker_paths": [
                "demo_top.g_lane[0].u_lane.u_stream_hold_sva",
                "demo_top.g_lane[1].u_lane.u_stream_hold_sva",
            ],
        },
        "L4": {
            "LANES": 4,
            "DATA_W": 32,
            "lane_paths": [f"demo_top.g_lane[{i}].u_lane" for i in range(4)],
            "checker_paths": [f"demo_top.g_lane[{i}].u_lane.u_stream_hold_sva" for i in range(4)],
        },
        "W64": {"LANES": 2, "DATA_W": 64},
    },
)
t.add(
    "data_i_actual",
    {
        "instance": "demo_top.g_lane[0].u_lane",
        "formal": "data_i",
        "actual_text": "data_i[lane]",
        "kind": "ElementSelect",
        "referenced": ["data_i", "lane"],
        **t.span("rtl/demo_top.sv", ".data_i  (data_i[lane])", "data_i[lane]"),
    },
)
t.add(
    "bind",
    {
        **t.span("sva/demo_bind.sv", "bind demo_lane stream_hold_sva", "bind demo_lane"),
        "target_text": "demo_lane",
    },
)
t.d("module", "demo_lane", "rtl/demo_lane.sv", "module demo_lane #(", "demo_lane")
t.d("module", "demo_top", "rtl/demo_top.sv", "module demo_top #(", "demo_top")
t.d(
    "property",
    "stream_hold_sva.p_stall_holds",
    "sva/stream_hold_sva.sv",
    "property p_stall_holds;",
    "p_stall_holds",
)
t.add(
    "assert",
    {
        "label": "ap_stall_holds",
        "kind": "assert",
        **t.span("sva/stream_hold_sva.sv", "ap_stall_holds: assert property (p_stall_holds);"),
    },
)
t.add("clock", "@(posedge clk)")
t.add("disable_iff", "!rst_n")

# =============================================================================== SEM-04
t = g("sem04_ifdef", "language", "sem04_ifdef", top="sem04_top")
t.add(
    "variants",
    {
        "default": {
            "defines": [],
            "active_definition": "branch_b",
            "inactive_modules": [
                {"name": "branch_a", **t.span("top.sv", "module branch_a", "module")}
            ],
            "disabled_directive": "IfDefDirective",
            "condition": "USE_A",
        },
        "USE_A": {
            "defines": ["USE_A"],
            "active_definition": "branch_a",
            "inactive_modules": [
                {"name": "branch_b", **t.span("top.sv", "module branch_b", "module")}
            ],
            "disabled_directive": "ElseDirective",
        },
    },
)
t.add(
    "never_instantiated",
    {
        "name": "sem04_never_instantiated",
        **t.span("top.sv", "module sem04_never_instantiated", "sem04_never_instantiated"),
    },
)
t.d("module", "sem04_top", "top.sv", "module sem04_top(", "sem04_top")
t.add("instance_path", "sem04_top.u_branch")

# =============================================================================== SEM-05
t = g("sem05_ports", "language", "sem05_ports", top="sem05_top")
P = ["a", "b", "y", "c", "d", "e"]
t.add("ports", P)
t.add("directions", {"a": "in", "b": "in", "y": "out", "c": "in", "d": "in", "e": "in"})
t.add(
    "bindings",
    {
        "sem05_top.u_named": {
            "style": {p: "named" for p in P},
            "actual": {"a": "a", "b": "b", "y": "y1", "c": "c", "d": "d", "e": "e"},
        },
        "sem05_top.u_pos": {
            "style": {p: "positional" for p in P},
            "actual": {"a": "a", "b": "b", "y": "y2", "c": "c", "d": "d", "e": "e"},
        },
        "sem05_top.u_star": {
            "style": {
                "a": "wildcard",
                "b": "wildcard",
                "y": "named",
                "c": "wildcard",
                "d": "named",
                "e": "wildcard",
            },
            "actual": {"a": "a", "b": "b", "y": "y3", "c": "c", "d": "d", "e": "e"},
        },
        "sem05_top.u_const": {
            "style": {p: "named" for p in P},
            "actual": {"a": "1'b1", "b": "2'b01", "y": "y4", "c": "c", "d": "4'hF", "e": "e"},
            "constant": ["a", "b", "d"],
        },
        "sem05_top.u_concat": {
            "style": {p: "named" for p in P},
            "actual": {"a": "a", "b": "b", "y": "y5", "c": "c", "d": "{hi, lo}", "e": "e"},
            "concat": {
                "port": "d",
                "parts": ["hi", "lo"],
                **t.span("top.sv", ".d({hi, lo})", "{hi, lo}"),
            },
        },
        "sem05_top.u_open": {
            "style": {
                "a": "named",
                "b": "named",
                "y": "open",
                "c": "named",
                "d": "named",
                "e": "open",
            },
            "actual": {"a": "a", "b": "b", "y": None, "c": "c", "d": "d", "e": None},
            "open": ["y", "e"],
        },
    },
)
t.add("star_span", t.span("top.sv", "sub u_star   (.*, .y(y3)", ".*"))

# =============================================================================== SEM-06
t = g("sem06_iface", "language", "sem06_iface", top="sem06_top")
t.d("interface", "bus_if", "top.sv", "interface bus_if;", "bus_if")
t.d("variable", "bus_if.data", "top.sv", "logic [7:0] data;", "data")
t.d("variable", "bus_if.valid", "top.sv", "logic       valid;", "valid")
t.d("modport", "bus_if.mp_src", "top.sv", "modport mp_src(", "mp_src")
t.d("modport", "bus_if.mp_dst", "top.sv", "modport mp_dst(", "mp_dst")
t.add(
    "instances",
    [
        "sem06_top",
        "sem06_top.u_bus_a",
        "sem06_top.u_bus_b",
        "sem06_top.u_src_a",
        "sem06_top.u_src_b",
        "sem06_top.u_dst_a",
        "sem06_top.u_dst_b",
    ],
)
t.add(
    "interface_bindings",
    {
        "sem06_top.u_src_a": {"b": ["sem06_top.u_bus_a", "mp_src"]},
        "sem06_top.u_src_b": {"b": ["sem06_top.u_bus_b", "mp_src"]},
        "sem06_top.u_dst_a": {"b": ["sem06_top.u_bus_a", "mp_dst"]},
        "sem06_top.u_dst_b": {"b": ["sem06_top.u_bus_b", "mp_dst"]},
    },
)
t.d("field", "sem06_top.vif_drv::vif", "top.sv", "virtual bus_if.mp_src vif;", "vif")
t.add("virtual_interface", {"field": "sem06_top.vif_drv::vif", "bound_to_instance": None})
t.add(
    "same_name_signal",
    {"declaration": "bus_if.data", "interface_instances": 2, "declaration_count": 1},
)

# =============================================================================== SEM-07
t = g("sem07_errors", "language", "sem07_errors", top="err_top")
t.add(
    "diagnostics",
    [
        {
            "code": "NoImplicitConversion",
            "severity": "error",
            "file": "good.sv",
            **t.span("good.sv", "initial n = s;", "n = s"),
        },
        {
            "code": "UndeclaredIdentifier",
            "severity": "error",
            "file": "good.sv",
            **t.span("good.sv", "initial x = missing_sym;", "missing_sym"),
        },
        {
            "code": "ExpectedToken",
            "severity": "error",
            "file": "bad_syntax.sv",
            **t.span("bad_syntax.sv", "logic [7:0 broken_decl;", "[7:0"),
        },
    ],
)
t.add(
    "expect",
    {
        "job": "SUCCEEDED",
        "completeness": "PARTIAL",
        "diagnostic_outcome": "HAS_ERRORS",
        "published": False,
        "errors": 3,
    },
)
t.add(
    "unresolved",
    [
        {
            "name": "missing_sym",
            "reason": "UndeclaredIdentifier",
            **t.span("good.sv", "initial x = missing_sym;", "missing_sym"),
        }
    ],
)

# =============================================================================== SEM-08
t = g("sem08_virtual", "language", "sem08_virtual", top="sem08_top")
t.d("class", "vpkg::animal", "top.sv", "virtual class animal;", "animal")
t.d("class", "vpkg::dog", "top.sv", "class dog extends animal;", "dog")
t.d("class", "vpkg::cat", "top.sv", "class cat extends animal;", "cat")
t.d(
    "function",
    "vpkg::animal::speak",
    "top.sv",
    "pure virtual function string speak();",
    "speak",
    virtual=True,
)
t.d(
    "function",
    "vpkg::dog::speak",
    "top.sv",
    'virtual function string speak();\n      return "woof";',
    "speak",
    virtual=True,
)
t.d(
    "function",
    "vpkg::cat::speak",
    "top.sv",
    'virtual function string speak();\n      return "meow";',
    "speak",
    virtual=True,
)
t.r(
    "call",
    "top.sv",
    "a.speak());\n    a = c;",
    "speak",
    "vpkg::animal::speak",
    "dynamic_unresolved",
)
t.r(
    "call",
    "top.sv",
    'a.speak());\n    $display("%s", d.intro',
    "speak",
    "vpkg::animal::speak",
    "dynamic_unresolved",
)
t.r("call", "top.sv", "return speak();", "speak", "vpkg::animal::speak", "dynamic_unresolved")
t.r("call", "top.sv", "d.intro()", "intro", "vpkg::animal::intro")
t.add("override_candidates", ["vpkg::dog::speak", "vpkg::cat::speak"])
t.r("extends", "top.sv", "class dog extends animal;", "animal", "vpkg::animal")
t.r("extends", "top.sv", "class cat extends animal;", "animal", "vpkg::animal")

# =============================================================================== SEM-09
t = g("sem09_escaped", "language", "sem09_escaped", top="sem09_top")
t.add(
    "instances",
    [
        {"components": [["sem09_top", None], ["x.y", None]], "definition": "leaf"},
        {"components": [["sem09_top", None], ["x", None]], "definition": "wrap"},
        {"components": [["sem09_top", None], ["x", None], ["y", None]], "definition": "leaf"},
        {
            "components": [["sem09_top", None], ["g", [0]], ["h", [0]], ["u", None]],
            "definition": "leaf",
        },
        {
            "components": [["sem09_top", None], ["g", [0]], ["h", [1]], ["u", None]],
            "definition": "leaf",
        },
        {
            "components": [["sem09_top", None], ["g", [1]], ["h", [0]], ["u", None]],
            "definition": "leaf",
        },
        {
            "components": [["sem09_top", None], ["g", [1]], ["h", [1]], ["u", None]],
            "definition": "leaf",
        },
    ],
)
t.add(
    "naive_split_collision",
    {
        "a": ["sem09_top", "x.y"],
        "b": ["sem09_top", "x", "y"],
        "same_joined_string": "sem09_top.x.y",
    },
)
t.add("instance_count", 8)  # + the top itself
t.add("escaped_instance_span", t.span("top.sv", "leaf \\x.y (", "\\x.y"))

# =============================================================================== SEM-10
t = g("sem10_unused", "language", "sem10_unused", top="sem10_top", library_dirs=["lib"])
t.d("function", "pkg10::helper", "pkg10.sv", "function automatic int helper(int x);", "helper")
t.d("class", "pkg10::used_cls", "pkg10.sv", "class used_cls;", "used_cls")
t.d("class", "pkg10::gen_unused", "pkg10.sv", "class gen_unused #(", "gen_unused")
t.r("call", "pkg10.sv", "return helper(1);", "helper", "pkg10::helper")
t.add(
    "not_covered",
    {
        "uncovered_reference_sites": [
            "helper(2) in the never-specialized generic class gen_unused",
            "helper(3) in library module lib_unused (never instantiated)",
        ],
        "helper_2": t.span("pkg10.sv", "return helper(2);", "helper"),
        "helper_3": t.span("lib/lib_unused.sv", "q = helper(3);", "helper"),
        "gap_kinds": ["generic_class_not_specialized", "library_member_not_loaded"],
        "reference_count_for_helper": 1,
    },
)

# =============================================================================== LOC-01
t = g(
    "loc01_utf8",
    "language",
    "loc01_utf8",
    top="loc01_top",
    encoding_notes="CRLF line endings, UTF-8 Chinese/Greek/emoji text, tabs",
)
t.d("parameter", "loc01_top.MSG", "top.sv", "localparam string MSG", "MSG")
t.d("variable", "loc01_top.data_bus", "top.sv", "\t\tdata_bus,", "data_bus")
t.d("variable", "loc01_top.data_aux", "top.sv", "\t\tdata_aux;", "data_aux")
t.d("variable", "loc01_top.after_tab", "top.sv", "\tafter_tab;", "after_tab")
t.add(
    "positions",
    {
        "data_bus": {"line": 6, "col_utf8": 3, "lsp_line0": 5, "lsp_utf16": 2, "lsp_utf8": 2},
        "after_tab": {"line": 8, "col_utf8": 27, "lsp_line0": 7, "lsp_utf16": 18, "lsp_utf8": 26},
        "MSG": {"line": 4, "col_utf8": 20, "lsp_line0": 3, "lsp_utf16": 19, "lsp_utf8": 19},
    },
)
t.add(
    "multi_line_declaration",
    t.span(
        "top.sv",
        "logic [7:0]\r\n\t\tdata_bus,\r\n\t\tdata_aux;",
        "logic [7:0]\r\n\t\tdata_bus,\r\n\t\tdata_aux;",
    ),
)
t.add("line_count", 9)
t.add("crlf_pairs", 9)

# =============================================================================== LOC-02
t = g("loc02_macro", "language", "loc02_macro", top="loc02_top")
t.d("variable", "loc02_a.arg_reg", "top.sv", "`MAKE_REG(arg_reg)", "arg_reg")
t.d("variable", "loc02_a.fixed_reg", "defs.svh", "logic [3:0] fixed_reg;", "fixed_reg")
t.d("variable", "loc02_b.fixed_reg", "defs.svh", "logic [3:0] fixed_reg;", "fixed_reg")
t.d("parameter", "loc02_a.FROM_HEADER", "dup.svh", "localparam int FROM_HEADER = 5;", "FROM_HEADER")
t.d("parameter", "loc02_b.FROM_HEADER", "dup.svh", "localparam int FROM_HEADER = 5;", "FROM_HEADER")
t.d("variable", "loc02_b.after_line_reg", "top.sv", "logic after_line_reg;", "after_line_reg")
t.add(
    "macro_origins",
    {
        "loc02_a.arg_reg": {
            "macro": "MAKE_REG",
            "spelled_in": "macro_argument",
            "expansion": t.span("top.sv", "`MAKE_REG(arg_reg)", "`MAKE_REG"),
        },
        "loc02_a.fixed_reg": {
            "macro": "DECL_FIXED",
            "spelled_in": "macro_body",
            "expansion": t.span(
                "top.sv", "`DECL_FIXED\nendmodule\n\nmodule loc02_b", "`DECL_FIXED"
            ),
        },
        "loc02_b.fixed_reg": {
            "macro": "DECL_FIXED",
            "spelled_in": "macro_body",
            "expansion": t.span("top.sv", "`DECL_FIXED\n`line 100", "`DECL_FIXED"),
        },
    },
)
t.add(
    "line_directive",
    {
        "display_file": "virtual_file.sv",
        "display_line": 100,
        "physical_line": 12,
        "decl": "loc02_b.after_line_reg",
    },
)
t.add(
    "repeated_include",
    {"header": "dup.svh", "scopes": ["loc02_a", "loc02_b"], "distinct_declarations": 2},
)

# =============================================================================== SVA-02 / stub / BLD
t = g("sva02_unresolved", "language", "sva02_unresolved", top="sva02_top")
t.add(
    "diagnostics",
    [
        {
            "code": "UndeclaredIdentifier",
            **t.span("chk.sv", "@(posedge clk) a |-> missing_sig;", "missing_sig"),
        },
        {"code": "UnknownModule", **t.span("chk.sv", "bind no_such_module chk", "no_such_module")},
    ],
)
t.add(
    "assertion",
    {
        "label": "ap_unknown",
        "resolved": False,
        "errors": ["UndeclaredIdentifier"],
        **t.span("chk.sv", "ap_unknown: assert property (p_unknown);"),
    },
)
t.add(
    "bind",
    {
        "target_text": "no_such_module",
        "elaborated": 0,
        "resolved": False,
        "errors": ["UnknownModule"],
        **t.span("chk.sv", "bind no_such_module chk u_bad (.clk(clk), .a(a));"),
    },
)
t.add("expect", {"completeness": "PARTIAL"})

t = g("stub_boundary", "language", "stub_boundary", top="stub_top")
t.add(
    "stub",
    {
        "definition": "vendor_ip",
        "stub_name": "vendor_ip_v1",
        "instance": "stub_top.u_ip",
        "completeness": "PARTIAL",
        "gap": "stub_boundary",
    },
)

t = g("bld06_cu", "language", "bld06_cu", top="mod_b")
t.add(
    "expect",
    {
        "single": {"errors": 0, "completeness": "COMPLETE"},
        "per_file": {
            "errors": 1,
            "code": "UnknownDirective",
            "completeness": "PARTIAL",
            **t.span("macro_use.sv", "logic [`SHARED_W-1:0] x;", "`SHARED_W"),
        },
    },
)

t = g("bld07_incdir", "language", "bld07_incdir", top="inc_top")
t.add("expect", {"dir_a_first": {"P": "11"}, "dir_b_first": {"P": "22"}})
t.add(
    "headers",
    {
        "dir_a": t.span("dir_a/common.svh", "`define VAL 11", "VAL"),
        "dir_b": t.span("dir_b/common.svh", "`define VAL 22", "VAL"),
    },
)

t = g("bld10_groups", "language", "bld10_groups", top=["top1", "top2"])
t.add(
    "expect",
    {
        "groups": {"g1": {"W": "8", "top": "top1"}, "g2": {"W": "16", "top": "top2"}},
        "declarations": [
            {"qualified_name": "top1.WIDTH", "value": "8", "cu_differs": True},
            {"qualified_name": "top2.WIDTH", "value": "16", "cu_differs": True},
        ],
        "shared_header": t.span("inc/shared.svh", "localparam int WIDTH = `W;", "WIDTH"),
    },
)

# =============================================================================== UVM
t = g("uvm_base", "uvm", "", top="svw_uvm_top")
t.d(
    "class",
    "svw_demo_uvm_pkg::svw_driver",
    "svw_demo_uvm_pkg.sv",
    "class svw_driver extends",
    "svw_driver",
)
t.d(
    "class",
    "svw_demo_uvm_pkg::svw_monitor",
    "svw_demo_uvm_pkg.sv",
    "class svw_monitor extends",
    "svw_monitor",
)
t.d("class", "svw_demo_uvm_pkg::svw_env", "svw_demo_uvm_pkg.sv", "class svw_env extends", "svw_env")
t.d(
    "class",
    "svw_demo_uvm_pkg::svw_smoke_test",
    "svw_demo_uvm_pkg.sv",
    "class svw_smoke_test extends",
    "svw_smoke_test",
)
t.d(
    "task",
    "svw_demo_uvm_pkg::svw_driver::run_phase",
    "svw_demo_uvm_pkg.sv",
    'virtual task run_phase(uvm_phase phase);\n      `uvm_info("SVW_DRV"',
    "run_phase",
    virtual=True,
)
t.d(
    "task",
    "svw_demo_uvm_pkg::svw_monitor::run_phase",
    "svw_demo_uvm_pkg.sv",
    'virtual task run_phase(uvm_phase phase);\n      `uvm_info("SVW_MON"',
    "run_phase",
    virtual=True,
)
t.d(
    "function",
    "svw_demo_uvm_pkg::svw_env::build_phase",
    "svw_demo_uvm_pkg.sv",
    "virtual function void build_phase(uvm_phase phase);\n      super.build_phase(phase);\n      drv",
    "build_phase",
)
t.add(
    "bases",
    {
        "svw_demo_uvm_pkg::svw_driver": "uvm_pkg::uvm_driver",
        "svw_demo_uvm_pkg::svw_monitor": "uvm_pkg::uvm_monitor",
        "svw_demo_uvm_pkg::svw_env": "uvm_pkg::uvm_env",
        "svw_demo_uvm_pkg::svw_smoke_test": "uvm_pkg::uvm_test",
    },
)
t.add("run_test_call", t.span("svw_uvm_top.sv", 'run_test("svw_smoke_test")', "run_test"))
t.add("uvm_log_edit_site", t.span("svw_demo_uvm_pkg.sv", '"driver fixture"', "driver fixture"))

t = g("uvm_factory", "uvm", "", top="svw_uvm_ext_top")
t.d(
    "class",
    "svw_uvm_override_pkg::svw_test_ov_a",
    "svw_uvm_override_pkg.sv",
    "class svw_test_ov_a extends",
    "svw_test_ov_a",
)
t.d(
    "class",
    "svw_uvm_override_pkg::svw_test_ov_b",
    "svw_uvm_override_pkg.sv",
    "class svw_test_ov_b extends",
    "svw_test_ov_b",
)
t.add(
    "override_calls",
    [
        {
            "callee": "set_type_override_by_type",
            "in_class": "svw_uvm_override_pkg::svw_test_ov_a",
            "args": ["svw_base_item::get_type()", "svw_item_a::get_type()"],
            **t.span(
                "svw_uvm_override_pkg.sv",
                "set_type_override_by_type(svw_base_item",
                "set_type_override_by_type",
            ),
        },
        {
            "callee": "set_inst_override_by_type",
            "in_class": "svw_uvm_override_pkg::svw_test_ov_b",
            "args": ['"env.*"', "svw_base_item::get_type()", "svw_item_b::get_type()"],
            **t.span(
                "svw_uvm_override_pkg.sv",
                'set_inst_override_by_type("env.*"',
                "set_inst_override_by_type",
            ),
        },
    ],
)
t.add("not_claimed", "the effective runtime topology / which override wins")

t = g("uvm_config_db", "uvm", "", top="svw_uvm_ext_top")
t.add(
    "calls",
    [
        {
            "op": "set",
            "type": "int unsigned",
            "inst": '"env*"',
            "field": '"width"',
            "value": "32",
            "wildcard": True,
            **t.span(
                "svw_uvm_cfgdb_pkg.sv", 'uvm_config_db#(int unsigned)::set(this, "env*"', "set"
            ),
        },
        {
            "op": "set",
            "type": "int unsigned",
            "inst": '"env"',
            "field": '"width"',
            "value": "16",
            "wildcard": False,
            **t.span(
                "svw_uvm_cfgdb_pkg.sv",
                'uvm_config_db#(int unsigned)::set(this, "env", "width", 16)',
                "set",
            ),
        },
        {
            "op": "set",
            "type": "int",
            "inst": '"env"',
            "field": '"width"',
            "value": "7",
            "wildcard": False,
            **t.span("svw_uvm_cfgdb_pkg.sv", "uvm_config_db#(int)::set(this", "set"),
        },
        {
            "op": "set",
            "type": "string",
            "inst": '"*"',
            "field": '"tag"',
            "value": '"alpha"',
            "wildcard": True,
            **t.span("svw_uvm_cfgdb_pkg.sv", "uvm_config_db#(string)::set(this", "set"),
        },
        {
            "op": "get",
            "type": "int unsigned",
            "inst": '""',
            "field": '"width"',
            **t.span("svw_uvm_cfgdb_pkg.sv", "uvm_config_db#(int unsigned)::get(this", "get"),
        },
        {
            "op": "get",
            "type": "string",
            "inst": '""',
            "field": '"tag"',
            **t.span("svw_uvm_cfgdb_pkg.sv", "uvm_config_db#(string)::get(this", "get"),
        },
    ],
)
t.add("not_claimed", "that any get matches any set (precedence/type/wildcard matching is runtime)")

# =============================================================================== class members
t = g("sem_classmembers", "language", "sem_classmembers", top="cm_top")
t.d("class", "cm_pkg::base_item", "top.sv", "class base_item;", "base_item")
t.d("class", "cm_pkg::ext_item", "top.sv", "class ext_item extends", "ext_item")
t.d("field", "cm_pkg::base_item::created", "top.sv", "static int created;", "created")
t.d("field", "cm_pkg::base_item::addr", "top.sv", "rand bit [7:0] addr;", "addr", rand_mode="rand")
t.d("constraint", "cm_pkg::base_item::c_addr", "top.sv", "constraint c_addr", "c_addr")
t.d("constraint", "cm_pkg::ext_item::c_data", "top.sv", "constraint c_data", "c_data")
t.d("covergroup", "cm_pkg::base_item::cg_item", "top.sv", "covergroup cg_item", "cg_item", nth=0)
t.d("coverpoint", "cm_pkg::base_item.cp_addr", "top.sv", "cp_addr: coverpoint addr;", "cp_addr")
t.d("function", "cm_pkg::base_item::new", "top.sv", "function new();", "new", constructor=True)
t.d(
    "function",
    "cm_pkg::base_item::count",
    "top.sv",
    "static function int count();",
    "count",
    static=True,
)
t.d(
    "function",
    "cm_pkg::c_add",
    "top.sv",
    "function int c_add(int a, int b);",
    "c_add",
    dpi_import=True,
    dpi_spec="DPI-C",
)
t.add("bases", {"cm_pkg::ext_item": "cm_pkg::base_item"})
t.r("extends", "top.sv", "class ext_item extends base_item;", "base_item", "cm_pkg::base_item")
t.r("call", "top.sv", "c_add(addr, data)", "c_add", "cm_pkg::c_add")
t.r("call", "top.sv", "base_item::count()", "count", "cm_pkg::base_item::count")
t.r("call", "top.sv", "it.total()", "total", "cm_pkg::ext_item::total")


# ============================================================================ closed scopes (C4)
BY = {x.name: x for x in GOLDENS}
ROOT_INSTANCE = ("the hierarchy root is modelled as an `instance` row spanning the same bytes as its "
                 "module declaration; the module row is the golden entry")

x = BY["sem01_scopes"]
for v, pk in (("v", "pkg_a"), ("v", "pkg_b")):
    x.r("value", f"{pk}.sv", "count += v" if pk == "pkg_a" else "total -= v", v,
        f"{pk}::agent::write.v")
x.r("new", "top.sv", "a = new();", "new()", "pkg_a::agent")
x.r("new", "top.sv", "b = new();", "new()", "pkg_b::agent")
x.r("value", "top.sv", "a = new();", "a", "sem01_top.a")
x.r("value", "top.sv", "b = new();", "b", "sem01_top.b")
x.r("value", "top.sv", "a.write(1)", "a", "sem01_top.a")
x.r("value", "top.sv", "b.write(2)", "b", "sem01_top.b")
x.r("value", "top.sv", "a.run_phase()", "a", "sem01_top.a")
x.r("value", "top.sv", "b.run_phase()", "b", "sem01_top.b")
x.closed(["pkg_a.sv", "pkg_b.sv", "top.sv"], ["pkg_a.sv", "pkg_b.sv", "top.sv"])
x.xd("instance", "sem01_top", "top.sv", "module sem01_top;", "sem01_top", ROOT_INSTANCE)

x = BY["sem02_paramclass"]
x.r("type", "box_pkg.sv", "T item;", "T", "box_pkg::box::T")
x.r("value", "box_pkg.sv", "return W;", "W", "box_pkg::box::W")
x.r("import", "top.sv", "import box_pkg::*;", "box_pkg::*", "box_pkg")
x.r("new", "top.sv", "b8 = new()", "new()", "box_pkg::box")
x.r("new", "top.sv", "b16 = new()", "new()", "box_pkg::box")
x.r("system_call", "top.sv", '$display("%0d %0d"', "$display", None)
x.r("value", "top.sv", "b8.width()", "b8", "sem02_top.b8")
x.r("value", "top.sv", "b16.width()", "b16", "sem02_top.b16")
x.d("module", "sem02_top", "top.sv", "module sem02_top;", "sem02_top")
x.closed(["box_pkg.sv", "top.sv"], ["box_pkg.sv", "top.sv"])
x.xd("instance", "sem02_top", "top.sv", "module sem02_top;", "sem02_top", ROOT_INSTANCE)

x = BY["sem08_virtual"]
D1 = 'a = d;\n    $display("%s", a.speak()'
D2 = 'a = c;\n    $display("%s", a.speak()'
x.r("import", "top.sv", "import vpkg::*;", "vpkg::*", "vpkg")
x.r("type", "top.sv", "animal a;", "animal", "vpkg::animal")
x.r("type", "top.sv", "dog d = new();", "dog", "vpkg::dog")
x.r("new", "top.sv", "dog d = new();", "new()", "vpkg::dog")
x.r("type", "top.sv", "cat c = new();", "cat", "vpkg::cat")
x.r("new", "top.sv", "cat c = new();", "new()", "vpkg::cat")
x.r("value", "top.sv", "a = d;", "a", "sem08_top.a", at=0)
x.r("value", "top.sv", "a = d;", "d", "sem08_top.d", at=4)
x.r("system_call", "top.sv", D1, "$display", None, at=D1.index("$display"))
x.r("value", "top.sv", D1, "a", "sem08_top.a", at=D1.index("a.speak"))
x.r("value", "top.sv", "a = c;", "a", "sem08_top.a", at=0)
x.r("value", "top.sv", "a = c;", "c", "sem08_top.c", at=4)
x.r("system_call", "top.sv", D2, "$display", None, at=D2.index("$display"))
x.r("value", "top.sv", D2, "a", "sem08_top.a", at=D2.index("a.speak"))
x.r("system_call", "top.sv", '$display("%s", d.intro', "$display", None, at=0)
x.r("value", "top.sv", "d.intro()", "d", "sem08_top.d", at=0)
x.d("package", "vpkg", "top.sv", "package vpkg;", "vpkg")
x.d("function", "vpkg::animal::intro", "top.sv", "function string intro();", "intro", virtual=False)
x.d("module", "sem08_top", "top.sv", "module sem08_top;", "sem08_top")
x.d("variable", "sem08_top.a", "top.sv", "animal a;", "a", at=7)
x.d("variable", "sem08_top.d", "top.sv", "dog d = new();", "d", at=4)
x.d("variable", "sem08_top.c", "top.sv", "cat c = new();", "c", at=4)
x.closed(["top.sv"], ["top.sv"])
x.xd("instance", "sem08_top", "top.sv", "module sem08_top;", "sem08_top", ROOT_INSTANCE)

x = BY["sem10_unused"]
x.r("value", "pkg10.sv", "return x + 1;", "x", "pkg10::helper.x", at=7)
x.r("import", "top.sv", "import pkg10::*;", "pkg10::*", "pkg10")
x.r("type", "top.sv", "used_cls u = new();", "used_cls", "pkg10::used_cls")
x.r("new", "top.sv", "u = new();", "new()", "pkg10::used_cls")
x.r("value", "top.sv", "r = u.f();", "r", "sem10_top.r", at=0)
x.r("value", "top.sv", "r = u.f();", "u", "sem10_top.u", at=4)
x.r("call", "top.sv", "u.f()", "f", "pkg10::used_cls::f")
x.d("package", "pkg10", "pkg10.sv", "package pkg10;", "pkg10")
x.d("argument", "pkg10::helper.x", "pkg10.sv", "helper(int x);", "x", at=11)
x.d("function", "pkg10::used_cls::f", "pkg10.sv", "function int f();", "f", at=13)
x.d("module", "sem10_top", "top.sv", "module sem10_top;", "sem10_top")
x.d("variable", "sem10_top.u", "top.sv", "used_cls u = new();", "u", at=9)
x.d("variable", "sem10_top.r", "top.sv", "int r;", "r", at=4)
x.closed(["pkg10.sv", "top.sv"], ["pkg10.sv", "top.sv"])
x.xd("instance", "sem10_top", "top.sv", "module sem10_top;", "sem10_top", ROOT_INSTANCE)

x = BY["sem_classmembers"]
x.r("value", "top.sv", "constraint c_addr { addr inside", "addr", "cm_pkg::base_item::addr",
    at=len("constraint c_addr { "))
x.r("value", "top.sv", "@(posedge clk)", "clk", "cm_pkg::base_item::clk", at=10)
x.r("value", "top.sv", "coverpoint addr", "addr", "cm_pkg::base_item::addr", at=11)
x.r("value", "top.sv", "cg_item = new();", "cg_item", "cm_pkg::base_item::cg_item", at=0)
x.r("value", "top.sv", "created++", "created", "cm_pkg::base_item::created", at=0)
x.r("value", "top.sv", "return created;", "created", "cm_pkg::base_item::created", at=7)
x.r("value", "top.sv", "data > 8'h80", "data", "cm_pkg::base_item::data", at=0)
x.r("value", "top.sv", "c_add(addr, data)", "addr", "cm_pkg::base_item::addr", at=6)
x.r("value", "top.sv", "c_add(addr, data)", "data", "cm_pkg::base_item::data", at=12)
x.r("import", "top.sv", "import cm_pkg::*;", "cm_pkg::*", "cm_pkg")
x.r("type", "top.sv", "ext_item it;", "ext_item", "cm_pkg::ext_item")
x.r("value", "top.sv", "it = new();", "it", "cm_top.it", at=0)
x.r("new", "top.sv", "it = new();", "new()", "cm_pkg::ext_item")
x.r("value", "top.sv", "n = base_item::count()", "n", "cm_top.n", at=0)
x.r("value", "top.sv", "it.total()", "it", "cm_top.it", at=0)
x.d("package", "cm_pkg", "top.sv", "package cm_pkg;", "cm_pkg")
x.d("argument", "cm_pkg::c_add.a", "top.sv", "c_add(int a, int b)", "a", at=10)
x.d("argument", "cm_pkg::c_add.b", "top.sv", "c_add(int a, int b)", "b", at=17)
x.d("field", "cm_pkg::base_item::data", "top.sv", "rand bit [7:0] data;", "data")
x.d("field", "cm_pkg::base_item::clk", "top.sv", "bit clk;", "clk")
x.d("field", "cm_pkg::base_item::cg_item", "top.sv", "covergroup cg_item @", "cg_item")
x.d("function", "cm_pkg::ext_item::total", "top.sv", "function int total();", "total")
x.d("module", "cm_top", "top.sv", "module cm_top;", "cm_top")
x.d("variable", "cm_top.it", "top.sv", "ext_item it;", "it", at=9)
x.d("variable", "cm_top.n", "top.sv", "int n;", "n", at=4)
x.closed(["top.sv"], ["top.sv"])
x.xd("instance", "cm_top", "top.sv", "module cm_top;", "cm_top", ROOT_INSTANCE)


def render(t: T) -> str:
    return json.dumps(t.doc, indent=2, ensure_ascii=False, sort_keys=False) + "\n"


def main() -> int:
    check = "--check" in sys.argv
    bad = 0
    for t in GOLDENS:
        out = HERE / f"{t.name}.json"
        text = render(t)
        if check:
            if not out.exists() or out.read_text(encoding="utf-8") != text:
                print("DIFF", out.name)
                bad += 1
        else:
            out.write_text(text, encoding="utf-8")
            print("wrote", out.name)
    return 1 if bad else 0


if __name__ == "__main__":
    raise SystemExit(main())
