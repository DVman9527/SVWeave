"""Extractor DTO invariants over every fixture, run in the mandatory sandboxed real pyslang
worker: spans are real byte spans of the declared token, JSON-safe, and
the driver's own failures are reported instead of swallowed."""

from __future__ import annotations

import json
import shutil
from pathlib import Path
from typing import Any

import pytest

from svweave.adapters.slang.protocol import WorkerRequest
from tests.integration.helpers import LANG, UVM_SRC, raw_dto

pytestmark = pytest.mark.integration

CASES: list[tuple[str, list[str]]] = [
    ("sem01_scopes", ["--top", "sem01_top"]),
    ("sem02_paramclass", ["--top", "sem02_top"]),
    ("sem04_ifdef", ["--top", "sem04_top"]),
    ("sem04_ifdef", ["--top", "sem04_top", "-D", "USE_A"]),
    ("sem05_ports", ["--top", "sem05_top"]),
    ("sem06_iface", ["--top", "sem06_top"]),
    ("sem07_errors", ["--top", "err_top"]),
    ("sem08_virtual", ["--top", "sem08_top"]),
    ("sem09_escaped", ["--top", "sem09_top"]),
    ("sem10_unused", ["--top", "sem10_top"]),
    ("sem_classmembers", ["--top", "cm_top"]),
    ("loc01_utf8", ["--top", "loc01_top"]),
    ("loc02_macro", ["--top", "loc02_top"]),
    ("sva02_unresolved", ["--top", "sva02_top"]),
    ("stub_boundary", ["--top", "stub_top"]),
]


def extract(topic: str, extra: list[str], tmp_path: Path) -> tuple[dict[str, Any], Path]:
    root = tmp_path / topic
    shutil.copytree(LANG / topic, root)
    files = [
        str(root / ln.strip()) for ln in (root / "files.f").read_text().splitlines() if ln.strip()
    ]
    req = WorkerRequest(snapshot_root=str(root), args=[*extra, *files])
    return raw_dto(req), root


def sized(root: Path, loc: dict[str, Any]) -> bytes:
    return (root / loc["file"]).read_bytes()


@pytest.mark.parametrize(
    ("topic", "extra"), CASES, ids=[f"{t}{'-D' if '-D' in e else ''}" for t, e in CASES]
)
def test_dto_invariants(topic: str, extra: list[str], tmp_path: Path) -> None:
    r, root = extract(topic, extra, tmp_path)
    assert r["status"]["compiled"] is True and r["stats"]["errors"] == 0
    assert json.loads(json.dumps(r)) == r  # plain JSON only, no native objects
    n_decl = len(r["declarations"])
    for d in r["declarations"]:
        loc = d["loc"]
        data = sized(root, loc)
        assert 0 <= loc["start"] < loc["end"] <= len(data)
        text = data[loc["start"] : loc["end"]].decode("utf-8")
        if "macro" not in loc:
            assert text.lstrip("\\") == d["name"], (d["qualified_name"], text)
        assert d["qualified_name"].endswith(d["name"]) or d["kind"] in ("instance", "module")
    keys = {
        (
            d["loc"]["file"],
            d["loc"]["start"],
            d["loc"]["end"],
            d["kind"],
            d["qualified_name"],
            d["cu"],
            json.dumps(d["loc"].get("macro")),
        )
        for d in r["declarations"]
    }
    assert len(keys) == n_decl  # one record per source occurrence
    for x in r["references"]:
        loc = x["loc"]
        data = sized(root, loc)
        assert 0 <= loc["start"] <= loc["end"] <= len(data)
        if x["target_idx"] is not None:
            assert 0 <= x["target_idx"] < n_decl
        assert x["status"] in (
            "resolved_static",
            "dynamic_unresolved",
            "candidate_source",
            "unsupported",
        )
        if x["kind"] in ("call", "value") and x["target_idx"] is not None and "macro" not in loc:
            assert (
                data[loc["start"] : loc["end"]].decode()
                == r["declarations"][x["target_idx"]]["name"]
            )
    for i, inst in enumerate(r["instances"]):
        assert inst["idx"] == i and (inst["parent_idx"] is None or inst["parent_idx"] < i)
    comps = [json.dumps(i["components"]) for i in r["instances"]]
    assert len(set(comps)) == len(comps)
    for b in r["port_bindings"]:
        if b["actual_loc"] and not b["implicit_actual"]:
            data = sized(root, b["actual_loc"])
            assert (
                data[b["actual_loc"]["start"] : b["actual_loc"]["end"]].decode().strip()
                == b["actual_text"]
            )
    for a in r["assertions"]:
        assert a["execution_status"] == "NOT_RUN"
    deps = r["dependencies"]
    assert all(not f["outside_root"] for f in deps["files"])
    assert {f["path"] for f in deps["files"]} <= {
        p.relative_to(root).as_posix() for p in root.rglob("*") if p.is_file()
    }


def test_driver_failures_are_reported_not_swallowed(tmp_path: Path) -> None:
    (tmp_path / "a.sv").write_text("module a; endmodule\n")
    bad = raw_dto(
        WorkerRequest(snapshot_root=str(tmp_path), args=["--no-such-flag", "a.sv"])
    )
    assert bad["status"]["compiled"] is False and bad["declarations"] == []
    assert any("no-such-flag" in m for m in bad["driver_messages"])
    missing = raw_dto(
        WorkerRequest(snapshot_root=str(tmp_path), args=["a.sv", "gone.sv"])
    )
    assert missing["status"]["processOptions"] is False
    assert any("gone.sv" in m for m in missing["driver_messages"])
    assert [d["name"] for d in missing["declarations"] if d["kind"] == "module"] == ["a"]


def test_text_cap_and_escaped_identifier_span(tmp_path: Path) -> None:
    long_expr = " + ".join(["a"] * 3000)
    (tmp_path / "t.sv").write_text(
        f"module leaf(input logic i); endmodule\nmodule t(input logic a);\n"
        f"  logic [7:0] big;\n  assign big = {long_expr};\n  leaf \\a.b (.i(a));\nendmodule\n"
    )
    r = raw_dto(WorkerRequest(snapshot_root=str(tmp_path), args=["--top", "t", "t.sv"]))
    esc = [d for d in r["declarations"] if d["kind"] == "instance" and d["name"] == "a.b"]
    assert esc and esc[0]["loc"]["end"] - esc[0]["loc"]["start"] == len(
        "\\a.b"
    )  # includes the backslash
    texts = [b["actual_text"] for b in r["port_bindings"] if b["actual_text"]]
    assert texts == ["a"]
    assert all(len(t) <= 4097 for t in texts)


@pytest.mark.skipif(not (UVM_SRC / "uvm_pkg.sv").is_file(), reason="pinned UVM library missing")
def test_uvm_dto_in_process_stays_consistent(tmp_path: Path) -> None:
    # Retain the historical case id, but never load pyslang in the pytest process or mount REPO.
    root = tmp_path / "snapshot"
    uvm = root / "uvm"
    shutil.copytree(UVM_SRC, uvm)
    fx = root / "fixtures"
    fx.mkdir()
    for name in ("svw_uvm_override_pkg.sv", "svw_uvm_cfgdb_pkg.sv", "svw_uvm_ext_top.sv"):
        shutil.copyfile(LANG.parent / "uvm" / name, fx / name)
    args = [
        "-I",
        str(uvm),
        str(uvm / "uvm_pkg.sv"),
        str(fx / "svw_uvm_override_pkg.sv"),
        str(fx / "svw_uvm_cfgdb_pkg.sv"),
        str(fx / "svw_uvm_ext_top.sv"),
        "--top",
        "svw_uvm_ext_top",
    ]
    r = raw_dto(WorkerRequest(snapshot_root=str(root), args=args))
    assert r["stats"]["errors"] == 0 and r["stats"]["specializations"] > 100
    n = len(r["declarations"])
    assert n > 10000 and all(
        x["target_idx"] is None or x["target_idx"] < n for x in r["references"]
    )
    assert sum(1 for g in r["gaps"] if g["kind"] == "generic_class_not_specialized") > 20
    cats = {x["status"] for x in r["references"]}
    assert "candidate_source" not in cats and "unsupported" not in cats
