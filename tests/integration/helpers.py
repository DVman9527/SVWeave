"""Helpers for the real-backend integration tests (no mocks: real pyslang worker)."""

from __future__ import annotations

import shutil
from pathlib import Path
from typing import Any

from svweave.application.options import WorkspaceOptions
from svweave.domain.envelope import Envelope
from tests.conftest import FIXTURES, Ws, profile

LANG = FIXTURES / "language"
UVM_SRC = Path(__file__).resolve().parents[2] / ".toolchain" / "uvm-core" / "src"


def lang_ws(tmp_path: Path, topic: str, top: str | list[str], **prof: Any) -> Ws:
    """Workspace whose root is a copy of fixtures/language/<topic>, profile 'p'."""
    root = tmp_path / "ws"
    shutil.copytree(LANG / topic, root)
    w = Ws(root)
    tops = [top] if isinstance(top, str) else list(top)
    base: dict[str, Any] = {
        "source_inputs": [{"kind": "filelist", "path": "files.f", "path_mode": "filelist"}],
        "top_modules": tops,
    }
    base.update(prof)
    w.config({"p": profile(**base)})
    w.policy()
    return w


def uvm_ws(tmp_path: Path, files: list[str], top: str, **prof: Any) -> Ws:
    """Workspace with the real pinned UVM library mapped as external root 'uvm'."""
    root = tmp_path / "uvm_ws"
    (root / "fixtures" / "uvm").mkdir(parents=True)
    for f in (FIXTURES / "uvm").glob("*.sv"):
        shutil.copy(f, root / "fixtures" / "uvm" / f.name)
    w = Ws(root)
    srcs = [{"kind": "file", "path": "@uvm/uvm_pkg.sv"}] + [
        {"kind": "file", "path": f"fixtures/uvm/{f}"} for f in files
    ]
    base: dict[str, Any] = {"source_inputs": srcs, "top_modules": [top], "include_dirs": ["@uvm"]}
    base.update(prof)
    w.config(
        {"p": profile(**base)},
        source_roots=[
            {"id": "project", "path": ".", "include": ["**"], "exclude": ["**/.svweave/**"]},
            {"id": "uvm", "include": ["**"], "exclude": []},
        ],
    )
    w.policy(allowed_roots=[str(root.resolve()), str(UVM_SRC.resolve())])
    w.local([{"id": "uvm", "path": str(UVM_SRC)}])
    return w


def opts(w: Ws) -> WorkspaceOptions:
    return WorkspaceOptions(root=w.root)


def ok(env: Envelope) -> Envelope:
    assert env.error is None or env.outcome.value in ("PARTIAL",), env.model_dump_json(indent=1)[
        :2000
    ]
    return env


# --------------------------------------------------------------------------- build / query
def build(w: Ws, **kw: Any) -> Envelope:
    from svweave.application.build import run_build

    return run_build(opts(w), "p", "req-test", **kw)


def golden(name: str) -> dict[str, Any]:
    import json

    return json.loads((FIXTURES / "expected" / f"{name}.json").read_text(encoding="utf-8"))


def dbq(w: Ws, sql: str, *args: Any) -> list[dict[str, Any]]:
    from svweave.storage.db import Database

    with Database(w.root / ".svweave" / "svweave.db") as db:
        return [dict(r) for r in db.conn.execute(sql, args).fetchall()]


def file_matches(logical: str, rel: str) -> bool:
    return logical == rel or logical.endswith("/" + rel)


def decl_rows(w: Ws, bid: str) -> list[dict[str, Any]]:
    return dbq(w, "SELECT * FROM declarations WHERE build_id=? ORDER BY ord", bid)


def ref_rows(w: Ws, bid: str) -> list[dict[str, Any]]:
    return dbq(w, 'SELECT * FROM "references" WHERE build_id=? ORDER BY ord', bid)


def decl_by_qn(
    rows: list[dict[str, Any]], qn: str, kind: str | None = None
) -> list[dict[str, Any]]:
    return [r for r in rows if r["qualified_name"] == qn and (kind is None or r["kind"] == kind)]


def check_declarations(w: Ws, bid: str, g: dict[str, Any]) -> None:
    """Every golden declaration exists with the exact kind, qualified name and byte span."""
    rows = decl_rows(w, bid)
    for e in g.get("declarations", []):
        hits = [
            r
            for r in rows
            if r["qualified_name"] == e["qualified_name"]
            and r["kind"] == e["kind"]
            and file_matches(r["file"], e["file"])
            and r["span_start"] == e["start"]
            and r["span_end"] == e["end"]
        ]
        assert len(hits) == 1, (e, [r for r in rows if r["qualified_name"] == e["qualified_name"]])
    check_closed_declarations(w, bid, g)


def _span_key(e: dict[str, Any]) -> tuple[Any, ...]:
    return (e["file"], e["start"], e["end"])


def _norm_file(logical: str, files: set[str]) -> str | None:
    """The golden-relative name of ``logical`` if it is one of ``files`` (else None)."""
    return next((f for f in files if file_matches(logical, f)), None)


def check_closed_declarations(w: Ws, bid: str, g: dict[str, Any]) -> None:
    """Closed-scope check (plan 20.1): for ``g['closed_scope']`` the declarations the build reports
    in the listed files must equal the golden set plus the hand-reviewed ``exclude_declarations``
    (each with a written ``reason``); an unexpected extra declaration, or a golden one that is
    missing, fails. Compared on (kind, qualified_name, file, byte span)."""
    cs = g.get("closed_scope")
    if not cs:
        return
    files = set(cs["declaration_files"])
    want = {(e["kind"], e["qualified_name"], *_span_key(e)) for e in g.get("declarations", [])
            if e["file"] in files}
    excl = {(e["kind"], e["qualified_name"], *_span_key(e)) for e in cs.get("exclude_declarations", [])}
    assert all(e.get("reason") for e in cs.get("exclude_declarations", [])), "every exclusion needs a reason"
    assert not (want & excl), "an excluded declaration is also expected"
    got = set()
    for r in decl_rows(w, bid):
        f = _norm_file(r["file"], files)
        if f is not None:
            got.add((r["kind"], r["qualified_name"], f, r["span_start"], r["span_end"]))
    assert got - want - excl == set(), f"unexpected declarations: {sorted(got - want - excl)}"
    assert want - got == set(), f"golden declarations not produced: {sorted(want - got)}"
    assert excl - got == set(), f"stale exclusions (no longer produced): {sorted(excl - got)}"


def check_closed_references(w: Ws, bid: str, g: dict[str, Any]) -> None:
    """Closed-scope check for references whose source occurrence lies in ``reference_files``."""
    cs = g.get("closed_scope")
    if not cs or "reference_files" not in cs:
        return
    files = set(cs["reference_files"])
    want = {(e["kind"], *_span_key(e)) for e in g.get("references", []) if e["file"] in files}
    excl = {(e["kind"], *_span_key(e)) for e in cs.get("exclude_references", [])}
    assert all(e.get("reason") for e in cs.get("exclude_references", [])), "every exclusion needs a reason"
    got = set()
    for r in ref_rows(w, bid):
        f = _norm_file(r["file"], files)
        if f is not None:
            got.add((r["kind"], f, r["span_start"], r["span_end"]))
    assert got - want - excl == set(), f"unexpected references: {sorted(got - want - excl)}"
    assert want - got == set(), f"golden references not produced: {sorted(want - got)}"
    assert excl - got == set(), f"stale exclusions (no longer produced): {sorted(excl - got)}"


def check_references(w: Ws, bid: str, g: dict[str, Any]) -> None:
    rows = ref_rows(w, bid)
    decls = {r["declaration_id"]: r for r in decl_rows(w, bid)}
    import json

    for e in g.get("references", []):
        hits = [
            r
            for r in rows
            if file_matches(r["file"], e["file"])
            and r["span_start"] == e["start"]
            and r["span_end"] == e["end"]
            and r["kind"] == e["kind"]
        ]
        assert len(hits) == 1, (e, len(hits))
        r = hits[0]
        assert r["resolution_status"] == e["status"], (e, r["resolution_status"])
        tgt = decls[r["target_id"]]["qualified_name"] if r["target_id"] else None
        assert tgt == e["target"], (e, tgt)
        ctx = json.loads(r["context"])
        for k in ("specialization", "specialization_path"):
            if k in e:
                assert ctx.get(k) == e[k], (e, ctx)
    check_closed_references(w, bid, g)


def all_items(fn: Any, *args: Any, **kw: Any) -> tuple[list[dict[str, Any]], int]:
    """Follow cursors to the end; returns (items, pages)."""
    items: list[dict[str, Any]] = []
    cursor = None
    pages = 0
    while True:
        env = fn(*args, cursor=cursor, **kw)
        pages += 1
        items += env.data["items"]
        cursor = env.pagination.next_cursor
        if cursor is None:
            return items, pages


def build_profile(w: Ws, name: str, **kw: Any) -> Envelope:
    from svweave.application.build import run_build

    return run_build(opts(w), name, "req-test", **kw)
