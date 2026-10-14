"""LOC-01 / LOC-02: byte-accurate source mapping (plan 13.4, 20.3)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from svweave.application import queries as Q
from svweave.domain.errors import ErrorCode
from svweave.domain.locations import LineIndex, lsp_to_byte
from tests.integration.helpers import (
    LANG,
    build,
    check_declarations,
    dbq,
    decl_by_qn,
    decl_rows,
    golden,
    lang_ws,
    opts,
)

pytestmark = pytest.mark.integration


def test_LOC_01_utf8_crlf_tab_positions_round_trip(tmp_path: Path) -> None:
    w = lang_ws(tmp_path, "loc01_utf8", "loc01_top")
    env = build(w)
    bid = env.build_id
    assert bid and env.analysis_coverage is not None
    assert env.analysis_coverage.completeness == "COMPLETE"
    g = golden("loc01_utf8")
    check_declarations(w, bid, g)
    raw = (LANG / "loc01_utf8" / "top.sv").read_bytes()
    assert raw.count(b"\r\n") == g["crlf_pairs"] and raw.replace(b"\r\n", b"").count(b"\n") == 0
    idx = LineIndex(raw)
    assert idx.line_count == g["line_count"] + 1  # trailing CRLF opens an empty last line
    rows = decl_rows(w, bid)
    for name, pos in g["positions"].items():
        d = decl_by_qn(rows, f"loc01_top.{name}")[0]
        s = d["span_start"]
        # independent derivation from the raw bytes
        ls = max(raw.rfind(b"\n", 0, s), -1) + 1
        assert d["line"] == pos["line"] and d["col"] == pos["col_utf8"] == s - ls + 1
        assert len(raw[ls:s].decode("utf-8").encode("utf-16-le")) // 2 == pos["lsp_utf16"]
        # the service converts the canonical byte span for an LSP client in either encoding
        for enc, key in (("utf-16", "lsp_utf16"), ("utf-8", "lsp_utf8")):
            r = Q.lsp_positions(opts(w), bid, "top.sv", s, d["span_end"], enc)  # type: ignore[arg-type]
            assert r["start"] == {"line": pos["lsp_line0"], "character": pos[key]}
            back = lsp_to_byte(idx, r["start"]["line"], r["start"]["character"], enc)  # type: ignore[arg-type]
            assert back == s  # LSP -> original bytes round trip
    # multi-line declaration: the two declarators sit on consecutive lines inside the golden span
    ml = g["multi_line_declaration"]
    bus, aux = (decl_by_qn(rows, f"loc01_top.{n}")[0] for n in ("data_bus", "data_aux"))
    assert ml["start"] < bus["span_start"] < aux["span_end"] <= ml["end"]
    assert (bus["line"], aux["line"]) == (6, 7)
    # an exact edit of a declared name never splits a character or a line ending
    s, e = bus["span_start"], bus["span_end"]
    edited = raw[:s] + b"renamed_bus" + raw[e:]
    edited.decode("utf-8")
    assert edited.count(b"\r\n") == raw.count(b"\r\n")
    assert (
        edited.replace(b"\r\n", b"").count(b"\n") == 0
        and edited.replace(b"\r\n", b"").count(b"\r") == 0
    )
    # an offset inside a multi-byte character is rejected, not rounded
    cn = raw.index("註".encode()) + 1
    with pytest.raises(ValueError, match="multi-byte"):
        from svweave.domain.locations import byte_to_lsp

        byte_to_lsp(idx, cn)
    # string value kept as source text in the declaration (no re-encoding)
    msg = decl_by_qn(rows, "loc01_top.MSG")[0]
    assert json.loads(msg["attrs"])["value"] == '"你好，世界"'


def test_LOC_02_macro_line_directive_and_repeated_include(tmp_path: Path) -> None:
    w = lang_ws(tmp_path, "loc02_macro", "loc02_top")
    env = build(w)
    bid = env.build_id
    assert bid and env.analysis_coverage is not None
    g = golden("loc02_macro")
    check_declarations(w, bid, g)
    rows = decl_rows(w, bid)
    # spelling vs expansion location are separate fields
    for qn, want in g["macro_origins"].items():
        d = decl_by_qn(rows, qn)[0]
        loc = json.loads(d["loc"])
        mac = loc["macro"]
        assert mac["name"] == want["macro"] and mac["spelled_in"] == want["spelled_in"]
        assert (mac["expansion"]["file"], mac["expansion"]["start"]) == (
            want["expansion"]["file"],
            want["expansion"]["start"],
        )
        assert (loc["file"], loc["start"]) != (mac["expansion"]["file"], mac["expansion"]["start"])
    # `line directive: physical location stays canonical, the display location is separate
    ld = g["line_directive"]
    d = decl_by_qn(rows, ld["decl"])[0]
    loc = json.loads(d["loc"])
    assert loc["file"] == "top.sv" and loc["line"] == ld["physical_line"]
    assert loc["display"] == {"file": ld["display_file"], "line": ld["display_line"]}
    assert "display" not in json.loads(decl_by_qn(rows, "loc02_a.arg_reg")[0]["loc"])
    # a macro-body spelling shared by two expansions is ambiguous: candidates, no pick
    fx = g["declarations"][1]  # loc02_a.fixed_reg in defs.svh
    amb = Q.symbol(opts(w), bid, "r", file="defs.svh", byte=fx["start"])
    assert amb.error is not None and amb.error.code == ErrorCode.AMBIGUOUS_SYMBOL
    assert {c["qualified_name"] for c in amb.data["candidates"]} == {
        "loc02_a.fixed_reg",
        "loc02_b.fixed_reg",
    }
    assert all(c["macro_origin"]["spelled_in"] == "macro_body" for c in amb.data["candidates"])
    # a macro-ARGUMENT spelling is unique to its call site
    ar = Q.symbol(opts(w), bid, "r", file="top.sv", byte=g["declarations"][0]["start"])
    assert ar.error is None and ar.data["symbol"]["qualified_name"] == "loc02_a.arg_reg"
    # repeated include: same header span, two scoped declarations (never merged)
    fh = [d for d in rows if d["name"] == "FROM_HEADER"]
    assert len(fh) == g["repeated_include"]["distinct_declarations"]
    assert len({(d["file"], d["span_start"]) for d in fh}) == 1
    assert len({d["declaration_id"] for d in fh}) == 2
    inc = Q.symbol(opts(w), bid, "r", file="dup.svh", byte=fh[0]["span_start"])
    assert inc.error is not None and inc.error.code == ErrorCode.AMBIGUOUS_SYMBOL
    assert len(dbq(w, "SELECT 1 FROM source_files WHERE logical_path='dup.svh'")) == 1
