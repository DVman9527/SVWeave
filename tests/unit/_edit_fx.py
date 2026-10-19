"""Fixture for the M4 edit-loop tests: a tiny SystemVerilog workspace with editing enabled.

Real pyslang builds (a subprocess worker, a few seconds); Serena is always faked in unit tests.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from svweave.adapters.serena.guard import EditOutcome
from svweave.application import edit as E
from svweave.application.build import DB_FILENAME, run_build
from svweave.application.options import WorkspaceOptions
from svweave.domain.envelope import Envelope
from svweave.editing.diffing import FileChange
from svweave.storage.db import Database
from tests.conftest import Ws, profile

PKG = """\
package p;
  `include "defs.svh"
  class c;
    function new();
    endfunction
    function int f(int x);
      return x + `BUMP;
    endfunction
    function int g(int x);
      return x * 2;
    endfunction
    task t();
      $display("task hello");
    endtask
  endclass
  class d;
    function int f(int x);
      return x - 1;
    endfunction
  endclass
  class e;
    `MKF
  endclass
endpackage
"""
DEFS = "`define BUMP 1\n`define MKF function int hh(); return 0; endfunction\n"
TOP = """\
module top(input logic clk, output logic [3:0] q);
  import p::*;
  always_ff @(posedge clk) q <= q + 4'd1;
endmodule
"""
OTHER = "// other file, must stay untouched\nmodule other; endmodule\n"
BAD_TOP = TOP.replace("q + 4'd1", "q + undeclared_sig")


def sha(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


class EditWs:
    def __init__(self, root: Path, w: Ws) -> None:
        self.root, self.w = root, w
        self.opts = WorkspaceOptions(root=root)

    # ------------------------------------------------------------------ build / decl lookups
    def build(self, name: str = "p") -> Envelope:
        return run_build(self.opts, name, "req-fx")

    def db(self) -> Database:
        return Database(self.root / ".svweave" / DB_FILENAME)

    def decl(self, build_id: str, qn: str, kind: str | None = None) -> dict[str, Any]:
        with self.db() as db:
            rows = db.conn.execute(
                "SELECT * FROM declarations WHERE build_id=? AND qualified_name=?"
                + (" AND kind=?" if kind else ""),
                (build_id, qn, kind) if kind else (build_id, qn),
            ).fetchall()
        assert len(rows) == 1, (qn, len(rows))
        return dict(rows[0])

    def text(self, rel: str) -> str:
        return (self.root / rel).read_text()

    def read(self, rel: str) -> bytes:
        return (self.root / rel).read_bytes()

    def req(self, base: Envelope, edits: list[dict[str, Any]], key: str | None = None,
            profiles: list[str] | None = None) -> dict[str, Any]:
        return {
            "base_build_id": base.build_id,
            "snapshot_id": base.snapshot_id,
            "edits": edits,
            "validate_profiles": profiles if profiles is not None else ["p"],
            "request_key": key or ("key-" + uuid.uuid4().hex[:12]),
        }

    def sym_edit(self, build_id: str, qn: str, new_body: str, kind: str | None = "function"
                 ) -> dict[str, Any]:
        d = self.decl(build_id, qn, kind)
        return {"kind": "symbol_replacement", "declaration_id": d["declaration_id"],
                "new_body": new_body}

    def range_edit(self, rel: str, old: str, new: str, nth: int = 0) -> dict[str, Any]:
        data = self.read(rel)
        o = old.encode()
        pos = -1
        for _ in range(nth + 1):
            pos = data.index(o, pos + 1)
        return {"kind": "exact_range", "logical_path": rel, "start_byte": pos,
                "end_byte": pos + len(o), "expected_sha256": sha(o), "replacement": new}

    def trusted(self) -> Path:
        return self.w.ctx().trusted_dir

    def policy_json(self) -> dict[str, Any]:
        out: dict[str, Any] = json.loads(self.w.policy_path.read_text())
        return out

    def txn_rows(self) -> list[dict[str, Any]]:
        with self.db() as db:
            return [dict(r) for r in db.conn.execute("SELECT * FROM edit_transactions")]


def make_ws(tmp_path: Path, *, enabled: bool = True, extra_profiles: dict[str, Any] | None = None,
            editing: dict[str, Any] | None = None, writable_paths: list[str] | None = None,
            top: str = TOP, root_writable: bool = True, allow_test: bool = True) -> EditWs:
    root = tmp_path / "ws"
    w = Ws(root)
    w.write("rtl/pkg.sv", PKG)
    w.write("rtl/defs.svh", DEFS)
    w.write("rtl/top.sv", top)
    w.write("rtl/other.sv", OTHER)
    w.write("ro/lib.sv", "module lib; endmodule\n")
    prof = profile(
        source_inputs=[{"kind": "file", "path": "rtl/pkg.sv"},
                       {"kind": "file", "path": "rtl/top.sv"},
                       {"kind": "file", "path": "rtl/other.sv"},
                       {"kind": "file", "path": "ro/lib.sv"}],
        top_modules=["top"], include_dirs=["rtl"],
    )
    profiles = {"p": prof, **(extra_profiles or {})}
    ed = {"enabled": enabled, "allowed_paths": ["rtl/**", "ro/**"], "max_files_per_patch": 5,
          "max_changed_lines": 60, "require_human_approval": True, "validate_profiles": ["p"]}
    ed.update(editing or {})
    w.config(
        profiles,
        source_roots=[
            {"id": "project", "path": ".", "include": ["**"], "exclude": ["**/.svweave/**"],
             "writable": False},
            {"id": "rtl", "path": "rtl", "include": ["**"], "exclude": [],
             "writable": root_writable},
            {"id": "ro", "path": "ro", "include": ["**"], "exclude": [], "writable": False},
        ],
        editing=ed,
    )
    w.policy(editing_enabled=enabled, allow_test_approvals=allow_test,
             writable_paths=writable_paths if writable_paths is not None else ["rtl/**", "ro/**"])
    return EditWs(root, w)


def typed_confirm(typed: str | None = None, method: str = "test_token"):  # type: ignore[no-untyped-def]
    def confirm(info: dict[str, Any]) -> E.Confirmation:
        return E.Confirmation(typed if typed is not None else info["patch_hash"][:12], method)

    return confirm


class FakeSymbol:
    def __init__(self, name_path: str, kind: str, rel: str, data: bytes, qn_body: tuple[int, int]
                 ) -> None:
        self.name_path, self.kind, self.relative_path = name_path, kind, rel
        self.byte_start, self.byte_end = qn_body
        self.body = data[qn_body[0]:qn_body[1]].decode()


class FakeSerena:
    """Stands in for the Serena adapter in unit tests. ``mode`` selects misbehaviour."""

    def __init__(self, candidate: Path, decl_spans: dict[str, tuple[int, int]],
                 mode: str = "ok", kinds: dict[str, str] | None = None) -> None:
        self.candidate, self.spans, self.mode = candidate, decl_spans, mode
        self.kinds = kinds or {}
        self.calls: list[tuple[str, str]] = []

    def plan_symbol_edit(self, candidate: Path, name_path: str, relative_path: str) -> Any:
        from svweave.adapters.serena.adapter import SymbolEditPlan

        self.calls.append(("plan", name_path))
        key = name_path
        if key not in self.spans:
            return SymbolEditPlan("exact_range", "fake: not found")
        data = (candidate / relative_path).read_bytes()
        sym = FakeSymbol(name_path, self.kinds.get(key, "Function"), relative_path, data,
                         self.spans[key])
        return SymbolEditPlan("serena_symbol", "fake", sym, [sym])  # type: ignore[arg-type]

    def replace_symbol_body(self, candidate: Path, name_path: str, relative_path: str,
                            new_body: str, *, expected_body_sha256: str | None = None
                            ) -> EditOutcome:
        self.calls.append(("replace", name_path))
        p = candidate / relative_path
        data = p.read_bytes()
        a, b = self.spans[name_path]
        if self.mode == "write_other":  # EDT-06: Serena touches an extra file
            (candidate / "rtl" / "other.sv").write_text("// pwned by serena\n")
        if self.mode == "write_original":  # the candidate is a copy: the ORIGINAL must be safe
            pass
        p.write_bytes(data[:a] + new_body.encode() + data[b:])
        unexpected = ("rtl/other.sv",) if self.mode == "write_other" else ()
        return EditOutcome(
            edit_path="serena_symbol", relative_path=relative_path,
            changed_files=(relative_path, *unexpected), unexpected_files=unexpected,
            old_span=(a, b), new_span=(a, a + len(new_body)), within_target=True,
            counted_as_serena_semantic_edit=True,
            target={"name_path": name_path, "byte_start": a, "byte_end": b},
        )


def fake_factory(ws: EditWs, spans: dict[str, tuple[int, int]], mode: str = "ok"
                 ) -> tuple[Any, list[FakeSerena]]:
    made: list[FakeSerena] = []

    @contextmanager
    def factory(candidate: Path) -> Iterator[FakeSerena]:
        f = FakeSerena(candidate, spans, mode)
        made.append(f)
        yield f

    return factory, made


def tamper_candidate(ws: EditWs, txn_id: str, rel: str, mutate: Any) -> None:
    p = ws.root / ".svweave" / "candidates" / txn_id / rel
    p.write_bytes(mutate(p.read_bytes()))


def changes_of(fc: list[FileChange]) -> list[str]:
    return [c.logical_path for c in fc]



def spans_for(ws: EditWs, build_id: str, *qns: str) -> dict[str, tuple[int, int]]:
    """Serena name path ("p/c/g") -> declaration byte span, from the semantic database."""
    out: dict[str, tuple[int, int]] = {}
    for qn in qns:
        d = ws.decl(build_id, qn)
        out["/".join(qn.split("::"))] = (d["full_start"], d["full_end"])
    return out


def prepared(ws: EditWs, base: Envelope, edits: list[dict[str, Any]], *, serena: bool = True,
             mode: str = "ok", qns: tuple[str, ...] = ("p::c::g",), key: str | None = None,
             profiles: list[str] | None = None) -> Envelope:
    """prepare() with a fake Serena (or none)."""
    req = ws.req(base, edits, key, profiles)
    if serena:
        factory, _ = fake_factory(ws, spans_for(ws, base.build_id or "", *qns), mode)
        return E.prepare(ws.opts, "r-prep", req, serena_factory=factory)
    return E.prepare(ws.opts, "r-prep", req, use_serena=False)


def to_approval(ws: EditWs, base: Envelope, edits: list[dict[str, Any]], **kw: Any
                ) -> tuple[str, str]:
    """prepare + validate + approve (test token). Returns (txn_id, patch_hash)."""
    e = prepared(ws, base, edits, **kw)
    tid = e.data["transaction_id"]
    v = E.validate(ws.opts, "r-val", transaction_id=tid)
    assert v.error is None, v.error
    E.approve(ws.opts, "r-appr", transaction_id=tid, patch_hash=v.data["patch_hash"],
              confirm=typed_confirm())
    return tid, v.data["patch_hash"]
