#!/usr/bin/env python3
"""M0 raw LSP probe of the pinned verible-verilog-ls (no Serena in between).

Records: initialize capabilities, textDocument/documentSymbol for the UVM fixture and for a
minimal class/module containing both a task and a function. Used to root-cause what Serena's
SystemVerilog backend can and cannot address by name path.

Usage: .venv/bin/python scripts/spikes/verible_lsp_probe.py [--out PATH]
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
VLS = REPO / ".toolchain/verible/bin/verible-verilog-ls"

MINI = """package p;
  class c1;
    function void f1(); endfunction
    task t1(); endtask
    virtual task run_phase(int phase);
      $display("x");
    endtask
  endclass
endpackage
module m; task mt(); endtask function void mf(); endfunction endmodule
"""


class Lsp:
    def __init__(self, exe: Path, root: Path):
        self.p = subprocess.Popen([str(exe)], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                  stderr=subprocess.DEVNULL, cwd=str(root))
        self.i = 0

    def send(self, m: dict) -> None:
        b = json.dumps(m).encode()
        self.p.stdin.write(b"Content-Length: %d\r\n\r\n" % len(b) + b)
        self.p.stdin.flush()

    def recv(self) -> dict:
        h = {}
        while True:
            line = self.p.stdout.readline().decode().strip()
            if not line:
                break
            k, v = line.split(":", 1)
            h[k] = v.strip()
        return json.loads(self.p.stdout.read(int(h["Content-Length"])))

    def req(self, method: str, params) -> dict:
        self.i += 1
        self.send({"jsonrpc": "2.0", "id": self.i, "method": method, "params": params})
        while True:
            m = self.recv()
            if m.get("id") == self.i:
                return m

    def close(self) -> None:
        try:
            self.req("shutdown", None)
            self.send({"jsonrpc": "2.0", "method": "exit"})
        except Exception:
            pass
        try:
            self.p.wait(timeout=5)
        except subprocess.TimeoutExpired:
            self.p.kill()


def flatten(syms, depth=0, out=None):
    out = [] if out is None else out
    for s in syms or []:
        out.append({"depth": depth, "kind": s.get("kind"), "name": s.get("name"),
                    "range": s.get("range"), "selectionRange": s.get("selectionRange")})
        flatten(s.get("children"), depth + 1, out)
    return out


def doc_symbols(lsp: Lsp, path: Path) -> list:
    uri = "file://" + str(path)
    lsp.send({"jsonrpc": "2.0", "method": "textDocument/didOpen", "params": {"textDocument": {
        "uri": uri, "languageId": "systemverilog", "version": 1, "text": path.read_text()}}})
    r = lsp.req("textDocument/documentSymbol", {"textDocument": {"uri": uri}})
    return flatten(r.get("result"))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=str(REPO / "evidence/m0/verible_lsp_probe.json"))
    a = ap.parse_args()
    work = REPO / ".svweave/spike/verible"
    work.mkdir(parents=True, exist_ok=True)
    mini = work / "mini_task_function.sv"
    mini.write_text(MINI)
    ver = subprocess.run([str(VLS), "--version"], capture_output=True, text=True, timeout=10)
    lsp = Lsp(VLS, REPO / "fixtures")
    init = lsp.req("initialize", {"processId": os.getpid(), "rootUri": "file://" + str(REPO / "fixtures"),
                                  "capabilities": {"textDocument": {"documentSymbol": {
                                      "hierarchicalDocumentSymbolSupport": True}}}})
    lsp.send({"jsonrpc": "2.0", "method": "initialized", "params": {}})
    res = {"verible_ls_version": ver.stdout.strip(), "initialize_result": init.get("result"),
           "documentSymbol": {
               "fixtures/uvm/svw_demo_uvm_pkg.sv": doc_symbols(lsp, REPO / "fixtures/uvm/svw_demo_uvm_pkg.sv"),
               "mini_task_function.sv": doc_symbols(lsp, mini)},
           "mini_source": MINI}
    lsp.close()
    names = [s["name"] for s in res["documentSymbol"]["mini_task_function.sv"]]
    res["facts"] = {"mini_symbols": names,
                    "task_symbols_reported": any(n in names for n in ("t1", "run_phase", "mt")),
                    "function_symbols_reported": any(n in names for n in ("f1", "mf"))}
    Path(a.out).parent.mkdir(parents=True, exist_ok=True)
    Path(a.out).write_text(json.dumps(res, indent=2) + "\n")
    print(json.dumps(res["facts"], indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
