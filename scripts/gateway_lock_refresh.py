#!/usr/bin/env python3
"""(Re)generate the hash-pinned gateway-venv wheelhouse section of toolchain.lock.json (DEP-01).

Online, run by a maintainer when the gateway runtime closure changes (not part of normal use):

  .venv/bin/python scripts/gateway_lock_refresh.py

Steps: ``uv pip compile --generate-hashes`` scripts/gateway-requirements.in ->
scripts/gateway-requirements.txt; ``pip download`` the exact wheels for cp312 / manylinux_2_28
x86_64 into .toolchain/downloads/wheelhouse; look up each wheel URL on PyPI; write
``gateway_wheelhouse`` (requirements file sha256 + per-wheel filename, url, sha256) into the lock.
``scripts/bootstrap_locked.py`` then verifies and installs ONLY these files, offline if asked.
"""

from __future__ import annotations

import hashlib
import json
import subprocess
import sys
import urllib.request
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
IN, TXT = REPO / "scripts/gateway-requirements.in", REPO / "scripts/gateway-requirements.txt"
LOCK = REPO / "toolchain.lock.json"
WH = REPO / ".toolchain/downloads/wheelhouse"
PLATFORMS = ["manylinux_2_28_x86_64", "manylinux_2_27_x86_64", "manylinux2014_x86_64",
             "manylinux_2_17_x86_64", "manylinux2010_x86_64", "manylinux1_x86_64"]


def sha256(p: Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest()


def main() -> int:
    uv = str(REPO / ".venv/bin/uv")
    subprocess.run([uv, "pip", "compile", str(IN), "--generate-hashes", "--python-version", "3.12",
                    "--python-platform", "x86_64-manylinux_2_28", "--only-binary", ":all:",
                    "--no-header", "--custom-compile-command",
                    "scripts/gateway_lock_refresh.py", "-o", str(TXT)], check=True)
    if WH.exists():
        for f in WH.glob("*.whl"):
            f.unlink()
    WH.mkdir(parents=True, exist_ok=True)
    cmd = [sys.executable, "-m", "pip", "download", "--no-deps", "--require-hashes",
           "--only-binary=:all:", "--python-version", "3.12", "--implementation", "cp",
           "--abi", "cp312", "--abi", "abi3", "--abi", "none", "-r", str(TXT), "-d", str(WH)]
    for p in PLATFORMS:
        cmd += ["--platform", p]
    subprocess.run(cmd, check=True)
    wheels = []
    for f in sorted(WH.glob("*.whl")):
        name, version = f.name.split("-")[:2]
        meta = json.load(urllib.request.urlopen(
            f"https://pypi.org/pypi/{name.replace('_', '-')}/{version}/json", timeout=60))
        url = next(u["url"] for u in meta["urls"] if u["filename"] == f.name)
        want = next(u["digests"]["sha256"] for u in meta["urls"] if u["filename"] == f.name)
        assert want == sha256(f), f"{f.name}: PyPI digest != downloaded file"
        wheels.append({"filename": f.name, "sha256": want, "source_url": url})
    lock = json.loads(LOCK.read_text())
    lock["gateway_wheelhouse"] = {
        "requirements_in": "scripts/gateway-requirements.in",
        "requirements_file": "scripts/gateway-requirements.txt",
        "requirements_sha256": sha256(TXT),
        "wheelhouse_dir": ".toolchain/downloads/wheelhouse",
        "target": "cp312 / manylinux_2_28 x86_64 (glibc >= 2.28)",
        "install": "uv pip install --offline --no-index --find-links <wheelhouse> --require-hashes "
                   "-r scripts/gateway-requirements.txt",
        "wheels": wheels,
    }
    LOCK.write_text(json.dumps(lock, indent=2) + "\n")
    print(f"wrote gateway_wheelhouse: {len(wheels)} wheels")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
