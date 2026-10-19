"""Real-backend regression tests for the M3a review round: state split, per-snapshot mount,
degraded (no bwrap) operation, and 'no backend process outlives its adapter'."""

from __future__ import annotations

from pathlib import Path

import pytest

from svweave.adapters.codebase_memory import CbmAdapter
from svweave.adapters.codebase_memory import config as ccfg
from svweave.security.sandbox import SandboxRunner
from tests.integration.conftest import SID_A, SID_B

pytestmark = pytest.mark.integration
KEY = b"k" * 32


def test_f1_f9_sandbox_hides_host_only_state_and_unrequested_snapshots(
    cbm: CbmAdapter, sandbox: SandboxRunner, snapshots_root: Path
) -> None:
    spec = cbm._spec(ccfg.SANDBOX_RUNTIME_DIR, snapshots_root / SID_A)
    mappings_before = cbm.paths.mapping_file.read_text() if cbm.paths.mapping_file.exists() else None

    def sh(script: str) -> tuple[int, str]:
        p = sandbox.run_once(spec, ["/bin/sh", "-c", script])
        return p.returncode, p.stdout + p.stderr

    # host-only state: invisible (ENOENT) and unwritable from inside
    assert sh(f"ls {cbm.paths.logs}")[0] != 0
    assert sh(f"cat {cbm.paths.mapping_file}")[0] != 0
    sh(f"echo '{{\"forged\": 1}}' > {cbm.paths.mapping_file}")
    after = cbm.paths.mapping_file.read_text() if cbm.paths.mapping_file.exists() else None
    assert after == mappings_before, "a sandboxed process must not be able to forge mappings.json"
    # only the requested snapshot is mounted; the sibling snapshot and the snapshots dir are not
    assert sh(f"ls {snapshots_root / SID_A}")[0] == 0
    assert sh(f"ls {snapshots_root / SID_B}")[0] != 0
    assert sh(f"ls {snapshots_root}")[1].strip().split() == [SID_A]
    # the work dir is the only writable place
    assert sh(f"touch {cbm.paths.work}/probe && echo ok")[0] == 0
    assert sh(f"touch {snapshots_root / SID_A}/probe")[0] != 0


def test_cbm_switching_snapshot_remounts_and_still_queries(cbm: CbmAdapter, snapshots_root: Path) -> None:
    cbm.ensure_indexed(SID_B, snapshots_root / SID_B)
    assert cbm.describe()["bound_snapshot"] == SID_B
    assert cbm.search(SID_B, "demo_lane").candidates
    cbm.ensure_indexed(SID_A, snapshots_root / SID_A)
    assert cbm.describe()["bound_snapshot"] == SID_A
    qn = cbm.search(SID_A, "demo_lane").candidates[0].upstream_qualified_name
    assert "module demo_lane" in cbm.code_snippet(SID_A, qn)["source"]


