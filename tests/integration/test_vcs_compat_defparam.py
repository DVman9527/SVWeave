"""``language_options.compat: vcs`` and source ``defparam`` support, with the real pyslang."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from svweave.adapters.slang.protocol import WorkerRequest
from svweave.adapters.slang.worker import build_and_extract
from tests.integration.helpers import LANG

pytestmark = pytest.mark.integration


def extract(topic: str, extra: list[str]) -> dict[str, Any]:
    root = LANG / topic
    files = [str(root / ln.strip()) for ln in (root / "files.f").read_text().splitlines() if ln.strip()]
    return build_and_extract(WorkerRequest(snapshot_root=str(root), args=[*extra, *files]))


def codes(r: dict[str, Any]) -> set[str]:
    return {d["code"] for d in r["diagnostics"] if d["severity"] == "error"}


def test_compat_vcs_relaxes_use_before_declare_but_not_vendor_string_methods() -> None:
    strict = codes(extract("compat01", ["--top", "cv_top"]))
    relaxed = codes(extract("compat01", ["--top", "cv_top", "--compat", "vcs"]))
    assert strict == {"UsedBeforeDeclared", "UnknownSystemMethod"}
    # only what the flag really addresses disappears; the VCS string.match() extension stays an error
    assert relaxed == {"UnknownSystemMethod"}


def test_source_defparam_is_a_reference_to_the_overridden_parameter() -> None:
    r = extract("defparam01", ["--top", "dp_top"])
    assert r["unsupported_nodes"] == []
    refs = [x for x in r["references"] if x["kind"] == "defparam"]
    assert len(refs) == 1  # one source site, however many generate iterations expand it
    ref = refs[0]
    assert ref["status"] == "resolved_static"
    target = r["declarations"][ref["target_idx"]]
    assert target["name"] == "LEAF_ID"
    root = Path(LANG / "defparam01" / "dp.sv")
    text = root.read_bytes()[ref["loc"]["start"] : ref["loc"]["end"]].decode()
    assert text == "g[i].u_leaf.LEAF_ID"


def test_defparam_values_are_applied_in_the_elaborated_hierarchy() -> None:
    """The reference is only half of the claim: slang must actually elaborate the override."""
    import os
    import shlex

    from pyslang import ast
    from pyslang import driver as drv

    old = os.getcwd()
    os.chdir(LANG / "defparam01")
    try:
        d = drv.Driver()
        d.addStandardArgs()
        assert d.parseCommandLine(
            " ".join(shlex.quote(a) for a in ["slang", "--top", "dp_top", "dp.sv"]),
            drv.CommandLineOptions(),
        )
        assert d.processOptions() and d.parseAllSources()
        comp = d.createCompilation()
        seen: dict[str, str] = {}

        def visit(n: Any) -> Any:
            if n.kind == ast.SymbolKind.Parameter and n.name == "LEAF_ID":
                seen[n.hierarchicalPath] = str(n.value)
            return ast.VisitAction.Advance

        comp.getRoot().visit(visit)
    finally:
        os.chdir(old)
    assert seen == {f"dp_top.g[{i}].u_leaf.LEAF_ID": str(i) for i in range(3)}


def test_defparam_with_unresolvable_target_stays_unsupported_not_a_fabricated_reference() -> None:
    r = extract("defparam02", ["--top", "dp2_top"])
    assert not [x for x in r["references"] if x["kind"] == "defparam"]
    assert [(u["kind"], u["count"]) for u in r["unsupported_nodes"]] == [("DefParam", 1)]
    assert "CouldNotResolveHierarchicalPath" in codes(r)
