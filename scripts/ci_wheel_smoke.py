#!/usr/bin/env python3
"""DEP-01 packaging smoke: build/install a wheel and use it outside the checkout.

Requires the already-installed, exact build backend and the verified bootstrap's
local gateway wheelhouse. Every install below is offline. No analysis backend,
MCP server, real client configuration or trusted workspace policy is executed.
"""

from __future__ import annotations

import hashlib
import json
import subprocess
import sys
import tempfile
import tomllib
import zipfile
from importlib import metadata
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
OUT = REPO / ".svweave" / "ci" / "wheel-smoke.json"

INSTALLED_CHECK = r"""
import hashlib, importlib.metadata, json, os, sys
from pathlib import Path
import svweave
import svweave.launcher
from svweave.application.toolchain import find_lock
from svweave.orchestration.toolchain import ENV_ROOT, Toolchain, installed_root
shared, expected_json = sys.argv[1:]
expected = json.loads(expected_json)
package = Path(svweave.__file__).resolve().parent
assert package.is_relative_to(Path(sys.prefix)), 'source checkout imported instead of wheel'
runtime = installed_root()
assert runtime == package / '_runtime'
assert Toolchain.discover().root == runtime
assert find_lock(Path.cwd()) == runtime / 'toolchain.lock.json'
for relative, digest in expected.items():
    assert hashlib.sha256((package.parent / relative).read_bytes()).hexdigest() == digest
assert (runtime / 'fixtures/demo/rtl/demo_lane.sv').is_file()
assert (runtime / 'fixtures/uvm/svw_demo_uvm_pkg.sv').is_file()
assert importlib.metadata.version('mcp') == Toolchain.discover().component('mcp_sdk')['version']
os.environ[ENV_ROOT] = shared
assert Toolchain.discover().root == Path(shared)
assert find_lock() == Path(shared) / 'toolchain.lock.json'
os.environ[ENV_ROOT] = str(Path.cwd() / 'missing-shared-install')
try:
    Toolchain.discover()
except Exception as exc:
    from svweave.domain.errors import SvwError
    assert isinstance(exc, SvwError)
else:
    raise AssertionError('invalid explicit toolchain silently fell back')
print(json.dumps({'installed_package': True, 'bundled_assets_match': True,
                  'hostile_cwd_ignored': True, 'shared_toolchain_override': True,
                  'invalid_override_fails_closed': True}))
"""


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def run(argv: list[str], cwd: Path, env: dict[str, str]) -> str:
    result = subprocess.run(argv, cwd=cwd, env=env, capture_output=True, text=True, timeout=300)
    if result.returncode:
        raise RuntimeError(f"{Path(argv[0]).name} failed ({result.returncode}): {result.stderr[-4000:]}")
    return result.stdout


def smoke() -> dict[str, object]:
    config = tomllib.loads((REPO / "pyproject.toml").read_text())
    build_requires = config["build-system"]["requires"]
    if build_requires != ["hatchling==1.29.0"] or metadata.version("hatchling") != "1.29.0":
        raise RuntimeError("wheel smoke requires the project's exact hatchling==1.29.0 build backend")
    lock = json.loads((REPO / "toolchain.lock.json").read_text())
    uv = REPO / lock["uv"]["binary"]
    if not uv.is_file() or sha256(uv) != lock["uv"]["binary_sha256"]:
        raise RuntimeError("bootstrap's pinned uv is missing or its SHA-256 differs from the lock")
    requirements = REPO / lock["gateway_wheelhouse"]["requirements_file"]
    if sha256(requirements) != lock["gateway_wheelhouse"]["requirements_sha256"]:
        raise RuntimeError("gateway requirements SHA-256 differs from the lock")
    assets = config["tool"]["hatch"]["build"]["targets"]["wheel"]["force-include"]
    expected = {}
    for source, target in assets.items():
        if not (REPO / source).is_file() or not target.startswith("svweave/_runtime/"):
            raise RuntimeError("wheel runtime assets must be individually allowlisted files")
        expected[target] = sha256(REPO / source)

    with tempfile.TemporaryDirectory(prefix="svweave-wheel-smoke-") as directory:
        scratch = Path(directory)
        home, tmp = scratch / "home", scratch / "tmp"
        home.mkdir()
        tmp.mkdir()
        env = {"PATH": "/usr/bin:/bin", "HOME": str(home), "TMPDIR": str(tmp),
               "XDG_CONFIG_HOME": str(home / "config"), "LANG": "C.UTF-8",
               "UV_OFFLINE": "1", "UV_NO_CACHE": "1", "UV_PYTHON_DOWNLOADS": "never"}
        run([sys.executable, "-I", "-c", "from hatchling.build import build_wheel; "
             f"build_wheel({str(scratch)!r})"], REPO, env)
        [wheel] = scratch.glob("*.whl")
        with zipfile.ZipFile(wheel) as archive:
            for name, digest in expected.items():
                if hashlib.sha256(archive.read(name)).hexdigest() != digest:
                    raise RuntimeError(f"wheel asset bytes differ: {name}")
        venv = scratch / "venv"
        run([sys.executable, "-I", "-m", "venv", "--without-pip", str(venv)], scratch, env)
        python = str(venv / "bin" / "python")
        run([str(uv), "pip", "install", "--python", python, "--offline", "--no-index",
             "--find-links", str(REPO / ".toolchain/downloads/wheelhouse"),
             "--require-hashes", "-r", str(requirements)], scratch, env)
        run([str(uv), "pip", "install", "--python", python, "--offline", "--no-index",
             "--no-deps", str(wheel)], scratch, env)
        project = scratch / "unrelated-hdl-project"
        project.mkdir()
        for name in ("toolchain.lock.json", "capability_report.json"):
            (project / name).write_text('{"hostile_project_metadata": true}')
        checks = json.loads(run([python, "-I", "-c", INSTALLED_CHECK,
                                 str(REPO), json.dumps(expected)], project, env))
        for executable in ("svweave", "svweave-mcp"):
            output = run([str(venv / "bin" / executable), "--help"], project, env)
            if "Usage:" not in output:
                raise RuntimeError(f"{executable} --help did not return CLI help")
        return {"status": "PASS", "scope": "pure wheel packaging; not backend qualification",
                "toolchain_lock_sha256": sha256(REPO / "toolchain.lock.json"),
                "wheel_sha256": sha256(wheel), "runtime_asset_sha256": expected,
                "checks": checks, "console_help": ["svweave", "svweave-mcp"],
                "native_backends": "NOT_RUN", "client_settings": "NOT_MODIFIED"}


def main() -> int:
    try:
        report = smoke()
    except Exception as exc:
        report = {"status": "FAIL", "scope": "pure wheel packaging; not backend qualification",
                  "error": f"{type(exc).__name__}: {exc}", "native_backends": "NOT_RUN"}
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n")
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0 if report["status"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
