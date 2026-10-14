"""Evidence import/read: UVM-04 (unlinked never attached), REL-01 (states stay separate), SVA-03."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from svweave.application import evidence as E
from svweave.application.build import DB_FILENAME
from svweave.domain.errors import ErrorCode, SvwError
from svweave.storage.db import Database
from tests.conftest import Ws
from tests.unit._gateway_fx import record, stub_build, write_artifact, write_manifest


def imp(opts, tmp: Path, data, name="ev.json"):  # type: ignore[no-untyped-def]
    return E.import_manifest(opts, write_manifest(tmp, data, name), "req-ev")


def rows(ws: Ws) -> int:
    with Database(ws.ctx().state_dir / DB_FILENAME) as db:
        return int(db.conn.execute("SELECT COUNT(*) FROM evidence").fetchone()[0])


def test_schema_violations_are_rejected_and_nothing_is_stored(ws: Ws, tmp_path: Path) -> None:
    opts, bid, sid = stub_build(ws)
    for bad in (
        record(extra_field=1),
        record(result="MAYBE"),
        record(kind="static"),
        record(build_id="short"),
        record(schema_version="2.0"),
        record(artifacts=[{"path": "a", "sha256": "zz"}]),
        record(seed="s" * 100),
    ):
        with pytest.raises(SvwError) as ei:
            imp(opts, tmp_path, bad)
        assert ei.value.code is ErrorCode.INVALID_CONFIG
    with pytest.raises(SvwError):
        E.import_manifest(opts, tmp_path / "missing.json", "r")
    (tmp_path / "bad.json").write_text("{not json")
    with pytest.raises(SvwError) as ei:
        E.import_manifest(opts, tmp_path / "bad.json", "r")
    assert ei.value.code is ErrorCode.INVALID_CONFIG
    assert rows(ws) == 0


def test_artifact_hash_mismatch_and_path_policy(ws: Ws, tmp_path: Path) -> None:
    opts, bid, sid = stub_build(ws)
    rel, digest = write_artifact(ws)
    ok = imp(
        opts,
        ws.root,
        record(build_id=bid, snapshot_id=sid, artifacts=[{"path": rel, "sha256": digest}]),
        "ok.json",
    )
    assert ok.data["records"][0]["link_state"] == "linked_build"
    n = rows(ws)
    with pytest.raises(SvwError) as ei:
        imp(
            opts,
            ws.root,
            record(test_name="other", artifacts=[{"path": rel, "sha256": "0" * 64}]),
            "mm.json",
        )
    assert ei.value.details["reason"] == "artifact_hash_mismatch"
    for escape in ("../../../../etc/hostname", "/etc/hostname"):
        with pytest.raises(SvwError) as ei:
            imp(
                opts,
                ws.root,
                record(test_name="esc", artifacts=[{"path": escape, "sha256": "0" * 64}]),
                "esc.json",
            )
        assert ei.value.code is ErrorCode.PATH_NOT_ALLOWED
    link = ws.root / "out" / "link.log"
    link.symlink_to("/etc/hostname")
    with pytest.raises(SvwError) as ei:
        imp(
            opts,
            ws.root,
            record(test_name="sym", artifacts=[{"path": "out/link.log", "sha256": "0" * 64}]),
            "sym.json",
        )
    assert ei.value.code is ErrorCode.PATH_NOT_ALLOWED
    with pytest.raises(SvwError) as ei:
        imp(
            opts,
            ws.root,
            record(test_name="gone", artifacts=[{"path": "out/none.log", "sha256": "0" * 64}]),
            "gone.json",
        )
    assert ei.value.code is ErrorCode.MISSING_INPUT
    assert rows(ws) == n  # a rejected manifest stores nothing, even partially


def test_untrusted_producer_cannot_claim_runtime_observed(ws: Ws, tmp_path: Path) -> None:
    opts, bid, sid = stub_build(ws)
    env = imp(opts, tmp_path, record(trust_class="RUNTIME_OBSERVED", build_id=bid, snapshot_id=sid))
    r = env.data["records"][0]
    assert r["trust_class"] == "EXTERNAL_DECLARED"
    assert any("stored as EXTERNAL_DECLARED" in w for w in env.warnings)
    assert frozenset() == E.TRUSTED_RUNNERS


def test_UVM_04_unlinked_evidence_is_never_attached_to_a_build(ws: Ws, tmp_path: Path) -> None:
    opts, bid, sid = stub_build(ws)
    cases = {
        "no_source_linkage": record(test_name="a"),
        "build_not_found": record(test_name="b", build_id="9" * 64),
        "snapshot_not_found": record(test_name="c", snapshot_id="8" * 64),
        "snapshot_does_not_match_build": record(test_name="d", build_id=bid, snapshot_id="7" * 64),
        "profile_does_not_match_build": record(
            test_name="e", build_id=bid, snapshot_id=sid, profile="other"
        ),
    }
    for reason, rec in cases.items():
        env = imp(opts, tmp_path, rec, f"{reason}.json")
        r = env.data["records"][0]
        assert (r["link_state"], r["link_reason"], r["build_id"]) == ("unlinked", reason, None), (
            reason
        )
        assert any("UNLINKED" in w for w in env.warnings)
    got = E.get_evidence(opts, "r", build_id=bid)
    assert [i["evidence_type"] for i in got.data["items"]] == ["static"]
    assert got.data["states"]["simulation"] == {
        "state": "NOT_RUN",
        "records": 0,
        "by_result": {},
        "runs": [],
    }
    assert got.data["unlinked_records_in_store"] == len(cases)
    # the unlinked PASS stays retrievable by id, labelled unlinked
    one = E.get_evidence(opts, "r", evidence_id=rows_id(ws, "a"))
    assert one.data["items"][0]["link_state"] == "unlinked" and one.build_id is None


def rows_id(ws: Ws, test_name: str) -> str:
    with Database(ws.ctx().state_dir / DB_FILENAME) as db:
        return str(
            db.conn.execute(
                "SELECT evidence_id FROM evidence WHERE test_name=?", (test_name,)
            ).fetchone()[0]
        )


def test_REL_01_states_are_separate_and_never_merged(ws: Ws, tmp_path: Path) -> None:
    opts, bid, sid = stub_build(ws)
    base = E.get_evidence(opts, "r", build_id=bid).data["states"]
    assert base["static_analysis"]["state"] == "HDL_STATIC_RESOLVED"
    for k in ("eda_compile", "simulation", "formal", "coverage", "sva_execution"):
        assert base[k]["state"] == "NOT_RUN", k
    link = {"build_id": bid, "snapshot_id": sid}
    imp(
        opts,
        tmp_path,
        [
            record(**link, seed="1", result="PASS"),
            record(**link, seed="2", result="FAIL", run_id="r2"),
            record(**link, kind="formal", test_name="f", result="PASS", run_id="f1"),
        ],
    )
    s = E.get_evidence(opts, "r", build_id=bid).data["states"]
    assert s["simulation"]["state"] == "EXTERNAL_DECLARED"
    assert s["simulation"]["by_result"] == {"PASS": 1, "FAIL": 1}  # not collapsed to one PASS
    assert {r["seed"] for r in s["simulation"]["runs"]} == {"1", "2"}
    assert s["formal"]["by_result"] == {"PASS": 1}
    assert s["eda_compile"]["state"] == "NOT_RUN"  # other kinds untouched (SVA-03/REL-01)
    assert s["sva_execution"]["state"] == "EXTERNAL_DECLARED" and "note" in s["sva_execution"]
    assert s["static_analysis"]["state"] == "HDL_STATIC_RESOLVED"


def test_id_is_content_hash_import_is_idempotent_and_relinks(ws: Ws, tmp_path: Path) -> None:
    opts, bid, sid = stub_build(ws)
    rec = record(build_id=bid, snapshot_id=sid)
    a = imp(opts, tmp_path, rec).data["records"][0]
    b = imp(opts, tmp_path, rec).data["records"][0]
    assert a["evidence_id"] == b["evidence_id"] and b["status"] == "already_present"
    assert a["evidence_id"] == E.evidence_id_of(
        E._effective_record(E.EvidenceManifest.model_validate(rec), [])
    )
    assert rows(ws) == 1
    # an unlinked record becomes linked once the build exists (same id, link metadata only)
    import hashlib

    future = hashlib.sha256(f"{sid}:late".encode()).hexdigest()
    late = record(test_name="late", build_id=future, snapshot_id=sid)
    first = imp(opts, tmp_path, late, "late.json").data["records"][0]
    assert first["link_state"] == "unlinked"
    stub_build(ws, tag="late")  # the build now exists
    second = imp(opts, tmp_path, late, "late.json").data["records"][0]
    assert second["evidence_id"] == first["evidence_id"] and second["status"] == "relinked"
    assert second["link_state"] == "linked_build"


def test_read_tools_do_not_write(ws: Ws) -> None:
    opts, bid, sid = stub_build(ws)
    before = rows(ws)
    out = E.get_evidence(opts, "r", build_id=bid)
    assert (
        out.data["items"][0]["evidence_type"] == "static" and out.data["items"][0]["result"] is None
    )
    assert rows(ws) == before  # the static record is shown, not stored, by a read
    E.record_static_for(opts, bid)
    E.record_static_for(opts, bid)  # idempotent
    assert rows(ws) == before + 1
    again = E.get_evidence(opts, "r", build_id=bid)
    assert len(again.data["items"]) == 1
    assert again.data["items"][0]["evidence_id"] == out.data["items"][0]["evidence_id"]


def test_filters_pagination_and_errors(ws: Ws, tmp_path: Path) -> None:
    opts, bid, sid = stub_build(ws)
    link = {"build_id": bid, "snapshot_id": sid}
    imp(opts, tmp_path, [record(**link, seed=str(i), run_id=f"r{i}") for i in range(5)])
    p1 = E.get_evidence(opts, "r", build_id=bid, limit=2)
    assert len(p1.data["items"]) == 2 and p1.pagination.truncated and p1.pagination.next_cursor
    p2 = E.get_evidence(opts, "r", build_id=bid, limit=10, cursor=p1.pagination.next_cursor)
    assert len(p2.data["items"]) == 4 and not p2.pagination.truncated
    sims = E.get_evidence(opts, "r", build_id=bid, evidence_type="simulation")
    assert sims.data["total_matches"] == 5
    with pytest.raises(SvwError):
        E.get_evidence(opts, "r", build_id=bid, evidence_id=bid)
    with pytest.raises(SvwError):
        E.get_evidence(opts, "r")
    with pytest.raises(SvwError) as ei:
        E.get_evidence(opts, "r", evidence_id="1" * 64)
    assert ei.value.code is ErrorCode.MISSING_INPUT
    with pytest.raises(SvwError):  # cursor of another build/query is refused
        E.get_evidence(
            opts, "r", build_id=bid, evidence_type="formal", cursor=p1.pagination.next_cursor
        )


def test_docs_example_validates_against_the_schema() -> None:
    import re

    doc = (Path(__file__).resolve().parents[2] / "docs" / "operations.md").read_text()
    m = re.search(r"```json evidence-manifest\n(.*?)```", doc, re.S)
    assert m
    E.EvidenceManifest.model_validate(json.loads(m.group(1)))
