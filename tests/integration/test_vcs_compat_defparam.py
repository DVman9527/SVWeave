"""``language_options.compat: vcs`` and source ``defparam`` support, with the real pyslang."""

from __future__ import annotations

import shutil
from pathlib import Path
from typing import Any

import pytest

from svweave.adapters.slang.protocol import WorkerRequest
from tests.integration.helpers import LANG, raw_dto

pytestmark = pytest.mark.integration


def extract(topic: str, extra: list[str], tmp_path: Path) -> dict[str, Any]:
    root = tmp_path / topic
    shutil.copytree(LANG / topic, root, dirs_exist_ok=True)
    files = [str(root / ln.strip()) for ln in (root / "files.f").read_text().splitlines() if ln.strip()]
    return raw_dto(WorkerRequest(snapshot_root=str(root), args=[*extra, *files]))


def codes(r: dict[str, Any]) -> set[str]:
    return {d["code"] for d in r["diagnostics"] if d["severity"] == "error"}


def test_compat_vcs_relaxes_use_before_declare_but_not_vendor_string_methods(tmp_path: Path) -> None:
    strict = codes(extract("compat01", ["--top", "cv_top"], tmp_path))
    relaxed = codes(extract("compat01", ["--top", "cv_top", "--compat", "vcs"], tmp_path))
    assert strict == {"UsedBeforeDeclared", "UnknownSystemMethod"}
    # only what the flag really addresses disappears; the VCS string.match() extension stays an error
    assert relaxed == {"UnknownSystemMethod"}


def test_source_defparam_is_a_reference_to_the_overridden_parameter(tmp_path: Path) -> None:
    r = extract("defparam01", ["--top", "dp_top"], tmp_path)
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


def test_defparam_values_are_applied_in_the_elaborated_hierarchy(tmp_path: Path) -> None:
    """The reference is only half of the claim: slang must actually elaborate the override."""
    r = extract("defparam01", ["--top", "dp_top"], tmp_path)
    seen = {
        f"{instance['hier_path']}.{param['name']}": param["value"]
        for instance in r["instances"]
        for param in instance["parameters"]
        if param["name"] == "LEAF_ID"
    }
    assert seen == {f"dp_top.g[{i}].u_leaf.LEAF_ID": str(i) for i in range(3)}


def test_defparam_with_unresolvable_target_stays_unsupported_not_a_fabricated_reference(tmp_path: Path) -> None:
    r = extract("defparam02", ["--top", "dp2_top"], tmp_path)
    assert not [x for x in r["references"] if x["kind"] == "defparam"]
    assert [(u["kind"], u["count"]) for u in r["unsupported_nodes"]] == [("DefParam", 1)]
    assert "CouldNotResolveHierarchicalPath" in codes(r)
