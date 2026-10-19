"""OBS-01 (plan 20.4): a debug bundle from a real slang build is enough to reproduce the query
results. The build is re-run in a FRESH workspace state (new root, no .svweave) that matches the
bundle's snapshot identity and normalized manifest; the normalized semantic rows and diagnostics
must be identical. Times, job ids and host paths are excluded from every comparison."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

import pytest
from click.testing import CliRunner

from svweave.cli import cli
from tests.integration.demo_support import demo_ws
from tests.integration.helpers import dbq

pytestmark = pytest.mark.integration


def run(root: Path, *args: str) -> dict[str, Any]:
    res = CliRunner().invoke(cli, ["--root", str(root), "--compact", *args])
    assert res.stdout.strip(), res.output
    data: dict[str, Any] = json.loads(res.stdout)
    # a PARTIAL build is still a published, reproducible result (exit 3); anything else must be 0
    assert res.exit_code in ((0, 3) if data["outcome"] == "PARTIAL" else (0,)), data
    return data


def rows_hash(w: Any, bid: str) -> tuple[str, dict[str, int]]:
    """Hash of the sorted normalized rows (no ids, no timestamps, no job ids)."""
    decl = dbq(w, "SELECT kind, qualified_name, file, span_start, span_end FROM declarations "
                  "WHERE build_id=?", bid)
    ref = dbq(w, 'SELECT r.kind, r.file, r.span_start, r.span_end, r.resolution_status, '
                 'd.qualified_name AS target FROM "references" r LEFT JOIN declarations d '
                 "ON d.build_id=r.build_id AND d.declaration_id=r.target_id WHERE r.build_id=?", bid)
    inst = dbq(w, "SELECT path, definition, specialization FROM instances WHERE build_id=?", bid)
    diag = dbq(w, "SELECT severity, code, locations FROM diagnostics WHERE build_id=?", bid)
    tables = {"declarations": decl, "references": ref, "instances": inst, "diagnostics": diag}
    norm = {k: sorted(json.dumps(r, sort_keys=True) for r in v) for k, v in tables.items()}
    assert len(decl) > 10 and len(ref) > 10 and len(inst) >= 3, "the demo build must produce rows"
    blob = json.dumps(norm, sort_keys=True).encode()
    return hashlib.sha256(blob).hexdigest(), {k: len(v) for k, v in tables.items()}


def bundle_files(w: Any, bid: str, out: Path) -> dict[str, Any]:
    env = run(w.root, "debug", "bundle", "--build", bid, "--out", str(out))
    assert env["data"]["contains_source_text"] is False
    return {p.name: p.read_text() for p in out.iterdir() if p.is_file()}


def test_OBS_01_bundle_reproduces_the_same_normalized_semantic_result(tmp_path: Path) -> None:
    w1 = demo_ws(tmp_path / "first")
    b1 = run(w1.root, "build", "--profile", "p")
    bid1 = b1["build_id"]
    assert bid1
    h1, counts1 = rows_hash(w1, bid1)
    files1 = bundle_files(w1, bid1, w1.root / ".svweave" / "bundle_out")
    bundle = json.loads(files1["bundle.json"])
    assert bundle["semantic_digest"] and bundle["contains_source_text"] is False
    assert "normalized_manifest.json" in files1, sorted(files1)
    assert "/first/" not in "".join(files1.values()), "host path leaked into the bundle"

    # ---- fresh state: other root, no .svweave, nothing shared with the first workspace
    w2 = demo_ws(tmp_path / "second")
    assert not (w2.root / ".svweave" / "svweave.db").exists()
    b2 = run(w2.root, "build", "--profile", "p")
    bid2 = b2["build_id"]
    # snapshot identity from the bundle: same snapshot/config/build ids and the same inventory
    for k in ("snapshot_id", "config_id", "toolchain_id", "profile", "semantic_schema_version"):
        assert bundle["build"][k] == (b2["data"].get(k) or dbq(w2, "SELECT * FROM builds WHERE build_id=?",
                                                               bid2)[0][k]), k
    assert bid2 == bundle["build"]["build_id"]
    files2 = bundle_files(w2, bid2, w2.root / ".svweave" / "bundle_out")
    inv1 = json.loads(files1["source_inventory.json"])
    assert json.loads(files2["source_inventory.json"]) == inv1 and len(inv1) >= 5
    # the normalized manifest the bundle recorded is the manifest the fresh run resolved
    assert files2["normalized_manifest.json"] == files1["normalized_manifest.json"]

    # ---- normalized semantic results and diagnostics are identical
    h2, counts2 = rows_hash(w2, bid2)
    assert (h2, counts2) == (h1, counts1)
    bundle2 = json.loads(files2["bundle.json"])
    assert bundle2["semantic_digest"] == bundle["semantic_digest"]
    assert files2["diagnostics_summary.json"] == files1["diagnostics_summary.json"]
    assert bundle2["toolchain_lock_hash"] == bundle["toolchain_lock_hash"]
    # hash is sensitive: a different configuration (L4) is NOT the same result
    w3 = demo_ws(tmp_path / "third", lanes=4)
    bid3 = run(w3.root, "build", "--profile", "p")["build_id"]
    assert rows_hash(w3, bid3)[0] != h1
