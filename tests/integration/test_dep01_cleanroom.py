"""DEP-01 (plan 20.2): clean Linux environment + all pinned artifacts provided, NO network ->
three backends start and probe successfully, no temporary download, versions/hashes match the lock.

The clean room is an EMPTY temp directory that receives only the repository's scripts, fixtures,
svweave source tree (``src/``) and lock (no .toolchain, no .venv). ``scripts/bootstrap_locked.py
--offline`` installs from the locally cached artifacts (archives, the hash-locked gateway wheelhouse,
a local Serena checkout, a copy of the uv cache) and links ``src/`` into the fresh venv while running
under ``bwrap --unshare-net``; then ``scripts/probe_backends.py`` of the FRESH install probes pyslang,
cbm and Serena, also without network (the MCP probes use svweave's bounded stdio transport). Evidence (no host paths) is
compared with / written to ``evidence/dep01/cleanroom.json`` (``SVWEAVE_WRITE_EVIDENCE=1``).

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
from pathlib import Path
from typing import Any

import pytest

from tests.integration.conftest import skip_or_fail

pytestmark = pytest.mark.integration

REPO = Path(__file__).resolve().parents[2]
EVIDENCE = REPO / "evidence" / "dep01" / "cleanroom.json"
sys.path.insert(0, str(REPO / "scripts"))
from _cleanroom_preflight import collect_nested_preflight  # noqa: E402

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


def test_DEP_01_offline_bootstrap_into_empty_dir_then_probe_all_three_backends() -> None:
    # a SHORT path: cbm's daemon socket lives under the room and AF_UNIX paths are limited to ~108 bytes
    base = Path(tempfile.mkdtemp(prefix="dep01-", dir="/tmp"))
    try:
        _cleanroom(base)
    finally:
        shutil.rmtree(base, ignore_errors=True)


def _cleanroom(tmp_path: Path) -> None:
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
    assert probe.returncode == 0 and probe.stdout.strip().startswith("BLOCKED"), probe.stdout + probe.stderr

    room = tmp_path / "cleanroom"
    room.mkdir()
    assert list(room.iterdir()) == []  # EMPTY
    # src/: the probes speak MCP through svweave's bounded stdio transport, so the fresh venv must
    # import svweave; bootstrap links it offline exactly as on a real offline host
    for rel in ("scripts", "fixtures", "src"):
        shutil.copytree(REPO / rel, room / rel, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    shutil.copy(REPO / "toolchain.lock.json", room / "toolchain.lock.json")
    cache_copy = tmp_path / "uv-cache"
    shutil.copytree(uv_cache, cache_copy, symlinks=True)

    boot = run_netless(
        ["python3", "scripts/bootstrap_locked.py", "--offline", "--create-venv",
         "--artifact-dir", str(art), "--serena-source", str(serena_src),
         "--uv-cache-dir", str(cache_copy), "--report", str(room / "boot.json")],
        room, 1800)
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
    vers = {d: subprocess.run([str(py), "-I", "-c", f"import importlib.metadata as m;print(m.version('{d}'))"],
                              capture_output=True, text=True).stdout.strip() for d in ("pyslang", "mcp")}
    assert vers == {"pyslang": comps["pyslang"]["version"], "mcp": comps["mcp_sdk"]["version"]}

    ver = run_netless(["python3", "scripts/bootstrap_locked.py", "--verify-only", "--artifact-dir", str(art)],
                      room, 300)
    assert ver.returncode == 0, scrub(ver.stdout[-2000:], room)

    # Keep the offline outer namespace AND the mandatory inner SandboxRunner. Record the
    # actual nested denial before the temporary clean room is removed; do not bypass it.
    preflight = collect_nested_preflight(py, room, run_netless)
    preflight["outer_argv"] = NETLESS
    diagnostic = REPO / ".svweave/ci/dep01/nested-sandbox-preflight.json"
    diagnostic.parent.mkdir(parents=True, exist_ok=True)
    diagnostic.write_text(scrub(json.dumps(preflight, indent=2), room) + "\n")
    assert preflight["ready"], scrub(json.dumps(preflight), room)

    prb = run_netless([str(py), "scripts/probe_backends.py", "--out", str(room / "capability_report.json")],
                      room, 2400)
    assert (room / "capability_report.json").is_file(), scrub(prb.stdout[-2000:] + prb.stderr[-2000:], room)
    rep: dict[str, Any] = json.loads((room / "capability_report.json").read_text())
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
        "test": "DEP-01 offline clean-room bootstrap + three-backend probe",
        "network": "disabled: bwrap --unshare-net around bootstrap, verify and probe "
                   "(1.1.1.1:443 connect probe: BLOCKED)",
        "lock_sha256": sha256(REPO / "toolchain.lock.json"),
        "bootstrap": {"command": "scripts/bootstrap_locked.py --offline --create-venv --artifact-dir "
                                 "${REPO}/.toolchain/downloads --serena-source ${REPO}/.toolchain/serena-src "
                                 "--uv-cache-dir <copy of the uv cache>",
                      "ok": breport["ok"], "errors": breport["errors"],
                      "gateway_wheelhouse": breport["components"]["gateway_wheelhouse"]},
        "installed": {"binaries_sha256_equal_lock": ["slang", "codebase_memory_mcp", "verible"],
                      "gateway_venv": vers, "serena_agent": breport["components"]["serena"]},
        "verify_only_rc": ver.returncode,
        "nested_sandbox_preflight": preflight,
        "probe": {"summary": summary, "limitations": rep["limitations"],
                  "schema_digest": {b: rep["backends"][b]["schema_digest"] for b in ("cbm", "serena")},
                  "matches_committed_capability_report": True},
    }
    out = json.loads(scrub(json.dumps(ev), room))
    assert "/home/" not in json.dumps(out)
    if os.environ.get("SVWEAVE_WRITE_EVIDENCE") == "1":
        EVIDENCE.parent.mkdir(parents=True, exist_ok=True)
        EVIDENCE.write_text(json.dumps(out, indent=2) + "\n")
    else:
        assert EVIDENCE.is_file(), "evidence/dep01/cleanroom.json missing (SVWEAVE_WRITE_EVIDENCE=1)"
        old = json.loads(EVIDENCE.read_text())
        assert old["probe"]["summary"] == out["probe"]["summary"]
        # old["lock_sha256"] is informational: refresh the evidence (SVWEAVE_WRITE_EVIDENCE=1) after a lock change
