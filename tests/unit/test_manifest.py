from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest

from svweave.domain.errors import ErrorCode, SvwError
from svweave.domain.ids import compute_build_id, sha256_hex
from svweave.manifest.lexer import (
    env_names,
    expand_env,
    shell_syntax_reason,
    split_unquoted,
    tokenize,
    unquote,
)
from svweave.manifest.resolver import IGNORED_OPTIONS, resolve_profile, write_manifest
from svweave.manifest.slang_args import to_slang_args
from svweave.snapshot.capture import capture_snapshot
from tests.conftest import FIXTURES, Ws, manifest_dict, profile

FL = {"kind": "filelist", "dialect": "svweave-filelist-v1"}


def fl(path: str, **kw: Any) -> list[dict[str, Any]]:
    return [{**FL, "path": path, **kw}]


def resolve(ws: Ws, **prof: Any) -> Any:
    ws.config({"p": profile(**prof)})
    return resolve_profile(ws.ctx(), "p", {})


def blocked(ws: Ws, code: ErrorCode, **prof: Any) -> SvwError:
    ws.config({"p": profile(**prof)})
    with pytest.raises(SvwError) as ei:
        resolve_profile(ws.ctx(), "p", {})
    assert ei.value.code == code, ei.value
    return ei.value


# --------------------------------------------------------------------------- lexer
def test_lexer_tokens_comments_and_quotes() -> None:
    text = (
        "// full line\n# hash\n  a.sv   // trailing\n"
        '"dir with space/b.sv" c//d.sv e#f.sv\r\n-DMSG="x y" +define+A="p q"+B=1\n'
    )
    toks = tokenize(text, "t.f")
    assert [t.raw for t in toks] == [
        "a.sv",
        '"dir with space/b.sv"',
        "c//d.sv",
        "e#f.sv",
        '-DMSG="x y"',
        '+define+A="p q"+B=1',
    ]
    assert toks[0].line == 3 and toks[0].col == 3
    assert tokenize("﻿x.sv", "t")[0].raw == "x.sv"
    assert tokenize("", "t") == []


def test_lexer_errors() -> None:
    with pytest.raises(SvwError) as e:
        tokenize('ok.sv\n"open.sv\n', "t.f")
    assert e.value.code == ErrorCode.INVALID_CONFIG and e.value.details["line"] == 2
    with pytest.raises(SvwError) as e2:
        tokenize("/* x */\n", "t.f")
    assert e2.value.code == ErrorCode.UNSUPPORTED_OPTION


def test_lexer_unquote_split_escape() -> None:
    assert unquote('"a b/c.sv"') == "a b/c.sv"
    assert unquote('dir/"a b".sv') == "dir/a b.sv"
    assert unquote(r'"say \"hi\" \\ \n"') == 'say "hi" \\ \\n'
    assert split_unquoted('A=1+B="x+y"+C', "+") == ["A=1", 'B="x+y"', "C"]
    assert split_unquoted(r'A="q\"+z"+B', "+") == [r'A="q\"+z"', "B"]


def test_lexer_shell_syntax_detection() -> None:
    assert shell_syntax_reason("`cat x`")
    assert shell_syntax_reason('"`cat x`"')  # even inside quotes
    assert shell_syntax_reason("$(ls)")
    assert shell_syntax_reason("${bad")
    assert shell_syntax_reason("a;b") and shell_syntax_reason("a|b") and shell_syntax_reason("a>b")
    assert shell_syntax_reason('"a;b"') is None  # quoted metachar is data
    assert shell_syntax_reason("${OK}/x") is None and shell_syntax_reason("$OK/x") is None
    assert shell_syntax_reason("plain/path.sv") is None
    assert env_names("${A}/$B/x$") == ["A", "B"]
    assert expand_env("${A}/$B", {"A": "1", "B": "2"}) == "1/2"


# --------------------------------------------------------------------------- BLD-01
def test_BLD_01_f_vs_F_resolution_bases(ws: Ws) -> None:
    ws.write("lists/top1.f", "-f lists/sub/nested.f\n-F lists/sub/nested.f\n")
    nm = resolve(ws, source_inputs=fl("lists/top1.f"))
    srcs = nm.units[0].sources
    assert [s.path for s in srcs] == ["rel.sv", "lists/sub/rel.sv"]  # same name, distinct files
    assert [s.base for s in srcs] == ["", "lists/sub"]
    assert [s.raw for s in srcs] == ["rel.sv", "rel.sv"]
    assert srcs[0].chain == ["lists/top1.f", "lists/sub/nested.f"]
    assert all(s.origin.file == "lists/sub/nested.f" and s.origin.line == 1 for s in srcs)
    led = [e for e in nm.ledger if e.kind == "filelist"]
    assert [(e.resolved, e.classification) for e in led] == [
        ("lists/top1.f", "cwd"),
        ("lists/sub/nested.f", "cwd"),
        ("lists/sub/nested.f", "filelist"),
    ]
    assert nm.filelists == ["lists/top1.f", "lists/sub/nested.f", "lists/sub/nested.f"]


def test_BLD_01_path_mode_filelist_for_top_level(ws: Ws) -> None:
    nm = resolve(ws, source_inputs=fl("lists/sub/nested.f", path_mode="filelist"))
    assert nm.units[0].sources[0].path == "lists/sub/rel.sv"
    nm = resolve(ws, source_inputs=fl("lists/sub/nested.f", path_mode="cwd"))
    assert nm.units[0].sources[0].path == "rel.sv"


def test_BLD_01_profile_cwd_changes_f_base(ws: Ws) -> None:
    ws.write("lists/sub/only.sv")
    ws.write("lists/sub/l.f", "only.sv\n")
    nm = resolve(ws, cwd="lists/sub", source_inputs=fl("l.f"))
    assert nm.units[0].sources[0].path == "lists/sub/only.sv"
    assert nm.cwd == "lists/sub"


def test_file_kind_source_input(ws: Ws) -> None:
    nm = resolve(ws, source_inputs=[{"kind": "file", "path": "rtl/a.sv"}])
    assert nm.units[0].sources[0].path == "rtl/a.sv"
    assert nm.filelists == []


# --------------------------------------------------------------------------- BLD-02
def test_BLD_02_quotes_defines_and_ledger(ws: Ws) -> None:
    nm = resolve(ws, source_inputs=fl("lists/main.f"))
    u = nm.units[0]
    assert [s.path for s in u.sources] == ["rtl/a.sv", "rtl/b.sv", "rtl/with space/sp.sv"]
    assert [s.raw for s in u.sources][2] == '"rtl/with space/sp.sv"'
    assert [(d.name, d.value) for d in u.defines] == [
        ("SVW_DEMO", "1"),
        ("WIDTH", "8"),
        ("MSG", '"hello world"'),
        ("EMPTY", ""),
    ]
    assert [d.path for d in u.include_dirs] == ["inc_a", "inc_b", "inc_b"]  # ordered, no dedupe
    assert [i.option for i in nm.ignored_options] == ["-l"]
    assert nm.ignored_options[0].args == ["sim.log"]
    assert nm.ignored_options[0].classification == "output_only"
    assert [e.seq for e in nm.ledger] == list(range(len(nm.ledger)))
    assert {"-l", "-logfile", "-nologo", "-quiet", "-q"} == set(IGNORED_OPTIONS)


def test_BLD_02_allowlisted_env_expansion(ws: Ws) -> None:
    ws.config(
        {"p": profile(source_inputs=fl("lists/with_env.f"), environment_allowlist=["RTL_DIR"])}
    )
    nm = resolve_profile(ws.ctx(), "p", {"RTL_DIR": "rtl", "UNRELATED": "zzz"})
    assert [s.path for s in nm.units[0].sources] == ["rtl/a.sv", "rtl/b.sv"]
    assert nm.units[0].sources[0].raw == "${RTL_DIR}/a.sv"


def test_BLD_02_unset_empty_or_unlisted_env_is_blocked_not_empty_string(ws: Ws) -> None:
    ws.config(
        {"p": profile(source_inputs=fl("lists/with_env.f"), environment_allowlist=["RTL_DIR"])}
    )
    for env in ({}, {"RTL_DIR": ""}):
        with pytest.raises(SvwError) as ei:
            resolve_profile(ws.ctx(), "p", env)
        e = ei.value
        assert e.code == ErrorCode.MISSING_INPUT
        assert {i["reason"] for i in e.details["issues"]} == {"env_not_set"}
        assert e.details["issues"][0]["variable"] == "RTL_DIR"
        assert e.details["issues"][0]["origin"]["line"] == 1
    ws.config({"p": profile(source_inputs=fl("lists/with_env.f"))})  # no allowlist
    with pytest.raises(SvwError) as ei:
        resolve_profile(ws.ctx(), "p", {"RTL_DIR": "rtl"})
    assert {i["reason"] for i in ei.value.details["issues"]} == {"env_not_allowlisted"}


def test_BLD_02_default_environ_is_os_environ(ws: Ws, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("RTL_DIR", "rtl")
    ws.config(
        {"p": profile(source_inputs=fl("lists/with_env.f"), environment_allowlist=["RTL_DIR"])}
    )
    assert resolve_profile(ws.ctx(), "p").units[0].sources[0].path == "rtl/a.sv"


# --------------------------------------------------------------------------- BLD-03 / SEC-02
def test_BLD_03_unsupported_options_reported_with_origin(ws: Ws) -> None:
    for fx, opt in (("unsupported_plus", "+notimingcheck"), ("unsupported_dash", "-vendor_magic")):
        n = Ws(ws.root.parent / fx)
        shutil.copytree(FIXTURES / "negative" / "proj_neg", n.root)
        n.policy()
        e = blocked(n, ErrorCode.UNSUPPORTED_OPTION, source_inputs=fl(f"lists/{fx}.f"))
        iss = e.details["issues"][0]
        assert iss["option"] == opt
        assert iss["origin"] == {"file": f"lists/{fx}.f", "line": 2, "col": 1}
        assert iss["chain"] == [f"lists/{fx}.f"]
        assert "BLOCKED" in iss["message"]


def test_BLD_03_all_unsupported_options_collected_not_dropped(neg: Ws) -> None:
    neg.write("lists/many.f", "rtl/a.sv\n-x1\n+y2\n-x3\n")
    e = blocked(neg, ErrorCode.UNSUPPORTED_OPTION, source_inputs=fl("lists/many.f"))
    assert [i["option"] for i in e.details["issues"]] == ["-x1", "+y2", "-x3"]


def test_BLD_03_unsupported_D_separate_arg_form(neg: Ws) -> None:
    neg.write("lists/d.f", "rtl/a.sv\n-D FOO=1\n")
    e = blocked(neg, ErrorCode.UNSUPPORTED_OPTION, source_inputs=fl("lists/d.f"))
    assert e.details["issues"][0]["reason"] == "unsupported_form"


@pytest.mark.parametrize("fx", ["backtick", "dollar_paren", "semicolon"])
def test_SEC_02_shell_substitution_never_executed(
    neg: Ws, fx: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    def boom(*a: Any, **k: Any) -> None:
        raise AssertionError("subprocess must never be used")

    monkeypatch.setattr(subprocess, "Popen", boom)
    monkeypatch.setattr("os.system", boom)
    e = blocked(neg, ErrorCode.UNSUPPORTED_OPTION, source_inputs=fl(f"lists/{fx}.f"))
    assert e.details["issues"][0]["reason"] == "shell_syntax"
    assert not (neg.root / "pwned").exists()
    assert not list(neg.root.glob("**/pwned*"))


def test_SEC_02_quoted_metachar_is_data_not_syntax(ws: Ws) -> None:
    ws.write("weird;name.sv")
    ws.write("lists/q.f", '"weird;name.sv"\n+define+K="a|b"\n')
    nm = resolve(ws, source_inputs=fl("lists/q.f"))
    assert nm.units[0].sources[0].path == "weird;name.sv"
    assert nm.units[0].defines[0].value == '"a|b"'


def test_SEC_02_block_comment_and_unterminated_quote(neg: Ws) -> None:
    blocked(neg, ErrorCode.UNSUPPORTED_OPTION, source_inputs=fl("lists/block_comment.f"))
    e = blocked(neg, ErrorCode.INVALID_CONFIG, source_inputs=fl("lists/unterminated.f"))
    assert e.details["line"] == 2


def test_SEC_01_filelist_path_escape_rejected(neg: Ws) -> None:
    (neg.root.parent / "outside.sv").write_text("module o; endmodule\n")
    blocked(neg, ErrorCode.PATH_NOT_ALLOWED, source_inputs=fl("lists/escape.f"))


# --------------------------------------------------------------------------- BLD-04
@pytest.mark.parametrize(
    "fx,reason",
    [
        ("missing_src", "source_missing"),
        ("missing_filelist", "filelist_missing"),
        ("missing_inc", "dir_missing"),
    ],
)
def test_BLD_04_missing_inputs_block(neg: Ws, fx: str, reason: str) -> None:
    e = blocked(neg, ErrorCode.MISSING_INPUT, source_inputs=fl(f"lists/{fx}.f"))
    assert e.details["issues"][0]["reason"] == reason
    assert e.details["issues"][0]["origin"]["line"] == 2


def test_BLD_04_missing_top_level_filelist_include_dir_library(ws: Ws) -> None:
    blocked(ws, ErrorCode.MISSING_INPUT, source_inputs=fl("lists/ghost.f"))
    blocked(ws, ErrorCode.MISSING_INPUT, include_dirs=["no_such_dir"])
    blocked(ws, ErrorCode.MISSING_INPUT, library_dirs=["no_such_lib"])
    blocked(ws, ErrorCode.MISSING_INPUT, library_files=["no_such.v"])
    blocked(ws, ErrorCode.MISSING_INPUT, source_inputs=[{"kind": "file", "path": "nope.sv"}])
    e = blocked(
        ws,
        ErrorCode.MISSING_INPUT,
        source_inputs=[{"kind": "file", "path": "nope.sv"}, {"kind": "file", "path": "nope2.sv"}],
    )
    assert len(e.details["issues"]) == 2  # all blockers reported, not just the first
    ws.write("lists/libf.f", "-v nofile.v\n-y nodir\nrtl/a.sv\n")
    e = blocked(ws, ErrorCode.MISSING_INPUT, source_inputs=fl("lists/libf.f"))
    assert {i["reason"] for i in e.details["issues"]} == {"library_missing", "dir_missing"}


def test_filelist_syntax_errors(ws: Ws) -> None:
    for body in ("-f\n", "+incdir+\n", "+define+\n", "+define+1bad=2\n", "+libext+v\n", "-I"):
        ws.write("lists/bad.f", body)
        blocked(ws, ErrorCode.INVALID_CONFIG, source_inputs=fl("lists/bad.f"))
    ws.write("lists/dollar.f", "rtl/a.sv\n-D\n")
    blocked(ws, ErrorCode.UNSUPPORTED_OPTION, source_inputs=fl("lists/dollar.f"))
    (ws.root / "lists" / "bin.f").write_bytes(b"\xff\xfe\x00bad")
    blocked(ws, ErrorCode.INVALID_CONFIG, source_inputs=fl("lists/bin.f"))


# --------------------------------------------------------------------------- BLD-05
def _cycle_ws(tmp_path: Path) -> Ws:
    w = Ws(tmp_path / "cyc")
    shutil.copytree(FIXTURES / "negative" / "cycle", w.root)
    w.policy()
    return w


def test_BLD_05_cycle_detection(tmp_path: Path) -> None:
    w = _cycle_ws(tmp_path)
    e = blocked(w, ErrorCode.MISSING_INPUT, source_inputs=fl("a.f"))
    assert e.details["reason"] == "filelist_cycle"
    assert e.details["chain"] == ["a.f", "lists/b.f", "a.f"]
    assert "a.f -> lists/b.f -> a.f" in e.message


def test_BLD_05_self_include_and_depth_limit(ws: Ws) -> None:
    ws.write("lists/self.f", "-f lists/self.f\n")
    e = blocked(ws, ErrorCode.MISSING_INPUT, source_inputs=fl("lists/self.f"))
    assert e.details["chain"] == ["lists/self.f", "lists/self.f"]

    def chain(n: int) -> None:
        for i in range(n):
            nxt = f"-f lists/c{i + 1}.f\n" if i + 1 < n else "rtl/a.sv\n"
            ws.write(f"lists/c{i}.f", nxt)

    chain(32)  # exactly the maximum nesting depth
    assert resolve(ws, source_inputs=fl("lists/c0.f")).units[0].sources[0].path == "rtl/a.sv"
    chain(33)
    e = blocked(ws, ErrorCode.MISSING_INPUT, source_inputs=fl("lists/c0.f"))
    assert e.details["reason"] == "filelist_depth_exceeded" and e.details["max_depth"] == 32
    assert len(e.details["chain"]) == 33


def test_BLD_05_same_filelist_twice_without_cycle_is_allowed(ws: Ws) -> None:
    nm = resolve(ws, source_inputs=fl("lists/sub/nested.f") + fl("lists/sub/nested.f"))
    assert [s.path for s in nm.units[0].sources] == ["rel.sv", "rel.sv"]  # no silent dedupe


# --------------------------------------------------------------------------- BLD-06
def _bld6(ws: Ws, mode: str) -> Any:
    return resolve(ws, source_inputs=fl("lists/macros.f"), compilation_units={"mode": mode})


def test_BLD_06_single_vs_per_file_cu_boundaries(ws: Ws) -> None:
    single, per = _bld6(ws, "single"), _bld6(ws, "per_file")
    assert len(single.units) == 1 and len(per.units) == 2
    assert single.all_source_paths() == per.all_source_paths() == ["macros/def.sv", "macros/use.sv"]
    assert [u.sources[0].path for u in per.units] == ["macros/def.sv", "macros/use.sv"]
    assert single.config_id != per.config_id
    # same bytes, so same snapshot; the *build* must differ because config differs
    s1 = capture_snapshot(ws.ctx(), single).snapshot_id
    s2 = capture_snapshot(ws.ctx(), per).snapshot_id
    assert s1 == s2
    tc = sha256_hex("tc")
    assert compute_build_id(s1, single.config_id, tc) != compute_build_id(s2, per.config_id, tc)
    # adapter input reflects the CU boundary
    a_single = to_slang_args(single, Path("/snap/files"))
    a_per = to_slang_args(per, Path("/snap/files"))
    assert "--single-unit" in a_single and "--single-unit" not in a_per
    assert a_single[-2:] == ["/snap/files/macros/def.sv", "/snap/files/macros/use.sv"]
    assert a_per[-2:] == a_single[-2:]


# --------------------------------------------------------------------------- BLD-07
def test_BLD_07_include_dir_order_changes_config_and_snapshot(ws: Ws) -> None:
    ws.write("inc_a/common.svh", "`define COMMON_FROM_A 1\n")
    src = [{"kind": "file", "path": "rtl/a.sv"}]
    ab = resolve(ws, source_inputs=src, include_dirs=["inc_a", "inc_b"])
    ba = resolve(ws, source_inputs=src, include_dirs=["inc_b", "inc_a"])
    assert [d.path for d in ab.units[0].include_dirs] == ["inc_a", "inc_b"]
    assert [d.path for d in ba.units[0].include_dirs] == ["inc_b", "inc_a"]
    assert ab.config_id != ba.config_id
    assert capture_snapshot(ws.ctx(), ab).snapshot_id != capture_snapshot(ws.ctx(), ba).snapshot_id
    assert to_slang_args(ab, Path("/s"))[:5] == [
        "--single-unit",
        "-I",
        "/s/inc_a",
        "-I",
        "/s/inc_b",
    ]
    # swapping order inside a filelist behaves the same
    ws.write("lists/i1.f", "+incdir+inc_a+inc_b\nrtl/a.sv\n")
    ws.write("lists/i2.f", "+incdir+inc_b+inc_a\nrtl/a.sv\n")
    assert (
        resolve(ws, source_inputs=fl("lists/i1.f")).config_id
        != resolve(ws, source_inputs=fl("lists/i2.f")).config_id
    )


def test_declared_includes_precede_filelist_includes(ws: Ws) -> None:
    ws.write("lists/i3.f", "-I inc_b\nrtl/a.sv\n")
    nm = resolve(ws, source_inputs=fl("lists/i3.f"), include_dirs=["inc_a"])
    assert [d.path for d in nm.units[0].include_dirs] == ["inc_a", "inc_b"]
    assert nm.units[0].include_dirs[0].origin.field == "profiles.p.include_dirs[0]"
    assert nm.units[0].include_dirs[1].origin.file == "lists/i3.f"
    ws.write("lists/i4.f", "-Iinc_b\nrtl/a.sv\n")
    assert resolve(ws, source_inputs=fl("lists/i4.f")).units[0].include_dirs[0].path == "inc_b"


# --------------------------------------------------------------------------- BLD-08
def test_BLD_08_library_inputs_order_and_new_member(ws: Ws) -> None:
    nm = resolve(ws, source_inputs=fl("lists/libs.f"))
    u = nm.units[0]
    assert [x.path for x in u.library_dirs] == ["libs/lib_a", "libs/lib_b"]
    assert [x.path for x in u.library_files] == ["rtl/b.sv"]
    assert u.library_extensions == [".v", ".sv", ".vh"]
    swapped = resolve(
        ws,
        source_inputs=[{"kind": "file", "path": "rtl/a.sv"}],
        library_dirs=["libs/lib_b", "libs/lib_a"],
    )
    orig = resolve(
        ws,
        source_inputs=[{"kind": "file", "path": "rtl/a.sv"}],
        library_dirs=["libs/lib_a", "libs/lib_b"],
    )
    assert swapped.config_id != orig.config_id
    ctx = ws.ctx()
    snap_orig = capture_snapshot(ctx, orig)
    assert capture_snapshot(ctx, swapped).snapshot_id != snap_orig.snapshot_id
    # the files that library search could load are traceable in the snapshot
    paths = {f.logical_path: f.role for f in snap_orig.files}
    assert paths["libs/lib_a/libmod.sv"] == "library_lookup"
    assert paths["libs/lib_b/libmod.sv"] == "library_lookup"
    args = to_slang_args(orig, Path("/s"))
    assert args[args.index("-y") : args.index("-y") + 4] == [
        "-y",
        "/s/libs/lib_a",
        "-y",
        "/s/libs/lib_b",
    ]
    assert args.count("--libext") == 2
    # a new, higher priority candidate in a library dir changes the snapshot
    ws.write("libs/lib_a/newmod.sv", "module newmod; endmodule\n")
    again = capture_snapshot(ws.ctx(), orig)
    assert again.snapshot_id != snap_orig.snapshot_id
    assert "libs/lib_a/newmod.sv" in {f.logical_path for f in again.files}


# --------------------------------------------------------------------------- BLD-09
def test_BLD_09_generated_input_declared_but_missing_blocks(ws: Ws) -> None:
    e = blocked(ws, ErrorCode.MISSING_INPUT, generated_inputs=[{"path": "gen/regs.sv"}])
    assert e.details["issues"][0]["reason"] == "generated_input_missing"
    assert not (ws.root / "gen").exists()  # nothing was created or executed
    ws.write("lists/g.f", "rtl/a.sv\ngen/regs.sv\n")
    e = blocked(
        ws,
        ErrorCode.MISSING_INPUT,
        source_inputs=fl("lists/g.f"),
        generated_inputs=[{"path": "gen/regs.sv", "generator_command_id": "gen-regs"}],
    )
    assert {i["reason"] for i in e.details["issues"]} == {"generated_input_missing"}
    assert len(e.details["issues"]) == 2
    assert not (ws.root / "gen" / "regs.sv").exists()


def test_BLD_09_generated_input_present_records_provenance(ws: Ws) -> None:
    ws.write("gen/regs.sv", "module regs; endmodule\n")
    digest = sha256_hex(b"module regs; endmodule\n")
    nm = resolve(ws, generated_inputs=[{"path": "gen/regs.sv"}])
    g = nm.generated_inputs[0]
    assert g.provenance_incomplete is True and g.sha256 == digest
    nm2 = resolve(
        ws,
        generated_inputs=[
            {"path": "gen/regs.sv", "generator_command_id": "gen-regs", "output_hash": digest}
        ],
    )
    assert nm2.generated_inputs[0].provenance_incomplete is False
    assert nm2.config_id != nm.config_id
    e = blocked(
        ws,
        ErrorCode.MISSING_INPUT,
        generated_inputs=[
            {"path": "gen/regs.sv", "generator_command_id": "g", "output_hash": "0" * 64}
        ],
    )
    assert e.details["issues"][0]["reason"] == "generated_input_hash_mismatch"
    snap = capture_snapshot(ws.ctx(), nm)
    assert {f.logical_path: f.role for f in snap.files}["gen/regs.sv"] == "generated"


# --------------------------------------------------------------------------- BLD-10
def test_BLD_10_same_source_in_multiple_groups_not_deduped(ws: Ws) -> None:
    grp = lambda gid, dfn: {  # noqa: E731
        "id": gid,
        "sources": [{"kind": "file", "path": "macros/use.sv"}],
        "defines": [{"name": "MODE", "value": dfn}],
    }
    nm = resolve(
        ws,
        source_inputs=[],
        compilation_units={"mode": "explicit_groups", "groups": [grp("g1", "1"), grp("g2", "2")]},
    )
    assert nm.cu_mode == "explicit_groups"
    assert [u.id for u in nm.units] == ["cu_g1", "cu_g2"]
    assert [u.sources[0].path for u in nm.units] == ["macros/use.sv", "macros/use.sv"]
    assert [u.defines[0].value for u in nm.units] == ["1", "2"]  # different macro contexts
    other = resolve(
        ws,
        source_inputs=[],
        compilation_units={"mode": "explicit_groups", "groups": [grp("g1", "1"), grp("g2", "3")]},
    )
    assert other.config_id != nm.config_id
    # group order is significant
    rev = resolve(
        ws,
        source_inputs=[],
        compilation_units={"mode": "explicit_groups", "groups": [grp("g2", "2"), grp("g1", "1")]},
    )
    assert rev.config_id != nm.config_id
    snap = capture_snapshot(ws.ctx(), nm)
    assert [f.logical_path for f in snap.files].count("macros/use.sv") == 1  # bytes stored once
    with pytest.raises(SvwError) as ei:
        to_slang_args(nm, Path("/s"))
    assert ei.value.code == ErrorCode.CAPABILITY_MISMATCH


def test_BLD_10_same_file_twice_in_single_mode_keeps_both_occurrences(ws: Ws) -> None:
    ws.write("lists/dup.f", "rtl/a.sv\nrtl/a.sv\n")
    nm = resolve(ws, source_inputs=fl("lists/dup.f"))
    assert [s.origin.line for s in nm.units[0].sources] == [1, 2]


def test_explicit_group_library_and_includes(ws: Ws) -> None:
    g = {
        "id": "g",
        "sources": fl("lists/libs.f"),
        "include_dirs": ["inc_a"],
        "library_dirs": ["libs/lib_b"],
        "library_files": ["rtl/b.sv"],
    }
    nm = resolve(ws, source_inputs=[], compilation_units={"mode": "explicit_groups", "groups": [g]})
    u = nm.units[0]
    assert [d.path for d in u.include_dirs] == ["inc_a"]
    assert [x.path for x in u.library_dirs] == ["libs/lib_b", "libs/lib_a", "libs/lib_b"]
    assert u.group == "g"


# --------------------------------------------------------------------------- macros / params
def test_conflicting_defines_rejected_identical_allowed(neg: Ws, ws: Ws) -> None:
    e = blocked(neg, ErrorCode.INVALID_CONFIG, source_inputs=fl("lists/conflict.f"))
    assert e.details["reason"] == "conflicting_define" and e.details["name"] == "X"
    assert e.details["first"]["line"] == 2 and e.details["second"]["line"] == 3
    ws.write("lists/d1.f", "+define+K=1\n-DK=1\nrtl/a.sv\n")
    nm = resolve(ws, source_inputs=fl("lists/d1.f"))
    assert [(d.name, d.value) for d in nm.units[0].defines] == [("K", "1")]
    assert [e.classification for e in nm.ledger if e.kind == "define"] == [
        None,
        "duplicate_identical",
    ]
    # profile-level define vs filelist define
    e = blocked(
        ws,
        ErrorCode.INVALID_CONFIG,
        source_inputs=fl("lists/d1.f"),
        defines=[{"name": "K", "value": "2"}],
    )
    assert e.details["reason"] == "conflicting_define"
    # `K` (no value) differs from `K=` (empty): treated as conflicting, never merged
    ws.write("lists/d2.f", "+define+K\n+define+K=\nrtl/a.sv\n")
    blocked(ws, ErrorCode.INVALID_CONFIG, source_inputs=fl("lists/d2.f"))


def test_define_value_token_exact_with_equals_and_escape(ws: Ws) -> None:
    ws.write("lists/d3.f", 'rtl/a.sv\n-DEXPR=a=b\n-DQ="say \\"hi\\""\n')
    d = {x.name: x.value for x in resolve(ws, source_inputs=fl("lists/d3.f")).units[0].defines}
    assert d == {"EXPR": "a=b", "Q": '"say \\"hi\\""'}


def test_top_parameters_dedupe_and_conflict(ws: Ws) -> None:
    tp = lambda v: {"top": "a", "name": "W", "value": v}  # noqa: E731
    nm = resolve(ws, top_parameters=[tp("8"), tp("8")])
    assert [(t.name, t.value) for t in nm.top_parameters] == [("W", "8")]
    e = blocked(ws, ErrorCode.INVALID_CONFIG, top_parameters=[tp("8"), tp("9")])
    assert e.details["reason"] == "conflicting_parameter"


def test_SNP_04_defines_and_params_change_config_id_only(ws: Ws) -> None:
    base = resolve(ws)
    with_def = resolve(ws, defines=[{"name": "X", "value": "1"}])
    with_param = resolve(ws, top_parameters=[{"top": "a", "name": "W", "value": "4"}])
    ids = {base.config_id, with_def.config_id, with_param.config_id}
    assert len(ids) == 3
    ctx = ws.ctx()
    assert len({capture_snapshot(ctx, n).snapshot_id for n in (base, with_def, with_param)}) == 1


def test_config_id_ignores_filelist_whitespace_and_comments(ws: Ws) -> None:
    ws.write("lists/w1.f", "rtl/a.sv\nrtl/b.sv\n")
    ws.write("lists/w2.f", "// comment\n\n   rtl/a.sv   \n\n# c\nrtl/b.sv // x\n")
    # different filelist files, same effective semantics -> same config_id
    a = resolve(ws, source_inputs=fl("lists/w1.f"))
    b = resolve(ws, source_inputs=fl("lists/w2.f"))
    assert a.semantics()["units"][0]["sources"] == b.semantics()["units"][0]["sources"]
    assert a.config_id == b.config_id
    # source order IS semantic
    ws.write("lists/w3.f", "rtl/b.sv\nrtl/a.sv\n")
    assert resolve(ws, source_inputs=fl("lists/w3.f")).config_id != a.config_id


def test_ignored_options_do_not_change_config_id(ws: Ws) -> None:
    ws.write("lists/n1.f", "rtl/a.sv\n")
    ws.write("lists/n2.f", "rtl/a.sv\n-nologo\n-l x.log\n-q\n")
    a = resolve(ws, source_inputs=fl("lists/n1.f"))
    b = resolve(ws, source_inputs=fl("lists/n2.f"))
    assert [i.option for i in b.ignored_options] == ["-nologo", "-l", "-q"]
    assert a.config_id == b.config_id


# --------------------------------------------------------------------------- outputs
def test_write_manifest_and_slang_args(ws: Ws) -> None:
    nm = resolve(
        ws,
        source_inputs=fl("lists/main.f"),
        top_modules=["a", "b"],
        top_parameters=[
            {"top": "a", "name": "W", "value": "8"},
            {"top": "b", "name": "W", "value": "8"},
        ],
    )
    path = write_manifest(ws.ctx(), nm)
    assert path == ws.root / ".svweave" / "manifests" / f"{nm.config_id}.json"
    data = json.loads(path.read_text())
    assert data["config_id"] == nm.config_id and data["ledger"][0]["seq"] == 0
    assert not list(path.parent.glob(".*.tmp"))
    args = to_slang_args(nm, Path("/snap/files"))
    assert args[0] == "--single-unit"
    assert ["-D", 'MSG="hello world"'] in [args[i : i + 2] for i in range(len(args) - 1)]
    assert args.count("-G") == 2 and "a.W=8" in args and "b.W=8" in args  # top-scoped, never global
    assert args[args.index("--top") + 1] == "a" and args.count("--top") == 2
    assert all(isinstance(a, str) for a in args)
    assert args[-3:] == [
        "/snap/files/rtl/a.sv",
        "/snap/files/rtl/b.sv",
        "/snap/files/rtl/with space/sp.sv",
    ]  # one argv item per path, spaces intact


def test_slang_args_conflicting_param_across_tops_and_external_root(ws: Ws) -> None:
    nm = resolve(
        ws,
        top_modules=["a", "b"],
        top_parameters=[
            {"top": "a", "name": "W", "value": "8"},
            {"top": "b", "name": "W", "value": "9"},
        ],
    )
    args = to_slang_args(nm, Path("/s"))  # B5: scoped -G expresses per-top values
    assert [x for x in args if x.endswith(".W=8") or x.endswith(".W=9")] == ["a.W=8", "b.W=9"]
    nm2 = nm.model_copy(update={"top_parameters": []})
    nm2.units[0].sources[0].path = "@uvm/pkg.sv"  # external root -> _ext/<id>/
    assert to_slang_args(nm2, Path("/s"))[-3] == "/s/_ext/uvm/pkg.sv"


def test_manifest_models_roundtrip_json(ws: Ws) -> None:
    nm = resolve(ws, source_inputs=fl("lists/main.f"))
    again = type(nm).model_validate_json(nm.model_dump_json())
    assert again == nm and again.compute_config_id() == nm.config_id


def test_unused_helper_manifest_dict() -> None:
    assert manifest_dict({"x": {}})["project"]["default_profile"] == "x"


def test_example_svweave_yaml_profiles_resolve_against_demo_fixtures(tmp_path: Path) -> None:
    repo = FIXTURES.parent
    if not (FIXTURES / "demo" / "filelists" / "rtl.f").is_file():
        pytest.skip("fixtures/demo not present")
    root = tmp_path / "repo"
    shutil.copytree(FIXTURES / "demo", root / "fixtures" / "demo")
    (root / "fixtures" / "uvm").mkdir(parents=True)
    shutil.copy(repo / "svweave.yaml", root / "svweave.yaml")
    w = Ws(root)
    w.policy()
    ctx = w.ctx()  # source_roots include only fixtures/**, cwd "." is just a base
    a = resolve_profile(ctx, "rtl_demo", {})
    b = resolve_profile(ctx, "rtl_demo_l4", {})
    assert a.config_id != b.config_id  # LANES=2 vs LANES=4
    assert [t.value for t in a.top_parameters] == ["32", "2"]
    assert [t.value for t in b.top_parameters] == ["32", "4"]
    assert capture_snapshot(ctx, a).snapshot_id == capture_snapshot(ctx, b).snapshot_id
    assert [u.defines[0].name for u in a.units] == ["SVW_DEMO"]
    with pytest.raises(SvwError) as ei:  # external root "uvm" is not mapped
        resolve_profile(ctx, "uvm_smoke", {})
    assert ei.value.code == ErrorCode.MISSING_INPUT


# --------------------------------------------------------------------------- compat: vcs
def test_language_option_compat_vcs_is_explicit_recorded_and_off_by_default(ws: Ws) -> None:
    base = resolve(ws, source_inputs=[{"kind": "file", "path": "rtl/a.sv"}])
    vcs = resolve(
        ws, source_inputs=[{"kind": "file", "path": "rtl/a.sv"}], language_options={"compat": "vcs"}
    )
    assert "--compat" not in to_slang_args(base, ws.root)
    args = to_slang_args(vcs, ws.root)
    assert args[args.index("--compat") + 1] == "vcs"
    assert vcs.config_id != base.config_id  # strict and relaxed builds never share an identity
    assert not any("--compat vcs" in d for d in base.semantic_deviations)
    assert any("compat=vcs" in d for d in vcs.semantic_deviations)
    assert any("weaker evidence" in d for d in vcs.semantic_deviations)


def test_language_option_compat_rejects_other_vendors_and_free_text(ws: Ws) -> None:
    for bad in ("questa", "vcs --allow-hierarchical-const", ""):
        ws.config(
            {"p": profile(source_inputs=[{"kind": "file", "path": "rtl/a.sv"}],
                          language_options={"compat": bad})}
        )
        with pytest.raises(SvwError):
            resolve_profile(ws.ctx(), "p", {})
