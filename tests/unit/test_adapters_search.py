"""svw_search: validation, cursor binding (API-02), text fallback bounds, graph->fallback routing."""

from __future__ import annotations

import base64
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from svweave.adapters.codebase_memory.models import CbmCandidate, IndexMapping
from svweave.application import search as S
from svweave.domain.envelope import Outcome
from svweave.domain.errors import ErrorCode, SvwError
from svweave.domain.states import BackendAvailability, Completeness, EvidenceKind
from svweave.orchestration.health import BackendHealth
from svweave.security.sandbox import IsolationMode

SID = "a" * 64
KEY = b"k" * 32


def ssearch(**kw: Any) -> Any:
    kw.setdefault("cursor_key", KEY)
    return S.svw_search(**kw)


@pytest.fixture
def snap(tmp_path: Path) -> Path:
    root = tmp_path / "snap"
    (root / "rtl").mkdir(parents=True)
    (root / "rtl" / "a.sv").write_text("module demo_lane;\n  // demo_lane again\nendmodule\n", encoding="utf-8")
    (root / "rtl" / "b.sv").write_text("// 注釋 demo_lane\nmodule b; endmodule\n", encoding="utf-8")
    (root / "inc").mkdir()
    (root / "inc" / "c.svh").write_text("`define X demo_lane\n")
    return root


def run(snap: Path, query: str = "demo_lane", **kw: Any) -> Any:
    return ssearch(snapshot_id=SID, snapshot_root=snap, query=query, **kw)


def test_no_registry_means_marked_text_fallback(snap: Path) -> None:
    env = run(snap)
    assert env.data["backend_used"] == "text_fallback" and env.data["degraded"] is True
    assert env.outcome is Outcome.PARTIAL and env.snapshot_id == SID
    assert env.provenance.backend == "text_fallback" and env.provenance.evidence_kind is EvidenceKind.SOURCE_CANDIDATE
    assert env.warnings[0].startswith("DEGRADED") and "no graph backend" in env.warnings[0]
    assert env.analysis_coverage is not None and env.analysis_coverage.basis == "literal_text_scan"
    assert any("literal text matches only" in x for x in env.analysis_coverage.limitations)
    items = env.data["items"]
    assert [(i["file"], i["line"]) for i in items] == [("inc/c.svh", 1), ("rtl/a.sv", 1), ("rtl/a.sv", 2), ("rtl/b.sv", 1)]
    assert all(i["evidence_kind"] == "SOURCE_CANDIDATE" and i["backend"] == "text_fallback" for i in items)
    b = [i for i in items if i["file"] == "rtl/b.sv"][0]
    data = (snap / "rtl" / "b.sv").read_bytes()
    assert data[b["byte_start"]:b["byte_end"]] == b"demo_lane" and b["byte_start"] == len("// 注釋 ".encode())
    assert env.analysis_coverage.completeness is Completeness.COMPLETE
    assert not env.pagination.truncated and env.pagination.next_cursor is None


def test_scope_limits_files_and_validation(snap: Path) -> None:
    env = run(snap, scope="rtl/*.sv")
    assert {i["file"] for i in env.data["items"]} == {"rtl/a.sv", "rtl/b.sv"}
    for kw in ({"query": ""}, {"query": "x" * 600}, {"query": "a\x00b"}, {"scope": "../x"}, {"scope": "/abs"},
               {"scope": "a/../b"}, {"limit": 0}, {"limit": 501}):
        with pytest.raises(SvwError) as ei:
            run(snap, **kw)
        assert ei.value.code is ErrorCode.INVALID_CONFIG
    with pytest.raises(SvwError):
        ssearch(snapshot_id="bad id!", snapshot_root=snap, query="x")
    with pytest.raises(SvwError) as ei2:
        run(snap / "missing")
    assert ei2.value.code is ErrorCode.MISSING_INPUT


def test_regex_metacharacters_are_literal(snap: Path) -> None:
    (snap / "rtl" / "r.sv").write_text("a.*b\naXXb\n")
    env = run(snap, "a.*b")
    assert [(i["file"], i["line"]) for i in env.data["items"]] == [("rtl/r.sv", 1)]


def test_pagination_and_cursor_binding_api02(snap: Path) -> None:
    p1 = run(snap, limit=2)
    assert len(p1.data["items"]) == 2 and p1.pagination.truncated and p1.pagination.next_cursor
    assert p1.analysis_coverage is not None and p1.analysis_coverage.completeness is Completeness.PARTIAL
    p2 = run(snap, limit=2, cursor=p1.pagination.next_cursor)
    assert len(p2.data["items"]) == 2 and not p2.pagination.truncated
    seen = [(i["file"], i["line"]) for i in p1.data["items"] + p2.data["items"]]
    assert len(set(seen)) == 4
    # a different query / scope / snapshot cannot reuse the cursor
    for kw in ({"query": "module"}, {"scope": "rtl/*"}):
        with pytest.raises(SvwError) as ei:
            run(snap, cursor=p1.pagination.next_cursor, **kw)
        assert "different" in ei.value.message
    with pytest.raises(SvwError) as ei2:
        ssearch(snapshot_id="b" * 64, snapshot_root=snap, query="demo_lane", cursor=p1.pagination.next_cursor)
    assert "different" in ei2.value.message
    for bad in ("!!!", base64.urlsafe_b64encode(b'{"v":2}').decode(), base64.urlsafe_b64encode(b"[]").decode()):
        with pytest.raises(SvwError) as ei3:
            run(snap, cursor=bad)
        assert ei3.value.code is ErrorCode.INVALID_CONFIG
    empty_page = run(snap, limit=2, cursor=S.encode_cursor(SID, S.query_hash("demo_lane", None), S.TEXT_FALLBACK, 99, KEY))
    assert empty_page.data["items"] == [] and not empty_page.pagination.truncated


def test_text_search_bounds(tmp_path: Path) -> None:
    root = tmp_path / "s"
    root.mkdir()
    (root / "big.sv").write_text("needle " + "x" * 5000)
    (root / "bin.dat").write_bytes(b"\x00\x01needle")
    (root / "ok.sv").write_text("needle\n")
    (root / "link.sv").symlink_to(root / "ok.sv")
    limits = S.TextSearchLimits(max_file_bytes=1000)
    scan = S.text_search(root, "needle", None, 0, 10, limits)
    assert [h.file for h in scan.hits] == ["ok.sv"]
    assert any("big.sv" in s and "larger" in s for s in scan.files_skipped)
    assert any("bin.dat" in s and "binary" in s for s in scan.files_skipped)
    env = ssearch(snapshot_id=SID, snapshot_root=root, query="needle", text_limits=limits)
    assert env.analysis_coverage is not None and env.analysis_coverage.completeness is Completeness.PARTIAL
    assert any("larger" in x for x in env.analysis_coverage.limitations)

    many = tmp_path / "m"
    many.mkdir()
    for i in range(10):
        (many / f"f{i}.sv").write_text("needle\n")
    capped = S.text_search(many, "needle", None, 0, 100, S.TextSearchLimits(max_files=3))
    assert len(capped.hits) == 3 and capped.truncated_by_budget and not capped.has_more
    expired = S.text_search(many, "needle", None, 0, 100, S.TextSearchLimits(time_budget_s=-1))
    assert expired.hits == [] and expired.truncated_by_budget and any("time budget" in s for s in expired.files_skipped)
    byte_cap = S.text_search(many, "needle", None, 0, 100, S.TextSearchLimits(max_total_bytes=14))
    assert len(byte_cap.hits) == 2 and byte_cap.truncated_by_budget


def test_response_size_is_capped_with_cursor(tmp_path: Path) -> None:
    root = tmp_path / "big"
    root.mkdir()
    line = "needle " + "y" * 190 + "\n"
    (root / "a.sv").write_text(line * 400)
    env = ssearch(snapshot_id=SID, snapshot_root=root, query="needle", limit=500)
    assert len(env.to_json(indent=None)) <= S.MAX_RESPONSE_BYTES
    assert env.pagination.truncated and env.pagination.next_cursor
    assert any("capped" in w for w in env.warnings)
    nxt = ssearch(snapshot_id=SID, snapshot_root=root, query="needle", limit=500, cursor=env.pagination.next_cursor)
    assert nxt.data["items"][0]["line"] == len(env.data["items"]) + 1


# ------------------------------------------------------------------------------- routing with a fake registry
class FakeCbm:
    def __init__(self) -> None:
        self.calls: list[str] = []
        self.err: SvwError | None = None
        self.index_err: SvwError | None = None
        self.isolation = IsolationMode.BWRAP
        self.cands = [CbmCandidate(SID, "demo_lane", "Class", "demo.demo_lane", "p.demo.demo_lane", "rtl/a.sv", 1, 3, -1.0)]
        self.positions = [0]
        self.consumed = 1
        self.has_more = True
        self.search_args: list[dict[str, Any]] = []

    def ensure_indexed(self, sid: str, root: Path) -> None:
        self.calls.append("index")
        if self.index_err:
            raise self.index_err

    def search(self, sid: str, q: str, *, file_pattern: str | None, limit: int, offset: int) -> Any:
        self.calls.append("search")
        self.search_args.append({"offset": offset, "limit": limit})
        if self.err:
            raise self.err
        m = IndexMapping(sid, "/r", "p", "fp", 3, 4, None, "full", "0.11.0", "h", parse_unusable=("u.sv",))
        return SimpleNamespace(candidates=self.cands, total=5, has_more=self.has_more,
                               limitations=["u.sv: parse_unusable"], warnings=[], mapping=m,
                               rows_consumed=self.consumed, raw_positions=self.positions)


class FakeRegistry:
    def __init__(self, cbm: FakeCbm, usable: bool = True) -> None:
        self._cbm, self._usable = cbm, usable
        self.failures: list[SvwError] = []

    def usable(self, name: str) -> bool:
        return self._usable

    def health(self, name: str) -> BackendHealth:
        return BackendHealth(name=name, status=BackendAvailability.UNAVAILABLE, reasons=["down since 12:00"])

    def cbm(self, root: Path | None = None) -> FakeCbm:
        return self._cbm

    def note_failure(self, name: str, err: SvwError) -> None:
        self.failures.append(err)


def test_graph_path_envelope(snap: Path) -> None:
    cbm = FakeCbm()
    env = run(snap, registry=FakeRegistry(cbm), limit=1, scope="rtl/*")  # type: ignore[arg-type]
    assert cbm.calls == ["index", "search"]
    assert env.data["backend_used"] == "codebase-memory-mcp" and env.data["degraded"] is False
    assert env.provenance.backend == "codebase-memory-mcp" and env.provenance.evidence_kind is EvidenceKind.SOURCE_CANDIDATE
    assert env.outcome is Outcome.PARTIAL and env.analysis_coverage is not None
    assert env.analysis_coverage.completeness is Completeness.PARTIAL  # graph is never presented as complete
    assert env.analysis_coverage.limitations == ["u.sv: parse_unusable"]
    assert env.data["index_fingerprint"] == "fp" and env.data["total"] == 5
    assert env.pagination.truncated and env.pagination.next_cursor
    mode, off = S.decode_cursor(env.pagination.next_cursor, SID, S.query_hash("demo_lane", "rtl/*"), KEY)
    assert (mode, off) == ("codebase-memory-mcp", 1)


@pytest.mark.parametrize("code", [ErrorCode.BACKEND_UNAVAILABLE, ErrorCode.CAPABILITY_MISMATCH,
                                  ErrorCode.STALE_SNAPSHOT, ErrorCode.BUILD_TIMEOUT])
def test_graph_failures_fall_back_with_visible_reason(snap: Path, code: ErrorCode) -> None:
    cbm = FakeCbm()
    cbm.err = SvwError(code, "boom", {"reason": "because"})
    reg = FakeRegistry(cbm)
    env = run(snap, registry=reg)  # type: ignore[arg-type]
    assert env.data["backend_used"] == "text_fallback" and env.data["degraded"] is True
    assert code.value in env.data["degraded_reason"] and "because" in env.data["degraded_reason"]
    assert env.data["items"]
    expect_noted = code in (ErrorCode.BACKEND_UNAVAILABLE, ErrorCode.CAPABILITY_MISMATCH)
    assert bool(reg.failures) is expect_noted


def test_index_failure_also_falls_back(snap: Path) -> None:
    cbm = FakeCbm()
    cbm.index_err = SvwError(ErrorCode.BUILD_TIMEOUT, "slow", {"reason": "index_timeout"})
    env = run(snap, registry=FakeRegistry(cbm))  # type: ignore[arg-type]
    assert env.data["backend_used"] == "text_fallback" and "BUILD_TIMEOUT" in env.data["degraded_reason"]


def test_non_backend_errors_propagate(snap: Path) -> None:
    cbm = FakeCbm()
    cbm.err = SvwError(ErrorCode.POLICY_DENIED, "bug", {})
    with pytest.raises(SvwError) as ei:
        run(snap, registry=FakeRegistry(cbm))  # type: ignore[arg-type]
    assert ei.value.code is ErrorCode.POLICY_DENIED


def test_unusable_backend_is_skipped_without_trying(snap: Path) -> None:
    cbm = FakeCbm()
    env = run(snap, registry=FakeRegistry(cbm, usable=False))  # type: ignore[arg-type]
    assert cbm.calls == [] and env.data["backend_used"] == "text_fallback"
    assert "UNAVAILABLE" in env.data["degraded_reason"] and "down since" in env.data["degraded_reason"]


def test_graph_cursor_after_fallback_restarts_and_says_so(snap: Path) -> None:
    cbm = FakeCbm()
    first = run(snap, registry=FakeRegistry(cbm), limit=1)  # type: ignore[arg-type]
    cbm.err = SvwError(ErrorCode.BACKEND_UNAVAILABLE, "died", {"reason": "process_exited"})
    nxt = run(snap, registry=FakeRegistry(cbm), limit=1, cursor=first.pagination.next_cursor)  # type: ignore[arg-type]
    assert nxt.data["backend_used"] == "text_fallback"
    assert any("restarted as a text_fallback" in w for w in nxt.warnings)
    assert nxt.data["items"][0]["line"] == 1  # offset reset, not silently misapplied


def test_text_cursor_keeps_text_backend_even_if_graph_recovers(snap: Path) -> None:
    t = run(snap, limit=1)
    cbm = FakeCbm()
    env = run(snap, registry=FakeRegistry(cbm), limit=1, cursor=t.pagination.next_cursor)  # type: ignore[arg-type]
    assert cbm.calls == [] and env.data["backend_used"] == "text_fallback"


def test_cursor_key_signs_cursors_and_rejects_foreign_keys(snap: Path) -> None:
    p1 = run(snap, limit=1, cursor_key=KEY)
    assert p1.pagination.next_cursor
    ok = run(snap, limit=1, cursor=p1.pagination.next_cursor, cursor_key=KEY)
    assert ok.data["items"]
    with pytest.raises(SvwError) as ei:
        run(snap, limit=1, cursor=p1.pagination.next_cursor, cursor_key=b"other" * 8)
    assert ei.value.code is ErrorCode.INVALID_CONFIG and "signature" in ei.value.message


def test_budget_exhaustion_is_reported_as_truncated_without_a_useless_cursor(tmp_path: Path) -> None:
    root = tmp_path / "m"
    root.mkdir()
    for i in range(6):
        (root / f"f{i}.sv").write_text("needle\n")
    env = ssearch(snapshot_id=SID, snapshot_root=root, query="needle", text_limits=S.TextSearchLimits(max_files=2))
    assert len(env.data["items"]) == 2 and env.pagination.truncated and env.pagination.next_cursor is None
    assert env.analysis_coverage is not None and env.analysis_coverage.completeness is Completeness.PARTIAL
    assert any("budget" in w for w in env.warnings)
