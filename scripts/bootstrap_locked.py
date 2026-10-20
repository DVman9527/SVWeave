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

``--fetch-only`` also materializes the pinned Serena checkout (``<toolchain>/serena-src``) and warms the
uv cache (``<toolchain>/state/uv-cache``), so copying the whole repository directory including
``.toolchain/`` to the offline host is enough: ``--offline`` finds the existing checkout and cache.
Offline, the svweave package itself is linked into the gateway venv from this source tree (the
hash-locked wheelhouse holds no build backend, so an ``uv pip install -e .`` cannot work there).

``--uv`` defaults to ``.venv/bin/uv`` if present, else ``uv`` from PATH; it is checked before anything
is downloaded.

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
import tempfile
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
            if m.islnk():  # a hardlink names another MEMBER: it must be stripped the same way
                lparts = Path(m.linkname).parts
                if len(lparts) <= strip:
                    raise LockError(f"hardlink {m.name!r} -> {m.linkname!r} leaves the archive root")
                m.linkname = str(Path(*lparts[strip:]))
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


def resolve_uv(arg: str | None) -> str:
    """The uv executable: --uv, else .venv/bin/uv, else PATH. Checked before any download."""
    default = REPO / ".venv/bin/uv"
    cand = arg or (str(default) if default.exists() else shutil.which("uv"))
    if cand and ("/" in cand or os.sep in cand):
        ok = Path(cand).is_file() and os.access(cand, os.X_OK)
    else:  # a bare name is looked up on PATH
        ok = bool(cand) and shutil.which(cand) is not None
    if not cand or not ok:
        raise SystemExit(
            f"uv not found or not executable ({cand or 'not on PATH'}): install uv "
            "(https://docs.astral.sh/uv/) or pass --uv PATH. Nothing was downloaded."
        )
    return cand


def _checkout_serena(c: dict, src: Path, serena_source: Path | None) -> None:
    """Materialize the pinned Serena commit in ``src`` (network-free when a local source is given)."""
    if not (src / ".git").exists():
        if serena_source is not None:
            cmd = ["git", "clone", "-q", "--depth", "1", "--no-local", f"file://{serena_source}", str(src)]
        elif OFFLINE:
            raise LockError("offline: no --serena-source given and no existing serena checkout")
        else:
            cmd = ["git", "clone", "-q", "--filter=blob:none", c["source_url"], str(src)]
        try:
            subprocess.run(cmd, check=True, timeout=600)
        except subprocess.CalledProcessError as e:
            # The JSON report only redacts REPO/HOME, so keep an arbitrary --serena-source path out of the
            # LockError (it lands in the report) and show it on stderr only.
            if serena_source is not None:
                print(f"serena: git clone of {serena_source} failed", file=sys.stderr)
            raise LockError(
                f"serena: git clone of {'--serena-source' if serena_source is not None else c['source_url']} "
                f"failed (exit {e.returncode}); with --serena-source the path must be a git checkout that is "
                "visible to this process (check sandbox bind mounts)"
            ) from e
    if not OFFLINE:
        subprocess.run(["git", "-C", str(src), "fetch", "-q", "origin", c["commit"]], check=False,
                       timeout=600)
    subprocess.run(["git", "-C", str(src), "checkout", "-q", "--detach", c["commit"]], check=True, timeout=120)


def _serena_sync(c: dict, src: Path, venv: Path, uv: str, uv_cache: Path) -> None:
    env = dict(os.environ, UV_PROJECT_ENVIRONMENT=str(venv), UV_CACHE_DIR=str(uv_cache))
    py = shutil.which("python3.12") or sys.executable
    cmd = [uv, "sync", "--frozen", "--no-dev", "--python", py]
    if OFFLINE:
        env["UV_PYTHON_DOWNLOADS"] = "never"
        cmd.insert(1, "--offline")
    subprocess.run(cmd, cwd=src, env=env, check=True, timeout=1800)


def fetch_serena(lock: dict, toolchain: Path, uv: str, report: dict,
                 uv_cache: Path | None = None) -> None:
    """--fetch-only: pinned checkout plus a warm uv cache, no installed Serena venv."""
    c = lock["components"]["serena"]
    src = rel_to_toolchain(c["src_dir"], toolchain)
    cache = uv_cache or toolchain / "state/uv-cache"
    _checkout_serena(c, src, None)
    head = subprocess.run(["git", "-C", str(src), "rev-parse", "HEAD"], capture_output=True, text=True).stdout.strip()
    lock_ok = (src / "uv.lock").exists() and sha256(src / "uv.lock") == c["dependency_lock_sha256"]
    entry: dict = {"commit_ok": head == c["commit"], "uv_lock_sha256_ok": lock_ok, "cached": False}
    if not entry["commit_ok"] or not lock_ok:
        report["errors"].append(f"serena: commit/uv.lock mismatch (HEAD={head})")
    else:
        (toolchain / "state").mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(dir=toolchain / "state", prefix="serena-fetch-") as tmp:
            _serena_sync(c, src, Path(tmp) / "venv", uv, cache)  # populates the uv cache, then discarded
        entry["cached"] = True
    report["components"]["serena"] = entry


def install_serena(lock: dict, toolchain: Path, verify_only: bool, uv: str, report: dict,
                   serena_source: Path | None = None, uv_cache: Path | None = None) -> None:
    c = lock["components"]["serena"]
    src = rel_to_toolchain(c["src_dir"], toolchain)
    venv = rel_to_toolchain(c["venv_dir"], toolchain)
    entry: dict = {}
    if not verify_only:
        _checkout_serena(c, src, serena_source)
    head = subprocess.run(["git", "-C", str(src), "rev-parse", "HEAD"], capture_output=True, text=True).stdout.strip() \
        if (src / ".git").exists() else None
    entry["commit_ok"] = head == c["commit"]
    lock_ok = (src / "uv.lock").exists() and sha256(src / "uv.lock") == c["dependency_lock_sha256"]
    entry["uv_lock_sha256_ok"] = lock_ok
    lic_ok = (src / "LICENSE").exists() and sha256(src / "LICENSE") == c["license_file_sha256"]
    entry["license_sha256_ok"] = lic_ok
    if not entry["commit_ok"] or not lock_ok:
        report["errors"].append(f"serena: commit/uv.lock mismatch (HEAD={head})")
    elif not lic_ok:  # the license is part of what was reviewed (ADR-010): a swap is not "ok"
        report["errors"].append("serena: LICENSE sha256 != lock")
    elif not verify_only:
        _serena_sync(c, src, venv, uv, uv_cache or toolchain / "state/uv-cache")
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
    if verify_only and venv_python and not Path(venv_python).exists():
        report["errors"].append(
            "gateway venv missing: nothing to verify at " + str(venv_python) + " (use --no-venv to skip)"
        )
    if venv_python and Path(venv_python).exists():
        for name in WHEELS:
            dist = {"pyslang": "pyslang", "mcp_sdk": "mcp"}[name]
            v = _dist_version(venv_python, dist)
            report["components"].setdefault(name, {})["installed_version"] = v or None
            if v != lock["components"][name]["version"]:
                report["errors"].append(f"{name}: installed {v} != lock {lock['components'][name]['version']}")
        if not verify_only and not report["errors"]:
            link_self(venv_python, report)


def link_self(venv_python: str, report: dict) -> None:
    """Make ``svweave`` importable in the gateway venv when it is not installed there.

    The online path (``uv pip install -e .``) already does this. Offline the wheelhouse has no build
    backend (hatchling), so link this source tree the way an editable install does: a ``.pth`` file
    plus a console script. Existing installs are left alone.
    """
    if _dist_version(venv_python, "svweave"):
        report["components"]["svweave"] = {"installed": "present"}
        return
    if not (REPO / "src" / "svweave").is_dir():  # e.g. the DEP-01 clean room holds scripts/ only
        report["components"]["svweave"] = {"installed": None, "note": "no source tree next to scripts/"}
        return
    purelib = subprocess.run([venv_python, "-I", "-c", "import sysconfig;print(sysconfig.get_paths()['purelib'])"],
                             capture_output=True, text=True, timeout=60).stdout.strip()
    if not purelib:
        report["errors"].append("svweave: cannot locate the venv site-packages")
        return
    Path(purelib, "svweave_src.pth").write_text(str(REPO / "src") + "\n")
    script = Path(venv_python).parent / "svweave"
    if not script.exists():
        script.write_text(f"#!{venv_python}\nimport sys\nfrom svweave.cli import main\nsys.exit(main())\n")
        script.chmod(0o755)
    ok = subprocess.run([venv_python, "-I", "-c", "import svweave"], capture_output=True).returncode == 0
    report["components"]["svweave"] = {"installed": "linked-src" if ok else None}
    if not ok:
        report["errors"].append("svweave: linking the source tree into the venv failed")


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
    ap.add_argument("--uv", default=None, help="uv executable (default: .venv/bin/uv, else uv on PATH)")
    ap.add_argument("--report", default=None, help="write JSON report here")
    a = ap.parse_args()
    OFFLINE = a.offline
    if a.offline and a.fetch_only:
        raise SystemExit("--offline and --fetch-only are contradictory")
    if not a.verify_only:
        a.uv = resolve_uv(a.uv)  # fail before any download
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
        if not a.skip_serena and a.fetch_only:
            fetch_serena(lock, toolchain, a.uv, report,
                         Path(a.uv_cache_dir).resolve() if a.uv_cache_dir else None)
        if not a.skip_serena and not a.fetch_only:
            install_serena(lock, toolchain, a.verify_only, a.uv, report,
                           Path(a.serena_source).resolve() if a.serena_source else None,
                           Path(a.uv_cache_dir).resolve() if a.uv_cache_dir else None)
    except (LockError, subprocess.CalledProcessError, OSError, tarfile.TarError, KeyError) as e:
        report["errors"].append(f"{type(e).__name__}: {e}")  # always a JSON report, never a traceback
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
