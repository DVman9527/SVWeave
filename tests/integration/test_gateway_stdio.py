"""M3 end-to-end over the real stdio server: build job -> search (text fallback) -> hierarchy."""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import anyio
import pytest
from mcp import Client
from mcp.client.stdio import StdioServerParameters

from tests.contract.test_stdio_server import REPO, server_cmd, server_env
from tests.integration.demo_support import demo_ws

pytestmark = pytest.mark.integration


def test_build_search_hierarchy_over_real_stdio(tmp_path: Path) -> None:
    ws = demo_ws(tmp_path)
    params = StdioServerParameters(
        command=sys.executable, args=server_cmd(ws.root)[1:], env=server_env(), cwd=str(REPO)
    )

    async def flow() -> dict[str, Any]:
        async with Client(params) as c:

            async def tool(name: str, args: dict[str, Any]) -> dict[str, Any]:
                r = await c.call_tool(name, args)
                assert r.structured_content is not None
                return dict(r.structured_content)

            st0 = await tool("svw_status", {})
            started = await tool("svw_build", {"profile": "p"})
            jid = started["data"]["job_id"]
            job: dict[str, Any] = {}
            with anyio.fail_after(240):
                while True:
                    job = await tool("svw_job", {"job_id": jid})
                    if job["data"]["lifecycle"] not in ("QUEUED", "RUNNING"):
                        break
                    await anyio.sleep(0.3)
            bid, sid = job["data"]["build_id"], job["data"]["snapshot_id"]
            search = await tool("svw_search", {"snapshot_id": sid, "query": "demo_lane"})
            hier = await tool("svw_hierarchy", {"build_id": bid, "top": "demo_top"})
            ev = await tool("svw_evidence", {"build_id": bid})
            st1 = await tool("svw_status", {})
            return {"st0": st0, "job": job, "search": search, "hier": hier, "ev": ev, "st1": st1}

    out = anyio.run(flow)
    assert out["st0"]["data"]["profiles"][0]["current_build"] is None
    assert out["job"]["data"]["lifecycle"] == "SUCCEEDED"
    assert out["job"]["data"]["result"]["completeness"] == "COMPLETE"
    assert out["job"]["data"]["result"]["published"] is True
    s = out["search"]
    assert s["outcome"] == "PARTIAL" and s["data"]["backend_used"] == "text_fallback"
    assert any("demo_lane" in i["snippet"] for i in s["data"]["items"])
    h = out["hier"]
    assert h["outcome"] == "OK" and h["analysis_coverage"]["completeness"] == "COMPLETE"
    paths = [i["path"] for i in h["data"]["items"]]
    assert paths[0] == "demo_top" and any("g_lane" in p for p in paths)
    assert h["provenance"]["evidence_kind"] == "HDL_STATIC_RESOLVED"
    ev = out["ev"]["data"]
    assert [i["evidence_type"] for i in ev["items"]] == ["static"]
    assert ev["states"]["simulation"]["state"] == "NOT_RUN"
    cur = out["st1"]["data"]["profiles"][0]["current_build"]
    assert cur["build_id"] == h["build_id"] and cur["freshness"] == "CURRENT"
    assert {b["status"] for b in out["st1"]["data"]["backends"].values()} == {"NOT_PROBED"}
    # the build job recorded the static-analysis evidence of the build (and nothing else)
    from svweave.application.options import WorkspaceOptions
    from svweave.storage.db import Database

    ctx = WorkspaceOptions(root=ws.root).load()
    with Database(ctx.state_dir / "svweave.db") as db:
        rows = db.conn.execute(
            "SELECT evidence_type, kind, trust_class, link_state FROM evidence"
        ).fetchall()
    assert [tuple(r) for r in rows] == [
        ("static", "HDL_STATIC_RESOLVED", "HDL_STATIC_RESOLVED", "linked_build")
    ]
