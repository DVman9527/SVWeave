"""Driver of the plan 1.3 demo flow. Every step calls the REAL backends; nothing is mocked.

Steps (plan 1.3): load profile -> snapshot -> backend health check (real probes) -> graph search
-> slang hierarchy / symbol / references -> Serena edit in a candidate -> candidate slang
validation, diff and limitations -> approval -> CAS commit -> post-apply snapshot/build.

The flow is run ONCE (module-scoped fixture); the individual tests then assert on the recorded
results. Host memory is small: one backend at a time, always torn down.
"""

from __future__ import annotations

import json
import os
import shutil
import traceback
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

from svweave.application import edit as E
from svweave.application import queries, services
from svweave.application.build import run_build
from svweave.application.options import WorkspaceOptions
from svweave.application.search import svw_search
from svweave.application.session import Session
from svweave.domain.errors import SvwError
from svweave.editing.confirm import TEST_TOKEN_ENV, env_token_confirm
from svweave.manifest.resolver import resolve_profile
from svweave.orchestration.registry import CBM, PYSLANG, SERENA, BackendRegistry
from svweave.orchestration.toolchain import Toolchain
from svweave.security.sandbox import probe_host
from svweave.snapshot.capture import load_record, snapshots_dir
from tests.conftest import FIXTURES, Ws, profile
from tests.unit._edit_fx import EditWs

REPO = Path(__file__).resolve().parents[2]
PROFILE = "acc"
TARGET_FILE = "fixtures/language/sem08_virtual/top.sv"
TARGET_QN = "vpkg::dog::speak"
OTHER_QN = "vpkg::cat::speak"
BIND_ONLY_TERM = "u_stream_hold_sva"  # appears only in fixtures/demo/sva/demo_bind.sv


def queries_evidence(opts: WorkspaceOptions, build_id: str) -> dict[str, Any]:
    from svweave.application.evidence import get_evidence

    env = get_evidence(opts, "acc-ev", build_id=build_id)
    return dict(env.data.get("states", {}))


@dataclass
class Flow:
    root: Path
    steps: dict[str, dict[str, Any]] = field(default_factory=dict)
    data: dict[str, Any] = field(default_factory=dict)
    failed_step: str | None = None
    error: str | None = None

    def ok(self, name: str, **detail: Any) -> None:
        self.steps[name] = {"status": "PASS", **detail}

    def fail(self, name: str, exc: BaseException) -> None:
        self.steps[name] = {"status": "FAIL", "error": f"{type(exc).__name__}: {exc}"[:2000]}
        self.failed_step = name
        self.error = "".join(traceback.format_exception(exc))[-4000:]


def backends_available() -> str | None:
    """Return a reason string when a real backend is missing, else None."""
    tc = Toolchain.load(REPO)
    if not probe_host().available:
        return "bwrap is not usable on this host"
    if not tc.cbm_binary.is_file():
        return "codebase-memory-mcp binary not installed"
    if not tc.serena_exe.is_file():
        return "serena venv not installed"
    return None


def make_workspace(tmp: Path) -> EditWs:
    """A temp workspace holding COPIES of fixtures/demo and fixtures/language (never edited in place)."""
    root = tmp / "ws"
    (root / "fixtures").mkdir(parents=True)
    for n in ("demo", "language"):
        shutil.copytree(FIXTURES / n, root / "fixtures" / n)
    w = Ws(root)
    prof = profile(
        source_inputs=[
            {"kind": "filelist", "path": "fixtures/demo/filelists/rtl.f", "path_mode": "cwd"},
            {"kind": "file", "path": TARGET_FILE},
        ],
        top_modules=["demo_top", "sem08_top"],
        defines=[{"name": "SVW_DEMO", "value": "1"}],
        include_dirs=["fixtures/demo/include"],
        top_parameters=[{"top": "demo_top", "name": "LANES", "value": "2"},
                        {"top": "demo_top", "name": "DATA_W", "value": "32"}],
    )
    w.config(
        {PROFILE: prof},
        source_roots=[
            {"id": "project", "path": ".", "include": ["**"], "exclude": ["**/.svweave/**"],
             "writable": False},
            {"id": "fx", "path": "fixtures", "include": ["**"], "exclude": [], "writable": True},
        ],
        editing={"enabled": True, "allowed_paths": ["fixtures/**"], "max_files_per_patch": 5,
                 "max_changed_lines": 40, "require_human_approval": True,
                 "validate_profiles": [PROFILE]},
    )
    w.policy(writable_paths=["fixtures/**"], editing_enabled=True)
    return EditWs(root, w)


def run_flow(tmp: Path) -> Flow:
    mp = pytest.MonkeyPatch()
    mp.setenv("XDG_CONFIG_HOME", str(tmp / "xdg"))
    (tmp / "xdg").mkdir()
    mp.delenv(TEST_TOKEN_ENV, raising=False)
    ws = make_workspace(tmp)
    fl = Flow(root=ws.root)
    opts = WorkspaceOptions(root=ws.root)
    tc = Toolchain.load(REPO)
    step = "init"
    try:
        # ---------------------------------------------------------------- 1 profile + snapshot
        step = "01_profile_and_snapshot"
        ctx = ws.w.ctx()
        nm = resolve_profile(ctx, PROFILE, {})
        env1 = run_build(opts, PROFILE, "acc-build-1")
        assert env1.error is None, env1.error
        assert env1.analysis_coverage is not None
        assert env1.analysis_coverage.completeness.value == "COMPLETE"
        sid, bid = env1.snapshot_id, env1.build_id
        assert sid and bid
        srec = load_record(snapshots_dir(ctx) / sid)
        fl.data.update(snapshot_id=sid, build_id=bid, config_id=nm.config_id,
                       ledger_entries=len(nm.ledger), tops=nm.top_modules,
                       snapshot_files=[f.logical_path for f in srec.files],
                       build_counts=env1.data.get("counts"))
        fl.ok(step, snapshot_id=sid, build_id=bid, config_id=nm.config_id)
        original_target = ws.read(TARGET_FILE)
        fl.data["original_sha_file_count"] = len(srec.files)

        # ---------------------------------------------------------------- 2 real health probes
        step = "02_backend_health_probes"
        reg = BackendRegistry.for_workspace(ctx, tc)
        try:
            health = {n: h for n, h in reg.probe_all().items()}
        finally:
            reg.shutdown()
        fl.data["health"] = {n: {"status": h.status.value, "probed": h.probed,
                                 "isolation": h.isolation, "version": h.version,
                                 "reasons": list(h.reasons)[:6],
                                 "limitations": list(h.limitations)[:6]}
                             for n, h in health.items()}
        for n in (PYSLANG, CBM, SERENA):
            assert health[n].probed, f"{n} was not really probed"
            assert health[n].status.value in ("READY", "DEGRADED"), (n, health[n])
        fl.ok(step, statuses={n: h.status.value for n, h in health.items()})

        # ---------------------------------------------------------------- 3 graph search
        step = "03_graph_search_and_gap"
        sess = Session(opts)
        try:
            hits: dict[str, Any] = {}
            for q in ("demo_lane", "dog", BIND_ONLY_TERM):
                e = services.search(sess, "acc-search", snapshot_id=sid, query=q, limit=100)
                hits[q] = {
                    "backend_used": e.data.get("backend_used"),
                    "degraded": e.data.get("degraded"),
                    "outcome": e.outcome.value,
                    "files": sorted({i["file"] for i in e.data.get("items", [])}),
                    "evidence_kind": e.provenance.evidence_kind.value if e.provenance else None,
                    "limitations": list(e.analysis_coverage.limitations)
                    if e.analysis_coverage else [],
                    "warnings": list(e.warnings),
                }
        finally:
            sess.close()  # cbm is gone before slang/Serena start
        fl.data["graph"] = hits
        # the literal text search is a DIFFERENT, marked mechanism: used to show what cbm missed
        text = svw_search(snapshot_id=sid, snapshot_root=snapshots_dir(ctx) / sid / "files",
                          query=BIND_ONLY_TERM, registry=None, cursor_key=b"k" * 32)
        fl.data["text_fallback_bind_files"] = sorted({i["file"] for i in text.data["items"]})
        fl.data["text_fallback_backend"] = text.data["backend_used"]
        fl.ok(step, queries=list(hits))

        # ---------------------------------------------------------------- 4 slang queries
        step = "04_slang_hierarchy_symbol_references"
        h = queries.hierarchy(opts, bid, "acc-h", top="demo_top", limit=100)
        fl.data["hierarchy_paths"] = [i["path"] for i in h.data["items"]]
        fl.data["hierarchy_freshness"] = h.freshness.value
        sym = queries.symbol(opts, bid, "acc-s", name="demo_lane")
        sid_lane = sym.data["symbol"]["symbol_id"]
        refs = queries.references(opts, bid, sid_lane, "acc-r", limit=100)
        fl.data["demo_lane_refs"] = {
            "searched_scope": refs.data.get("searched_scope", {}).get("scope"),
            "n_items": len(refs.data.get("items", [])),
            "coverage": refs.analysis_coverage.completeness.value if refs.analysis_coverage else None,
        }
        # same-name methods stay distinct (impact scope of the edit target)
        dog = queries.symbol(opts, bid, "acc-s2", name=TARGET_QN)
        cat = queries.symbol(opts, bid, "acc-s3", name=OTHER_QN)
        fl.data["same_name_methods"] = {
            TARGET_QN: dog.data["symbol"]["declaration_id"],
            OTHER_QN: cat.data["symbol"]["declaration_id"],
        }
        speak_refs = queries.references(opts, bid, dog.data["symbol"]["symbol_id"], "acc-r2",
                                        limit=100)
        fl.data["target_refs"] = {
            "n_items": len(speak_refs.data.get("items", [])),
            "limitations": list(speak_refs.analysis_coverage.limitations)
            if speak_refs.analysis_coverage else [],
        }
        fl.ok(step)

        # ---------------------------------------------------------------- 5 Serena edit (candidate)
        step = "05_serena_edit_in_candidate"
        d = ws.decl(bid, TARGET_QN)
        body = original_target[d["full_start"]:d["full_end"]].decode()
        assert 'return "woof";' in body
        edit = {"kind": "symbol_replacement", "declaration_id": d["declaration_id"],
                "new_body": body.replace('"woof"', '"woof woof"')}
        reg = BackendRegistry.for_workspace(ctx, tc)
        try:
            prep = E.prepare(opts, "acc-prep", ws.req(env1, [edit], "acc-prepare-0001", [PROFILE]),
                             registry=reg)
        finally:
            reg.shutdown()
        tid = prep.data["transaction_id"]
        fl.data["prepare"] = {"transaction_id": tid, "state": prep.data.get("state"),
                              "edits": prep.data["edits"]}
        fl.data["workspace_unchanged_after_prepare"] = ws.read(TARGET_FILE) == original_target
        cand = ws.root / ".svweave" / "candidates" / tid / TARGET_FILE
        fl.data["candidate_changed"] = cand.is_file() and cand.read_bytes() != original_target
        fl.ok(step, transaction_id=tid)

        # ---------------------------------------------------------------- 6 candidate validation
        step = "06_candidate_slang_validation"
        val = E.validate(opts, "acc-val", transaction_id=tid)
        assert val.error is None, val.error
        pre_commit_err = None
        try:
            E.commit(opts, "acc-c0", transaction_id=tid,
                     expected_patch_hash=val.data["patch_hash"], request_key="acc-commit-noappr")
        except SvwError as e:
            pre_commit_err = e.code.value
        fl.data["commit_without_approval"] = pre_commit_err
        fl.data["validate"] = {
            "patch_hash": val.data["patch_hash"],
            "diff": val.data["diff"],
            "files_changed": val.data["files_changed"],
            "diagnostics_delta": val.data["diagnostics_delta"],
            "not_run": val.data["not_run"],
            "limitations": list(val.analysis_coverage.limitations) if val.analysis_coverage else [],
            "warnings": list(val.warnings),
            "validation_ok": val.data["validation"]["ok"],
            "auto_committable": val.data["auto_committable"],
        }
        fl.ok(step, patch_hash=val.data["patch_hash"])
        phash = val.data["patch_hash"]

        # ---------------------------------------------------------------- 7 approval + CAS commit
        step = "07_approval_and_cas_commit"
        # the unqualified test-token path is refused unless the TRUSTED policy opts in
        mp.setenv(TEST_TOKEN_ENV, phash[:12])
        try:
            E.approve(opts, "acc-ap00", transaction_id=tid, patch_hash=phash,
                      confirm=env_token_confirm)
            fl.data["test_token_refused_by_default"] = None
        except SvwError as e:
            fl.data["test_token_refused_by_default"] = e.code.value
        ws.w.policy(writable_paths=["fixtures/**"], editing_enabled=True,
                    allow_test_approvals=True)
        wrong = None
        mp.setenv(TEST_TOKEN_ENV, "0" * 12)
        try:
            E.approve(opts, "acc-ap0", transaction_id=tid, patch_hash=phash,
                      confirm=env_token_confirm)
        except SvwError as e:
            wrong = e.code.value
        fl.data["approve_wrong_token"] = wrong
        mp.setenv(TEST_TOKEN_ENV, phash[:12])
        appr = E.approve(opts, "acc-ap1", transaction_id=tid, patch_hash=phash,
                         confirm=env_token_confirm)
        fl.data["approval"] = {"method": appr.data["approval"]["method"],
                               "warnings": list(appr.warnings)}
        mp.delenv(TEST_TOKEN_ENV, raising=False)
        com = E.commit(opts, "acc-c1", transaction_id=tid, expected_patch_hash=phash,
                       request_key="acc-commit-0001")
        assert com.error is None, com.error
        replay = E.commit(opts, "acc-c2", transaction_id=tid, expected_patch_hash=phash,
                          request_key="acc-commit-0001")
        fl.data["commit"] = {k: com.data[k] for k in
                             ("state", "patch_hash", "post_apply", "edit_paths", "not_run")}
        fl.data["commit_replay_idempotent"] = bool(replay.data.get("idempotent_replay"))
        fl.data["commit_files_changed"] = com.data["files_changed"]
        fl.data["final_file_text"] = ws.read(TARGET_FILE).decode()
        fl.data["journal"] = json.loads(
            (ws.root / ".svweave" / "journal" / f"{tid}.json").read_text())
        fl.ok(step)

        # ---------------------------------------------------------------- 8 post-apply state
        step = "08_post_apply_state"
        pa = com.data["post_apply"][PROFILE]
        new_bid = pa["build_id"]
        old = queries.symbol(opts, bid, "acc-old", name=TARGET_QN)
        new = queries.symbol(opts, new_bid, "acc-new", name=TARGET_QN)
        fl.data["post"] = {
            "new_build_id": new_bid, "old_build_id": bid,
            "new_snapshot_id": pa["snapshot_id"], "old_snapshot_id": sid,
            "old_freshness": old.freshness.value, "new_freshness": new.freshness.value,
            "completeness": pa["completeness"], "snapshot_equals_candidate":
            pa["snapshot_equals_candidate"],
        }
        evs = queries_evidence(opts, new_bid)
        fl.data["evidence_states"] = evs
        fl.ok(step)
    except BaseException as e:  # noqa: BLE001 - recorded, tests assert on it
        fl.fail(step, e)
    finally:
        mp.undo()
        os.environ.pop(TEST_TOKEN_ENV, None)
    return fl
