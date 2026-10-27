"""API-PAR: concurrent read tool calls through the real ``svweave serve --transport stdio``.

The server runs every tool call in a worker thread (``anyio.to_thread.run_sync(gateway.call)``)
and each call opens its own SQLite connection (WAL). These tests prove, with the real pyslang
build and the official MCP client, that many reads in flight at once return exactly what the same
calls return one at a time, also while a new build of the same profile runs, and from two gateway
processes on one workspace.

Comparison rule: an envelope is compared whole (``model_dump`` JSON) after removing ONLY
``request_id``, which is a fresh random id minted per call by design (``new_request_id``). Nothing
else is excluded: ``build_id``, ``snapshot_id``, ``freshness``, ``analysis_coverage``, ``data``,
``pagination`` (including the signed ``next_cursor``, which is a deterministic function of build,
query and offset) and ``warnings`` must all be bit-identical to the sequential baseline.
For the during-rebuild test ``freshness`` is allowed to move, and only along the
honest path STALE (source touched before the rebuild, so never CURRENT) -> HISTORICAL
(superseded by the new build), and never backwards per reader. Its exact warning and the
``reference_coverage.blockers`` freshness entry must agree with that state before those three
coupled values are canonicalized. All other coverage fields, blockers and warnings stay compared.

Plan mapping: the plan has no dedicated concurrency test id. These tests are an EXTENSION of
API-01..05 (plan section 20 API table) and SVW-023 (MCP gateway / job manager); they keep the
``API-PAR`` ids and do not stand in for any plan API-0x result.

Concurrency is measured SERVER side, not by the client: every ``gateway/tool:*`` record in
``.svweave/runs/serve-*/events.jsonl`` carries ``ts`` (end of the call, ms) and ``duration_s``;
the intervals ``[ts - duration_s, ts]`` give the peak number of tool calls that really overlapped
inside one gateway process. Peak overlap is capped by anyio's default thread limiter (40 worker
threads), so the threshold is 4 (normal runs reach about 12), not the 90 tasks the client keeps in flight. The client-side
"in flight" counter only proves the client sent them together; it cannot see serialization.
Mutation check: set ``SVW_TEST_PYTHONPATH_PREPEND`` to a directory holding a ``sitecustomize.py``
that wraps ``Gateway.call`` in a global lock; the server-side peak drops to about 1 (2 ms start margin
removes timestamp-truncation artefacts) and the tests go red (the variable is passed to the server process only).
"""

from __future__ import annotations

import json
import os
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any

import anyio
import pytest
from mcp import Client
from mcp.client.stdio import StdioServerParameters

from tests.contract.test_stdio_server import REPO, server_cmd, server_env
from tests.integration.demo_support import demo_ws

pytestmark = pytest.mark.integration

MIN_IN_FLIGHT = 48  # CLIENT-side tasks in flight (not a server-side claim)
MIN_SERVER_OVERLAP = 4  # SERVER-side peak overlap (normal runs reach ~12; serialized gives <=1)
Env = dict[str, Any]


def params_for(root: Path) -> StdioServerParameters:
    env = server_env()
    extra = os.environ.get("SVW_TEST_PYTHONPATH_PREPEND")  # mutation / instrumentation hook
    if extra:
        env["PYTHONPATH"] = extra + os.pathsep + env["PYTHONPATH"]
    return StdioServerParameters(
        command=sys.executable, args=server_cmd(root)[1:], env=env, cwd=str(REPO)
    )


READ_TOOLS = {
    "svw_symbol", "svw_references", "svw_hierarchy", "svw_connections",
    "svw_assertions", "svw_evidence", "svw_search",
}  # fmt: skip


def _ts(v: str) -> float:
    return datetime.fromisoformat(v).timestamp()


def server_events(root: Path) -> dict[str, list[dict[str, Any]]]:
    """events.jsonl records per gateway process (one ``serve-*`` run dir each)."""
    out: dict[str, list[dict[str, Any]]] = {}
    for f in sorted((root / ".svweave" / "runs").glob("serve-*/events.jsonl")):
        out[f.parent.name] = [json.loads(x) for x in f.read_text().splitlines() if x.strip()]
    return out


def tool_intervals(
    events: list[dict[str, Any]], names: set[str] | None = None
) -> list[tuple[float, float]]:
    iv = []
    for e in events:
        if (
            e["component"] == "gateway"
            and str(e["event"]).startswith("tool:")
            and (names is None or e["event"][5:] in names)
        ):
            end = _ts(e["ts"])
            iv.append((end - float(e["duration_s"]), end))
    return iv


TS_MARGIN_S = 0.002  # ``ts`` is wall clock truncated to 1 ms, ``duration_s`` is monotonic


def peak_overlap(iv: list[tuple[float, float]]) -> int:
    """Peak number of simultaneous intervals. Each interval start is moved in by TS_MARGIN_S
    (2 ms) first: the truncation of ``ts`` to 1 ms can make two strictly serialized calls look
    ~1 ms overlapped, which would count as false parallelism."""
    iv = [(a + TS_MARGIN_S, b) for a, b in iv if b > a + TS_MARGIN_S]
    pts = sorted([(a, 1) for a, _ in iv] + [(b, -1) for _, b in iv], key=lambda x: (x[0], x[1]))
    cur = peak = 0
    for _, d in pts:  # at equal timestamps ends (-1) sort first: touching is not overlapping
        cur += d
        peak = max(peak, cur)
    return peak


def norm(env: Env, *, freshness_moves: bool = False) -> str:
    """Compare the whole envelope except ``request_id`` without mutating the response.

    During a rebuild, first validate the exact freshness warning and reference-coverage blocker
    against the top-level state, then remove only those known dynamic values for comparison.
    Missing, duplicate or contradictory warnings/blockers fail instead of being hidden.
    """
    e = {k: v for k, v in env.items() if k != "request_id"}
    if freshness_moves:
        fresh = e.pop("freshness")
        build_bound = e["build_id"] is not None
        assert fresh in ({"CURRENT", "STALE", "HISTORICAL"} if build_bound else {"CURRENT", "STALE"})
        expected_warnings = []
        if fresh != "CURRENT":
            expected_warnings = [
                f"freshness is {fresh}: the workspace changed since this build's snapshot; "
                "results describe the snapshot, not the current files"
                if build_bound else
                "the workspace changed since this snapshot; results describe the snapshot"
            ]
        freshness_warnings = [
            w for w in e["warnings"]
            if w.startswith(("freshness is ", "the workspace changed since this snapshot"))
        ]
        assert freshness_warnings == expected_warnings, (fresh, freshness_warnings)
        e["warnings"] = [w for w in e["warnings"] if w not in expected_warnings]
        if build_bound:
            coverage = e["analysis_coverage"]
            refs = coverage["reference_coverage"]
            assert refs is not None, "build-bound reads must carry reference coverage"
            expected_blockers = [] if fresh == "CURRENT" else [f"freshness_{fresh.lower()}"]
            freshness_blockers = [b for b in refs["blockers"] if b.startswith("freshness_")]
            assert freshness_blockers == expected_blockers, (fresh, freshness_blockers)
            # Copy the nested dictionaries: the original response retains all its warnings and
            # blockers, and no other coverage information is omitted from the equality check.
            e["analysis_coverage"] = coverage | {"reference_coverage": refs | {
                "blockers": [b for b in refs["blockers"] if b not in expected_blockers],
            }}
    return json.dumps(e, sort_keys=True, separators=(",", ":"))


class Probe:
    """A live MCP session + counters for calls in flight."""

    def __init__(self, c: Client) -> None:
        self.c = c
        self.in_flight = 0
        self.max_in_flight = 0
        self.total = 0

    async def tool(self, name: str, args: dict[str, Any], allow_error: bool = False) -> Env:
        self.in_flight += 1
        self.total += 1
        self.max_in_flight = max(self.max_in_flight, self.in_flight)
        try:
            r = await self.c.call_tool(name, args)
        finally:
            self.in_flight -= 1
        assert allow_error or not r.is_error, (name, args, r.content)
        assert r.structured_content is not None, (name, args)
        env = dict(r.structured_content)
        assert json.loads(r.content[0].text) == env  # text block == structured content
        return env

    async def build(self, profile: str = "p", **kw: Any) -> Env:
        started = await self.tool("svw_build", {"profile": profile, **kw})
        jid = started["data"]["job_id"]
        with anyio.fail_after(240):
            while True:
                job = await self.tool("svw_job", {"job_id": jid})
                if job["data"]["lifecycle"] not in ("QUEUED", "RUNNING"):
                    return job
                await anyio.sleep(0.2)

    async def pages(self, name: str, args: dict[str, Any]) -> list[Env]:
        """Follow next_cursor to the end; every page is returned whole."""
        out: list[Env] = []
        cur: str | None = None
        for _ in range(100):
            a = dict(args) | ({"cursor": cur} if cur else {})
            e = await self.tool(name, a)
            out.append(e)
            cur = e["pagination"]["next_cursor"]
            if not cur:
                return out
        raise AssertionError("pagination did not terminate")


# A "call" is (label, async callable(probe) -> normalised result string).
Call = tuple[str, Any]


async def discover(p: Probe, bid: str, sid: str) -> list[Call]:
    """Pick real ids from the built workspace and return the fixed read-call set (>= 10 kinds)."""
    sym = await p.tool("svw_symbol", {"build_id": bid, "name": "demo_lane"})
    sym_id = sym["data"]["candidates"][0]["symbol_id"]
    hier = await p.tool("svw_hierarchy", {"build_id": bid, "top": "demo_top"})
    items = hier["data"]["items"]
    assert len(items) >= 3, items
    # a symbol with >= 2 references so that limit=1 really paginates (demo_lane.data_o)
    ref_id = ""
    for name in ("data_o",):
        cands = (await p.tool("svw_symbol", {"build_id": bid, "name": name}, True))["data"].get(
            "candidates", []
        )
        for cand in cands:
            r = await p.tool("svw_references", {"build_id": bid, "symbol_id": cand["symbol_id"]})
            if len(r["data"]["items"]) >= 2:
                ref_id = cand["symbol_id"]
                break
        if ref_id:
            break
    assert ref_id, "no multi-reference symbol found in the demo build"
    inst = next(i for i in items if i["path"] != "demo_top")

    async def one(name: str, args: dict[str, Any], allow_error: bool = False) -> str:
        return norm(await p.tool(name, args, allow_error))

    async def paged(name: str, args: dict[str, Any]) -> str:
        return json.dumps([norm(e) for e in await p.pages(name, args)])

    return [
        ("symbol_name", lambda: one("svw_symbol", {"build_id": bid, "name": "demo_lane"})),
        ("symbol_assert", lambda: one("svw_symbol", {"build_id": bid, "name": "ap_stall_holds"})),
        # deterministic error envelope (isError=True) must also be identical under concurrency
        (
            "symbol_ambiguous",
            lambda: one("svw_symbol", {"build_id": bid, "name": "demo_top"}, True),
        ),
        ("symbol_id", lambda: one("svw_symbol", {"build_id": bid, "symbol_id": sym_id})),
        ("references", lambda: one("svw_references", {"build_id": bid, "symbol_id": ref_id})),
        ("hierarchy", lambda: one("svw_hierarchy", {"build_id": bid, "top": "demo_top"})),
        (
            "connections",
            lambda: one("svw_connections", {"build_id": bid, "instance_id": inst["instance_id"]}),
        ),
        ("assertions", lambda: one("svw_assertions", {"build_id": bid})),
        ("evidence", lambda: one("svw_evidence", {"build_id": bid})),
        ("search", lambda: one("svw_search", {"snapshot_id": sid, "query": "demo_lane"})),
        ("status", lambda: one("svw_status", {})),
        # paginated: follow next_cursor concurrently
        ("hier_pages", lambda: paged("svw_hierarchy", {"build_id": bid, "limit": 1})),
        (
            "refs_pages",
            lambda: paged("svw_references", {"build_id": bid, "symbol_id": ref_id, "limit": 1}),
        ),
        ("assert_pages", lambda: paged("svw_assertions", {"build_id": bid, "limit": 1})),
        (
            "search_pages",
            lambda: paged("svw_search", {"snapshot_id": sid, "query": "demo", "limit": 2}),
        ),
    ]


def leaked(root: Path) -> list[str]:
    """Live processes (other than this one) whose command line mentions the workspace root."""
    found: list[str] = []
    needle = str(root).encode()
    for d in Path("/proc").iterdir():
        if not d.name.isdigit() or int(d.name) == os.getpid():
            continue
        try:
            raw = (d / "cmdline").read_bytes()
            state = (d / "stat").read_text().rsplit(")", 1)[1].split()[0]
        except OSError:
            continue
        if needle in raw and state != "Z":
            found.append(f"{d.name}: {raw.replace(b'\0', b' ').decode(errors='replace')[:200]}")
    return found


def no_leaks(root: Path) -> None:
    for _ in range(100):  # the server tears down asynchronously after stdin closes
        if not leaked(root):
            return
        time.sleep(0.1)
    raise AssertionError(f"leaked processes: {leaked(root)}")


async def run_concurrent(calls: list[Call], order: list[int]) -> dict[int, list[str]]:
    """Start every call in ``order`` at once (one task each) and collect results by call index."""
    results: dict[int, list[str]] = {}
    async with anyio.create_task_group() as tg:

        async def runner(idx: int) -> None:
            results.setdefault(idx, []).append(await calls[idx][1]())

        for idx in order:
            tg.start_soon(runner, idx)
    return results


def mixed_order(n_calls: int, repeats: int) -> list[int]:
    """Deterministic interleaving of ``repeats`` copies of every call (no RNG, reproducible)."""
    base = list(range(n_calls))
    out: list[int] = []
    for r in range(repeats):
        k = (r * 5 + 3) % n_calls
        seq = base[k:] + base[:k]
        out += seq[::-1] if r % 2 else seq
    return out


def test_API_PAR_01_concurrent_reads_equal_sequential_baseline(tmp_path: Path) -> None:
    ws = demo_ws(tmp_path)
    repeats = 6
    stats: dict[str, Any] = {}

    async def flow() -> None:
        async with Client(params_for(ws.root)) as c:
            p = Probe(c)
            job = await p.build()
            assert job["data"]["lifecycle"] == "SUCCEEDED", job
            bid, sid = job["data"]["build_id"], job["data"]["snapshot_id"]
            calls = await discover(p, bid, sid)

            # sequential baseline (also run twice: the call set itself must be deterministic)
            baseline = [await fn() for _, fn in calls]
            again = [await fn() for _, fn in calls]
            for (label, _), a, b in zip(calls, baseline, again, strict=True):
                assert a == b, f"{label} is not deterministic even sequentially"
            for (label, _), r in zip(calls, baseline, strict=True):
                if label.endswith("_pages"):
                    assert len(json.loads(r)) >= 2, f"{label} produced a single page"
                    continue
                e = json.loads(r)
                if label == "symbol_ambiguous":
                    assert e["error"]["code"] == "AMBIGUOUS_SYMBOL", e
                    continue
                assert e["outcome"] in ("OK", "PARTIAL"), (label, e["outcome"], e.get("error"))
                if label not in ("status", "search"):
                    assert e["build_id"] == bid and e["snapshot_id"] == sid, label

            # concurrent: every call x repeats, mixed order, all tasks started before any awaits
            order = mixed_order(len(calls), repeats)
            p.max_in_flight = 0
            t0 = time.monotonic()
            results = await run_concurrent(calls, order)
            stats["wall_s"] = time.monotonic() - t0
            stats["max_in_flight"] = p.max_in_flight
            stats["tasks"] = len(order)

            for idx, (label, _) in enumerate(calls):
                got = results[idx]
                assert len(got) == repeats, label
                for g in got:
                    assert g == baseline[idx], f"{label}: concurrent result != sequential baseline"

            # the gateway is still healthy and answers the same afterwards
            after = await p.tool("svw_status", {})
            assert json.loads(baseline[calls.index(next(c for c in calls if c[0] == "status"))]) | {
                "request_id": ""
            } == after | {"request_id": ""}
            assert (await p.tool("svw_hierarchy", {"build_id": bid, "top": "demo_top"}))[
                "outcome"
            ] == "OK"

    t = time.monotonic()
    anyio.run(flow)
    print(
        f"\nAPI-PAR-01 tasks={stats['tasks']} max_in_flight(client)={stats['max_in_flight']} "
        f"concurrent_wall={stats['wall_s']:.2f}s total_test={time.monotonic() - t:.2f}s"
    )
    assert stats["tasks"] >= MIN_IN_FLIGHT and stats["max_in_flight"] >= MIN_IN_FLIGHT
    assert stats["wall_s"] < 120  # generous sanity bound only
    (evs,) = server_events(ws.root).values()
    peak = peak_overlap(tool_intervals(evs))
    print(f"API-PAR-01 server_peak_overlap={peak} server_tool_calls={len(tool_intervals(evs))}")
    assert peak >= MIN_SERVER_OVERLAP, f"server-side overlap {peak} < {MIN_SERVER_OVERLAP}"
    no_leaks(ws.root)


def test_API_PAR_02_reads_during_rebuild_are_consistent(tmp_path: Path) -> None:
    ws = demo_ws(tmp_path)
    top = ws.root / "fixtures" / "demo" / "rtl" / "demo_top.sv"
    stats: dict[str, Any] = {}

    async def flow() -> None:
        async with Client(params_for(ws.root)) as c:
            p = Probe(c)
            job = await p.build()
            assert job["data"]["lifecycle"] == "SUCCEEDED", job
            bid1, sid1 = job["data"]["build_id"], job["data"]["snapshot_id"]
            calls = [x for x in await discover(p, bid1, sid1) if x[0] != "status"]
            baseline = [await fn() for _, fn in calls]

            # change the source so the next build is a NEW snapshot/build (reuse cannot apply)
            top.write_text(top.read_text() + "\n// touched by API-PAR-02\n")
            started = await p.tool("svw_build", {"profile": "p", "reuse": False})
            jid = started["data"]["job_id"]

            states: set[str] = set()
            fresh_seen: set[str] = set()
            done = anyio.Event()
            job_box: dict[str, Env] = {}
            during = 0  # reads that finished while the build job was observed QUEUED/RUNNING
            total = 0
            status_seen: set[str] = set()

            def check(label: str, got: str, base: str, allowed: set[str]) -> list[str]:
                """Old-build query: equal to the pre-rebuild baseline except freshness; returns
                the freshness of every page in order (one entry for an unpaged call)."""
                paged = label.endswith("_pages")
                gl, bl = (json.loads(got), json.loads(base)) if paged else ([got], [base])
                assert len(gl) == len(bl), label
                seq: list[str] = []
                if label.startswith("search"):
                    # svw_search is keyed by snapshot, not build: a snapshot is never superseded,
                    # it is STALE for as long as the workspace differs from it (observed, honest)
                    allowed = {"STALE"}
                for a, b in zip(gl, bl, strict=True):
                    ea, eb = json.loads(a), json.loads(b)
                    fresh_seen.add(ea["freshness"])
                    # CURRENT is impossible: the file was edited before the rebuild started
                    assert ea["freshness"] in allowed, (label, ea["freshness"])
                    assert norm(ea, freshness_moves=True) == norm(eb, freshness_moves=True), (
                        f"{label}: differs from pre-rebuild baseline"
                    )
                    seq.append(ea["freshness"])
                return seq

            async def reader(seed: int) -> None:
                nonlocal during, total
                n = 0
                rank = 0  # per reader: STALE (0) may be followed by HISTORICAL (1), never back
                while not done.is_set():
                    idx = (seed * 7 + n * 3) % len(calls)
                    n += 1
                    got = await calls[idx][1]()
                    total += 1
                    if not done.is_set():
                        during += 1
                    seq = check(calls[idx][0], got, baseline[idx], {"STALE", "HISTORICAL"})
                    if calls[idx][0].startswith("search"):
                        continue  # snapshot-keyed: stays STALE, not part of the ordering
                    ranks = [1 if f == "HISTORICAL" else 0 for f in seq]
                    # pages only move forward (STALE -> HISTORICAL) within one call, and a call
                    # never starts behind what this reader already saw
                    assert ranks == sorted(ranks), f"{calls[idx][0]}: HISTORICAL then STALE {seq}"
                    assert ranks[0] >= rank, f"{calls[idx][0]}: STALE seen after HISTORICAL"
                    rank = max(rank, ranks[-1])

            async def status_reader() -> None:
                """svw_status looks up the CURRENT build: it is build 1 (STALE, file edited) or
                build 2 (CURRENT) - always one of the two, with that build's own freshness."""
                while not done.is_set():
                    e = await p.tool("svw_status", {"profile": "p"})
                    cb = e["data"]["profiles"][0]["current_build"]
                    status_seen.add(cb["build_id"])
                    if cb["build_id"] == bid1:
                        # still the published build while the rebuild runs: STALE, never
                        # HISTORICAL (a newer unpublished build row does not supersede it)
                        assert cb["freshness"] == "STALE", (cb["freshness"], cb)
                    else:
                        assert cb["freshness"] == "CURRENT", cb
                    assert e["freshness"] == cb["freshness"], e["freshness"]

            async def poller() -> None:
                while True:
                    j = await p.tool("svw_job", {"job_id": jid})
                    states.add(j["data"]["lifecycle"])
                    if j["data"]["lifecycle"] not in ("QUEUED", "RUNNING"):
                        job_box["job"] = j
                        done.set()
                        return
                    await anyio.sleep(0.05)

            t0 = time.monotonic()
            with anyio.fail_after(240):
                async with anyio.create_task_group() as tg:
                    for sd in range(12):
                        tg.start_soon(reader, sd)
                    tg.start_soon(status_reader)
                    tg.start_soon(poller)
            job2 = job_box["job"]
            stats.update(
                reads_total=total,
                reads_during_build=during,
                wall_s=time.monotonic() - t0,
                states=sorted(states),
                fresh=sorted(fresh_seen),
                status_builds_seen=len(status_seen),
                max_in_flight=p.max_in_flight,
            )
            assert job2["data"]["lifecycle"] == "SUCCEEDED", job2
            bid2 = job2["data"]["build_id"]
            assert bid2 and bid2 != bid1 and job2["data"]["snapshot_id"] != sid1
            assert status_seen <= {bid1, bid2}, status_seen
            assert {"STALE", "HISTORICAL"} <= fresh_seen, fresh_seen
            assert "RUNNING" in states, states

            # after publication: the old build answers identically and is exactly HISTORICAL
            # (pages compared page by page); the new build is queryable and CURRENT.
            results = await run_concurrent(calls, mixed_order(len(calls), 2))
            for idx, (label, _) in enumerate(calls):
                for g in results[idx]:
                    check(label, g, baseline[idx], {"HISTORICAL"})
            new = await p.tool("svw_hierarchy", {"build_id": bid2, "top": "demo_top"})
            assert new["outcome"] == "OK" and new["freshness"] == "CURRENT", new
            cb = (await p.tool("svw_status", {}))["data"]["profiles"][0]["current_build"]
            assert cb["build_id"] == bid2 and cb["freshness"] == "CURRENT", cb
            stats["bid1"], stats["bid2"] = bid1, bid2

    anyio.run(flow)
    evs = next(iter(server_events(ws.root).values()))
    # job window of the SECOND build, from the server's own job events
    q = [e for e in evs if e["component"] == "jobs" and e["event"] == "queued"][-1]
    f = [e for e in evs if e["component"] == "jobs" and e["event"] == "finished"][-1]
    assert q["job_id"] == f["job_id"]
    w0, w1 = _ts(q["ts"]), _ts(f["ts"])
    reads = [(a, b) for a, b in tool_intervals(evs, READ_TOOLS) if a < w1 and b > w0]
    stats["server_reads_overlapping_build"] = len(reads)
    stats["server_peak_read_overlap_in_build"] = peak_overlap(reads)
    print(f"\nAPI-PAR-02 {stats}")
    assert len(reads) >= 2 and stats["server_peak_read_overlap_in_build"] >= 2, stats
    assert stats["reads_during_build"] >= 12  # the reads really overlapped the running build
    no_leaks(ws.root)


def test_API_PAR_03_two_gateway_processes_same_workspace(tmp_path: Path) -> None:
    ws = demo_ws(tmp_path)
    stats: dict[str, Any] = {}

    async def flow() -> None:
        async with Client(params_for(ws.root)) as c1:
            p1 = Probe(c1)
            job = await p1.build()
            assert job["data"]["lifecycle"] == "SUCCEEDED", job
            bid, sid = job["data"]["build_id"], job["data"]["snapshot_id"]
            calls = [x for x in await discover(p1, bid, sid) if x[0] != "status"]
            baseline = [await fn() for _, fn in calls]
            async with Client(params_for(ws.root)) as c2:
                p2 = Probe(c2)
                # the second gateway must see the same published build and answer identically
                calls2 = [x for x in await discover(p2, bid, sid) if x[0] != "status"]
                assert [lbl for lbl, _ in calls2] == [lbl for lbl, _ in calls]
                both = [(p1, calls), (p2, calls2)]
                results: dict[tuple[int, int], list[str]] = {}
                t0 = time.monotonic()
                async with anyio.create_task_group() as tg:

                    async def runner(g: int, idx: int) -> None:
                        results.setdefault((g, idx), []).append(await both[g][1][idx][1]())

                    for idx in mixed_order(len(calls), 4):
                        for g in (0, 1):
                            tg.start_soon(runner, g, idx)
                stats["wall_s"] = time.monotonic() - t0
                stats["tasks"] = 2 * 4 * len(calls)
                stats["max_in_flight"] = (p1.max_in_flight, p2.max_in_flight)
                for (g, idx), got in results.items():
                    for r in got:
                        assert r == baseline[idx], f"gw{g} {calls[idx][0]} != baseline"
                assert (await p2.tool("svw_hierarchy", {"build_id": bid, "top": "demo_top"}))[
                    "outcome"
                ] == "OK"

    anyio.run(flow)
    evs = server_events(ws.root)
    assert len(evs) == 2, list(evs)
    peaks = {k: peak_overlap(tool_intervals(v)) for k, v in evs.items()}
    stats["server_peaks"] = peaks
    print(f"\nAPI-PAR-03 {stats}")
    for k, v in peaks.items():
        assert v >= MIN_SERVER_OVERLAP, f"{k}: server-side overlap {v} < {MIN_SERVER_OVERLAP}"
    no_leaks(ws.root)
