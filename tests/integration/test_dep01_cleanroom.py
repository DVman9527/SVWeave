"""DEP-01-R2-host-controller: offline install/verification + individually isolated backends.

The clean room is an EMPTY temp directory that receives only the repository's scripts, fixtures,
svweave source tree (``src/``) and lock (no .toolchain, no .venv). ``scripts/bootstrap_locked.py
--offline`` installs from the locally cached artifacts (archives, the hash-locked gateway wheelhouse,
a local Serena checkout, a copy of the uv cache) and links ``src/`` into the fresh venv while running
under ``bwrap --unshare-net``. The FRESH install's trusted probe controller runs on the HOST:
its own network access is NOT isolated. All source-analyzing backend children still require
the existing read-only, no-network SandboxRunner. This explicitly narrower R2 acceptance does
not turn the original nested-controller DEP-01 FAIL/BLOCKED evidence into PASS. Fresh evidence
is always retained under ``.svweave/ci/dep01-r2/``; optional committed evidence uses a new path.

Host prerequisites (docs/install.md, "Running DEP-01"): the cached artifacts under ``.toolchain/``
(``downloads/`` -- after a ``vendor/`` install, ``ln -s ../vendor/downloads .toolchain/downloads`` --
``serena-src/`` and ``state/uv-cache/``), and ``python3`` on ``PATH`` >= 3.11 (it runs the installer
inside the sandbox)."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from tests.integration.conftest import skip_or_fail

pytestmark = pytest.mark.integration

REPO = Path(__file__).resolve().parents[2]
EVIDENCE = REPO / "evidence" / "dep01-r2" / "cleanroom.json"
sys.path.insert(0, str(REPO / "scripts"))
from _bench_common import source_identity  # noqa: E402
from _cleanroom_controller import (  # noqa: E402
    CONTRACT_REVISION,
    probe_argv,
    run_controller,
    validate_contract_identity,
    validate_probe_evidence,
)
from _cleanroom_preflight import collect_sandbox_preflight  # noqa: E402

NETLESS = ["bwrap", "--unshare-net", "--die-with-parent", "--bind", "/", "/", "--dev", "/dev",
           "--proc", "/proc", "--"]


def sha256(p: Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest()


def scrub(text: str, room: Path) -> str:
    return (text.replace(str(room), "${CLEANROOM}").replace(str(REPO), "${REPO}")
            .replace(str(Path.home()), "${HOME}"))


def run_netless(args: list[str], cwd: Path, timeout: float) -> subprocess.CompletedProcess[str]:
    env = {k: v for k, v in os.environ.items() if k in ("PATH", "HOME", "LANG", "LC_ALL")}
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    return subprocess.run([*NETLESS, *args], cwd=cwd, env=env, capture_output=True, text=True,
                          timeout=timeout)


def source_digest(root: Path) -> str:
    hashes = {p.relative_to(root).as_posix(): sha256(p) for p in sorted(root.rglob("*"))
              if p.is_file() and "__pycache__" not in p.parts and p.suffix != ".pyc"}
    return hashlib.sha256(json.dumps(hashes, sort_keys=True).encode()).hexdigest()


def record_result(name: str, result: subprocess.CompletedProcess[str], artifacts: Path, room: Path) -> None:
    for stream in ("stdout", "stderr"):
        (artifacts / f"{name}.{stream}.log").write_text(scrub(getattr(result, stream), room))
    (artifacts / f"{name}.execution.json").write_text(json.dumps({"returncode": result.returncode}))


def test_DEP_01_R2_offline_install_then_host_controlled_isolated_backend_probes() -> None:
    # a SHORT path: cbm's daemon socket lives under the room and AF_UNIX paths are limited to ~108 bytes
    base = Path(tempfile.mkdtemp(prefix="dep01-", dir="/tmp"))
    out = REPO / ".svweave/ci/dep01-r2"
    out.mkdir(parents=True, exist_ok=True)
    artifacts = Path(tempfile.mkdtemp(prefix="run-", dir=out))
    before = source_digest(REPO / "fixtures")
    after = None
    contract: dict[str, Any] = {
        "revision": CONTRACT_REVISION, "status": "RUNNING",
        "original_contract_status": "BLOCKED", "original_contract_reclassified": False,
        "controller_network_isolated": False,
        "offline_install_verification_network_isolated": True,
        "backend_isolation": "mandatory_bwrap", "release_qualified": False,
        "ci_identity_verified": False,
        "source_identity": source_identity(), "lock_sha256": sha256(REPO / "toolchain.lock.json"),
        "workflow_sha256": sha256(REPO / ".github/workflows/ci.yml"),
        "artifacts": artifacts.relative_to(REPO).as_posix(),
        "started_at_utc": datetime.now(UTC).isoformat(),
    }
    try:
        identity = REPO / ".svweave/ci/identity.json"
        if identity.exists():
            validate_contract_identity(contract, json.loads(identity.read_text()))
            contract["ci_identity_verified"] = True
        elif os.environ.get("GITHUB_ACTIONS") == "true":
            raise ValueError("R2 CI validation requires current identity.json")
        _cleanroom(base, artifacts)
        after = source_digest(REPO / "fixtures")
        assert after == before, "original fixture source changed"
        contract["status"] = "PASS"
    except BaseException as error:
        contract.update(status="FAIL", error=f"{type(error).__name__}: {scrub(str(error), base)}")
        raise
    finally:
        contract["source_manifest_sha256"] = {
            "before": before, "after": after if after is not None else source_digest(REPO / "fixtures"),
        }
        contract["finished_at_utc"] = datetime.now(UTC).isoformat()
        text = json.dumps(contract, indent=2) + "\n"
        (artifacts / "contract.json").write_text(text)
        (out / "contract.json").write_text(text)  # current-run pointer; raw evidence stays in its fresh leaf
        shutil.rmtree(base, ignore_errors=True)


def _cleanroom(tmp_path: Path, artifacts: Path) -> None:
    if shutil.which("bwrap") is None:
        skip_or_fail("bwrap not installed (needed to disable the network)")
    art = REPO / ".toolchain" / "downloads"
    serena_src = REPO / ".toolchain" / "serena-src"
    uv_cache = REPO / ".toolchain" / "state" / "uv-cache"
    # the pinned uv tarball (lock section `uv`) and the Serena build backends come from the artifact dir
    uv_tgz = art / ("uv-" + json.loads((REPO / "toolchain.lock.json").read_text())["uv"]["source_url"].rsplit("/", 1)[1])
    for need in (art / "wheelhouse", art / "serena-build-wheelhouse", serena_src / ".git", uv_cache, uv_tgz):
        if not need.exists():
            skip_or_fail(f"cached artifact missing: {need.relative_to(REPO)} "
                         "(run scripts/bootstrap_locked.py --fetch-only)")
    probe = run_netless(["python3", "-c", "import socket,sys\n"
                         "try:\n socket.create_connection(('1.1.1.1',443),timeout=3);print('CONNECTED')\n"
                         "except OSError as e:\n print('BLOCKED errno=%s' % e.errno)"], tmp_path, 60)
    record_result("offline-network-check", probe, artifacts, tmp_path)
    assert probe.returncode == 0 and probe.stdout.strip().startswith("BLOCKED"), probe.stdout + probe.stderr

    room = tmp_path / "cleanroom"
    room.mkdir()
    assert list(room.iterdir()) == []  # EMPTY
    # src/: the probes speak MCP through svweave's bounded stdio transport, so the fresh venv must
    # import svweave; bootstrap links it offline exactly as on a real offline host
    for rel in ("scripts", "fixtures", "src"):
        shutil.copytree(REPO / rel, room / rel, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    shutil.copy(REPO / "toolchain.lock.json", room / "toolchain.lock.json")
    before = source_digest(room / "fixtures")
    cache_copy = tmp_path / "uv-cache"
    shutil.copytree(uv_cache, cache_copy, symlinks=True)

    boot = run_netless(
        ["python3", "scripts/bootstrap_locked.py", "--offline", "--create-venv",
         "--artifact-dir", str(art), "--serena-source", str(serena_src),
         "--uv-cache-dir", str(cache_copy), "--report", str(room / "boot.json")],
        room, 1800)
    record_result("bootstrap", boot, artifacts, room)
    if (room / "boot.json").is_file():
        (artifacts / "bootstrap.json").write_text(scrub((room / "boot.json").read_text(), room))
    assert boot.returncode == 0, scrub(boot.stdout[-3000:] + boot.stderr[-3000:], room)
    breport: dict[str, Any] = json.loads((room / "boot.json").read_text())
    assert breport["ok"] is True and breport["offline"] is True and breport["errors"] == []
    assert breport["components"]["uv"]["source"] == "pinned" and breport["components"]["uv"]["verified"] is True
    assert breport["components"]["svweave"] == {"installed": "linked-src"}
    # nothing was installed outside the room and the locked bytes are what is installed
    lock = json.loads((REPO / "toolchain.lock.json").read_text())
    comps = lock["components"]
    tc = room / ".toolchain"
    for name, key in (("slang", "slang/slang"), ("cbm", "cbm/codebase-memory-mcp"),
                      ("verible", "verible/bin/verible-verilog-ls")):
        want = comps[{"cbm": "codebase_memory_mcp"}.get(name, name)]["binary_sha256"]
        assert sha256(tc / key) == want, name
    py = room / ".venv" / "bin" / "python"
    assert py.is_file()
    vers = {}
    for distribution in ("pyslang", "mcp"):
        result = run_netless(
            [str(py), "-I", "-c", f"import importlib.metadata as m;print(m.version('{distribution}'))"], room, 60)
        record_result(f"installed-{distribution}", result, artifacts, room)
        assert result.returncode == 0, scrub(result.stderr, room)
        vers[distribution] = result.stdout.strip()
    assert vers == {"pyslang": comps["pyslang"]["version"], "mcp": comps["mcp_sdk"]["version"]}

    ver = run_netless(["python3", "scripts/bootstrap_locked.py", "--verify-only", "--artifact-dir", str(art)],
                      room, 300)
    record_result("verify-only", ver, artifacts, room)
    assert ver.returncode == 0, scrub(ver.stdout[-2000:], room)

    # R2 explicitly moves only trusted orchestration to the host. Native backend launches
    # remain mandatory SandboxRunner calls; do not retry the known-denied original nesting.
    preflight = collect_sandbox_preflight(py, room, run_controller)
    preflight.update(contract_revision=CONTRACT_REVISION, controller_network_isolated=False)
    diagnostic = artifacts / "controller-sandbox-preflight.json"
    diagnostic.write_text(scrub(json.dumps(preflight, indent=2), room) + "\n")
    (artifacts.parent / diagnostic.name).write_text(diagnostic.read_text())
    assert preflight["ready"], scrub(json.dumps(preflight), room)

    assert not (room / "capability_report.json").exists(), "fresh probe output required"
    prb = run_controller(probe_argv(room), room, 2400)
    record_result("probe-controller", prb, artifacts, room)
    assert (room / "capability_report.json").is_file(), scrub(prb.stdout[-2000:] + prb.stderr[-2000:], room)
    (artifacts / "capability-report.json").write_text(scrub((room / "capability_report.json").read_text(), room))
    assert prb.returncode == 0, scrub(prb.stdout[-2000:] + prb.stderr[-2000:], room)
    rep: dict[str, Any] = json.loads((room / "capability_report.json").read_text())
    validate_probe_evidence(rep, prb.returncode)
    assert source_digest(room / "fixtures") == before, "fresh installed fixture source changed"
    ref: dict[str, Any] = json.loads((REPO / "capability_report.json").read_text())
    summary = rep["summary"]
    assert set(summary) == {"pyslang", "slang_cli", "cbm", "serena"}
    detail = {b: (v.get("error"), [c["check"] for c in v.get("checks", []) if not c["pass"]])
              for b, v in rep["backends"].items()}
    assert "UNAVAILABLE" not in summary.values(), scrub(json.dumps((summary, detail)), room)
    assert summary == ref["summary"], "fresh install probes differently from the committed report"
    for b in ("cbm", "serena"):  # same upstream tool schemas as the committed capability report
        assert rep["backends"][b]["schema_digest"] == ref["backends"][b]["schema_digest"], b
    text = (room / "capability_report.json").read_text()
    assert str(room) not in text, "probe wrote a clean-room host path"

    ev = {
        "test": CONTRACT_REVISION,
        "controller_network_isolated": False,
        "network": "offline bootstrap/installed verification: NETLESS; trusted probe controller: host; "
                   "every native backend: mandatory no-network SandboxRunner",
        "lock_sha256": sha256(REPO / "toolchain.lock.json"),
        "bootstrap": {"command": "scripts/bootstrap_locked.py --offline --create-venv --artifact-dir "
                                 "${REPO}/.toolchain/downloads --serena-source ${REPO}/.toolchain/serena-src "
                                 "--uv-cache-dir <copy of the uv cache>",
                      "ok": breport["ok"], "errors": breport["errors"],
                      "gateway_wheelhouse": breport["components"]["gateway_wheelhouse"]},
        "installed": {"binaries_sha256_equal_lock": ["slang", "codebase_memory_mcp", "verible"],
                      "gateway_venv": vers, "serena_agent": breport["components"]["serena"]},
        "verify_only_rc": ver.returncode,
        "controller_sandbox_preflight": preflight,
        "probe": {"summary": summary, "limitations": rep["limitations"],
                  "schema_digest": {b: rep["backends"][b]["schema_digest"] for b in ("cbm", "serena")},
                  "matches_committed_capability_report": True},
    }
    out = json.loads(scrub(json.dumps(ev), room))
    assert "/home/" not in json.dumps(out)
    (artifacts / "cleanroom-r2.json").write_text(json.dumps(out, indent=2) + "\n")
    if os.environ.get("SVWEAVE_WRITE_EVIDENCE") == "1":
        EVIDENCE.parent.mkdir(parents=True, exist_ok=True)
        EVIDENCE.write_text(json.dumps(out, indent=2) + "\n")
