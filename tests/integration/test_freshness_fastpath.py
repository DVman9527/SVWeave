"""Read-path freshness fast path (stat fingerprint + cached verdict) must give the SAME verdict as
the full hash check, compute it once per query, and fall back to the full check on any change."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from svweave.application import queries as Q
from svweave.application import semantic_common as sc
from tests.integration.helpers import build, lang_ws, opts

pytestmark = pytest.mark.integration


class Counter:
    def __init__(self, mp: pytest.MonkeyPatch) -> None:
        self.n = 0
        orig = sc._inputs_current_full

        def counted(*a, **k):  # type: ignore[no-untyped-def]
            self.n += 1
            return orig(*a, **k)

        mp.setattr(sc, "_inputs_current_full", counted)


def fresh(w, bid: str) -> str:  # type: ignore[no-untyped-def]
    return str(Q.hierarchy(opts(w), bid, "r").freshness.value)


def strict(w, bid: str) -> str:  # type: ignore[no-untyped-def]
    ctx = opts(w).load()
    from svweave.storage.db import Database

    with Database(ctx.state_dir / "svweave.db") as db:
        return sc.freshness_of(ctx, db, sc.require_build(db, bid), strict=True).value


def test_full_check_runs_once_then_never_while_nothing_changes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    w = lang_ws(tmp_path, "sem09_escaped", "sem09_top")
    bid = build(w).build_id
    assert bid
    sc._VERDICTS.clear()
    c = Counter(monkeypatch)
    assert fresh(w, bid) == "CURRENT" and c.n == 1  # cold: one full check, not one per envelope
    page = Q.hierarchy(opts(w), bid, "r", limit=3)  # a paged query builds 2 envelopes internally
    assert page.freshness.value == "CURRENT" and c.n == 1
    for _ in range(5):
        Q.hierarchy(opts(w), bid, "r")
    assert c.n == 1  # hot path: stat fingerprint only


def test_fast_path_agrees_with_the_full_check_after_every_kind_of_change(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    w = lang_ws(tmp_path, "bld07_incdir", "inc_top", include_dirs=["dir_a", "dir_b"])
    bid = build(w).build_id
    assert bid
    assert fresh(w, bid) == strict(w, bid) == "CURRENT"
    c = Counter(monkeypatch)
    top = w.root / "inc_top.sv"
    orig = top.read_bytes()
    # (1) content edit that changes size
    top.write_bytes(orig + b"// x\n")
    assert fresh(w, bid) == strict(w, bid) == "STALE" and c.n >= 1
    top.write_bytes(orig)
    os.utime(top, ns=(top.stat().st_atime_ns, top.stat().st_mtime_ns))
    assert fresh(w, bid) == strict(w, bid) == "CURRENT"
    # (2) same size AND same mtime restored: ctime still moves, so the shortcut notices
    stt = top.stat()
    top.write_bytes(orig.replace(b"VAL", b"VAX"))
    os.utime(top, ns=(stt.st_atime_ns, stt.st_mtime_ns))
    assert top.stat().st_size == stt.st_size and top.stat().st_mtime_ns == stt.st_mtime_ns
    assert fresh(w, bid) == strict(w, bid) == "STALE"
    top.write_bytes(orig)
    assert fresh(w, bid) == strict(w, bid) == "CURRENT"
    # (3) a new candidate header earlier in the include search space (no listed file changed)
    (w.root / "dir_a" / "other.svh").write_text("// new\n")
    assert fresh(w, bid) == strict(w, bid) == "STALE"
    (w.root / "dir_a" / "other.svh").unlink()
    assert fresh(w, bid) == strict(w, bid) == "CURRENT"
    # (4) a listed file disappears
    hdr = w.root / "dir_b" / "common.svh"
    data = hdr.read_bytes()
    hdr.unlink()
    assert fresh(w, bid) == strict(w, bid) == "STALE"
    hdr.write_bytes(data)
    assert fresh(w, bid) == strict(w, bid) == "CURRENT"
    # (5) configuration only (svweave.yaml): same files, different resolved config_id
    from tests.conftest import profile

    w.config(
        {
            "p": profile(
                source_inputs=[{"kind": "filelist", "path": "files.f", "path_mode": "filelist"}],
                top_modules=["inc_top"],
                include_dirs=["dir_b", "dir_a"],
            )
        }
    )
    assert fresh(w, bid) == strict(w, bid) == "STALE"


def test_strict_mode_and_publish_paths_never_use_the_cached_verdict(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    w = lang_ws(tmp_path, "sem09_escaped", "sem09_top")
    bid = build(w).build_id
    assert bid
    assert fresh(w, bid) == "CURRENT"  # warms the cache
    c = Counter(monkeypatch)
    assert strict(w, bid) == "CURRENT" and c.n == 1 and strict(w, bid) == "CURRENT" and c.n == 2
    # a build always re-captures and hashes: editing without touching mtime tricks still changes id
    top = w.root / "top.sv"
    top.write_text(top.read_text() + "// again\n")
    e2 = build(w)
    assert e2.build_id != bid and e2.freshness.value == "CURRENT"
