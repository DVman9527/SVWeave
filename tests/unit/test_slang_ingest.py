"""Worker DTO -> rows: identity rules and the completeness verdict (plan 9.1, 9.5, 13.1)."""

from __future__ import annotations

import copy
from typing import Any

import pytest

from svweave.adapters.slang.deps import check_dependencies
from svweave.adapters.slang.ingest import (
    ingest,
    logical_of_snapshot_rel,
    snapshot_rel_of_logical,
)
from svweave.domain.ids import sha256_hex
from svweave.domain.states import Completeness, DiagnosticOutcome
from svweave.snapshot.models import (
    LookupInventory,
    SnapFile,
    SnapshotRecord,
    SnapshotSpec,
)

SID = sha256_hex("snap")
BID = sha256_hex("build")


def snap(*paths: str) -> SnapshotRecord:
    files = [
        SnapFile(
            logical_path=p,
            original_path="/o/" + p,
            snapshot_path="files/" + snapshot_rel_of_logical(p),
            sha256=sha256_hex(p),
            size=1,
            role="source",
        )
        for p in paths
    ]
    return SnapshotRecord(
        snapshot_id=SID,
        workspace_id="w",
        profile="p",
        config_id=sha256_hex("c"),
        created_at="t",
        attempts=1,
        spec=SnapshotSpec(explicit=[], lookups=[], root_ids=["project"]),
        files=files,
        lookup=[],
    )


def loc(file: str = "a.sv", start: int = 0, end: int = 3, **kw: Any) -> dict[str, Any]:
    return {
        "file": file,
        "start": start,
        "end": end,
        "line": 1,
        "col": start + 1,
        "end_line": 1,
        "end_col": end + 1,
        **kw,
    }


def decl(
    idx: int, name: str = "m", kind: str = "module", qn: str | None = None, **kw: Any
) -> dict[str, Any]:
    return {
        "idx": idx,
        "kind": kind,
        "name": name,
        "qualified_name": qn or name,
        "loc": kw.pop("loc", loc()),
        "full": None,
        "cu": kw.pop("cu", 0),
        "library": None,
        "type": None,
        "attrs": kw.pop("attrs", {}),
        **kw,
    }


def dto(**over: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "schema_version": "svw-extract-1",
        "extractor_version": "x",
        "pyslang_version": "12",
        "status": {"compiled": True},
        "driver_messages": [],
        "diagnostics": [],
        "dependencies": {},
        "declarations": [decl(0)],
        "references": [],
        "instances": [],
        "port_bindings": [],
        "assertions": [],
        "specializations": [],
        "inactive": [],
        "gaps": [],
        "unsupported_nodes": [],
        "stats": {},
    }
    base.update(over)
    return base


def run(result: dict[str, Any], *files: str) -> Any:
    return ingest(result, snap(*(files or ("a.sv",))), BID, "job", ["m"])


def ref(
    kind: str = "value", status: str = "resolved_static", target: int | None = 0, **kw: Any
) -> dict[str, Any]:
    return {
        "kind": kind,
        "loc": kw.pop("loc", loc("a.sv", 10, 12)),
        "target_idx": target,
        "target_name": kw.pop("target_name", None),
        "status": status,
        "reason": kw.pop("reason", None),
        "context": kw.pop("context", {}),
        "container_idx": None,
        **kw,
    }


def test_clean_build_is_complete_and_names_its_scope() -> None:
    r = run(dto())
    assert (
        r.completeness == Completeness.COMPLETE
        and r.diagnostic_outcome == DiagnosticOutcome.NO_ERRORS
    )
    assert r.reasons == [] and r.scope == "m"
    assert any("NOT_RUN" in x for x in r.limitations)  # the standing limitations are always present


@pytest.mark.parametrize(
    ("over", "needle"),
    [
        (
            {
                "diagnostics": [
                    {
                        "severity": "error",
                        "code": "X",
                        "is_error": True,
                        "message": "m",
                        "option": None,
                        "loc": loc(),
                        "ranges": [],
                        "macro_notes": [],
                    }
                ]
            },
            "HAS_ERRORS",
        ),
        ({"driver_messages": ["error: 'x.sv': No such file or directory"]}, "HAS_ERRORS"),
        ({"status": {"compiled": False}}, "command line"),
        (
            {
                "references": [
                    ref(
                        "unresolved",
                        "candidate_source",
                        None,
                        target_name="zz",
                        reason="UndeclaredIdentifier",
                    )
                ]
            },
            "unresolved",
        ),
        (
            {"references": [ref("call", "unsupported", None, target_name="f", reason="r")]},
            "unresolved",
        ),
        ({"gaps": [{"kind": "stub_boundary", "name": "ip", "detail": "stub"}]}, "stub"),
        (
            {
                "unsupported_nodes": [
                    {
                        "kind": "Checker",
                        "scope": "project",
                        "count": 1,
                        "sample": loc("a.sv"),
                        "detail": "d",
                    }
                ]
            },
            "unsupported node",
        ),
        (
            {
                "unsupported_nodes": [
                    {
                        "kind": "extractor_exception:x",
                        "scope": "project",
                        "count": 1,
                        "sample": None,
                        "detail": "d",
                    }
                ]
            },
            "unsupported node",
        ),
    ],
)
def test_partial_conditions_are_never_complete(over: dict[str, Any], needle: str) -> None:
    r = run(dto(**over))
    assert r.completeness == Completeness.PARTIAL, r.reasons  # mutation guard: PARTIAL -> COMPLETE
    assert any(needle in x for x in r.reasons)


def test_warnings_dynamic_dispatch_and_external_unsupported_do_not_block_completeness() -> None:
    warn = {
        "severity": "warning",
        "code": "W",
        "is_error": False,
        "message": "w",
        "option": "x",
        "loc": loc(),
        "ranges": [],
        "macro_notes": [],
    }
    r = run(
        dto(
            diagnostics=[warn],
            references=[
                ref("call", "dynamic_unresolved", 0, reason="virtual_method_runtime_dispatch")
            ],
            unsupported_nodes=[
                {
                    "kind": "Checker",
                    "scope": "external",
                    "count": 4,
                    "sample": loc("_ext/uvm/x.svh"),
                    "detail": "d",
                }
            ],
            gaps=[{"kind": "generic_class_not_specialized", "name": "g", "detail": "d"}],
        ),
        "a.sv",
        "@uvm/x.svh",
    )
    assert (
        r.completeness == Completeness.COMPLETE and r.dynamic_count == 1 and r.unresolved_count == 0
    )
    assert any("dynamic_unresolved" in x for x in r.limitations)
    assert any("Generic classes" in x for x in r.limitations)
    assert r.summary["warnings"] == 1


def test_declaration_identity_separates_cu_macro_and_scope_but_not_repeats() -> None:
    a = decl(0, "W", "parameter", "t1.W", loc=loc("h.svh", 5, 6), cu=0)
    b = decl(1, "W", "parameter", "t1.W", loc=loc("h.svh", 5, 6), cu=1)  # other CU
    c = decl(2, "W", "parameter", "t2.W", loc=loc("h.svh", 5, 6), cu=0)  # other scope
    d = decl(
        3,
        "W",
        "parameter",
        "t1.W",
        loc=loc("h.svh", 5, 6, macro={"name": "M", "expansion": {"file": "a.sv", "start": 9}}),
        cu=0,
    )
    r = run(dto(declarations=[a, b, c, d]), "h.svh")
    ids = [x["declaration_id"] for x in r.rows["declarations"]]
    assert len(ids) == 4 == len(set(ids))
    assert (
        r.rows["declarations"][0]["declaration_id"]
        == run(dto(declarations=[a]), "h.svh").rows["declarations"][0]["declaration_id"]
    )
    # ids change with the file bytes hash (source_id), i.e. per snapshot content
    other = ingest(dto(declarations=[a]), snap("h.svh"), sha256_hex("other"), "job", [])
    assert (
        other.rows["declarations"][0]["declaration_id"]
        == r.rows["declarations"][0]["declaration_id"]
    )
    changed = SnapshotRecord.model_validate(
        {
            **snap("h.svh").model_dump(),
            "files": [{**snap("h.svh").files[0].model_dump(), "sha256": sha256_hex("new")}],
        }
    )
    assert (
        ingest(dto(declarations=[a]), changed, BID, "job", []).rows["declarations"][0][
            "declaration_id"
        ]
        != r.rows["declarations"][0]["declaration_id"]
    )


def test_instance_ids_come_from_components_never_from_a_split_path() -> None:
    def inst(idx: int, comps: list[Any], parent: int | None) -> dict[str, Any]:
        return {
            "idx": idx,
            "path": "t.x.y",
            "hier_path": "t.x.y",
            "components": comps,
            "parent_idx": parent,
            "definition": "leaf",
            "definition_decl_idx": 0,
            "parameters": [],
            "specialization": "",
            "generate_context": [],
            "is_top": parent is None,
            "is_interface": False,
            "stub": False,
            "stub_name": None,
            "blackbox_candidate": False,
            "loc": None,
            "bind": None,
        }

    c = lambda n: {"kind": "instance", "name": n, "index": None}  # noqa: E731
    r = run(
        dto(
            instances=[
                inst(0, [c("t")], None),
                inst(1, [c("t"), c("x.y")], 0),
                inst(2, [c("t"), c("x"), c("y")], 0),
            ]
        )
    )
    ids = [i["instance_id"] for i in r.rows["instances"]]
    assert len(set(ids)) == 3  # identical display paths, distinct identities
    # build-scoped and specialization-sensitive
    r2 = ingest(dto(instances=[inst(0, [c("t")], None)]), snap("a.sv"), sha256_hex("b2"), "job", [])
    assert r2.rows["instances"][0]["instance_id"] != ids[0]
    spec = inst(0, [c("t")], None)
    spec["specialization"] = "W=8"
    assert run(dto(instances=[spec])).rows["instances"][0]["instance_id"] != ids[0]


def test_reference_rows_are_deduplicated_per_occurrence_and_keep_status() -> None:
    r = run(
        dto(
            references=[
                ref(),
                ref(),
                ref("call"),
                ref("call", "dynamic_unresolved", 0, reason="v", loc=loc("a.sv", 20, 22)),
            ]
        )
    )
    assert len(r.rows["references"]) == 3
    assert {x["resolution_status"] for x in r.rows["references"]} == {
        "resolved_static",
        "dynamic_unresolved",
    }
    assert all(len(x["reference_id"]) == 64 for x in r.rows["references"])


def test_snapshot_path_mapping_is_inverse() -> None:
    for logical in ("rtl/a.sv", "@uvm/base/x.svh", "@uvm/uvm_pkg.sv"):
        assert logical_of_snapshot_rel(snapshot_rel_of_logical(logical)) == logical
    assert snapshot_rel_of_logical("@uvm/a/b.sv") == "_ext/uvm/a/b.sv"


def test_dependency_check_flags_outside_and_uninventoried_but_allows_adapter_files() -> None:
    s = snap("a.sv", "@uvm/p.sv")
    deps = {
        "files": [
            {"path": "a.sv", "outside_root": False, "kind": "DesignFile"},
            {"path": "_ext/uvm/p.sv", "outside_root": False, "kind": "DesignFile"},
            {"path": "/run/g1.f", "outside_root": True, "kind": "DesignFile"},
        ],
        "total_buffers": 3,
    }
    assert check_dependencies(copy.deepcopy(deps), s, ["/run/g1.f"])["ok"] is True
    bad = copy.deepcopy(deps)
    bad["files"] += [
        {"path": "/etc/passwd", "outside_root": True, "kind": "IncludeFile"},
        {"path": "sneaky.svh", "outside_root": False, "kind": "IncludeFile"},
    ]
    rep = check_dependencies(bad, s, ["/run/g1.f"])
    assert rep["ok"] is False
    assert {(v["path"], v["reason"]) for v in rep["violations"]} == {
        ("/etc/passwd", "read_outside_snapshot"),
        ("sneaky.svh", "not_in_snapshot_inventory"),
    }


def test_dependency_check_lists_library_members_that_were_never_loaded() -> None:
    s = snap("a.sv", "lib/used.sv", "lib/unused.sv")
    s.lookup.append(
        LookupInventory(
            kind="library",
            logical_dir="lib",
            recursive=False,
            original_dir="/o/lib",
            entries=["unused.sv", "used.sv"],
        )
    )
    deps = {
        "files": [
            {"path": "a.sv", "outside_root": False, "kind": "DesignFile"},
            {"path": "lib/used.sv", "outside_root": False, "kind": "LibraryFile"},
        ]
    }
    rep = check_dependencies(deps, s, [])
    assert rep["ok"] and rep["library_members_not_loaded"] == ["lib/unused.sv"]


def test_L1_declaration_id_collision_is_an_internal_error_not_a_silent_merge() -> None:
    from svweave.domain.errors import ErrorCode, SvwError

    a = decl(0, "W", "parameter", "t1.W", loc=loc("h.svh", 5, 6), cu=0)
    twin = decl(1, "W", "parameter", "t1.W", loc=loc("h.svh", 5, 6), cu=0)  # same identity
    with pytest.raises(SvwError) as ei:
        run(dto(declarations=[a, twin]), "h.svh")
    assert ei.value.code == ErrorCode.INTERNAL and "collision" in ei.value.message
    assert ei.value.details["first_idx"] == 0 and ei.value.details["second_idx"] == 1


def test_M5_driver_step_flags_are_read_not_inferred_from_stderr_text() -> None:
    for step in ("parseCommandLine", "processOptions", "parseAllSources"):
        st = {
            "compiled": True,
            "parseCommandLine": True,
            "processOptions": True,
            "parseAllSources": True,
            step: False,
        }
        r = run(dto(status=st))  # no driver message, no diagnostic: only the flag says it failed
        assert r.completeness == Completeness.PARTIAL, step
        assert r.diagnostic_outcome == DiagnosticOutcome.HAS_ERRORS
        assert any(step in x for x in r.reasons)
    ok = {
        "compiled": True,
        "parseCommandLine": True,
        "processOptions": True,
        "parseAllSources": True,
    }
    assert run(dto(status=ok)).completeness == Completeness.COMPLETE


def unsup(scope: str, n: int = 1, kind: str = "Specparam") -> dict[str, Any]:
    f = "_ext/lib/x.sv" if scope == "external" else "a.sv"
    return {"kind": kind, "scope": scope, "count": n, "sample": loc(f), "detail": "d"}


def test_B1_unsupported_occurrences_are_judged_per_occurrence_not_first_sample() -> None:
    both = [unsup("external", 5), unsup("project", 2)]
    for order in (both, both[::-1]):  # instantiation order must not matter
        r = run(dto(unsupported_nodes=order), "a.sv", "@lib/x.sv")
        assert r.completeness == Completeness.PARTIAL
        assert any("2 unsupported node occurrence(s) in project files" in x for x in r.reasons)
        assert any("5 unsupported node occurrence(s) in external" in x for x in r.limitations)
    ext_only = run(dto(unsupported_nodes=[unsup("external", 9)]), "a.sv", "@lib/x.sv")
    assert ext_only.completeness == Completeness.COMPLETE
    assert any("9 unsupported node occurrence(s) in external" in x for x in ext_only.limitations)
    rows = run(dto(unsupported_nodes=both), "a.sv", "@lib/x.sv").rows["unsupported_nodes"]
    assert sorted((r["in_scope"], r["count"]) for r in rows) == [(0, 5), (1, 2)]


def test_M1_dropped_reference_occurrences_make_project_builds_partial() -> None:
    proj = run(dto(dropped_refs=[{"reason": "iterator_variable", "scope": "project", "count": 3}]))
    assert proj.completeness == Completeness.PARTIAL
    assert any("3 reference occurrence(s) in project files" in x for x in proj.reasons)
    assert any(
        "3 reference occurrence(s) dropped in project files: iterator_variable" in x
        for x in proj.limitations
    )
    ext = run(
        dto(dropped_refs=[{"reason": "iterator_variable", "scope": "external", "count": 1765}])
    )
    assert ext.completeness == Completeness.COMPLETE
    assert any(
        "1765 reference occurrence(s) dropped in external files" in x for x in ext.limitations
    )
    assert ext.summary["dropped_refs_external"] == 1765 and ext.summary["dropped_refs_project"] == 0


def diag(code: str, message: str = "m", *, error: bool = True, at: int = 0) -> dict[str, Any]:
    return {
        "severity": "error" if error else "warning",
        "code": code,
        "is_error": error,
        "message": message,
        "option": None,
        "loc": loc(start=at, end=at),
        "ranges": [],
        "macro_notes": [],
    }


def test_missing_timescale_verdict_names_the_profile_knob() -> None:
    r = run(dto(diagnostics=[diag("MissingTimeScale"), diag("MissingTimeScale")]))
    assert r.completeness == Completeness.PARTIAL
    assert any("2 MissingTimeScale" in x and "language_options.timescale" in x for x in r.reasons)
    assert not any("MissingTimeScale" in x for x in run(dto()).reasons)


def _param_diag(mod: str, at: int) -> dict[str, Any]:
    return diag("ParameterDoesNotExist", f"parameter 'W' does not exist in '{mod}'", at=at)


def test_parameter_errors_on_a_protected_module_are_attributed_to_the_envelope() -> None:
    result = dto(
        declarations=[
            decl(0, "prot", loc=loc(start=7, end=11), full={"start": 0, "end": 238}),
            decl(1, "plain", loc=loc(start=246, end=251), full={"start": 239, "end": 274}),
        ],
        diagnostics=[
            diag("ProtectedEnvelope", error=False, at=162),
            _param_diag("prot", 300),
            _param_diag("prot", 310),
            _param_diag("plain", 320),
        ],
    )
    r = run(result)
    (line,) = [x for x in r.reasons if "ParameterDoesNotExist" in x]
    assert line.startswith("2 ParameterDoesNotExist") and "(prot)" in line and "plain" not in line
    # without an envelope in the module's text there is no attribution
    result["diagnostics"] = [d for d in result["diagnostics"] if d["code"] != "ProtectedEnvelope"]
    assert not any("ParameterDoesNotExist" in x for x in run(result).reasons)


def test_an_envelope_outside_a_modules_extent_is_not_attributed_to_it() -> None:
    """`plain` is followed by an encrypted interface: the envelope lies after plain's extent, so a real
    parameter error on `plain` must not be called an artifact of the encryption."""
    result = dto(
        declarations=[decl(0, "plain", loc=loc(start=7, end=12), full={"start": 0, "end": 40})],
        diagnostics=[diag("ProtectedEnvelope", error=False, at=100), _param_diag("plain", 200)],
    )
    assert not any("ParameterDoesNotExist" in x for x in run(result).reasons)


def test_ambiguous_or_extent_less_modules_get_no_attribution() -> None:
    envelope = diag("ProtectedEnvelope", error=False, at=10)
    twice = dto(
        declarations=[
            decl(0, "m", loc=loc(start=1, end=2), full={"start": 0, "end": 50}),
            decl(1, "m", loc=loc("b.sv", 1, 2), full={"start": 0, "end": 50}),
        ],
        diagnostics=[envelope, _param_diag("m", 60)],
    )
    assert not any("ParameterDoesNotExist" in x for x in run(twice, "a.sv", "b.sv").reasons)
    no_full = dto(
        declarations=[decl(0, "m", loc=loc(start=1, end=2), full=None)],
        diagnostics=[envelope, _param_diag("m", 60)],
    )
    assert not any("ParameterDoesNotExist" in x for x in run(no_full).reasons)
