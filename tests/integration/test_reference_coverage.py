"""HDL-COV-01/02: real mandatory-sandbox slang -> DTO -> storage -> query coverage.

These tests do not use an in-process extractor, a fallback backend, or Verilator inventory.
Inline expected spans/counts are hand-reviewed source expectations, not generated goldens.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from svweave.application import impact, queries, rename
from svweave.domain.envelope import Envelope, ReferenceCoverage
from tests.conftest import Ws, profile
from tests.integration.helpers import build, decl_by_qn, decl_rows, lang_ws, opts

pytestmark = pytest.mark.integration


def _coverage(env: Envelope) -> ReferenceCoverage:
    assert env.analysis_coverage is not None
    cov = env.analysis_coverage.reference_coverage
    assert cov is not None and cov.absence_proof_supported is False
    return cov


def test_HDL_COV_01_complete_build_does_not_prove_absent_dimension_type_references(
    tmp_path: Path,
) -> None:
    w = lang_ws(tmp_path, "refs_coverage", "cov_top")
    env = build(w)
    bid = env.build_id
    assert bid and env.analysis_coverage is not None
    assert env.analysis_coverage.completeness == "COMPLETE", env.to_json()
    assert _coverage(env).status == "PARTIAL"
    rows = decl_rows(w, bid)
    for qn, feature in (
        ("cov_pkg::WIDTH", "packed_dimension_expressions"),
        ("cov_pkg::return_t", "subroutine_return_type_names"),
    ):
        target = decl_by_qn(rows, qn)[0]
        refs = queries.references(opts(w), bid, target["symbol_id"], "r")
        assert refs.data["items"] == [] and refs.data["total_matches"] == 0
        assert _coverage(refs).features[feature] == "UNSUPPORTED"
        assert "known_reference_omissions" in _coverage(refs).blockers
        affected = impact.impact(opts(w), bid, "r", symbol_id=target["symbol_id"])
        assert _coverage(affected).query == "impact"
        preview = rename.rename_preview(opts(w), bid, "r", symbol_id=target["symbol_id"],
                                        new_name="changed_name")
        assert _coverage(preview).query == "rename_preview"
    # Closed expected target occurrence set: exactly one active call, exact original bytes.
    called = decl_by_qn(rows, "cov_pkg::make_value")[0]
    refs = queries.references(opts(w), bid, called["symbol_id"], "r")
    start = (w.root / "top.sv").read_bytes().index(b"make_value()")
    assert [(r["location"]["file"], r["location"]["start"], r["location"]["end"],
             r["target_declaration_id"], r["resolution_status"]) for r in refs.data["items"]] == [
        ("top.sv", start, start + len("make_value"), called["declaration_id"], "resolved_static")
    ]
    assert _coverage(refs).scopes["uninstantiated_generic_bodies"] == "NOT_SEARCHED"


def test_HDL_COV_02_inactive_and_unloaded_scopes_are_not_searched(tmp_path: Path) -> None:
    unused = lang_ws(tmp_path / "unused", "sem10_unused", "sem10_top", library_dirs=["lib"])
    env = build(unused)
    cov = _coverage(env)
    assert cov.observations is not None
    assert cov.observations.scope_gaps["generic_class_not_specialized"] > 0
    assert cov.observations.scope_gaps["library_member_not_loaded"] > 0
    assert cov.scopes["unloaded_library_members"] == "NOT_SEARCHED"
    inactive = lang_ws(tmp_path / "inactive", "sem04_ifdef", "sem04_top")
    env = build(inactive)
    cov = _coverage(env)
    assert cov.observations is not None
    assert cov.observations.inactive_regions["disabled_conditional_region"] > 0
    assert cov.observations.scope_gaps["definition_not_elaborated"] > 0
    assert cov.scopes["inactive_branches"] == "NOT_SEARCHED"


@pytest.mark.parametrize("external", [False, True])
def test_HDL_COV_02_unsupported_project_and_external_root_are_distinct(
    tmp_path: Path, external: bool,
) -> None:
    root = tmp_path / "ws"
    root.mkdir()
    w = Ws(root)
    w.write("top.sv", "module cov_top(input wire a, output wire y);\n"
            "  timing_cell u_cell(a, y);\nendmodule\n")
    source = (
        "module timing_cell(input wire a, output wire y);\n"
        "  assign y = a;\n"
        "  specify\n    specparam delay_value = 1;\n"
        "    (a => y) = delay_value;\n  endspecify\nendmodule\n"
    )
    kwargs = {}
    if external:
        lib = tmp_path / "external"
        lib.mkdir()
        (lib / "cell.sv").write_text(source)
        path = "@lib/cell.sv"
        kwargs["source_roots"] = [
            {"id": "project", "path": ".", "include": ["**"], "exclude": []},
            {"id": "lib", "include": ["**"], "exclude": []},
        ]
        w.local([{"id": "lib", "path": str(lib)}])
        w.policy(allowed_roots=[str(root), str(lib)])
    else:
        path = "cell.sv"
        w.write(path, source)
        w.policy()
    w.config({"p": profile(
        source_inputs=[{"kind": "file", "path": path}, {"kind": "file", "path": "top.sv"}],
        top_modules=["cov_top"],
    )}, **kwargs)
    env = build(w)
    cov = _coverage(env)
    assert cov.observations is not None
    origin = "external" if external else "project"
    assert getattr(cov.observations, f"unsupported_nodes_{origin}") > 0
    assert f"unsupported_{origin}_nodes" in cov.blockers
    assert env.analysis_coverage is not None
    assert env.analysis_coverage.completeness == ("COMPLETE" if external else "PARTIAL")
