#!/usr/bin/env python3
"""Deterministic synthetic SystemVerilog corpus generator (plan 22.3).

Default scale 1.0 = about 500 source files / about 100,000 lines: packages (with parameters,
typedefs, enums, structs, functions and class hierarchies), interfaces (with modports) and modules
(parameters, generate-for / generate-if, always_ff, functions, interface instances, class use) that
form ONE instance tree under ``bench_top`` (every module is instantiated exactly once, so the
elaborated design stays linear in the file count).

The same ``--seed`` and ``--scale`` always give byte-identical output. The corpus is generated
data: it is written under ``.svweave/bench/corpus`` by default (git-ignored) and is never
committed. It contains no third-party or company code.

Output layout::

    <out>/svweave.yaml            profile "bench" (top bench_top, ordered filelist)
    <out>/corpus.f                one path per line, packages -> interfaces -> modules -> top
    <out>/rtl/pkg/*.sv  rtl/ifc/*.sv  rtl/mod/*.sv  rtl/top/bench_top.sv
    <out>/corpus_stats.json       files, lines, bytes, content hash

Usage::

    .venv/bin/python scripts/gen_corpus.py [--out DIR] [--scale 1.0] [--seed 1] [--force]
"""

from __future__ import annotations

import argparse
import hashlib
import json
import random
import shutil
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
DEFAULT_OUT = REPO / ".svweave" / "bench" / "corpus"
BASE_FILES = 500
LINES_PER_FILE = 200
FANOUT = 3


def _pkg(i: int, rng: random.Random, target: int) -> str:
    n = f"pkg_{i:03d}"
    out = [
        f"// synthetic package {n}",
        f"package {n};",
        f"  localparam int unsigned {n.upper()}_W = {8 + 8 * (i % 4)};",
        f"  localparam int unsigned {n.upper()}_DEPTH = {2 + i % 5};",
        f"  typedef logic [{n.upper()}_W-1:0] word_t;",
        "  typedef enum logic [1:0] {IDLE = 2'd0, RUN = 2'd1, DONE = 2'd2, FAIL = 2'd3} state_t;",
        "  typedef struct packed {",
        "    logic       valid;",
        "    word_t      payload;",
        "    state_t     state;",
        "  } beat_t;",
        "",
        "  virtual class base_agent;",
        "    protected int unsigned count;",
        "    function new();",
        "      count = 0;",
        "    endfunction",
        "    pure virtual function word_t step(word_t x);",
        "    virtual function int unsigned get_count();",
        "      return count;",
        "    endfunction",
        "  endclass",
        "",
        "  class agent_a extends base_agent;",
        "    function new();",
        "      super.new();",
        "    endfunction",
        "    virtual function word_t step(word_t x);",
        "      count++;",
        f"      return x ^ word_t'({rng.randrange(1, 255)});",
        "    endfunction",
        "  endclass",
        "",
        "  class agent_b extends agent_a;",
        "    function new();",
        "      super.new();",
        "    endfunction",
        "    virtual function word_t step(word_t x);",
        "      count += 2;",
        "      return super.step(x) + word_t'(1);",
        "    endfunction",
        "  endclass",
        "",
    ]
    k = 0
    while len(out) < target - 1:
        out += [
            f"  function automatic word_t f_{k}(word_t a, word_t b);",
            f"    word_t t = (a << {k % 5}) ^ (b >> {k % 3});",
            f"    return t + word_t'({rng.randrange(1, 99)});",
            "  endfunction",
            "",
        ]
        k += 1
    out.append(f"endpackage : {n}")
    return "\n".join(out) + "\n"


def _ifc(i: int, target: int) -> str:
    n = f"ifc_{i:03d}"
    out = [
        f"// synthetic interface {n}",
        f"interface {n} #(parameter int unsigned W = 8) (input logic clk);",
        "  logic [W-1:0] data;",
        "  logic         valid;",
        "  logic         ready;",
        "  modport producer (output data, output valid, input ready, input clk);",
        "  modport consumer (input data, input valid, output ready, input clk);",
        "",
    ]
    k = 0
    while len(out) < target - 1:
        out += [
            f"  function automatic logic [W-1:0] mix_{k}(logic [W-1:0] a);",
            f"    return a ^ W'({k + 1});",
            "  endfunction",
            "",
        ]
        k += 1
    out.append("endinterface")
    return "\n".join(out) + "\n"


def _mod(i: int, children: list[int], npkg: int, nifc: int, rng: random.Random, target: int) -> str:
    n = f"mod_{i:04d}"
    pkg = f"pkg_{i % npkg:03d}"
    use_ifc = nifc > 0 and i % 3 == 0
    use_cls = i % 5 == 0
    lanes = 2 + i % 3
    out = [
        f"// synthetic module {n}",
        f"module {n} #(",
        "  parameter int unsigned W = 8,",
        "  parameter int unsigned N = 3",
        ") (",
        "  input  logic         clk,",
        "  input  logic         rst_n,",
        "  input  logic [W-1:0] din,",
        "  output logic [W-1:0] dout",
        ");",
        f"  import {pkg}::*;",
        "  typedef logic [W-1:0] vec_t;",
        "  vec_t stage [N];",
        "  vec_t acc;",
        "",
        "  // pipeline built from a generate-for",
        "  for (genvar s = 0; s < N; s++) begin : g_stage",
        "    if (s == 0) begin : g_first",
        "      always_ff @(posedge clk or negedge rst_n)",
        "        if (!rst_n) stage[s] <= '0;",
        f"        else        stage[s] <= din ^ vec_t'({rng.randrange(1, 200)});",
        "    end else begin : g_rest",
        "      always_ff @(posedge clk or negedge rst_n)",
        "        if (!rst_n) stage[s] <= '0;",
        "        else        stage[s] <= stage[s-1] + vec_t'(s);",
        "    end",
        "  end",
        "",
    ]
    if use_ifc:
        j = i % nifc
        out += [
            f"  ifc_{j:03d} #(.W(W)) u_ifc (.clk(clk));",
            "  assign u_ifc.data  = stage[N-1];",
            "  assign u_ifc.valid = 1'b1;",
            "  assign u_ifc.ready = 1'b1;",
            "",
        ]
    if use_cls:
        out += [
            f"  {pkg}::base_agent agent;",
            "  initial begin",
            f"    agent = ({i} % 2 == 0) ? {pkg}::agent_a'(new()) : {pkg}::agent_b'(new());",
            "  end",
            "",
        ]
    # child instances: one tree, each module instantiated exactly once
    outs: list[str] = []
    for c in children:
        v = f"c{c}_dout"
        outs.append(v)
        out += [
            f"  logic [W-1:0] {v};",
            f"  mod_{c:04d} #(.W(W), .N({2 + c % 3})) u_c{c} (",
            "    .clk   (clk),",
            "    .rst_n (rst_n),",
            "    .din   (stage[N-1]),",
            f"    .dout  ({v})",
            "  );",
            "",
        ]
    red = " ^ ".join(["stage[N-1]"] + outs)
    out += [
        "  always_comb begin",
        f"    acc = {red};",
        "  end",
        "  assign dout = acc;",
        "",
    ]
    k = 0
    # filler: repeated function + generate-if blocks until the file reaches its target length
    while len(out) < target - 2:
        out += [
            f"  function automatic vec_t fn_{k}(vec_t a);",
            f"    return (a << {k % 4}) ^ vec_t'({rng.randrange(1, 99)});",
            "  endfunction",
            f"  if (W >= {8 + (k % 3) * 8}) begin : g_w{k}",
            f"    vec_t tmp_{k};",
            f"    assign tmp_{k} = fn_{k}(acc);",
            "  end",
            "",
        ]
        k += 1
    out.append("endmodule")
    del lanes
    return "\n".join(out) + "\n"


def _top(nmods: int, children: list[int]) -> str:
    out = [
        "// synthetic top",
        "module bench_top (",
        "  input  logic        clk,",
        "  input  logic        rst_n,",
        "  input  logic [7:0]  din,",
        "  output logic [7:0]  dout",
        ");",
    ]
    outs = []
    for c in children:
        out += [
            f"  logic [7:0] t{c}_dout;",
            f"  mod_{c:04d} #(.W(8), .N(3)) u_t{c} (.clk(clk), .rst_n(rst_n), .din(din), .dout(t{c}_dout));",
        ]
        outs.append(f"t{c}_dout")
    out += [f"  assign dout = {' ^ '.join(outs) if outs else 'din'};", "endmodule"]
    del nmods
    return "\n".join(out) + "\n"


def generate(out: Path, scale: float, seed: int, force: bool) -> dict[str, object]:
    if out.exists():
        if not force:
            raise SystemExit(f"{out} exists; pass --force to regenerate")
        shutil.rmtree(out)
    total = max(8, round(BASE_FILES * scale))
    npkg = max(2, round(total * 0.06))
    nifc = max(1, round(total * 0.08))
    nmod = max(2, total - npkg - nifc - 1)
    rng = random.Random(seed)
    files: list[str] = []
    for sub in ("pkg", "ifc", "mod", "top"):
        (out / "rtl" / sub).mkdir(parents=True)

    def emit(rel: str, text: str) -> None:
        (out / rel).write_text(text, encoding="utf-8")
        files.append(rel)

    for i in range(npkg):
        emit(f"rtl/pkg/pkg_{i:03d}.sv", _pkg(i, rng, LINES_PER_FILE))
    for i in range(nifc):
        emit(f"rtl/ifc/ifc_{i:03d}.sv", _ifc(i, LINES_PER_FILE))
    # module i has children FANOUT*i+1 .. FANOUT*i+FANOUT; top instantiates module 0 (the root)
    for i in range(nmod):
        kids = [c for c in range(FANOUT * i + 1, FANOUT * i + FANOUT + 1) if c < nmod]
        emit(f"rtl/mod/mod_{i:04d}.sv", _mod(i, kids, npkg, nifc, rng, LINES_PER_FILE))
    emit("rtl/top/bench_top.sv", _top(nmod, [0]))
    (out / "corpus.f").write_text("\n".join(files) + "\n", encoding="utf-8")

    yaml_text = f"""\
# Generated by scripts/gen_corpus.py (seed={seed}, scale={scale}); do not commit.
schema_version: "1.0"
project:
  id: "svweave-bench"
  root: "."
  default_profile: "bench"
source_roots:
  - id: "project"
    path: "."
    include: ["rtl/**", "corpus.f"]
    exclude: ["**/.svweave/**"]
    writable: false
profiles:
  bench:
    cwd: "."
    language_mode: "systemverilog"
    source_inputs:
      - kind: "filelist"
        path: "corpus.f"
        dialect: "svweave-filelist-v1"
        path_mode: "cwd"
    compilation_units:
      mode: "single"
    top_modules: ["bench_top"]
    defines: []
    include_dirs: []
    top_parameters: []
    library_dirs: []
    library_files: []
    library_extensions: [".v", ".sv"]
    environment_allowlist: []
    generated_inputs: []
    diagnostic_policy: "no_errors"
analysis:
  missing_input_policy: "block"
  unsupported_option_policy: "block"
  stub_policy: "explicit_only"
  max_build_seconds: 600
  max_worker_memory_mib: 8192
editing:
  enabled: false
"""
    (out / "svweave.yaml").write_text(yaml_text, encoding="utf-8")

    lines = 0
    size = 0
    h = hashlib.sha256()
    for rel in files:
        data = (out / rel).read_bytes()
        lines += data.count(b"\n")
        size += len(data)
        h.update(rel.encode() + b"\0" + hashlib.sha256(data).digest())
    stats: dict[str, object] = {
        "generator": "scripts/gen_corpus.py",
        "seed": seed,
        "scale": scale,
        "files": len(files),
        "packages": npkg,
        "interfaces": nifc,
        "modules": nmod,
        "lines": lines,
        "bytes": size,
        "content_sha256": h.hexdigest(),
        "top": "bench_top",
        "profile": "bench",
    }
    (out / "corpus_stats.json").write_text(json.dumps(stats, indent=2) + "\n", encoding="utf-8")
    return stats


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    ap.add_argument("--out", type=Path, default=DEFAULT_OUT)
    ap.add_argument("--scale", type=float, default=1.0, help="1.0 = ~500 files / ~100k lines")
    ap.add_argument("--seed", type=int, default=1)
    ap.add_argument("--force", action="store_true", help="replace an existing output directory")
    a = ap.parse_args(argv)
    if a.scale <= 0:
        raise SystemExit("--scale must be > 0")
    stats = generate(a.out.resolve(), a.scale, a.seed, a.force)
    print(json.dumps(stats, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
