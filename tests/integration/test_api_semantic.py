"""API-02 (pagination / cursors / response cap), API-03 (timeout, crash, OOM, cancel with the
worker tree reaped), SNP-06 on the real worker, and the dependency-leak BLOCKED rule."""

from __future__ import annotations

import json
import os
import re
import signal
import threading
import time
from pathlib import Path

import pytest

from svweave.adapters.slang.runner import WorkerHandle
from svweave.application import queries as Q
from svweave.application.build import cancel_job
from svweave.application.runner import exit_code
from svweave.domain.cursor import MAX_RESPONSE_BYTES, encode_cursor
from svweave.domain.errors import ErrorCode, SvwError
from svweave.storage.lock import workspace_lock
from tests.conftest import Ws
from tests.integration.helpers import (
    UVM_SRC,
    all_items,
    build,
    dbq,
    lang_ws,
    opts,
    uvm_ws,
)

pytestmark = pytest.mark.integration


def alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    # a zombie child of ours would still answer; read its state
    try:
        st = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()[0]
    except OSError:
        return False
    return st != "Z"


def sleepers() -> set[int]:
    """Host pids of the fault-injection ``sleep 3600`` descendants (the worker runs in its own PID
    namespace under bwrap, so the pid it prints is not a host pid)."""
    out: set[int] = set()
    for d in Path("/proc").iterdir():
        if d.name.isdigit():
            try:
                live = (d / "stat").read_text().rsplit(")", 1)[1].split()[0] != "Z"
                if live and (d / "cmdline").read_bytes() == b"sleep\x003600\x00":
                    out.add(int(d.name))
            except OSError:
                pass
    return out


def no_new_sleepers(before: set[int]) -> bool:
    for _ in range(50):
        if not (sleepers() - before):
            return True
        time.sleep(0.1)
    return False


def child_pid(stderr_tail: str) -> int:
    m = re.search(r"FAULT_CHILD_PID=(\d+)", stderr_tail)
    assert m, stderr_tail
    return int(m.group(1))


# ---------------------------------------------------------------------------- API-02
def test_API_02_cursor_is_bound_to_build_query_and_key(tmp_path: Path) -> None:
    w = lang_ws(tmp_path, "sem09_escaped", "sem09_top")
    e1 = build(w)
    b1 = e1.build_id
    assert b1
    page1 = Q.hierarchy(opts(w), b1, "r", limit=3)
    assert page1.pagination.truncated and page1.pagination.next_cursor
    assert page1.data["total_matches"] == 8 and len(page1.data["items"]) == 3
    # follow to the end: no overlap, nothing lost, stable order
    items, pages = all_items(Q.hierarchy, opts(w), b1, "r", limit=3)
    assert pages == 3 and len({i["instance_id"] for i in items}) == 8 == len(items)
    assert [i["path"] for i in items][0] == "sem09_top"
    # same cursor, different query on the same build -> rejected
    with pytest.raises(SvwError) as ei:
        Q.hierarchy(opts(w), b1, "r", limit=3, depth=1, cursor=page1.pagination.next_cursor)
    assert ei.value.code == ErrorCode.INVALID_CONFIG and "different query" in ei.value.message
    # same query, different build (the workspace changed -> new snapshot -> new build)
    (w.root / "top.sv").write_text((w.root / "top.sv").read_text() + "// touched\n")
    e2 = build(w)
    assert e2.build_id and e2.build_id != b1
    with pytest.raises(SvwError) as ei:
        Q.hierarchy(opts(w), e2.build_id, "r", limit=3, cursor=page1.pagination.next_cursor)
    assert "different build" in ei.value.message
    # tampered / foreign tokens
    tok = page1.pagination.next_cursor or ""
    for bad in (tok[:-2] + "AA", "garbage", tok.split(".")[0], ""):
        if bad == "":
            continue
        with pytest.raises(SvwError):
            Q.hierarchy(opts(w), b1, "r", limit=3, cursor=bad)
    # a validly signed cursor past the end: empty page, not 'complete'
    ctx = opts(w).load()
    key = Q._cursor_key(ctx)
    from svweave.domain.cursor import query_hash

    qh = query_hash("hierarchy", {"instance": None, "top": None, "depth": None})
    cur = encode_cursor(key, b1, qh, "preorder", 50)
    empty = Q.hierarchy(opts(w), b1, "r", limit=3, cursor=cur)
    assert empty.data["items"] == [] and empty.data["total_matches"] == 8
    assert empty.pagination.truncated is False and empty.pagination.next_cursor is None
    assert any("page is empty" in x for x in empty.warnings)
    # limit rules
    for lim in (0, 501, -1):
        with pytest.raises(SvwError) as ei:
            Q.hierarchy(opts(w), b1, "r", limit=lim)
        assert ei.value.code == ErrorCode.INVALID_CONFIG
    assert Q.hierarchy(opts(w), b1, "r", limit=500).pagination.limit == 500
    assert Q.hierarchy(opts(w), b1, "r").pagination.limit == 100


@pytest.mark.skipif(not (UVM_SRC / "uvm_pkg.sv").is_file(), reason="pinned UVM library missing")
def test_API_02_response_cap_truncates_with_valid_json_and_a_cursor(tmp_path: Path) -> None:
    w = uvm_ws(tmp_path, ["svw_demo_uvm_pkg.sv", "svw_uvm_top.sv"], "svw_uvm_top")
    env = build(w)
    bid = env.build_id
    assert bid
    rows = dbq(
        w,
        'SELECT target_id, COUNT(*) n FROM "references" WHERE build_id=? AND target_id IS NOT NULL '
        "GROUP BY target_id ORDER BY n DESC LIMIT 1",
        bid,
    )
    sym = dbq(
        w,
        "SELECT symbol_id FROM declarations WHERE build_id=? AND declaration_id=?",
        bid,
        rows[0]["target_id"],
    )[0]
    total = rows[0]["n"]
    assert total > 500
    first = Q.references(opts(w), bid, sym["symbol_id"], "r", limit=500)
    text = first.to_json(indent=None)
    assert len(text.encode()) <= MAX_RESPONSE_BYTES
    json.loads(text)  # never invalid JSON
    assert first.pagination.truncated and first.pagination.next_cursor
    assert first.data["returned"] < 500  # the size cap bit before the item cap
    assert first.data["total_matches"] == total
    assert any("truncated" in x for x in first.warnings)
    items, pages = all_items(Q.references, opts(w), bid, sym["symbol_id"], "r", limit=500)
    assert len(items) == total and pages > 1
    assert len({i["reference_id"] for i in items}) == total
    # an unbounded text field can never break the cap: every page is <= cap
    cur = None
    while True:
        e = Q.references(opts(w), bid, sym["symbol_id"], "r", limit=500, cursor=cur)
        assert len(e.to_json(indent=None).encode()) <= MAX_RESPONSE_BYTES
        cur = e.pagination.next_cursor
        if cur is None:
            break


# ---------------------------------------------------------------------------- API-03
def cfg(w: Ws, **analysis: int) -> None:
    """Rewrite svweave.yaml with an analysis section (limits are configuration, faults are not)."""
    import yaml

    p = w.root / "svweave.yaml"
    d = yaml.safe_load(p.read_text())
    d["analysis"] = analysis
    p.write_text(yaml.safe_dump(d, sort_keys=False))


def test_API_03_timeout_kills_the_worker_tree_and_publishes_nothing(tmp_path: Path) -> None:
    w = lang_ws(tmp_path, "sem01_scopes", "sem01_top")
    cfg(w, max_build_seconds=1)
    before = sleepers()
    t0 = time.monotonic()
    env = build(w, test_fault="hang")
    assert time.monotonic() - t0 < 15
    assert env.error is not None and env.error.code == ErrorCode.BUILD_TIMEOUT
    assert exit_code(env) == 4 and env.data["lifecycle"] == "TIMED_OUT"
    assert child_pid(env.data["stderr_tail"])  # the worker's own child process
    assert no_new_sleepers(before)  # whole process tree reaped
    job = dbq(w, "SELECT * FROM jobs")[0]
    assert job["lifecycle"] == "TIMED_OUT" and "BUILD_TIMEOUT" in job["error"]
    assert dbq(w, "SELECT 1 FROM builds") == [] and dbq(w, "SELECT 1 FROM published_pointer") == []
    # no residual lock: an exclusive non-blocking lock is available right away
    with workspace_lock(opts(w).load().state_dir, exclusive=True, blocking=False):
        pass
    # the gateway process is fine and a normal build works afterwards
    ok = build(w)
    assert ok.error is None and ok.data["published"] is True


@pytest.mark.parametrize(("fault", "sig"), [("abort", signal.SIGABRT), ("segv", signal.SIGSEGV)])
def test_API_03_native_crash_fails_the_job_but_not_the_gateway(
    tmp_path: Path, fault: str, sig: signal.Signals
) -> None:
    w = lang_ws(tmp_path, "sem01_scopes", "sem01_top")
    good = build(w)
    assert good.data["published"] is True
    pub = dbq(w, "SELECT * FROM published_pointer")
    # change the source so the next build has a different identity, then crash it
    (w.root / "top.sv").write_text((w.root / "top.sv").read_text() + "// v2\n")
    env = build(w, test_fault=fault)
    assert env.error is not None and env.error.code == ErrorCode.BACKEND_UNAVAILABLE
    assert env.data["signal"] == int(sig) and env.data["lifecycle"] == "FAILED"
    job = [j for j in dbq(w, "SELECT * FROM jobs ORDER BY rowid") if j["build_id"] == env.build_id][
        0
    ]
    assert job["lifecycle"] == "FAILED" and job["exit_code"] == -int(sig)
    # SNP-06: the previous complete published build is untouched; no half-written rows
    assert dbq(w, "SELECT * FROM published_pointer") == pub
    assert dbq(w, "SELECT 1 FROM builds WHERE build_id=?", env.build_id) == []
    assert dbq(w, "SELECT 1 FROM declarations WHERE build_id=?", env.build_id) == []
    again = build(w)  # gateway survived and can build the same inputs successfully
    assert again.build_id == env.build_id and again.data["published"] is True


def test_API_03_exit_code_and_oom_are_recorded(tmp_path: Path) -> None:
    w = lang_ws(tmp_path, "sem01_scopes", "sem01_top")
    env = build(w, test_fault="exit3")
    assert (
        env.data["exit_code"] == 3 and env.error and env.error.code == ErrorCode.BACKEND_UNAVAILABLE
    )
    cfg(w, max_worker_memory_mib=512, max_build_seconds=60)
    oom = build(w, test_fault="oom")
    assert oom.error is not None and oom.error.code == ErrorCode.RESOURCE_LIMIT
    assert exit_code(oom) == 4
    assert dbq(w, "SELECT 1 FROM builds") == []


def test_API_03_cancel_kills_the_tree_and_marks_the_job_cancelled(tmp_path: Path) -> None:
    w = lang_ws(tmp_path, "sem01_scopes", "sem01_top")
    started = threading.Event()
    seen: dict[str, object] = {}
    result: dict[str, object] = {}

    def on_job(job_id: str, handle: WorkerHandle) -> None:
        seen["job"], seen["handle"] = job_id, handle
        started.set()

    def run() -> None:
        result["env"] = build(w, test_fault="hang", on_job=on_job)

    before = sleepers()
    t = threading.Thread(target=run)
    t.start()
    assert started.wait(10)
    time.sleep(1.0)  # let the worker start its child
    assert cancel_job(str(seen["job"])) is True
    t.join(15)
    assert not t.is_alive()
    env = result["env"]
    assert env.data["lifecycle"] == "CANCELLED"  # type: ignore[attr-defined]
    assert child_pid(env.data["stderr_tail"])  # type: ignore[attr-defined]
    assert no_new_sleepers(before)
    pgid = seen["handle"].pgid  # type: ignore[attr-defined]
    with pytest.raises(ProcessLookupError):
        os.killpg(pgid, 0)  # nothing of the process group is left
    assert dbq(w, "SELECT lifecycle FROM jobs")[0]["lifecycle"] == "CANCELLED"
    assert dbq(w, "SELECT 1 FROM builds") == []
    assert cancel_job(str(seen["job"])) is False  # already gone from the registry
    with workspace_lock(opts(w).load().state_dir, exclusive=True, blocking=False):
        pass


# ---------------------------------------------------------------------------- dependency leak
def test_outside_absolute_include_is_unreadable_in_the_worker_sandbox(tmp_path: Path) -> None:
    """A2: the host file is not READ at all (file-not-found inside the sandbox) and nothing of its
    content reaches the run artifacts or the database."""
    marker = "OUTSIDE_SECRET_MARKER_77c1"
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "hdr.svh").write_text(f"`define {marker} 1\nnot sv at all {marker}\n")
    w = lang_ws(tmp_path / "w", "sem01_scopes", "sem01_top")
    top = w.root / "top.sv"
    top.write_text(f'`include "{outside}/hdr.svh"\n' + top.read_text())
    env = build(w)
    assert env.data.get("violations", []) == []  # nothing was read outside: no leak to block
    assert env.analysis_coverage is not None and env.analysis_coverage.completeness != "COMPLETE"
    blob = env.model_dump_json()
    for f in (w.root / ".svweave").rglob("*"):
        if f.is_file() and f.suffix in {".json", ".jsonl", ".txt", ".log", ".f"}:
            blob += f.read_text(errors="replace")
    assert marker not in blob
    diag = " ".join(str(r) for r in dbq(w, "SELECT * FROM diagnostics"))
    assert marker not in diag and ("hdr.svh" in diag or "hdr.svh" in blob)


def test_dependency_outside_the_snapshot_blocks_analysis_and_is_never_published(
    tmp_path: Path,
) -> None:
    # The worker runs in a bwrap sandbox (A2): a file of the host outside the snapshot is not even
    # visible, so only a file the sandbox itself exposes (a minimal /etc entry) can still be READ
    # and reach the post-hoc dependency check.
    visible = Path("/etc/nsswitch.conf")
    if not visible.is_file():
        pytest.skip("no /etc/nsswitch.conf on this host")
    w = lang_ws(tmp_path / "w", "sem01_scopes", "sem01_top")
    top = w.root / "top.sv"
    top.write_text(f'`include "{visible}"\n' + top.read_text())
    env = build(w)
    assert env.outcome == "BLOCKED" and env.error is not None
    assert env.error.code == ErrorCode.PATH_NOT_ALLOWED and exit_code(env) == 6
    assert env.analysis_coverage is not None and env.analysis_coverage.completeness == "BLOCKED"
    assert env.data["published"] is False
    assert [v["reason"] for v in env.data["violations"]] == ["read_outside_snapshot"]
    b = dbq(w, "SELECT * FROM builds")[0]
    assert b["completeness"] == "BLOCKED"
    assert dbq(w, "SELECT 1 FROM published_pointer") == []
    # no semantic rows are kept for an unreproducible build
    for t in ("declarations", '"references"', "instances", "assertions"):
        assert dbq(w, f"SELECT 1 FROM {t}") == []
    # the explicit query refuses to present it as an analysis
    d = Q.hierarchy(opts(w), env.build_id, "r")  # type: ignore[arg-type]
    assert d.outcome == "BLOCKED" and d.data["items"] == []
