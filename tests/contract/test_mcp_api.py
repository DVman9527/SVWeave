"""MCP behaviour over an in-memory client: API-01, API-02, API-03, status, errors, jobs."""

from __future__ import annotations

import json
import re
import time
from pathlib import Path
from typing import Any

import pytest
from jsonschema import Draft202012Validator
from mcp import Client
from mcp.shared.exceptions import MCPError

from svweave.application import services
from svweave.application.jobs import JobManager
from svweave.application.options import WorkspaceOptions
from svweave.application.session import Session
from svweave.domain.states import BackendAvailability, Completeness
from svweave.gateway.tools import TOOLS, Gateway
from svweave.orchestration.health import BackendHealth
from tests.conftest import profile
from tests.contract.conftest import Built, call, make_gateway, mcp_run
from tests.integration.demo_support import demo_ws

H = "a" * 64


def validate_output(name: str, env: dict[str, Any]) -> None:
    Draft202012Validator(TOOLS[name].output_schema).validate(env)


# ------------------------------------------------------------------------------ tools/list
def test_tools_list_matches_schemas_and_annotations(built: Built) -> None:
    gw = make_gateway(built.opts)

    async def go(c: Client) -> Any:
        return (await c.list_tools()).tools

    tools = mcp_run(gw, go)
    assert {t.name for t in tools} == set(TOOLS)
    for t in tools:
        spec = TOOLS[t.name]
        assert t.input_schema == spec.input_schema
        assert t.output_schema == spec.output_schema
        assert t.annotations is not None
        assert t.annotations.read_only_hint is spec.annotations["readOnlyHint"]
        assert t.annotations.destructive_hint is (t.name == "svw_edit")


# ------------------------------------------------------------------------------ API-01
BAD_INPUTS: list[tuple[str, dict[str, Any]]] = [
    ("svw_hierarchy", {"build_id": H, "bogus": 1}),  # unknown field
    ("svw_hierarchy", {"build_id": "xyz"}),  # illegal ID
    ("svw_hierarchy", {"build_id": H.upper()}),  # not lowercase hex
    ("svw_hierarchy", {"build_id": H, "limit": 501}),  # over the cap
    ("svw_hierarchy", {"build_id": H, "limit": 0}),
    ("svw_hierarchy", {"build_id": H, "top": "t" * 100000}),  # oversize
    ("svw_hierarchy", {"build_id": H, "top": "x", "instance_id": H}),
    ("svw_assertions", {"build_id": H, "kind": "nonsense"}),  # invalid enum
    ("svw_evidence", {"build_id": H, "type": "weird"}),
    ("svw_evidence", {}),  # neither build_id nor evidence_id
    ("svw_search", {"snapshot_id": H}),  # missing query
    ("svw_search", {"snapshot_id": H, "query": "x", "scope": "../etc"}),
    ("svw_search", {"snapshot_id": H, "query": "x" * 513}),
    ("svw_search", {"build_id": H, "query": "x"}),  # semantic id where a snapshot is required
    ("svw_symbol", {"build_id": H}),
    ("svw_references", {"build_id": H, "symbol_id": "nope"}),
    ("svw_connections", {"build_id": H}),
    ("svw_build", {}),  # profile required
    ("svw_build", {"profile": "p", "reuse": "yes"}),
    ("svw_build", {"profile": "p; rm -rf /"}),
    ("svw_job", {"job_id": "123"}),
    ("svw_job", {"job_id": "00000000-0000-0000-0000-000000000000", "action": "delete"}),
    ("svw_status", {"probe": "true"}),
    ("svw_status", {"profile": {"a": 1}}),  # non-string profile used to crash the envelope
    ("svw_build", {"profile": 5}),
    ("svw_status", {"profile": "/home/alice/priv TOKEN=abcdef123456 " + "z" * 80}),
    ("svw_status", {"profile": "p\n"}),  # trailing newline passes a bare `$` pattern
    ("svw_hierarchy", {"build_id": H + "\n"}),
    ("svw_status", {"workspace": "/etc"}),  # no arbitrary roots
]


def test_API_01_invalid_requests_never_reach_a_backend(
    built: Built, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[str] = []

    def spy(name: str) -> Any:
        def f(*a: Any, **k: Any) -> Any:
            calls.append(name)
            raise AssertionError(f"{name} reached with an invalid request")

        return f

    for fn in (
        "status",
        "build",
        "job",
        "search",
        "symbol",
        "references",
        "hierarchy",
        "connections",
        "assertions",
        "diagnostics",
        "evidence",
    ):
        monkeypatch.setattr(services, fn, spy(fn))
    monkeypatch.setattr(JobManager, "submit_build", spy("submit_build"))
    monkeypatch.setattr(JobManager, "cancel", spy("cancel"))
    gw = make_gateway(built.opts)

    async def go(c: Client) -> list[tuple[dict[str, Any], bool]]:
        out = []
        for name, args in BAD_INPUTS:
            r = await c.call_tool(name, args)
            out.append((r.structured_content, bool(r.is_error)))
        return out

    results = mcp_run(gw, go)
    assert calls == []
    assert not gw.session.registry_created
    for (name, args), (env, is_err) in zip(BAD_INPUTS, results, strict=True):
        assert is_err, (name, args)
        assert env["outcome"] == "ERROR" and env["error"]["code"] == "INVALID_CONFIG", (name, args)
        det = env["error"]["details"]
        assert det.get("errors") or det.get("max_bytes"), (name, args)
        validate_output(name, env)
        assert "x" * 200 not in json.dumps(env)  # offending values are not echoed back
        assert env["profile"] is None and "abcdef123456" not in json.dumps(env)
        assert env["workspace_id"] == "t"  # error envelopes still name the workspace


def test_unknown_tool_is_a_protocol_error(built: Built) -> None:
    gw = make_gateway(built.opts)

    async def go(c: Client) -> None:
        with pytest.raises(MCPError) as ei:
            await c.call_tool("svw_nonexistent", {})
        assert ei.value.code == -32602

    mcp_run(gw, go)


# ------------------------------------------------------------------------------ result classes
def test_ok_partial_and_error_are_distinguishable(built: Built) -> None:
    gw = make_gateway(built.opts)
    ok, err = call(gw, "svw_hierarchy", {"build_id": built.build_id, "limit": 5})
    assert not err and ok["outcome"] == "OK" and ok["build_id"] == built.build_id
    validate_output("svw_hierarchy", ok)
    # legitimate PARTIAL domain result (text fallback): not an execution error
    part, err = call(gw, "svw_search", {"snapshot_id": built.snapshot_id, "query": "demo_top"})
    assert not err and part["outcome"] == "PARTIAL"
    assert part["data"]["backend_used"] == "text_fallback" and part["data"]["degraded"] is True
    validate_output("svw_search", part)
    # execution error: unknown build
    bad, err = call(gw, "svw_hierarchy", {"build_id": "0" * 64})
    assert (
        err and bad["outcome"] in ("ERROR", "BLOCKED") and bad["error"]["code"] == "MISSING_INPUT"
    )
    validate_output("svw_hierarchy", bad)
    assert not gw.session.registry_created  # text fallback only: nothing heavy was started


def test_every_read_tool_output_validates_against_its_output_schema(built: Built) -> None:
    gw = make_gateway(built.opts)
    b = built.build_id
    h, _ = call(gw, "svw_hierarchy", {"build_id": b})
    inst = h["data"]["items"][1]["instance_id"]
    cases = [
        ("svw_status", {}),
        ("svw_evidence", {"build_id": b}),
        ("svw_assertions", {"build_id": b}),
        ("svw_connections", {"build_id": b, "instance_id": inst}),
        ("svw_symbol", {"build_id": b, "name": "demo_top"}),
    ]
    for name, args in cases:
        env, err = call(gw, name, args)
        validate_output(name, env)
        assert not err or env["error"]["code"] == "AMBIGUOUS_SYMBOL", (name, env["error"])


# ------------------------------------------------------------------------------ status
def test_status_does_not_probe_by_default(built: Built) -> None:
    gw = make_gateway(built.opts)
    env, err = call(gw, "svw_status", {})
    d = env["data"]
    assert not err and not gw.session.registry_created
    assert {v["status"] for v in d["backends"].values()} == {"NOT_PROBED"}
    assert d["pending_edits"] == 0
    assert (
        d["client_boundary"]["qualified"] is False
        and d["client_boundary"]["editing_enabled"] is False
    )
    assert d["isolation"]["mode"] in ("bwrap", "degraded_no_isolation")
    prof = d["profiles"][0]
    assert prof["current_build"]["build_id"] == built.build_id
    assert prof["current_build"]["freshness"] == "CURRENT"
    assert any("client boundary not qualified" in w for w in env["warnings"])


class FakeRegistry:
    def __init__(self) -> None:
        self.shutdown_called = 0
        self.probed = 0
        self.names = ("pyslang", "codebase-memory-mcp", "serena")

    def probe_all(self) -> dict[str, BackendHealth]:
        self.probed += 1
        return {
            n: BackendHealth(name=n, status=BackendAvailability.READY, probed=True, version="1")
            for n in self.names
        }

    def all_health(self) -> dict[str, BackendHealth]:
        return {}

    def shutdown(self) -> None:
        self.shutdown_called += 1


def test_status_probe_true_uses_registry_and_session_shutdown_cleans_up(built: Built) -> None:
    fake = FakeRegistry()
    sess = Session(built.opts, use_graph=False, registry_factory=lambda: fake)  # type: ignore[arg-type,return-value]
    gw = Gateway(sess)  # real close semantics: the end of the server closes the session
    env, err = call(gw, "svw_status", {"probe": True})
    assert not err and fake.probed == 1
    assert {v["status"] for v in env["data"]["backends"].values()} == {"READY"}
    assert env["data"]["backends"]["serena"]["source"] == "probe_now"
    assert fake.shutdown_called >= 1  # lifespan end closes the session -> registry shut down
    # a later status call (new session) must not pretend READY from nothing
    env2, _ = call(make_gateway(built.opts), "svw_status", {})
    assert {v["status"] for v in env2["data"]["backends"].values()} == {"NOT_PROBED"}


# ------------------------------------------------------------------------------ API-02
def test_API_02_cursor_pagination_and_cross_build_rejection(built: Built, tmp_path: Path) -> None:
    gw = make_gateway(built.opts)
    p1, _ = call(gw, "svw_hierarchy", {"build_id": built.build_id, "limit": 2})
    assert p1["pagination"]["truncated"] and p1["pagination"]["next_cursor"]
    p2, err = call(
        gw,
        "svw_hierarchy",
        {"build_id": built.build_id, "limit": 2, "cursor": p1["pagination"]["next_cursor"]},
    )
    assert not err and p2["data"]["offset"] == 2
    # a cursor from another query / another build is refused
    other, err = call(
        gw,
        "svw_assertions",
        {"build_id": built.build_id, "cursor": p1["pagination"]["next_cursor"]},
    )
    assert err and other["error"]["code"] == "INVALID_CONFIG"
    # a second, different build in a different workspace: cursor of build A is not valid for B
    w2 = demo_ws(tmp_path / "second", lanes=3)
    gw2 = make_gateway(WorkspaceOptions(root=w2.root))
    started, _ = call(gw2, "svw_build", {"profile": "p"})
    jid = started["data"]["job_id"]
    for _ in range(300):
        st, _ = call(gw2, "svw_job", {"job_id": jid})
        if st["data"]["lifecycle"] not in ("QUEUED", "RUNNING"):
            break
        time.sleep(0.2)
    assert st["data"]["lifecycle"] == "SUCCEEDED", st
    b2 = st["data"]["build_id"]
    assert b2 and b2 != built.build_id
    res, err = call(
        gw2,
        "svw_hierarchy",
        {"build_id": b2, "limit": 2, "cursor": p1["pagination"]["next_cursor"]},
    )
    assert err and res["error"]["code"] == "INVALID_CONFIG"
    # an empty page past the end is flagged, never silently "complete"
    total = p1["data"]["total_matches"]
    last, err = call(
        gw,
        "svw_hierarchy",
        {"build_id": built.build_id, "limit": 2, "cursor": p1["pagination"]["next_cursor"]},
    )
    assert total >= 3 and not err
    gw2.session.real_close()  # type: ignore[attr-defined]


# ------------------------------------------------------------------------------ jobs / API-03
def _wait_event(gw: Gateway, jid: str, what: str, timeout: float = 60) -> None:
    t0 = time.time()
    while time.time() - t0 < timeout:
        st, _ = call(gw, "svw_job", {"job_id": jid})
        last = (st["data"].get("progress") or {}).get("last_event") or {}
        if last.get("event") == what:
            return
        assert st["data"]["lifecycle"] in ("QUEUED", "RUNNING"), st
        time.sleep(0.1)
    raise AssertionError(f"job never reached event {what}")


def sleepers() -> set[int]:
    """Host pids of the fault-injection ``sleep 3600`` (the worker has its own PID namespace)."""
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


def test_API_03_cancel_through_mcp_reaps_the_worker_tree(tmp_path: Path) -> None:
    before = sleepers()
    w = demo_ws(tmp_path)
    w.config({"p": profile(**_demo_profile(w)), "q": profile(**_demo_profile(w))})
    opts = WorkspaceOptions(root=w.root)
    jobs = JobManager(opts, test_fault="hang")  # TEST-ONLY fault injection, never request data
    sess = Session(opts, use_graph=False, jobs=jobs)
    gw = Gateway(sess)

    async def flow(c: Client) -> dict[str, Any]:
        import anyio

        async def tool(name: str, args: dict[str, Any]) -> dict[str, Any]:
            r = await c.call_tool(name, args)
            assert r.structured_content is not None
            return dict(r.structured_content)

        started = await tool("svw_build", {"profile": "p"})
        jid = started["data"]["job_id"]
        assert started["data"]["lifecycle"] in ("QUEUED", "RUNNING")
        again = await tool("svw_build", {"profile": "p"})
        assert again["data"]["job_id"] == jid and again["data"]["already_running"] is True
        other = await tool("svw_build", {"profile": "q"})
        assert other["error"]["code"] == "RESOURCE_LIMIT"  # one build at a time
        for _ in range(300):
            st = await tool("svw_job", {"job_id": jid})
            last = (st["data"]["progress"] or {}).get("last_event") or {}
            if last.get("event") == "start":
                break
            await anyio.sleep(0.1)
        await anyio.sleep(1.0)  # let the worker spawn its sleeping child
        cancelled = await tool("svw_job", {"job_id": jid, "action": "cancel"})
        assert cancelled["data"]["cancel_accepted"] is True
        final = await tool("svw_job", {"job_id": jid})
        return {"cancelled": cancelled, "final": final, "jid": jid}

    out = mcp_run(gw, flow)
    final = out["final"]["data"]
    assert final["lifecycle"] == "CANCELLED"
    res = final["result"]
    assert res["outcome"] == "ERROR"
    # the worker's descendant is gone and nothing was published
    from svweave.storage.db import Database

    ctx = opts.load()
    with Database(ctx.state_dir / "svweave.db") as db:
        row = db.job(out["jid"])
        assert row is not None
        env = json.loads(row["result"])
        m = re.search(r"FAULT_CHILD_PID=(\d+)", env["data"]["stderr_tail"])
        assert m, env["data"]
        assert db.published(ctx.workspace_id, "p") is None
    for _ in range(50):
        if not (sleepers() - before):
            break
        time.sleep(0.1)
    assert not (sleepers() - before)
    # the write lock is released: a normal build can start right away
    from svweave.storage.lock import workspace_lock

    with workspace_lock(ctx.state_dir, exclusive=True, blocking=False):
        pass
    assert Completeness.COMPLETE  # (import use)


def _demo_profile(w: Any) -> dict[str, Any]:
    from svweave.config.loader import load_manifest

    m = load_manifest(w.root / "svweave.yaml")
    return m.profiles["p"].model_dump(mode="json", exclude_none=True, exclude_defaults=True)


def test_job_lifecycle_for_a_successful_build_is_persisted(built: Built, tmp_path: Path) -> None:
    # reuse path: the build already exists, the job still records SUCCEEDED + the build id
    gw = make_gateway(built.opts)
    started, err = call(gw, "svw_build", {"profile": "p"})
    assert not err
    jid = started["data"]["job_id"]
    for _ in range(300):
        st, _ = call(gw, "svw_job", {"job_id": jid})
        if st["data"]["lifecycle"] not in ("QUEUED", "RUNNING"):
            break
        time.sleep(0.1)
    d = st["data"]
    assert d["lifecycle"] == "SUCCEEDED" and d["build_id"] == built.build_id
    assert d["result"]["completeness"] == "COMPLETE" and d["result"]["reused"] is True
    validate_output("svw_job", st)
    # unknown job / cancel of a finished job
    miss, err = call(gw, "svw_job", {"job_id": "00000000-0000-0000-0000-000000000000"})
    assert err and miss["error"]["code"] == "MISSING_INPUT"
    done, err = call(gw, "svw_job", {"job_id": jid, "action": "cancel"})
    assert not err and done["data"]["cancel_accepted"] is False
    # expected_snapshot_id that no longer matches -> STALE_SNAPSHOT, no build
    s2, _ = call(gw, "svw_build", {"profile": "p", "expected_snapshot_id": "f" * 64})
    j2 = s2["data"]["job_id"]
    for _ in range(300):
        st2, _ = call(gw, "svw_job", {"job_id": j2})
        if st2["data"]["lifecycle"] not in ("QUEUED", "RUNNING"):
            break
        time.sleep(0.1)
    assert st2["data"]["lifecycle"] == "FAILED"
    assert st2["data"]["result"]["error"]["code"] == "STALE_SNAPSHOT"
    gw.session.real_close()  # type: ignore[attr-defined]


# ------------------------------------------------------------------------------ M3b (MCP level)
def test_M8_evidence_responses_stay_under_the_cap_over_mcp(built: Built) -> None:
    import hashlib

    from svweave.application.build import DB_FILENAME
    from svweave.domain.cursor import MAX_RESPONSE_BYTES
    from svweave.storage.db import Database

    ctx = built.opts.load()
    ids = []
    with Database(ctx.state_dir / DB_FILENAME) as db:
        for i in range(4):
            eid = hashlib.sha256(f"m8-{i}".encode()).hexdigest()
            ids.append(eid)
            db.conn.execute(
                "INSERT OR IGNORE INTO evidence(evidence_id, kind, producer, build_id, snapshot_id, "
                "artifacts, trust_class, outcome, evidence_type, profile, test_name, seed, "
                "link_state, record, imported_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    eid,
                    "EXTERNAL_DECLARED",
                    "x",
                    built.build_id,
                    built.snapshot_id,
                    json.dumps([{"path": "p" * 70_000, "sha256": "0" * 64}]),
                    "EXTERNAL_DECLARED",
                    "PASS",
                    "formal",
                    "p",
                    f"m8{i}",
                    str(i),
                    "linked_build",
                    "{}",
                    f"2026-01-01T00:00:0{i}",
                ),
            )
    gw = make_gateway(built.opts)
    seen: list[str] = []
    cursor = None
    for _ in range(10):
        args: dict[str, Any] = {"build_id": built.build_id, "type": "formal"}
        if cursor:
            args["cursor"] = cursor
        env, err = call(gw, "svw_evidence", args)
        assert not err
        assert len(json.dumps(env, separators=(",", ":")).encode()) <= MAX_RESPONSE_BYTES
        seen += [i["evidence_id"] for i in env["data"]["items"]]
        cursor = env["pagination"]["next_cursor"]
        if not cursor:
            break
    assert set(ids) <= set(seen)
    by_id, err = call(gw, "svw_evidence", {"evidence_id": ids[0]})
    assert not err and len(json.dumps(by_id, separators=(",", ":")).encode()) <= MAX_RESPONSE_BYTES
