"""Fixes for the Opus whole-repository review (edit loop part)."""

from __future__ import annotations

from pathlib import Path

import pytest

from svweave.application import edit as E
from svweave.domain.errors import ErrorCode, SvwError
from svweave.editing.plan import define_spans
from tests.unit._edit_fx import make_ws, prepared, sha


def _range(ws: object, rel: str, start: int, end: int, new: str) -> dict[str, object]:
    data = (ws.root / rel).read_bytes()  # type: ignore[attr-defined]
    return {"kind": "exact_range", "logical_path": rel, "start_byte": start, "end_byte": end,
            "expected_sha256": sha(data[start:end]), "replacement": new}


def _refused(fn: object, *a: object, **kw: object) -> SvwError:
    with pytest.raises(SvwError) as e:
        fn(*a, **kw)  # type: ignore[operator]
    return e.value


def test_OPA_01_the_newline_that_ends_a_define_is_part_of_it(tmp_path: Path) -> None:
    ws = make_ws(tmp_path)
    base = ws.build()
    data = (ws.root / "rtl/defs.svh").read_bytes()
    nl = data.index(b"\n")  # the newline after `define BUMP 1
    # was accepted: "`define BUMP 1" + " + 41\n" silently changed the macro
    e = _refused(prepared, ws, base, [_range(ws, "rtl/defs.svh", nl, nl + 1, " + 41\n")], serena=False)
    assert e.details["reason"] == "macro_definition_edit_unsupported"


def test_OPA_02_defines_are_found_anywhere_on_a_line_and_not_in_comments() -> None:
    d = b"module m; endmodule `define W 8\n/* `define X 1 */\n// `define Y 2\n  `define Z 3\n"
    spans = [d[a:b] for a, b in define_spans(d)]
    assert spans == [b"`define W 8", b"  `define Z 3"]
    # the byte just before a line-start define belongs to it too (deleting it joins the lines)
    txt = b"// note\n`define W 8\n"
    (a, _b), = define_spans(txt)
    assert a == txt.index(b"`define")


def test_OPA_03_replacement_text_may_not_redefine_or_undefine_macros(tmp_path: Path) -> None:
    ws = make_ws(tmp_path)
    base = ws.build()
    data = (ws.root / "rtl/top.sv").read_bytes()
    at = data.index(b"  import p::*;")
    edit = _range(ws, "rtl/top.sv", at, at + 2, "`undef BUMP\n  `define BUMP 99\n  ")
    e = _refused(prepared, ws, base, [edit], serena=False)
    assert e.details["reason"] == "macro_definition_edit_unsupported"
    ok = _range(ws, "rtl/top.sv", at, at + 2, "  // `define in a comment is text\n  ")
    env = prepared(ws, base, [ok], serena=False, key="key-comment-ok")
    assert env.error is None, env.error


def test_OPA_04_an_abort_before_the_lock_cannot_leave_an_orphan_journal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ws = make_ws(tmp_path)
    base = ws.build()
    data = (ws.root / "rtl/top.sv").read_bytes()
    at = data.index(b"4'd1")
    env = prepared(ws, base, [_range(ws, "rtl/top.sv", at, at + 4, "4'd2")], serena=False)
    tid = env.data["transaction_id"]
    v = E.validate(ws.opts, "v", transaction_id=tid)
    from tests.unit._edit_fx import typed_confirm

    E.approve(ws.opts, "a", transaction_id=tid, patch_hash=v.data["patch_hash"], confirm=typed_confirm())
    real = E.appr.verify_approval

    def abort_after_approval_check(*a: object, **kw: object) -> object:
        out = real(*a, **kw)  # the approval is valid ...
        E.abort(ws.opts, "ab", transaction_id=tid)  # ... and an abort lands before the lock
        return out

    monkeypatch.setattr(E.appr, "verify_approval", abort_after_approval_check)
    err = _refused(E.commit, ws.opts, "c", transaction_id=tid, expected_patch_hash=v.data["patch_hash"],
                   request_key="commit-key-01")
    assert err.code is ErrorCode.EDIT_CONFLICT and err.details["reason"] == "state_changed_before_commit"
    assert not list((ws.root / ".svweave" / "journal").glob("*.json"))  # nothing to recover
    assert (ws.root / "rtl/top.sv").read_bytes() == data
    env2 = prepared(ws, base, [_range(ws, "rtl/top.sv", at, at + 4, "4'd3")], serena=False,
                    key="key-after-abort")
    assert env2.error is None, env2.error  # new edits are not blocked by RECOVERY_REQUIRED


# ----------------------------------------------------------------------------- security / config
def test_OPB_02_removing_a_tree_never_chmods_through_a_symlink(tmp_path: Path) -> None:
    import os
    import stat as st

    from svweave.snapshot.capture import remove_snapshot_dir

    secret = tmp_path / "secret"
    secret.mkdir(mode=0o700)
    tree = tmp_path / "snap" / ".tmp-abc"
    tree.mkdir(parents=True)
    os.symlink(secret, tree / "link")
    remove_snapshot_dir(tree)
    assert not tree.exists() and secret.is_dir()
    assert st.S_IMODE(secret.stat().st_mode) == 0o700  # used to become 0755
    os.symlink(secret, tmp_path / "snap" / "aslink")
    remove_snapshot_dir(tmp_path / "snap" / "aslink")  # a link given directly: only the link goes
    assert secret.is_dir() and st.S_IMODE(secret.stat().st_mode) == 0o700


def test_OPB_03_redaction_covers_colon_paths_quoted_keys_auth_headers_and_keys() -> None:
    from svweave.security.redaction import Redactor

    r = Redactor(["/proj"])
    assert "/home/alice" not in r.text("PATH=/home/alice/bin:/home/alice/.local/bin")
    assert "/home/alice" not in r.text("file:/home/alice/secret/x.sv")
    assert "/home/alice" not in r.text("file:///home/alice/secret/x.sv")
    assert "hunter2" not in r.text('{"password": "hunter2"}')
    assert "abcdef0123456789" not in r.text("Authorization: token abcdef0123456789")
    assert "/home/alice" not in str(r.value({"/home/alice/secret/x.sv": 1}))
    # and nothing that is not private is touched
    assert r.text("see https://github.com/a/b/c") == "see https://github.com/a/b/c"
    assert r.text("/proj/rtl/a.sv:12:3 error") == "/proj/rtl/a.sv:12:3 error"
    assert r.text("ratio 3/4/5 and /usr/lib/x/y") == "ratio 3/4/5 and /usr/lib/x/y"


def test_OPB_04_a_project_path_may_not_use_the_reserved_ext_prefix(tmp_path: Path) -> None:
    from tests.conftest import Ws, profile

    w = Ws(tmp_path / "ws")
    w.write("_ext/lib/x.sv", "module x; endmodule\n")
    w.write("rtl/ok.sv", "module ok; endmodule\n")
    w.config({"p": profile(source_inputs=[{"kind": "file", "path": "rtl/ok.sv"}], top_modules=["ok"])})
    w.policy()
    ctx = w.ctx()
    assert ctx.resolve("rtl/ok.sv", ctx.project_root).logical == "rtl/ok.sv"
    with pytest.raises(SvwError) as e:
        ctx.resolve("_ext/lib/x.sv", ctx.project_root)
    assert e.value.code is ErrorCode.PATH_NOT_ALLOWED and "_ext" in e.value.message


def test_OPB_06_a_world_writable_trusted_policy_is_refused(tmp_path: Path) -> None:
    import os

    from svweave.config.loader import load_policy
    from tests.conftest import Ws

    w = Ws(tmp_path / "ws")
    w.write("rtl/ok.sv", "module ok; endmodule\n")
    w.policy()
    p = w.policy_path
    load_policy(p)  # the default (umask) modes are fine, group-write included (private groups)
    os.chmod(p, 0o666)
    with pytest.raises(SvwError) as e:
        load_policy(p)
    assert e.value.code is ErrorCode.POLICY_DENIED and e.value.details["reason"] == "policy_writable_by_others"
    os.chmod(p, 0o600)
    os.chmod(p.parent, 0o777)
    with pytest.raises(SvwError):
        load_policy(p)  # a world-writable directory lets anyone swap the file
    os.chmod(p.parent, 0o700)


# ----------------------------------------------------------------------------- queries / gateway
def test_OPC_05_schema_validation_errors_never_echo_request_values() -> None:
    import svweave.gateway.tools  # noqa: F401  (registers the tools)
    from svweave.gateway.tools import TOOLS

    hostile = "TOPSECRET_" + "a" * 20
    cases = [
        ("svw_hierarchy", {"build_id": "0" * 64, "depth": hostile}),  # type
        ("svw_symbol", {"build_id": "0" * 64, "name": hostile, "symbol_id": "0" * 64}),  # oneOf
        ("svw_hierarchy", {"build_id": "0" * 64, hostile: 1}),  # additionalProperties
    ]
    for tool, args in cases:
        with pytest.raises(SvwError) as e:
            TOOLS[tool].validate(args)
        assert "TOPSECRET" not in str(e.value.details) and "TOPSECRET" not in e.value.message, tool
    with pytest.raises(SvwError) as e:
        TOOLS["svw_hierarchy"].validate({})
    assert "build_id" in str(e.value.details)  # "required" names a schema field: still explained


def test_OPC_07_08_schemas_match_the_handlers() -> None:
    import jsonschema

    from svweave.gateway import schemas

    assert schemas.INPUT["svw_symbol"]["properties"]["limit"]["default"] == 50  # the handler's page
    ev = schemas.INPUT["svw_evidence"]
    ok = {"evidence_id": "0" * 64}
    jsonschema.validate(ok, ev)
    for extra in ({"type": "simulation"}, {"limit": 5}, {"cursor": "x"}):
        with pytest.raises(jsonschema.ValidationError):  # was silently ignored by the handler
            jsonschema.validate({**ok, **extra}, ev)


def test_OPC_06_unknown_freshness_is_not_called_a_workspace_change() -> None:
    import inspect

    from svweave.application import semantic_common as sc

    src = inspect.getsource(sc.build_envelope)
    assert "freshness is UNKNOWN" in src and "could not be determined" in src


# ----------------------------------------------------------------------------- backends / installer
def test_OPD_02_a_fifo_in_the_candidate_never_blocks_and_unreadable_dirs_are_errors(tmp_path: Path) -> None:
    import os
    import time

    from svweave.adapters.serena.guard import tree_hashes

    (tmp_path / "a.sv").write_text("module a; endmodule\n")
    os.mkfifo(tmp_path / "p.sv")
    t0 = time.monotonic()
    h = tree_hashes(tmp_path)
    assert time.monotonic() - t0 < 2  # used to block forever on open()
    assert h["p.sv"].startswith("special:") and len(h["a.sv"]) == 64
    if os.geteuid() != 0:
        d = tmp_path / "evil"
        d.mkdir()
        (d / "x.sv").write_text("x")
        os.chmod(d, 0)
        try:
            with pytest.raises(SvwError) as e:
                tree_hashes(tmp_path)  # used to skip the subtree silently
            assert e.value.details["reason"] == "candidate_unreadable"
        finally:
            os.chmod(d, 0o700)


def _bootstrap() -> object:
    from tests.unit.test_bootstrap_locked import load

    return load()


def test_OPD_07_hardlinks_are_stripped_like_members_and_errors_stay_lockerrors(tmp_path: Path) -> None:
    import io
    import tarfile

    bl = _bootstrap()
    arc = tmp_path / "a.tar.gz"
    with tarfile.open(arc, "w:gz") as t:
        data = b"#!/bin/sh\n"
        ti = tarfile.TarInfo("pkg/bin/tool")
        ti.size = len(data)
        t.addfile(ti, io.BytesIO(data))
        hl = tarfile.TarInfo("pkg/bin/tool2")
        hl.type = tarfile.LNKTYPE
        hl.linkname = "pkg/bin/tool"
        t.addfile(hl)
    bl.safe_extract(arc, tmp_path / "out", 1)  # type: ignore[attr-defined]  # was KeyError
    assert (tmp_path / "out/bin/tool2").read_bytes() == b"#!/bin/sh\n"
    arc2 = tmp_path / "b.tar.gz"
    with tarfile.open(arc2, "w:gz") as t:
        hl = tarfile.TarInfo("pkg/x")
        hl.type = tarfile.LNKTYPE
        hl.linkname = "top"  # stripping 1 level leaves nothing
        t.addfile(hl)
    with pytest.raises(bl.LockError):  # type: ignore[attr-defined]
        bl.safe_extract(arc2, tmp_path / "out2", 1)  # type: ignore[attr-defined]


def test_OPD_04_verify_only_without_the_venv_is_not_ok(tmp_path: Path) -> None:
    import json

    bl = _bootstrap()
    lock = json.loads((Path(bl.REPO) / "toolchain.lock.json").read_text())  # type: ignore[attr-defined]
    report: dict[str, object] = {"errors": [], "components": {}}
    bl.install_wheels(lock, tmp_path / "tc", True, str(tmp_path / "no-venv/bin/python"),  # type: ignore[attr-defined]
                      "uv", report, tmp_path / "art")
    assert any("gateway venv missing" in e for e in report["errors"])  # type: ignore[union-attr]


def test_OPD_09_malformed_serena_output_is_backend_unavailable() -> None:
    from svweave.adapters.serena.adapter import _bad_output, _line_no
    from svweave.security.redaction import Redactor

    assert _line_no(3) == 3 and _line_no("12") == 12
    for bad in ("x", None, 1.5, True, [], {}):
        assert _line_no(bad) is None
    e = _bad_output(Redactor([]), "what", {"start_line": "x"})
    assert e.code is ErrorCode.BACKEND_UNAVAILABLE and e.details["reason"] == "bad_output"


# ----------------------------------------------------------------------------- jobs
def test_OPS_05_a_build_request_with_other_arguments_is_not_merged(tmp_path: Path) -> None:
    import threading

    from svweave.application.jobs import JobManager
    from svweave.application.options import WorkspaceOptions
    from tests.unit._edit_fx import make_ws

    ws = make_ws(tmp_path)
    gate = threading.Event()
    seen: list[object] = []

    def slow_build(opts: object, profile: object, rid: object, **kw: object) -> object:
        seen.append(kw.get("expected_snapshot_id"))
        gate.wait(10)
        raise SvwError(ErrorCode.INTERNAL, "test build stops here")

    jm = JobManager(WorkspaceOptions(root=ws.root), build_fn=slow_build)  # type: ignore[arg-type]
    try:
        first = jm.submit_build("p", "r1")
        again = jm.submit_build("p", "r1b")  # same arguments: joins
        assert again["already_running"] is True and again["job_id"] == first["job_id"]
        with pytest.raises(SvwError) as e:  # different expected_snapshot_id: used to join silently
            jm.submit_build("p", "r2", expected_snapshot_id="c" * 64)
        assert e.value.code is ErrorCode.RESOURCE_LIMIT
        assert e.value.details["reason"] == "different_build_arguments"
    finally:
        gate.set()
        jm.shutdown() if hasattr(jm, "shutdown") else None
