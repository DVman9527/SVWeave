"""M3b review fixes: every test here is a regression test (red without its fix)."""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any

import pytest
from click.testing import CliRunner

from svweave.application import evidence as E
from svweave.application import jobs as jobs_mod
from svweave.application import services
from svweave.application.build import DB_FILENAME, run_build
from svweave.application.debug_bundle import _write, make_bundle, scrub
from svweave.application.events import EventLog
from svweave.application.jobs import JobManager
from svweave.application.keys import cursor_key
from svweave.application.options import WorkspaceOptions
from svweave.application.report import build_report
from svweave.application.session import Session, load_probe, save_probe
from svweave.cli import cli
from svweave.domain.cursor import MAX_RESPONSE_BYTES
from svweave.domain.envelope import Envelope, Outcome
from svweave.domain.errors import ErrorCode, SvwError
from svweave.domain.states import JobLifecycle
from svweave.gateway import tools as T
from svweave.gateway.tools import Gateway, ToolSpec
from svweave.security.redaction import Redactor
from svweave.storage import migrations
from svweave.storage.db import Database
from svweave.storage.lock import build_lock
from tests.conftest import Ws, profile
from tests.unit._gateway_fx import record, stub_build, write_artifact, write_manifest
from tests.unit.test_gateway_jobs import FakeBuild, wait_done

H = "a" * 64


def gw_with_events(opts: WorkspaceOptions) -> tuple[Gateway, EventLog]:
    ctx = opts.load()
    ev = EventLog.for_session(ctx.state_dir, Redactor([ctx.project_root]))
    return Gateway(Session(opts, use_graph=False, events=ev), ev), ev


def insert_evidence(ws: Ws, bid: str, sid: str, n: int, artifacts_bytes: int) -> list[str]:
    """Store evidence rows directly (import caps keep real records small)."""
    ids = []
    with Database(ws.ctx().state_dir / DB_FILENAME) as db:
        for i in range(n):
            eid = hashlib.sha256(f"big{i}".encode()).hexdigest()
            arts = json.dumps([{"path": "p" * artifacts_bytes, "sha256": "0" * 64}])
            db.conn.execute(
                "INSERT INTO evidence(evidence_id, kind, producer, build_id, snapshot_id, "
                "artifacts, trust_class, outcome, evidence_type, profile, test_name, seed, "
                "link_state, record, imported_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    eid,
                    "EXTERNAL_DECLARED",
                    "x",
                    bid,
                    sid,
                    arts,
                    "EXTERNAL_DECLARED",
                    "PASS",
                    "simulation",
                    "p",
                    f"t{i}",
                    str(i),
                    "linked_build",
                    "{}",
                    f"2026-01-01T00:00:0{i}",
                ),
            )
            ids.append(eid)
    return ids


def size(env: Envelope) -> int:
    return len(env.to_json(indent=None).encode("utf-8"))


# ------------------------------------------------------------------------------------------ B1
def test_B1_evidence_listing_respects_the_response_cap_on_every_page(ws: Ws) -> None:
    opts, bid, sid = stub_build(ws)
    ids = insert_evidence(ws, bid, sid, 4, 60_000)  # 4 x 60 KB + static > 128 KiB
    seen: list[str] = []
    cursor = None
    for _ in range(10):
        env = E.get_evidence(opts, "r", build_id=bid, cursor=cursor)
        assert size(env) <= MAX_RESPONSE_BYTES, size(env)
        seen += [i["evidence_id"] for i in env.data["items"]]
        cursor = env.pagination.next_cursor
        if not cursor:
            break
        assert env.pagination.truncated
    assert set(ids) <= set(seen) and len(seen) == len(set(seen)) == 5  # nothing lost or doubled


def test_B1_oversize_item_becomes_a_placeholder_in_listing_and_by_id(ws: Ws) -> None:
    opts, bid, sid = stub_build(ws)
    [eid] = insert_evidence(ws, bid, sid, 1, 400_000)
    listing = E.get_evidence(opts, "r", build_id=bid, evidence_type="simulation")
    assert size(listing) <= MAX_RESPONSE_BYTES
    [ph] = listing.data["items"]
    assert ph["truncated_item"] is True and ph["id"] == eid and ph["reason"] == "item_exceeds_response_cap"
    # B9: truncation is explicit at the envelope level even for the LAST (only) item
    assert listing.pagination.truncated is True
    assert listing.data["oversize_items"][0]["id"] == eid
    assert listing.error is not None and listing.error.code == ErrorCode.OUTPUT_LIMIT.value
    one = E.get_evidence(opts, "r", evidence_id=eid)
    assert size(one) <= MAX_RESPONSE_BYTES
    assert one.data["items"][0]["truncated_item"] is True and one.data["items"][0]["id"] == eid


def test_B1_duplicate_artifacts_are_stored_once(ws: Ws, tmp_path: Path) -> None:
    opts, bid, sid = stub_build(ws)
    rel, digest = write_artifact(ws)
    art = {"path": rel, "sha256": digest}
    many = E.import_manifest(
        opts,
        write_manifest(
            ws.root, record(build_id=bid, snapshot_id=sid, artifacts=[art] * 500), "dup.json"
        ),
        "r",
    )
    one = E.import_manifest(
        opts,
        write_manifest(ws.root, record(build_id=bid, snapshot_id=sid, artifacts=[art]), "one.json"),
        "r",
    )
    a, b = many.data["records"][0], one.data["records"][0]
    assert a["evidence_id"] == b["evidence_id"] and b["status"] == "already_present"
    got = E.get_evidence(opts, "r", evidence_id=a["evidence_id"]).data["items"][0]
    assert got["artifacts"] == [art]


def test_B1_total_artifact_size_is_capped_at_import(ws: Ws) -> None:
    opts, bid, sid = stub_build(ws)
    arts = []
    for i in range(450):
        rel, digest = write_artifact(ws, f"out/{'d' * 120}/f{i}.log", f"x{i}")
        arts.append({"path": rel, "sha256": digest})
    with pytest.raises(SvwError) as ei:
        E.import_manifest(
            opts,
            write_manifest(ws.root, record(build_id=bid, snapshot_id=sid, artifacts=arts)),
            "r",
        )
    assert ei.value.code is ErrorCode.INVALID_CONFIG
    assert ei.value.details["reason"] == "artifacts_too_large"
    with Database(ws.ctx().state_dir / DB_FILENAME) as db:
        assert db.conn.execute("SELECT COUNT(*) FROM evidence").fetchone()[0] == 0


# ---------------------------------------------------------------------------------- B2 / B3
def test_B2_event_log_never_sees_raw_arguments_and_is_private(ws: Ws) -> None:
    opts, bid, sid = stub_build(ws)
    gw, ev = gw_with_events(opts)
    bad = "/home/alice/secret/chip.sv PASSWORD=hunter2 ghp_" + "a" * 30
    out = gw.call("svw_hierarchy", {"build_id": bad})
    assert out.is_error
    ok = gw.call("svw_hierarchy", {"build_id": bid, "limit": 1})
    assert not ok.is_error
    text = ev.path.read_text()
    assert "hunter2" not in text and "/home/alice/secret" not in text and "ghp_" not in text
    recs = [json.loads(x) for x in text.splitlines()]
    assert recs[0]["build_id"] is None  # not validated -> not logged
    assert recs[1]["build_id"] == bid
    assert (os.stat(ev.path).st_mode & 0o777) == 0o600
    assert (os.stat(ev.path.parent).st_mode & 0o777) == 0o700


def test_B2_event_ids_are_redacted_even_if_a_caller_passes_secrets(tmp_path: Path) -> None:
    ev = EventLog(tmp_path / "r" / "events.jsonl")
    ev.emit("c", "e", request_id="/home/x/y/z", job_id="API_TOKEN=abcdef123456", build_id="b")
    rec = json.loads(ev.path.read_text())
    assert "/home/x" not in json.dumps(rec) and "abcdef123456" not in json.dumps(rec)


@pytest.mark.parametrize(
    "bad", [{"a": 1}, ["x"], 5, True, "/home/alice/priv/x TOKEN=abcdef123456 " + "z" * 80]
)
def test_B3_bad_profile_is_a_clean_tool_error_and_never_echoed(ws: Ws, bad: Any) -> None:
    opts, bid, sid = stub_build(ws)
    gw, ev = gw_with_events(opts)
    for tool in ("svw_status", "svw_build"):
        out = gw.call(tool, {"profile": bad})  # must not raise (used to crash into JSON-RPC)
        env = out.envelope
        assert out.is_error and env.error is not None and env.error.code == "INVALID_CONFIG"
        assert env.profile is None
        assert "abcdef123456" not in env.to_json() and "/home/alice/priv" not in env.to_json()
        assert "abcdef123456" not in ev.path.read_text()


def test_B3_validated_profile_is_reported_on_success(ws: Ws) -> None:
    opts, bid, sid = stub_build(ws)
    gw, _ = gw_with_events(opts)
    out = gw.call("svw_status", {"profile": "p"})
    assert not out.is_error and out.envelope.profile == "p"


# ------------------------------------------------------------------------------------------ B4
def test_B4_run_build_refuses_a_second_concurrent_build(ws: Ws) -> None:
    opts = WorkspaceOptions(root=ws.root)
    with build_lock(ws.ctx().state_dir), pytest.raises(SvwError) as ei:
        run_build(opts, "p", "r")
    assert ei.value.code is ErrorCode.RESOURCE_LIMIT
    assert ei.value.details["reason"] == "build_in_progress"
    with Database(ws.ctx().state_dir / DB_FILENAME) as db:  # nothing was started
        assert db.conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0] == 0


def test_B4_cli_build_shares_the_lock_and_exits_4(ws: Ws) -> None:
    with build_lock(ws.ctx().state_dir):
        r = CliRunner().invoke(
            cli, ["--root", str(ws.root), "--compact", "build", "--profile", "p"]
        )
    env = json.loads(r.stdout)
    assert r.exit_code == 4 and env["error"]["code"] == "RESOURCE_LIMIT"


def test_B4_job_manager_sees_a_running_cli_build(ws: Ws) -> None:
    jm = JobManager(WorkspaceOptions(root=ws.root), build_fn=FakeBuild())
    with build_lock(ws.ctx().state_dir), pytest.raises(SvwError) as ei:
        jm.submit_build("p", "r")
    assert ei.value.code is ErrorCode.RESOURCE_LIMIT
    with Database(ws.ctx().state_dir / DB_FILENAME) as db:
        assert db.conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0] == 0


def test_B4_two_managers_cannot_both_insert_an_active_build(
    ws: Ws, monkeypatch: pytest.MonkeyPatch
) -> None:
    ws.config({"p": profile(), "q": profile()})
    real = jobs_mod.new_job_id

    def slow() -> str:
        time.sleep(0.3)  # widens the old check-then-insert window
        return real()

    monkeypatch.setattr(jobs_mod, "new_job_id", slow)
    opts = WorkspaceOptions(root=ws.root)
    Database(opts.load().state_dir / DB_FILENAME).close()
    fa, fb = FakeBuild(), FakeBuild()
    ma, mb = JobManager(opts, build_fn=fa), JobManager(opts, build_fn=fb)
    res: dict[str, Any] = {}

    def go(name: str, jm: JobManager, prof: str) -> None:
        try:
            res[name] = jm.submit_build(prof, "r")["job_id"]
        except SvwError as e:
            res[name] = e.code

    ts = [
        threading.Thread(target=go, args=("A", ma, "p")),
        threading.Thread(target=go, args=("B", mb, "q")),
    ]
    [t.start() for t in ts]
    [t.join() for t in ts]
    with Database(opts.load().state_dir / DB_FILENAME) as db:
        rows = db.conn.execute("SELECT lifecycle FROM jobs WHERE kind='build'").fetchall()
    assert len(rows) == 1, (res, [tuple(r) for r in rows])
    assert sorted(map(str, res.values())).count(str(ErrorCode.RESOURCE_LIMIT)) == 1
    fa.release.set()
    fb.release.set()
    time.sleep(0.5)


def test_B4_migration_is_safe_when_another_process_migrated_first(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    p = tmp_path / "x.db"
    conn = sqlite3.connect(p, isolation_level=None)
    assert migrations.migrate(conn) == migrations.LATEST_VERSION
    real = migrations.current_version
    calls = {"n": 0}

    def stale_first(c: sqlite3.Connection) -> int:
        calls["n"] += 1
        return 0 if calls["n"] == 1 else real(c)  # we read the version BEFORE the other won

    monkeypatch.setattr(migrations, "current_version", stale_first)
    assert migrations.migrate(conn) == migrations.LATEST_VERSION  # used to: table already exists


def test_B4_concurrent_fresh_database_opens(tmp_path: Path) -> None:
    errors: list[BaseException] = []
    for r in range(6):
        path = tmp_path / f"c{r}.db"
        barrier = threading.Barrier(6)

        def open_db(path: Path = path, barrier: threading.Barrier = barrier) -> None:
            try:
                barrier.wait()
                Database(path).close()
            except BaseException as e:  # noqa: BLE001
                errors.append(e)

        ts = [threading.Thread(target=open_db) for _ in range(6)]
        [t.start() for t in ts]
        [t.join() for t in ts]
    assert errors == []


# ------------------------------------------------------------------------------------------ B5
def test_B5_run_build_stops_on_unexpected_snapshot_without_building(ws: Ws) -> None:
    opts = WorkspaceOptions(root=ws.root)
    with pytest.raises(SvwError) as ei:
        run_build(opts, "p", "r", expected_snapshot_id="f" * 64)
    assert ei.value.code is ErrorCode.STALE_SNAPSHOT
    assert ei.value.details["current_snapshot_id"] != "f" * 64
    with Database(ws.ctx().state_dir / DB_FILENAME) as db:
        assert db.conn.execute("SELECT COUNT(*) FROM builds").fetchone()[0] == 0
        assert db.conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0] == 0


def test_B5_manager_passes_expected_and_rechecks_what_was_built(ws: Ws) -> None:
    class Builds(FakeBuild):
        pass

    fb = Builds()
    fb.release.set()
    jm = JobManager(WorkspaceOptions(root=ws.root), build_fn=fb)
    sub = jm.submit_build("p", "r", expected_snapshot_id="e" * 64)
    st = wait_done(jm, sub["job_id"])
    assert fb.calls[0]["expected_snapshot_id"] == "e" * 64  # handed to run_build, not re-captured
    assert st["lifecycle"] == "FAILED" and st["result"]["error"]["code"] == "STALE_SNAPSHOT"


# ------------------------------------------------------------------------------------------ B6
def test_B6_repo_local_probe_file_is_ignored_by_status_and_report(ws: Ws) -> None:
    opts, bid, sid = stub_build(ws)
    ctx = opts.load()
    ctx.state_dir.mkdir(exist_ok=True)
    forged = {
        n: {"status": "READY", "probed": True} for n in ("pyslang", "serena", "codebase-memory-mcp")
    }
    (ctx.state_dir / "backend_probe.json").write_text(json.dumps(forged))
    env = services.status(Session(opts, use_graph=False), "r")
    assert {b["status"] for b in env.data["backends"].values()} == {"NOT_PROBED"}
    rep = build_report(Session(opts, use_graph=False), "r", bid).data["report"]
    assert {b["status"] for b in rep["real_integration"].values()} == {"NOT_PROBED"}


def test_B6_probe_record_is_private_trusted_and_redacted(ws: Ws) -> None:
    opts, bid, sid = stub_build(ws)
    ctx = opts.load()
    save_probe(
        ctx.trusted_dir,
        {
            "serena": {
                "status": "DEGRADED",
                "probed": True,
                "checked_at": "t",
                "reasons": ["failed in /home/alice/secret/proj API_TOKEN=abcdef123456"],
            },
            "pyslang": {"status": "SUPER_READY", "probed": True},  # not a status: dropped
        },
    )
    p = ctx.trusted_dir / "backend_probe.json"
    assert (p.stat().st_mode & 0o777) == 0o600 and str(ctx.project_root) not in str(p)
    assert not (ctx.state_dir / "backend_probe.json").exists()
    p.write_text(p.read_text().replace('"DEGRADED"', '"DEGRADED"'))  # raw file keeps the secret
    assert "abcdef123456" in p.read_text()
    got = load_probe(ctx.trusted_dir)
    assert got is not None and "pyslang" not in got
    assert "abcdef123456" not in json.dumps(got) and "/home/alice/secret" not in json.dumps(got)
    env = services.status(Session(opts, use_graph=False), "r")
    assert "abcdef123456" not in env.to_json()
    gw, _ = gw_with_events(opts)
    assert "abcdef123456" not in gw.call("svw_status", {}).envelope.to_json()


# ------------------------------------------------------------------------------------------ B7
def test_B7_bundle_does_not_follow_symlinks_and_files_are_private(ws: Ws, tmp_path: Path) -> None:
    opts, bid, sid = stub_build(ws)
    ctx = opts.load()
    out = ctx.state_dir / "bundles" / bid[:16]
    out.mkdir(parents=True, mode=0o700)
    victim = tmp_path / "victim.txt"
    victim.write_text("ORIGINAL")
    os.symlink(victim, out / "bundle.json")
    env = make_bundle(opts, bid, "r")
    assert victim.read_text() == "ORIGINAL"  # the link was replaced, not written through
    bdir = ws.root / env.data["bundle_dir"]
    for f in bdir.iterdir():
        assert (f.stat().st_mode & 0o777) == 0o600, f.name
        assert not f.is_symlink()


def test_B7_bundle_output_inside_the_source_tree_is_rejected(ws: Ws) -> None:
    opts, bid, sid = stub_build(ws)
    with pytest.raises(SvwError) as ei:
        make_bundle(opts, bid, "r", ws.root / "rtl_bundle")
    assert ei.value.code is ErrorCode.PATH_NOT_ALLOWED
    assert ei.value.details["reason"] == "bundle_out_in_source_tree"
    assert not (ws.root / "rtl_bundle").exists()
    ok = make_bundle(opts, bid, "r", ws.ctx().state_dir / "mybundles" / "one")
    assert (ws.root / ok.data["bundle_dir"]).is_dir()
    assert ((ws.root / ok.data["bundle_dir"]).stat().st_mode & 0o777) == 0o700
    link = ws.ctx().state_dir / "linkdir"
    link.symlink_to(ws.ctx().state_dir / "mybundles")
    with pytest.raises(SvwError):
        make_bundle(opts, bid, "r", link)


# ------------------------------------------------------------------------------------------ B8
def test_B8_secret_named_defines_are_redacted_at_value_level(tmp_path: Path) -> None:
    red = Redactor([])
    nm = {
        "units": [
            {
                "defines": [
                    ["LICENSE_TOKEN", "s3cr3tvalue"],
                    ["MY_LICENSE", "lic-123"],
                    ["API_KEY", "k-123"],
                    ["WIDTH", "8"],
                ]
            }
        ],
        "defines": [{"name": "DB_PASSWORD", "value": "pw-xyz"}, {"name": "DEPTH", "value": "4"}],
        "args": ["+define+LICENSE_FILE=7788@host", "-DAUTH_X=zzz", "-DWIDTH=8"],
    }
    out = scrub(nm, red)
    blob = json.dumps(out)
    for secret in ("s3cr3tvalue", "lic-123", "k-123", "pw-xyz", "7788@host", "zzz"):
        assert secret not in blob, secret
    assert out["units"][0]["defines"][3] == ["WIDTH", "8"]  # harmless defines are kept
    assert out["defines"][1] == {"name": "DEPTH", "value": "4"}
    written = tmp_path / "w"
    written.mkdir()
    _write(red, written, "n.json", nm)
    assert "s3cr3tvalue" not in (written / "n.json").read_text()


# ------------------------------------------------------------------------------- lower findings
class PublishPhaseBuild:
    """Worker already done (events say so), the job is publishing and ignores cancel."""

    def __init__(self, hold: float, write_finish_event: bool) -> None:
        self.hold, self.write_finish_event = hold, write_finish_event
        self.started = threading.Event()

    def __call__(self, opts: WorkspaceOptions, prof: str, rid: str, **kw: Any) -> Envelope:
        ctx = opts.load()
        with Database(ctx.state_dir / DB_FILENAME) as db:
            db.start_job(kw["job_id"], "b" * 64, "s" * 64)
        if self.write_finish_event:
            d = ctx.state_dir / "runs" / kw["job_id"]
            d.mkdir(parents=True, exist_ok=True)
            (d / "events.jsonl").write_text(
                json.dumps({"component": "worker", "event": "finish", "outcome": "ok"}) + "\n"
            )
        self.started.set()
        time.sleep(self.hold)
        with Database(ctx.state_dir / DB_FILENAME) as db:
            db.finish_job(kw["job_id"], JobLifecycle.SUCCEEDED, exit_code=0)
        return Envelope(request_id=rid, outcome=Outcome.OK, build_id="b" * 64, snapshot_id="s" * 64)


def test_cancel_after_the_worker_finished_reports_not_accepted(ws: Ws) -> None:
    pb = PublishPhaseBuild(1.0, write_finish_event=True)
    jm = JobManager(WorkspaceOptions(root=ws.root), build_fn=pb)
    sub = jm.submit_build("p", "r")
    assert pb.started.wait(5)
    c = jm.cancel(sub["job_id"])
    assert c["cancel_accepted"] is False and "already finished" in c["cancel_note"]
    assert c["cancel_requested"] is False  # nothing was requested, the worker tree was not touched
    st = wait_done(jm, sub["job_id"], 15)
    assert st["lifecycle"] == "SUCCEEDED"


def test_cancel_that_loses_the_race_reports_the_true_state(ws: Ws) -> None:
    pb = PublishPhaseBuild(0.3, write_finish_event=False)  # no event: only the end state tells
    jm = JobManager(WorkspaceOptions(root=ws.root), build_fn=pb)
    sub = jm.submit_build("p", "r")
    assert pb.started.wait(5)
    c = jm.cancel(sub["job_id"])
    assert c["lifecycle"] == "SUCCEEDED" and c["cancel_accepted"] is False
    assert "before the cancel took effect" in c["cancel_note"] and c["cancel_requested"] is False


def test_newlines_and_control_characters_cannot_bypass_the_patterns() -> None:
    h = H
    spec = T.TOOLS
    bad: list[tuple[str, dict[str, Any]]] = [
        ("svw_search", {"snapshot_id": h, "query": "q", "scope": "foo\n"}),
        ("svw_search", {"snapshot_id": h, "query": "q", "scope": "a\n../../etc"}),
        ("svw_search", {"snapshot_id": h, "query": "q\nr"}),
        ("svw_search", {"snapshot_id": h + "\n", "query": "q"}),
        ("svw_hierarchy", {"build_id": h + "\n"}),
        ("svw_hierarchy", {"build_id": h, "top": "a\tb"}),
        ("svw_status", {"profile": "p\n"}),
        ("svw_job", {"job_id": "00000000-0000-0000-0000-000000000000\n"}),
        ("svw_symbol", {"build_id": h, "location": {"file": "a\n../../etc", "byte": 0}}),
        ("svw_references", {"build_id": h, "symbol_id": h, "scope": "x\x00y"}),
        ("svw_assertions", {"build_id": h, "cursor": "abc\n"}),
    ]
    for tool, args in bad:
        with pytest.raises(SvwError) as ei:
            spec[tool].validate(args)  # the gateway validator is a FULL match
        assert ei.value.code is ErrorCode.INVALID_CONFIG, (tool, args)


def test_services_reject_control_characters_and_traversal_for_cli_and_mcp(ws: Ws) -> None:
    opts, bid, sid = stub_build(ws)
    sess = Session(opts, use_graph=False)
    for call_ in (
        lambda: services.references(sess, "r", build_id=bid, symbol_id=H, scope="x\n.."),
        lambda: services.references(sess, "r", build_id=bid, symbol_id=H, scope="../x"),
        lambda: services.symbol(sess, "r", build_id=bid, file="a.sv\n../../etc", byte=0),
        lambda: services.symbol(sess, "r", build_id=bid, file="/etc/passwd", byte=0),
        lambda: services.symbol(sess, "r", build_id=bid, name="a\nb"),
        lambda: services.search(sess, "r", snapshot_id=sid, query="q", scope="foo\n"),
        lambda: services.hierarchy(sess, "r", build_id=bid, top="a\nb"),
        lambda: services.assertions(sess, "r", build_id=bid, scope="a\x07"),
    ):
        with pytest.raises(SvwError) as ei:
            call_()
        assert ei.value.code is ErrorCode.INVALID_CONFIG
    r = CliRunner().invoke(
        cli,
        [
            "--root",
            str(ws.root),
            "--compact",
            "query",
            "references",
            "--build",
            bid,
            "--symbol",
            H,
            "--scope",
            "x\ny",
        ],
    )
    assert r.exit_code == 2 and json.loads(r.stdout)["error"]["code"] == "INVALID_CONFIG"


def test_error_envelopes_carry_the_workspace_id(ws: Ws) -> None:
    opts, bid, sid = stub_build(ws)
    gw, _ = gw_with_events(opts)
    for tool, args in (
        ("svw_hierarchy", {"build_id": "bad"}),
        ("svw_hierarchy", {"build_id": "0" * 64}),
    ):
        env = gw.call(tool, args).envelope
        assert env.error is not None and env.workspace_id == "t"
    r = CliRunner().invoke(
        cli, ["--root", str(ws.root), "--compact", "query", "hierarchy", "--build", "0" * 64]
    )
    assert json.loads(r.stdout)["workspace_id"] == "t"
    job = gw.call("svw_job", {"job_id": "00000000-0000-0000-0000-000000000000"}).envelope
    assert job.error is not None and job.workspace_id == "t"


def test_cursor_key_is_strong_private_and_weak_keys_are_refused(ws: Ws) -> None:
    ctx = ws.ctx()
    key = cursor_key(ctx)
    p = ctx.trusted_dir / "cursor.key"
    assert len(key) >= 32 and (p.stat().st_mode & 0o777) == 0o600
    assert (ctx.trusted_dir.stat().st_mode & 0o777) == 0o700
    os.chmod(p, 0o644)
    assert cursor_key(ctx) == key and (p.stat().st_mode & 0o777) == 0o600  # tightened
    p.write_bytes(b"short")
    with pytest.raises(SvwError) as ei:
        cursor_key(ctx)
    assert (
        ei.value.code is ErrorCode.POLICY_DENIED and ei.value.details["reason"] == "cursor_key_weak"
    )
    p.write_bytes(b"")
    with pytest.raises(SvwError):
        cursor_key(ctx)
    p.unlink()
    other = ctx.trusted_dir / "elsewhere"
    other.write_bytes(os.urandom(32))
    p.symlink_to(other)
    with pytest.raises(SvwError) as ei2:
        cursor_key(ctx)
    assert ei2.value.details["reason"] == "cursor_key_untrusted"


def test_policy_help_text_points_at_the_trusted_dir() -> None:
    r = CliRunner().invoke(cli, ["--help"])
    flat = " ".join(r.output.split())
    assert "XDG_CONFIG_HOME" in flat and "<root>/.svweave/policy.json" not in flat


# ------------------------------------------------------------------------------------ M6 / M9
def test_M6_gateway_redacts_paths_and_secrets_in_every_free_text_part(
    ws: Ws, monkeypatch: pytest.MonkeyPatch
) -> None:
    opts, bid, sid = stub_build(ws)
    gw, _ = gw_with_events(opts)
    dirty = "failed at /home/alice/private/ip/top.sv with API_TOKEN=abcdef123456"

    def boom(*a: Any, **k: Any) -> Envelope:
        raise SvwError(
            ErrorCode.BACKEND_UNAVAILABLE,
            dirty,
            {"stderr": dirty, "path": "/home/alice/private/x/y.sv"},
        )

    monkeypatch.setattr(services, "hierarchy", boom)
    env = gw.call("svw_hierarchy", {"build_id": bid}).envelope
    assert "abcdef123456" not in env.to_json() and "/home/alice/private" not in env.to_json()

    def leaky(*a: Any, **k: Any) -> Envelope:
        return Envelope(
            request_id="r",
            outcome=Outcome.PARTIAL,
            warnings=[dirty],
            data={"stderr_tail": dirty, "error": dirty, "items": []},
        )

    monkeypatch.setattr(services, "assertions", leaky)
    env2 = gw.call("svw_assertions", {"build_id": bid}).envelope
    assert "abcdef123456" not in env2.to_json() and "/home/alice/private" not in env2.to_json()
    assert env2.outcome is Outcome.PARTIAL  # redacted, not turned into an error


def test_M9_request_cap_applies_to_the_total_not_only_per_field(
    ws: Ws, monkeypatch: pytest.MonkeyPatch
) -> None:
    opts, bid, sid = stub_build(ws)
    gw, _ = gw_with_events(opts)
    hits: list[int] = []
    edit = T.TOOLS["svw_edit"]
    monkeypatch.setitem(
        T.TOOLS,
        "svw_edit",
        ToolSpec(
            edit.name,
            edit.title,
            edit.description,
            edit.annotations,
            lambda *a: hits.append(1) or Envelope(request_id="r", outcome=Outcome.OK),
            edit.input_schema,
            edit.output_schema,
        ),
    )
    item = {
        "kind": "exact_range",
        "logical_path": "rtl/a.sv",
        "start_byte": 0,
        "end_byte": 1,
        "expected_sha256": H,
        "replacement": "x" * 2000,
    }
    args = {
        "action": "prepare",
        "base_build_id": bid,
        "snapshot_id": sid,
        "request_key": "key-0123456789",
        "edits": [item] * 50,
    }
    assert all(len(v) <= 1_048_576 for v in (item["replacement"],))  # every field is within its cap
    assert len(json.dumps(args)) > T.MAX_REQUEST_BYTES
    T.TOOLS["svw_edit"].validate(args)  # the schema alone accepts it
    out = gw.call("svw_edit", args)
    assert out.is_error and out.envelope.error is not None
    assert out.envelope.error.details["max_bytes"] == T.MAX_REQUEST_BYTES
    assert hits == []
    small = dict(args, edits=[item])
    gw.call("svw_edit", small)
    assert hits == [1]  # under the cap the request does reach the handler


def test_B4_database_setup_is_serialised_by_the_init_lock(tmp_path: Path) -> None:
    from svweave.storage.lock import init_lock

    path = tmp_path / "locked.db"
    done = threading.Event()

    def opener() -> None:
        Database(path).close()
        done.set()

    with init_lock(path):
        t = threading.Thread(target=opener)
        t.start()
        assert not done.wait(0.5)  # blocked while another opener is setting the database up
    assert done.wait(10)
    t.join()
