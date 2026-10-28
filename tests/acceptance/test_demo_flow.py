"""Plan 1.3 demo flow and plan 28.1 checkpoint artifacts, against the REAL pinned backends.

No mocks, no regex symbol lookup, no fixed JSON: pyslang (separate worker), codebase-memory-mcp
and Serena (+ Verible) are really started in their bwrap sandboxes. The workspace is a temporary
copy of the repository fixtures; the repository tree is never edited.
"""

from __future__ import annotations

import json
import os

import pytest

from tests.acceptance import flow as F
from tests.integration.conftest import skip_or_fail

pytestmark = [pytest.mark.acceptance, pytest.mark.integration]
REPO = F.REPO


@pytest.fixture(scope="module")
def flow(tmp_path_factory: pytest.TempPathFactory) -> F.Flow:
    why = F.backends_available()
    if why:
        skip_or_fail(why)
    tmp = tmp_path_factory.mktemp("acceptance")
    fl = F.run_flow(tmp)
    out = REPO / ".svweave" / "acceptance"
    out.mkdir(parents=True, exist_ok=True)
    (out / "last_flow.json").write_text(
        json.dumps({"steps": fl.steps, "failed_step": fl.failed_step, "data": fl.data},
                   indent=2, default=str)
    )
    return fl


def _need(flow: F.Flow, step: str) -> None:
    assert flow.failed_step is None, (
        f"flow stopped at {flow.failed_step}: {flow.steps.get(flow.failed_step or '')}\n{flow.error}"
    )
    assert flow.steps[step]["status"] == "PASS"


def test_flow_completes_every_step(flow: F.Flow) -> None:
    _need(flow, "08_post_apply_state")
    assert [s for s, v in flow.steps.items() if v["status"] != "PASS"] == []
    assert len(flow.steps) == 8


def test_01_profile_loaded_and_consistent_snapshot_built(flow: F.Flow) -> None:
    _need(flow, "01_profile_and_snapshot")
    d = flow.data
    assert len(d["snapshot_id"]) == 64 and len(d["build_id"]) == 64
    assert d["tops"] == ["demo_top", "sem08_top"]
    assert d["ledger_entries"] > 0
    files = d["snapshot_files"]
    assert any(f.endswith("demo/rtl/demo_top.sv") for f in files)
    assert any(f.endswith("demo/include/demo_defs.svh") for f in files), "header is in the snapshot"
    assert d["build_counts"]["errors"] == 0


def test_02_all_three_backends_really_probed(flow: F.Flow) -> None:
    _need(flow, "02_backend_health_probes")
    h = flow.data["health"]
    assert h["pyslang"]["status"] == "READY" and h["pyslang"]["probed"]
    for name, rec in h.items():
        assert rec["probed"], name
        assert rec["status"] in ("READY", "DEGRADED"), (name, rec)
    cbm = next(v for k, v in h.items() if "codebase" in k or k == "cbm")
    # a DEGRADED graph backend must carry its recorded reasons; never silently READY
    assert cbm["status"] != "READY" or not cbm["limitations"]
    if cbm["status"] == "DEGRADED":
        assert cbm["reasons"] or cbm["limitations"]


def test_03_graph_search_finds_candidate_files_with_limitations(flow: F.Flow) -> None:
    _need(flow, "03_graph_search_and_gap")
    g = flow.data["graph"]
    for q in ("demo_lane", "dog"):
        assert g[q]["backend_used"] == "codebase-memory-mcp", (q, g[q]["warnings"])
        assert g[q]["degraded"] is False
        assert g[q]["evidence_kind"] == "SOURCE_CANDIDATE"  # candidates, never semantic truth
        assert g[q]["limitations"], "graph results always state the extraction limitations"
    assert any(f.endswith("demo/rtl/demo_lane.sv") for f in g["demo_lane"]["files"])
    assert any(f.endswith("sem08_virtual/top.sv") for f in g["dog"]["files"])


def test_03b_a_file_the_graph_misses_is_reported_not_hidden(flow: F.Flow) -> None:
    """demo_bind.sv holds a bind directive that cbm does not extract (M0 spike)."""
    _need(flow, "03_graph_search_and_gap")
    g = flow.data["graph"][F.BIND_ONLY_TERM]
    bind_in_graph = any(f.endswith("demo/sva/demo_bind.sv") for f in g["files"])
    # the literal text search (a different, marked mechanism) does see the file
    assert flow.data["text_fallback_backend"] == "text_fallback"
    assert any(f.endswith("demo/sva/demo_bind.sv") for f in flow.data["text_fallback_bind_files"])
    if not bind_in_graph:
        # the gap is real: the response must not look complete
        assert g["outcome"] != "OK", "a graph miss must not be reported as a clean OK"
        lim = " ".join(g["limitations"])
        assert "absence of a hit is not proof of absence" in lim
        assert "bind" in lim and "partially" in lim
    else:  # cbm improved: the file is found, nothing to hide (still SOURCE_CANDIDATE evidence)
        assert g["evidence_kind"] == "SOURCE_CANDIDATE"


def test_04_slang_answers_for_the_selected_build(flow: F.Flow) -> None:
    _need(flow, "04_slang_hierarchy_symbol_references")
    d = flow.data
    paths = d["hierarchy_paths"]
    assert "demo_top" in paths
    assert "demo_top.g_lane[0].u_lane" in paths and "demo_top.g_lane[1].u_lane" in paths
    assert d["hierarchy_freshness"] == "CURRENT"
    assert d["demo_lane_refs"]["n_items"] >= 1
    assert d["demo_lane_refs"]["searched_scope"]  # the searched scope is stated
    a, b = d["same_name_methods"].values()
    assert a != b, "same-name methods in different classes stay distinct declarations"
    assert d["target_refs"]["limitations"], "reference results carry their limitations"


def test_05_serena_edits_the_candidate_and_never_the_original(flow: F.Flow) -> None:
    _need(flow, "05_serena_edit_in_candidate")
    d = flow.data
    [rec] = d["prepare"]["edits"]
    assert rec["edit_path"] == "serena_symbol" and rec["counted_as_serena_semantic_edit"]
    assert rec["decl_kind"] == "function"  # ADR-012: only functions are Serena-qualified
    assert rec["qualified_name"] == F.TARGET_QN
    assert rec["serena"]["within_target"] and rec["serena"]["name_path"].endswith("dog/speak")
    assert d["workspace_unchanged_after_prepare"] is True
    assert d["candidate_changed"] is True


def test_06_candidate_validated_by_slang_with_diff_and_limitations(flow: F.Flow) -> None:
    _need(flow, "06_candidate_slang_validation")
    v = flow.data["validate"]
    assert v["validation_ok"] and v["auto_committable"]
    assert '-      return "woof";' in v["diff"] and '+      return "woof woof";' in v["diff"]
    assert v["diff"].count("\n+") <= 2 + 1
    assert v["not_run"] == {"eda_compile": "NOT_RUN", "simulation": "NOT_RUN", "formal": "NOT_RUN"}
    [fc] = v["files_changed"]
    assert fc["logical_path"] == F.TARGET_FILE and fc["added_lines"] == 1 == fc["removed_lines"]
    delta = v["diagnostics_delta"][F.PROFILE]
    assert delta["errors"] == [0, 0] and delta["new_warnings"] == []
    assert v["limitations"], "the validation states what it does not prove"
    assert flow.data["commit_without_approval"] == "APPROVAL_REQUIRED"


def test_07_approval_is_required_and_commit_is_cas_checked(flow: F.Flow) -> None:
    _need(flow, "07_approval_and_cas_commit")
    d = flow.data
    assert d["test_token_refused_by_default"] == "APPROVAL_REQUIRED"  # policy must opt in
    assert d["approve_wrong_token"] == "APPROVAL_REQUIRED"  # wrong typed hash is refused
    assert d["approval"]["method"] == "test_token"
    assert any("NON-QUALIFIED" in w for w in d["approval"]["warnings"])  # flagged, not qualified
    assert d["commit"]["state"] == "POST_APPLY_VERIFIED"
    assert d["commit_replay_idempotent"] is True
    assert d["commit"]["edit_paths"] == ["serena_symbol"]
    text = d["final_file_text"]
    assert 'return "woof woof";' in text and 'return "meow";' in text  # only dog::speak changed
    assert d["journal"]["status"] == "complete"
    [jf] = d["journal"]["files"]
    assert jf["status"] == "applied" and jf["before_sha256"] != jf["after_sha256"]


def test_08_post_apply_current_old_historical_eda_not_run(flow: F.Flow) -> None:
    _need(flow, "08_post_apply_state")
    p = flow.data["post"]
    assert p["new_build_id"] != p["old_build_id"] and p["new_snapshot_id"] != p["old_snapshot_id"]
    assert p["new_freshness"] == "CURRENT"
    assert p["old_freshness"] == "HISTORICAL"
    assert p["completeness"] == "COMPLETE" and p["snapshot_equals_candidate"] is True
    ev = flow.data["evidence_states"]
    for k in ("eda_compile", "simulation", "formal"):
        assert ev[k]["state"] == "NOT_RUN", (k, ev[k])
    assert flow.data["commit"]["not_run"] == {
        "eda_compile": "NOT_RUN", "simulation": "NOT_RUN", "formal": "NOT_RUN"}


# ------------------------------------------------------------------------- plan 28.1 checkpoint
def test_checkpoint_28_1_artifacts(flow: F.Flow) -> None:
    """pinned toolchain + resolved manifest + real snapshot + generate hierarchy + real graph query
    + Serena candidate edit + capability report listing successes AND limitations."""
    for s in ("01_profile_and_snapshot", "03_graph_search_and_gap",
              "04_slang_hierarchy_symbol_references", "05_serena_edit_in_candidate"):
        _need(flow, s)
    # (1) a pinned toolchain: every component has a version and a hash, none null
    lock = json.loads((REPO / "toolchain.lock.json").read_text())
    comps = lock["components"]
    assert {"slang", "pyslang", "serena", "verible", "codebase_memory_mcp"} <= set(comps)

    def no_nulls(o: object, path: str = "") -> None:
        if isinstance(o, dict):
            for k, v in o.items():
                no_nulls(v, f"{path}.{k}")
        elif isinstance(o, list):
            for i, v in enumerate(o):
                no_nulls(v, f"{path}[{i}]")
        else:
            assert o not in (None, ""), f"null/empty lock field {path}"

    no_nulls(comps, "components")
    for name, c in comps.items():
        assert c.get("version") or c.get("commit") or c.get("package"), name
        assert any(("sha256" in k) and c[k] for k in c), f"{name}: no hash"
    # (2) a complete resolved manifest
    assert flow.data["config_id"] and flow.data["ledger_entries"] > 0
    manifests = list((flow.root / ".svweave" / "manifests").glob("*.json"))
    assert manifests, "the normalized manifest is stored"
    nm = json.loads(manifests[0].read_text())
    assert nm["top_modules"] == ["demo_top", "sem08_top"] and nm["ledger"]
    # (3) a real source snapshot
    sdir = flow.root / ".svweave" / "snapshots" / flow.data["snapshot_id"]
    assert (sdir / "snapshot.json").is_file() and (sdir / "files").is_dir()
    # (4) a generate hierarchy found by slang
    assert "demo_top.g_lane[0].u_lane" in flow.data["hierarchy_paths"]
    # (5) a real graph query
    assert flow.data["graph"]["demo_lane"]["backend_used"] == "codebase-memory-mcp"
    # (6) a Serena candidate method edit
    assert flow.data["prepare"]["edits"][0]["edit_path"] == "serena_symbol"
    # (7) a capability report listing successes and limits
    cap = json.loads((REPO / "capability_report.json").read_text())
    assert cap["summary"]["pyslang"] == "READY"
    assert cap["summary"]["cbm"] in ("READY", "DEGRADED")
    assert cap["summary"]["serena"] in ("READY", "DEGRADED")
    assert cap["limitations"], "the capability report lists limitations, not only successes"
    assert cap["qualification_state"] == "UNQUALIFIED"
    lock_hash = cap.get("toolchain_lock_components_sha256")
    assert lock_hash


# --------------------------------------------------------------------------------------- docs
def test_readme_exists_and_every_documented_command_resolves() -> None:
    """DOC-01 for the README (the unit test also scans it): every `svweave ...` shown must exist."""
    from tests.unit.test_gateway_cli import doc_commands, resolves

    readme = REPO / "README.md"
    assert readme.is_file(), "README.md is missing"
    cmds = doc_commands(readme.read_text())
    assert len(cmds) >= 10
    missing = [" ".join(w) for _, w in cmds if w and not resolves(w)]
    assert not missing, missing
    text = readme.read_text()
    assert "SystemVerilog-first" in text and "UNQUALIFIED" in text
    assert "[MIT License](LICENSE)" in text and "TBD by owner" not in text
    assert (REPO / "LICENSE").read_text().startswith("MIT License\n")
    assert os.path.isfile(REPO / "docs" / "limitations.md")
