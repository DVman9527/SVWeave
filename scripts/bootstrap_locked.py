#!/usr/bin/env python3
"""Reproduce the pinned M0 toolchain from toolchain.lock.json with SHA-256 verification.

Network is used ONLY here (plan §7.3: install phase). Normal analysis never downloads.

  python3.12 scripts/bootstrap_locked.py --create-venv          # install/repair into .toolchain/ and .venv
  .venv/bin/python scripts/bootstrap_locked.py --verify-only    # no network; check installed state vs lock
  .venv/bin/python scripts/bootstrap_locked.py --toolchain-dir /tmp/x --no-venv   # reproduce elsewhere

Offline / clean-room (DEP-01):

  scripts/bootstrap_locked.py --fetch-only                      # ONLINE: only fill the artifact cache
  scripts/bootstrap_locked.py --offline --artifact-dir A --serena-source S --uv-cache-dir C \
      --create-venv --venv-dir V --toolchain-dir T              # NO network: cached artifacts only

``--fetch-only`` also materializes the pinned Serena checkout (``<toolchain>/serena-src``) and warms the
uv cache (``<toolchain>/state/uv-cache``), so copying the whole repository directory including
``.toolchain/`` to the offline host is enough: ``--offline`` finds the existing checkout and cache.
Offline, the svweave package itself is linked into the gateway venv from this source tree (the
hash-locked wheelhouse holds no build backend, so an ``uv pip install -e .`` cannot work there).

uv: every uv command runs the PINNED uv (lock section ``uv``): its release tarball is downloaded,
verified against the lock sha256, extracted into ``<toolchain>/uv`` and the extracted binary is
verified again, before anything else is downloaded. ``--uv PATH`` overrides it; that uv is only
checked to be executable and is reported as UNVERIFIED (``warnings`` in the JSON report). uv never
downloads a Python interpreter here (``UV_PYTHON_DOWNLOADS=never``); ``--python`` names the base
interpreter of the venvs this script creates.

``--offline`` refuses every network access: an artifact or wheel missing from the artifact dir is an
error, Serena is cloned from the local ``--serena-source``, uv runs with ``--offline``. The gateway
venv is installed ONLY from the hash-locked ``gateway_wheelhouse`` of the lock
(``uv pip install --no-index --find-links <wheelhouse> --require-hashes``).

Serena build backend: ``uv.lock`` does not lock build dependencies, so the Serena venv is built
WITHOUT build isolation. Its build backends (hatchling for serena-agent itself, setuptools for the
sdist-only proxy-tools, lock section ``serena_build_wheelhouse``) are hash-verified wheels that are
first installed into the Serena venv from a local directory (``--no-index --require-hashes``), then
``uv sync --frozen --no-dev --no-build-isolation`` builds with exactly those; a missing build
dependency is a build error, never a download. The exact sync removes them again afterwards.

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


def install_archive(name: str, c: dict, toolchain: Path, verify_only: bool, report: dict,
                    artifact_dir: Path | None = None, fetch_only: bool = False,
                    extract_on_fetch: bool = False) -> dict:
    """One locked release archive: download + sha256 check, safe extraction, binary sha256 check."""
    url = c["source_url"]
    want = c["artifact_sha256"]
    fname = f"{name}-" + url.rsplit("/", 1)[1]
    dl = (artifact_dir or toolchain / "downloads") / fname
    inst = rel_to_toolchain(c["install_dir"], toolchain)
    entry: dict = {"source_url": url}
    report["components"][name] = entry
    if fetch_only and not extract_on_fetch:
        download(url, dl, want)
        entry["cached"] = True
        return entry
    if verify_only:
        entry["download_present"] = dl.exists()
        if dl.exists():
            entry["download_sha256_ok"] = sha256(dl) == want
    else:
        download(url, dl, want)  # refuses a wrong sha256 before the archive is opened
        safe_extract(dl, inst, int(c.get("archive_strip_components", 0)))
        entry["installed_to"] = str(inst)
        if fetch_only:
            entry["cached"] = True
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
    return entry


def install_archives(lock: dict, toolchain: Path, verify_only: bool, report: dict,
                     artifact_dir: Path | None = None, fetch_only: bool = False) -> None:
    for name in ARCHIVES:
        install_archive(name, lock["components"][name], toolchain, verify_only, report, artifact_dir, fetch_only)


def install_uv(lock: dict, toolchain: Path, verify_only: bool, report: dict,
               artifact_dir: Path | None = None, fetch_only: bool = False) -> str | None:
    """The pinned uv (lock section ``uv``): the release tarball is verified against the lock sha256
    before it is opened, the extracted binary again before it is run. Returns its path, or None when
    it is missing or differs (an error is then in the report). ``--fetch-only`` extracts it too: it
    runs the Serena cache warm-up."""
    c = lock["uv"]
    entry = install_archive("uv", c, toolchain, verify_only, report, artifact_dir, fetch_only,
                            extract_on_fetch=True)
    entry.update(source="pinned", version=c["version"], verified=entry.get("binary_sha256_ok") is True)
    return str(rel_to_toolchain(c["binary"], toolchain)) if entry["verified"] else None


def resolve_uv(arg: str) -> str:
    """An explicit ``--uv`` override: must be executable. Checked before any download. It is NOT
    verified against the lock (``main`` reports it as unverified)."""
    cand = arg
    if "/" in cand or os.sep in cand:
        ok = Path(cand).is_file() and os.access(cand, os.X_OK)
    else:  # a bare name is looked up on PATH
        found = shutil.which(cand)
        ok = found is not None
        cand = found or cand
    if not ok:
        raise SystemExit(
            f"uv not found or not executable ({cand}): fix --uv PATH, or omit --uv to use the pinned, "
            "hash-verified uv from toolchain.lock.json. Nothing was downloaded."
        )
    return cand


def uv_env(**extra: str) -> dict[str, str]:
    """Environment of every uv call: uv must never fetch a Python interpreter (it is not pinned)."""
    return dict(os.environ, UV_PYTHON_DOWNLOADS="never", **extra)


def base_python(arg: str | None) -> str:
    """Base interpreter of the venvs created here: --python, else python3.12 on PATH, else this one.
    A relative path is made absolute (the Serena sync runs in the Serena checkout); symlinks are kept."""
    if arg and os.sep in arg:
        return os.path.abspath(arg)
    return arg or shutil.which("python3.12") or sys.executable


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


def serena_build_wheels(lock: dict, toolchain: Path, verify_only: bool, report: dict,
                        artifact_dir: Path | None = None) -> tuple[Path, list[str]]:
    """The hash-pinned build backends of the Serena venv (lock ``serena_build_wheelhouse``).

    Each wheel is downloaded (or taken from the artifact dir) and its sha256 checked against the
    lock BEFORE anything is installed; a cached wheel with another sha256 is re-downloaded online and
    refused offline. Returns the wheelhouse directory and hash-pinned requirement lines for
    ``uv pip install --require-hashes`` (which checks every hash a second time)."""
    sb = lock["serena_build_wheelhouse"]
    wh = (artifact_dir or toolchain / "downloads") / "serena-build-wheelhouse"
    reqs: list[str] = []
    ok = 0
    for w in sb["wheels"]:
        f = wh / w["filename"]
        if not verify_only:
            download(w["source_url"], f, w["sha256"])  # LockError on a mismatch or, offline, if missing
        if f.exists() and sha256(f) == w["sha256"]:
            ok += 1
        elif f.exists() or not verify_only:
            report["errors"].append(f"serena build wheel {w['filename']}: sha256 != lock")
        dist, version = w["filename"].split("-")[:2]  # PEP 427: {distribution}-{version}-...
        reqs.append(f"{dist}=={version} --hash=sha256:{w['sha256']}")
    report["components"]["serena_build_wheelhouse"] = {"wheels_expected": len(sb["wheels"]),
                                                       "wheels_present_sha256_ok": ok}
    return wh, reqs


def _serena_sync(c: dict, src: Path, venv: Path, uv: str, uv_cache: Path, build_wh: Path,
                 build_reqs: list[str], python: str) -> None:
    """Build the Serena venv with the pinned build backends only (no build isolation, no index)."""
    env = uv_env(UV_PROJECT_ENVIRONMENT=str(venv), UV_CACHE_DIR=str(uv_cache))
    offline = ["--offline"] if OFFLINE else []
    vpy = venv / "bin/python"
    if not vpy.exists():
        subprocess.run([uv, "venv", "-q", *offline, "--python", python, str(venv)], env=env, check=True,
                       timeout=300)
    with tempfile.TemporaryDirectory(prefix="serena-build-") as tmp:
        req = Path(tmp) / "serena-build-requirements.txt"
        req.write_text("\n".join(build_reqs) + "\n")
        subprocess.run([uv, "pip", "install", "-q", "--python", str(vpy), "--offline", "--no-index",
                        "--find-links", str(build_wh), "--require-hashes", "-r", str(req)],
                       env=env, check=True, timeout=600)
    # --no-build-isolation: serena-agent and proxy-tools are built with the backends installed above;
    # nothing is fetched for a build. The exact sync then removes those build-only packages again.
    cmd = [uv, "sync", *offline, "--frozen", "--no-dev", "--no-build-isolation", "--python", python]
    subprocess.run(cmd, cwd=src, env=env, check=True, timeout=1800)


def fetch_serena(lock: dict, toolchain: Path, uv: str, report: dict,
                 uv_cache: Path | None = None, artifact_dir: Path | None = None,
                 python: str | None = None) -> None:
    """--fetch-only: pinned checkout, the pinned build wheels and a warm uv cache, no installed Serena venv."""
    c = lock["components"]["serena"]
    src = rel_to_toolchain(c["src_dir"], toolchain)
    cache = uv_cache or toolchain / "state/uv-cache"
    build_wh, build_reqs = serena_build_wheels(lock, toolchain, False, report, artifact_dir)
    _checkout_serena(c, src, None)
    head = subprocess.run(["git", "-C", str(src), "rev-parse", "HEAD"], capture_output=True, text=True).stdout.strip()
    lock_ok = (src / "uv.lock").exists() and sha256(src / "uv.lock") == c["dependency_lock_sha256"]
    entry: dict = {"commit_ok": head == c["commit"], "uv_lock_sha256_ok": lock_ok, "cached": False}
    if not entry["commit_ok"] or not lock_ok:
        report["errors"].append(f"serena: commit/uv.lock mismatch (HEAD={head})")
    elif not report["errors"]:
        (toolchain / "state").mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(dir=toolchain / "state", prefix="serena-fetch-") as tmp:
            # populates the uv cache, then discarded
            _serena_sync(c, src, Path(tmp) / "venv", uv, cache, build_wh, build_reqs, base_python(python))
        entry["cached"] = True
    report["components"]["serena"] = entry


def install_serena(lock: dict, toolchain: Path, verify_only: bool, uv: str | None, report: dict,
                   serena_source: Path | None = None, uv_cache: Path | None = None,
                   artifact_dir: Path | None = None, python: str | None = None) -> None:
    c = lock["components"]["serena"]
    build_wh, build_reqs = serena_build_wheels(lock, toolchain, verify_only, report, artifact_dir)
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
        if uv is None:
            raise LockError("serena: no uv to build the venv with")
        _serena_sync(c, src, venv, uv, uv_cache or toolchain / "state/uv-cache", build_wh, build_reqs,
                     base_python(python))
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


def install_wheels(lock: dict, toolchain: Path, verify_only: bool, venv_python: str | None, uv: str | None,
                   report: dict, artifact_dir: Path | None = None, fetch_only: bool = False,
                   create_venv: Path | None = None, python: str | None = None) -> None:
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
    if not verify_only and venv_python and uv is None:
        raise LockError("gateway venv: no uv to install with")
    if not verify_only and create_venv is not None and venv_python and uv is not None \
            and not Path(venv_python).exists():  # created by the pinned uv: no `uv venv` before this script
        subprocess.run([uv, "venv", "-q", *(["--offline"] if OFFLINE else []), "--python", base_python(python),
                        str(create_venv)], env=uv_env(), check=True, timeout=300)
    if not verify_only and venv_python and uv is not None and not report["errors"]:
        subprocess.run([uv, "pip", "install", "-q", "--python", venv_python, "--offline", "--no-index",
                        "--find-links", str(wh), "--require-hashes", "-r", str(reqs)],
                       env=uv_env(), check=True, timeout=900)
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
    ap.add_argument("--create-venv", action="store_true", help="create --venv-dir with the pinned uv if missing")
    ap.add_argument("--python", default=None,
                    help="base interpreter of the venvs created here (default: python3.12 on PATH, else this one)")
    ap.add_argument("--no-venv", action="store_true", help="skip wheel install/check in the gateway venv")
    ap.add_argument("--skip-serena", action="store_true")
    ap.add_argument("--verify-only", action="store_true")
    ap.add_argument("--offline", action="store_true", help="NO network: cached, hash-verified artifacts only")
    ap.add_argument("--fetch-only", action="store_true", help="online: only fill the artifact cache, install nothing")
    ap.add_argument("--artifact-dir", default=None, help="cache of archives + wheelhouses (default <toolchain>/downloads)")
    ap.add_argument("--serena-source", default=None, help="local git checkout to clone Serena from (offline)")
    ap.add_argument("--uv-cache-dir", default=None, help="uv cache for the Serena sync (default <toolchain>/state/uv-cache)")
    ap.add_argument("--uv", default=None,
                    help="override the pinned uv with this executable (NOT verified; reported as unverified)")
    ap.add_argument("--report", default=None, help="write JSON report here")
    a = ap.parse_args()
    OFFLINE = a.offline
    if a.offline and a.fetch_only:
        raise SystemExit("--offline and --fetch-only are contradictory")
    uv_override = resolve_uv(a.uv) if a.uv and not a.verify_only else None  # fail before any download
    lock = json.loads(Path(a.lock).read_text())
    toolchain = Path(a.toolchain_dir).resolve()
    art = Path(a.artifact_dir).resolve() if a.artifact_dir else None
    venv_dir = Path(a.venv_dir).resolve()
    venv_python = a.venv_python or str(venv_dir / "bin/python")
    uv_cache = Path(a.uv_cache_dir).resolve() if a.uv_cache_dir else None
    report: dict = {"lock": a.lock, "lock_sha256": sha256(Path(a.lock)), "toolchain_dir": str(toolchain),
                    "verify_only": a.verify_only, "offline": a.offline, "components": {}, "warnings": [],
                    "errors": []}
    try:
        uv: str | None
        if uv_override is not None and not a.fetch_only:
            uv = uv_override  # the pinned uv is neither downloaded nor used
            report["components"]["uv"] = {"source": "--uv", "path": uv, "verified": False}
        else:
            # the pinned uv is the FIRST download: a bad tarball stops the run before anything else
            uv = install_uv(lock, toolchain, a.verify_only, report, art, a.fetch_only)
            if uv is None and not a.verify_only:
                raise LockError("pinned uv is missing or differs from toolchain.lock.json; nothing installed")
            if uv_override is not None:  # --fetch-only still caches the pinned uv for the offline host
                uv = uv_override
                report["components"]["uv"].update(used_instead="--uv")
        if uv_override is not None:
            report["warnings"].append(
                f"uv: --uv {uv_override} is NOT verified against toolchain.lock.json (pinned uv "
                f"{lock['uv']['version']} not used); everything it installs depends on that binary")
            print(f"WARNING: {report['warnings'][-1]}", file=sys.stderr)
        install_archives(lock, toolchain, a.verify_only, report, art, a.fetch_only)
        install_wheels(lock, toolchain, a.verify_only,
                       None if a.no_venv else venv_python, uv, report, art, a.fetch_only,
                       venv_dir if a.create_venv else None, a.python)
        if not a.skip_serena and a.fetch_only:
            assert uv is not None
            fetch_serena(lock, toolchain, uv, report, uv_cache, art, a.python)
        if not a.skip_serena and not a.fetch_only:
            install_serena(lock, toolchain, a.verify_only, uv, report,
                           Path(a.serena_source).resolve() if a.serena_source else None, uv_cache, art, a.python)
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
