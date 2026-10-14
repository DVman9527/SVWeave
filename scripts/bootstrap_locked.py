#!/usr/bin/env python3
"""Reproduce the pinned M0 toolchain from toolchain.lock.json with SHA-256 verification.

Network is used ONLY here (plan §7.3: install phase). Normal analysis never downloads.

  .venv/bin/python scripts/bootstrap_locked.py                  # install/repair into .toolchain/ and .venv
  .venv/bin/python scripts/bootstrap_locked.py --verify-only    # no network; check installed state vs lock
  .venv/bin/python scripts/bootstrap_locked.py --toolchain-dir /tmp/x --no-venv   # reproduce elsewhere

Offline / clean-room (DEP-01):

  scripts/bootstrap_locked.py --fetch-only                      # ONLINE: only fill the artifact cache
  scripts/bootstrap_locked.py --offline --artifact-dir A --serena-source S --uv-cache-dir C \
      --create-venv --venv-dir V --toolchain-dir T --uv UV      # NO network: cached artifacts only

``--offline`` refuses every network access: an artifact or wheel missing from the artifact dir is an
error, Serena is cloned from the local ``--serena-source``, uv runs with ``--offline``. The gateway
venv is installed ONLY from the hash-locked ``gateway_wheelhouse`` of the lock
(``uv pip install --no-index --find-links <wheelhouse> --require-hashes``).

Every downloaded archive/wheel is verified against the lock before use; extracted binaries are
verified again. Archives with absolute or parent-traversing members are rejected. Any mismatch
aborts with a non-zero exit code (no silent fallback).
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tarfile
import urllib.request
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
ARCHIVES = ("slang", "verible", "codebase_memory_mcp", "uvm_core")
WHEELS = ("pyslang", "mcp_sdk")


class LockError(RuntimeError):
    pass


def sha256(p: Path) -> str:
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def expected_artifact_sha(c: dict) -> str:
    return c.get("artifact_sha256") or c.get("wheel_sha256")


OFFLINE = False  # set by --offline: any network access is a LockError


def download(url: str, dest: Path, want: str) -> Path:
    if dest.exists() and sha256(dest) == want:
        return dest
    if OFFLINE:
        raise LockError(f"offline: {dest.name} is not cached with the locked sha256 (would download {url})")
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(dest.suffix + ".part")
    print(f"  download {url}")
    with urllib.request.urlopen(url, timeout=120) as r, open(tmp, "wb") as f:
        shutil.copyfileobj(r, f)
    got = sha256(tmp)
    if got != want:
        tmp.unlink()
        raise LockError(f"sha256 mismatch for {url}: got {got}, lock says {want}")
    tmp.replace(dest)
    return dest


def safe_extract(archive: Path, dest: Path, strip: int) -> None:
    with tarfile.open(archive) as t:
        members = []
        for m in t.getmembers():
            parts = Path(m.name).parts
            if m.name.startswith("/") or ".." in parts:
                raise LockError(f"unsafe archive member {m.name!r} in {archive}")
            if m.issym() or m.islnk():
                if os.path.isabs(m.linkname) or ".." in Path(m.linkname).parts:
                    raise LockError(f"unsafe link {m.name!r} -> {m.linkname!r} in {archive}")
            if len(parts) <= strip:
                continue
            m.name = str(Path(*parts[strip:]))
            members.append(m)
        if dest.exists():
            shutil.rmtree(dest)
        dest.mkdir(parents=True)
        t.extractall(dest, members=members, filter="data")


def rel_to_toolchain(path_in_lock: str, toolchain: Path) -> Path:
    p = Path(path_in_lock)
    if p.parts and p.parts[0] == ".toolchain":
        return toolchain.joinpath(*p.parts[1:])
    return REPO / p


def install_archives(lock: dict, toolchain: Path, verify_only: bool, report: dict,
                     artifact_dir: Path | None = None, fetch_only: bool = False) -> None:
    for name in ARCHIVES:
        c = lock["components"][name]
        url = c["source_url"]
        want = c["artifact_sha256"]
        fname = f"{name}-" + url.rsplit("/", 1)[1]
        dl = (artifact_dir or toolchain / "downloads") / fname
        inst = rel_to_toolchain(c["install_dir"], toolchain)
        entry: dict = {"source_url": url}
        if fetch_only:
            download(url, dl, want)
            entry["cached"] = True
            report["components"][name] = entry
            continue
        if verify_only:
            entry["download_present"] = dl.exists()
            if dl.exists():
                entry["download_sha256_ok"] = sha256(dl) == want
        else:
            download(url, dl, want)
            safe_extract(dl, inst, int(c.get("archive_strip_components", 0)))
            entry["installed_to"] = str(inst)
        if c.get("binary"):
            b = rel_to_toolchain(c["binary"], toolchain)
            ok = b.exists() and sha256(b) == c["binary_sha256"]
            entry["binary_sha256_ok"] = ok
            if not ok:
                report["errors"].append(f"{name}: binary {b} missing or sha256 != lock")
        else:
            entry["install_dir_present"] = inst.exists()
            if not inst.exists():
                report["errors"].append(f"{name}: {inst} missing")
        if entry.get("download_sha256_ok") is False:
            report["errors"].append(f"{name}: download {dl} sha256 != lock")
        report["components"][name] = entry


def install_serena(lock: dict, toolchain: Path, verify_only: bool, uv: str, report: dict,
                   serena_source: Path | None = None, uv_cache: Path | None = None) -> None:
    c = lock["components"]["serena"]
    src = rel_to_toolchain(c["src_dir"], toolchain)
    venv = rel_to_toolchain(c["venv_dir"], toolchain)
    entry: dict = {}
    if not verify_only:
        if not (src / ".git").exists():
            if serena_source is not None:
                # local, network-free clone of the already-materialized pinned checkout
                subprocess.run(["git", "clone", "-q", "--depth", "1", "--no-local", f"file://{serena_source}",
                                str(src)], check=True, timeout=600)
            elif OFFLINE:
                raise LockError("offline: no --serena-source given and no existing serena checkout")
            else:
                subprocess.run(["git", "clone", "-q", "--filter=blob:none", c["source_url"], str(src)],
                               check=True, timeout=600)
        if not OFFLINE:
            subprocess.run(["git", "-C", str(src), "fetch", "-q", "origin", c["commit"]], check=False,
                           timeout=600)
        subprocess.run(["git", "-C", str(src), "checkout", "-q", "--detach", c["commit"]], check=True, timeout=120)
    head = subprocess.run(["git", "-C", str(src), "rev-parse", "HEAD"], capture_output=True, text=True).stdout.strip() \
        if (src / ".git").exists() else None
    entry["commit_ok"] = head == c["commit"]
    lock_ok = (src / "uv.lock").exists() and sha256(src / "uv.lock") == c["dependency_lock_sha256"]
    entry["uv_lock_sha256_ok"] = lock_ok
    lic_ok = (src / "LICENSE").exists() and sha256(src / "LICENSE") == c["license_file_sha256"]
    entry["license_sha256_ok"] = lic_ok
    if not entry["commit_ok"] or not lock_ok:
        report["errors"].append(f"serena: commit/uv.lock mismatch (HEAD={head})")
    elif not verify_only:
        env = dict(os.environ, UV_PROJECT_ENVIRONMENT=str(venv),
                   UV_CACHE_DIR=str(uv_cache or toolchain / "state/uv-cache"))
        py = shutil.which("python3.12") or sys.executable
        cmd = [uv, "sync", "--frozen", "--no-dev", "--python", py]
        if OFFLINE:
            env["UV_PYTHON_DOWNLOADS"] = "never"
            cmd.insert(1, "--offline")
        subprocess.run(cmd, cwd=src, env=env, check=True, timeout=1800)
    exe = venv / "bin/serena"
    entry["serena_exe_present"] = exe.exists()
    if exe.exists():
        v = subprocess.run([str(venv / "bin/python"), "-I", "-c",
                            "import importlib.metadata as m;print(m.version('serena-agent'))"],
                           capture_output=True, text=True, timeout=60).stdout.strip()
        entry["serena_agent_version"] = v
        if v != c["package"].split("==")[1]:
            report["errors"].append(f"serena: installed serena-agent {v} != lock")
    else:
        report["errors"].append("serena: venv missing")
    report["components"]["serena"] = entry


def _dist_version(venv_python: str, dist: str) -> str:
    return subprocess.run([venv_python, "-I", "-c",
                           f"import importlib.metadata as m;print(m.version('{dist}'))"],
                          capture_output=True, text=True, timeout=60).stdout.strip()


def install_wheels(lock: dict, toolchain: Path, verify_only: bool, venv_python: str | None, uv: str,
                   report: dict, artifact_dir: Path | None = None, fetch_only: bool = False,
                   create_venv: Path | None = None) -> None:
    """Gateway venv from the hash-locked wheelhouse only (no resolver, no index)."""
    gw = lock["gateway_wheelhouse"]
    art = artifact_dir or toolchain / "downloads"
    wh = art / "wheelhouse"
    reqs = REPO / gw["requirements_file"]
    entry: dict = {"requirements_sha256_ok": reqs.exists() and sha256(reqs) == gw["requirements_sha256"],
                   "wheels_expected": len(gw["wheels"])}
    if not entry["requirements_sha256_ok"]:
        report["errors"].append(f"gateway_wheelhouse: {gw['requirements_file']} sha256 != lock")
    present_ok = 0
    for w in gw["wheels"]:
        f = wh / w["filename"]
        if not verify_only:
            download(w["source_url"], f, w["sha256"])  # no-op when cached; LockError offline if missing
        if f.exists() and sha256(f) == w["sha256"]:
            present_ok += 1
        elif f.exists() or not verify_only:
            report["errors"].append(f"gateway wheel {w['filename']}: sha256 != lock")
    entry["wheels_present_sha256_ok"] = present_ok
    # the two single-file components must be the same bytes the wheelhouse holds
    by_name = {w["filename"]: w["sha256"] for w in gw["wheels"]}
    for name in WHEELS:
        c = lock["components"][name]
        if by_name.get(c["wheel_filename"]) != c["wheel_sha256"]:
            report["errors"].append(f"{name}: wheel in gateway_wheelhouse differs from components.{name}")
    report["components"]["gateway_wheelhouse"] = entry
    if fetch_only:
        return
    if not verify_only and create_venv is not None and venv_python:
        if not Path(venv_python).exists():
            py = shutil.which("python3.12") or sys.executable
            subprocess.run([uv, "venv", "-q", "--python", py, str(create_venv)], check=True, timeout=300)
    if not verify_only and venv_python and not report["errors"]:
        subprocess.run([uv, "pip", "install", "-q", "--python", venv_python, "--offline", "--no-index",
                        "--find-links", str(wh), "--require-hashes", "-r", str(reqs)],
                       check=True, timeout=900)
    if venv_python and Path(venv_python).exists():
        for name in WHEELS:
            dist = {"pyslang": "pyslang", "mcp_sdk": "mcp"}[name]
            v = _dist_version(venv_python, dist)
            report["components"].setdefault(name, {})["installed_version"] = v or None
            if v != lock["components"][name]["version"]:
                report["errors"].append(f"{name}: installed {v} != lock {lock['components'][name]['version']}")


def main() -> int:
    global OFFLINE
    ap = argparse.ArgumentParser()
    ap.add_argument("--lock", default=str(REPO / "toolchain.lock.json"))
    ap.add_argument("--toolchain-dir", default=str(REPO / ".toolchain"))
    ap.add_argument("--venv-python", default=None, help="python of the gateway venv (default <venv-dir>/bin/python)")
    ap.add_argument("--venv-dir", default=str(REPO / ".venv"))
    ap.add_argument("--create-venv", action="store_true", help="create --venv-dir (uv venv) if missing")
    ap.add_argument("--no-venv", action="store_true", help="skip wheel install/check in the gateway venv")
    ap.add_argument("--skip-serena", action="store_true")
    ap.add_argument("--verify-only", action="store_true")
    ap.add_argument("--offline", action="store_true", help="NO network: cached, hash-verified artifacts only")
    ap.add_argument("--fetch-only", action="store_true", help="online: only fill the artifact cache, install nothing")
    ap.add_argument("--artifact-dir", default=None, help="cache of archives + wheelhouse/ (default <toolchain>/downloads)")
    ap.add_argument("--serena-source", default=None, help="local git checkout to clone Serena from (offline)")
    ap.add_argument("--uv-cache-dir", default=None, help="uv cache for the Serena sync (default <toolchain>/state/uv-cache)")
    ap.add_argument("--uv", default=str(REPO / ".venv/bin/uv"))
    ap.add_argument("--report", default=None, help="write JSON report here")
    a = ap.parse_args()
    OFFLINE = a.offline
    if a.offline and a.fetch_only:
        raise SystemExit("--offline and --fetch-only are contradictory")
    lock = json.loads(Path(a.lock).read_text())
    toolchain = Path(a.toolchain_dir).resolve()
    art = Path(a.artifact_dir).resolve() if a.artifact_dir else None
    venv_dir = Path(a.venv_dir).resolve()
    venv_python = a.venv_python or str(venv_dir / "bin/python")
    report: dict = {"lock": a.lock, "lock_sha256": sha256(Path(a.lock)), "toolchain_dir": str(toolchain),
                    "verify_only": a.verify_only, "offline": a.offline, "components": {}, "errors": []}
    try:
        install_archives(lock, toolchain, a.verify_only, report, art, a.fetch_only)
        install_wheels(lock, toolchain, a.verify_only,
                       None if a.no_venv else venv_python, a.uv, report, art, a.fetch_only,
                       venv_dir if a.create_venv else None)
        if not a.skip_serena and not a.fetch_only:
            install_serena(lock, toolchain, a.verify_only, a.uv, report,
                           Path(a.serena_source).resolve() if a.serena_source else None,
                           Path(a.uv_cache_dir).resolve() if a.uv_cache_dir else None)
    except (LockError, subprocess.CalledProcessError, OSError) as e:
        report["errors"].append(f"{type(e).__name__}: {e}")
    report["ok"] = not report["errors"]
    out = json.dumps(report, indent=2)
    out = out.replace(str(REPO), "${REPO}").replace(str(Path.home()), "${HOME}")  # no private host paths
    if a.report:
        Path(a.report).parent.mkdir(parents=True, exist_ok=True)
        Path(a.report).write_text(out + "\n")
    print(out)
    return 0 if report["ok"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
