"""scripts/bootstrap_locked.py: behaviour that needs neither the network nor the pinned toolchain."""

from __future__ import annotations

import hashlib
import importlib.util
import io
import json
import os
import subprocess
import sys
import tarfile
from pathlib import Path
from types import ModuleType

import pytest

REPO = Path(__file__).resolve().parents[2]


def load() -> ModuleType:
    spec = importlib.util.spec_from_file_location("bootstrap_locked", REPO / "scripts/bootstrap_locked.py")
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_resolve_uv_fails_before_anything_is_downloaded(tmp_path: Path) -> None:
    bs = load()
    with pytest.raises(SystemExit) as e:
        bs.resolve_uv(str(tmp_path / "no-such-uv"))
    assert "uv not found" in str(e.value) and "Nothing was downloaded" in str(e.value)


def test_resolve_uv_accepts_an_explicit_executable(tmp_path: Path) -> None:
    bs = load()
    fake = tmp_path / "uv"
    fake.write_text("#!/bin/sh\n")
    fake.chmod(0o755)
    assert bs.resolve_uv(str(fake)) == str(fake)


@pytest.mark.parametrize("kind", ["directory", "not-executable", "dev-null"])
def test_resolve_uv_rejects_paths_that_cannot_run(tmp_path: Path, kind: str) -> None:
    """An existing path is not enough: a directory or a non-executable file would only fail after the
    downloads had started."""
    bs = load()
    if kind == "directory":
        cand = tmp_path
    elif kind == "dev-null":
        cand = Path("/dev/null")
    else:
        cand = tmp_path / "uv"
        cand.write_text("x")
        cand.chmod(0o644)
    with pytest.raises(SystemExit) as e:
        bs.resolve_uv(str(cand))
    assert "Nothing was downloaded" in str(e.value)


def test_serena_clone_failure_is_explained_without_putting_the_path_in_the_error(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    bs = load()
    missing = tmp_path / "not-a-checkout"
    c = {"commit": "0" * 40, "source_url": "https://example.invalid/serena"}
    with pytest.raises(bs.LockError) as e:
        bs._checkout_serena(c, tmp_path / "src", missing)
    assert "--serena-source" in str(e.value) and "visible to this process" in str(e.value)
    assert str(missing) not in str(e.value)  # the error ends up in the JSON report
    assert str(missing) in capsys.readouterr().err  # the user still sees which path failed


def _bare_venv(tmp_path: Path) -> str:
    venv = tmp_path / "venv"
    subprocess.run([sys.executable, "-m", "venv", "--without-pip", str(venv)], check=True)
    return str(venv / "bin/python")


def test_link_self_makes_svweave_importable_in_a_bare_venv(tmp_path: Path) -> None:
    bs = load()
    py = _bare_venv(tmp_path)
    report: dict = {"components": {}, "errors": []}
    bs.link_self(py, report)
    assert report["errors"] == [] and report["components"]["svweave"] == {"installed": "linked-src"}
    assert subprocess.run([py, "-I", "-c", "import svweave"], capture_output=True).returncode == 0
    script = Path(py).parent / "svweave"
    assert script.is_file() and script.stat().st_mode & 0o111
    assert script.read_text().startswith(f"#!{py}\n")
    # a second run leaves an existing launcher alone
    script.write_text("#!custom\n")
    bs.link_self(py, {"components": {}, "errors": []})
    assert script.read_text() == "#!custom\n"


def test_link_self_is_a_noop_without_a_source_tree(tmp_path: Path) -> None:
    """A copy of scripts/ without the source tree: nothing to link, and that is not an error."""
    bs = load()
    bs.REPO = tmp_path
    report: dict = {"components": {}, "errors": []}
    bs.link_self(_bare_venv(tmp_path), report)
    assert report["errors"] == [] and report["components"]["svweave"]["installed"] is None


# ------------------------------------------------------------------ supply chain: Serena build backend, pinned uv
COMMIT = "c" * 40


def _sha(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


def _tgz(path: Path, files: dict[str, bytes], mode: int = 0o755) -> str:
    """A tar.gz with one top directory (strip 1); returns its sha256."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with tarfile.open(path, "w:gz") as t:
        for name, data in files.items():
            ti = tarfile.TarInfo(f"top/{name}")
            ti.size, ti.mode = len(data), mode
            t.addfile(ti, io.BytesIO(data))
    return _sha(path.read_bytes())


def _build_wheels(wh: Path) -> list[dict]:
    wh.mkdir(parents=True, exist_ok=True)
    out = []
    for fn in ("hatchling-1.32.4-py3-none-any.whl", "setuptools-84.0.0-py3-none-any.whl"):
        data = f"wheel {fn}".encode()
        (wh / fn).write_bytes(data)
        out.append({"filename": fn, "sha256": _sha(data), "source_url": f"https://files.example.invalid/{fn}"})
    return out


def _serena_lock(tmp_path: Path) -> tuple[dict, Path]:
    """A lock whose Serena pins match a fake checkout, and the pinned build wheels in the default
    artifact dir (<toolchain>/downloads/serena-build-wheelhouse)."""
    toolchain = tmp_path / "tc"
    src = toolchain / "serena-src"
    src.mkdir(parents=True)
    (src / ".git").mkdir()
    (src / "uv.lock").write_text("lock\n")
    (src / "LICENSE").write_text("MIT\n")
    lock = {
        "components": {"serena": {
            "commit": COMMIT, "source_url": "https://example.invalid/serena", "package": "serena-agent==1.7.0",
            "src_dir": ".toolchain/serena-src", "venv_dir": ".toolchain/serena-venv",
            "dependency_lock_sha256": _sha(b"lock\n"), "license_file_sha256": _sha(b"MIT\n")}},
        "serena_build_wheelhouse": {"wheels": _build_wheels(toolchain / "downloads" / "serena-build-wheelhouse")},
    }
    return lock, toolchain


class _Runs:
    """Records every subprocess.run of the installer (git is answered, nothing is executed)."""

    def __init__(self) -> None:
        self.calls: list[list[str]] = []
        self.requirements: list[str] = []

    def __call__(self, cmd: list[str], *a: object, **kw: object) -> subprocess.CompletedProcess[str]:
        cmd = [str(c) for c in cmd]
        self.calls.append(cmd)
        if "-r" in cmd:  # the requirement file is temporary: read it while the call runs
            self.requirements = Path(cmd[cmd.index("-r") + 1]).read_text().split("\n")
        out = COMMIT + "\n" if cmd[:1] == ["git"] and "rev-parse" in cmd else ""
        return subprocess.CompletedProcess(cmd, 0, stdout=out, stderr="")

    def uv(self) -> list[list[str]]:
        return [c for c in self.calls if c[0] == "UVBIN"]


@pytest.mark.parametrize("offline", [False, True])
def test_serena_sync_builds_without_isolation_from_the_pinned_build_wheels_only(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, offline: bool
) -> None:
    """uv.lock does not lock build dependencies: the Serena venv must be built with build isolation
    OFF, after the hash-pinned build backends were installed from a local directory with no index."""
    bs = load()
    bs.OFFLINE = offline
    lock, toolchain = _serena_lock(tmp_path)
    runs = _Runs()
    monkeypatch.setattr(bs.subprocess, "run", runs)
    report: dict = {"components": {}, "errors": []}
    bs.install_serena(lock, toolchain, False, "UVBIN", report)
    uv = runs.uv()
    sync = [c for c in uv if "sync" in c[1:3]]  # `uv sync ...` or `uv --offline sync ...`
    assert len(sync) == 1, uv
    assert "--no-build-isolation" in sync[0] and "--frozen" in sync[0] and "--no-dev" in sync[0]
    pre = [c for c in uv if c[1:3] == ["pip", "install"]]
    assert len(pre) == 1 and uv.index(pre[0]) < uv.index(sync[0]), "build backends must be installed first"
    pip = pre[0]
    wh = toolchain / "downloads" / "serena-build-wheelhouse"
    assert {"--no-index", "--offline", "--require-hashes"} <= set(pip)
    assert pip[pip.index("--find-links") + 1] == str(wh)
    assert pip[pip.index("--python") + 1] == str(toolchain / "serena-venv" / "bin/python")
    assert "--index-url" not in pip and "--extra-index-url" not in pip
    want = {f"{w['filename'].split('-')[0]}=={w['filename'].split('-')[1]} --hash=sha256:{w['sha256']}"
            for w in lock["serena_build_wheelhouse"]["wheels"]}
    assert want <= set(runs.requirements)
    assert all(("--offline" in c) == offline for c in uv if c[1] in ("venv", "sync"))


@pytest.mark.parametrize("mode", ["offline-cached", "online-download"])
def test_serena_build_wheel_with_a_wrong_sha256_is_refused_before_uv_runs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mode: str
) -> None:
    bs = load()
    lock, toolchain = _serena_lock(tmp_path)
    bad = toolchain / "downloads" / "serena-build-wheelhouse" / "hatchling-1.32.4-py3-none-any.whl"
    bad.write_bytes(b"tampered hatchling")
    if mode == "offline-cached":
        bs.OFFLINE = True
    else:  # online: the re-download returns other bytes than the lock pins
        monkeypatch.setattr(bs.urllib.request, "urlopen", lambda *a, **k: io.BytesIO(b"evil"))
    runs = _Runs()
    monkeypatch.setattr(bs.subprocess, "run", runs)
    with pytest.raises(bs.LockError, match="hatchling-1.32.4"):
        bs.install_serena(lock, toolchain, False, "UVBIN", {"components": {}, "errors": []})
    assert runs.uv() == [], "uv ran although a pinned build wheel did not verify"
    if mode == "online-download":
        assert not bad.with_suffix(".whl.part").exists()


# ------------------------------------------------------------------ pinned uv, end to end through main()
FAKE_UV = """#!/bin/sh
echo "$0 $*" >> "{log}"
if [ "$1" = venv ]; then for a; do last="$a"; done; exec {py} -m venv --without-pip "$last"; fi
exit 0
"""


def _fake_toolchain(tmp_path: Path, uv_sha_ok: bool = True) -> tuple[Path, Path, Path]:
    """A complete fake lock + artifact dir (archives, gateway wheels, pinned uv tarball). Returns
    (lock path, artifact dir, uv log)."""
    art = tmp_path / "art"
    log = tmp_path / "uv.log"
    comps: dict = {}
    for name, binary in (("slang", "slang"), ("verible", "bin/verible-verilog-ls"),
                         ("codebase_memory_mcp", "codebase-memory-mcp"), ("uvm_core", None)):
        data = f"{name} binary".encode()
        url = f"https://example.invalid/{name}.tar.gz"
        c = {"source_url": url, "artifact_sha256": _tgz(art / f"{name}-{name}.tar.gz", {binary or "x": data}),
             "install_dir": f".toolchain/{name}", "archive_strip_components": 1}
        if binary:
            c.update(binary=f".toolchain/{name}/{binary}", binary_sha256=_sha(data))
        comps[name] = c
    wheels = []
    for name, fn, ver in (("pyslang", "pyslang-12.0.0-cp311-abi3-x.whl", "12.0.0"),
                          ("mcp_sdk", "mcp-2.3.0-py3-none-any.whl", "2.3.0")):
        data = fn.encode()
        (art / "wheelhouse").mkdir(parents=True, exist_ok=True)
        (art / "wheelhouse" / fn).write_bytes(data)
        comps[name] = {"version": ver, "wheel_filename": fn, "wheel_sha256": _sha(data)}
        wheels.append({"filename": fn, "sha256": _sha(data), "source_url": f"https://example.invalid/{fn}"})
    reqs = tmp_path / "gateway-requirements.txt"
    reqs.write_text("pyslang==12.0.0\n")
    uv_bin = FAKE_UV.format(log=log, py=sys.executable).encode()
    uv_tgz = art / "uv-uv-x86_64-unknown-linux-gnu.tar.gz"
    tgz_sha = _tgz(uv_tgz, {"uv": uv_bin})
    lock = {
        "components": comps,
        "gateway_wheelhouse": {"requirements_file": str(reqs), "requirements_sha256": _sha(reqs.read_bytes()),
                               "wheels": wheels},
        "serena_build_wheelhouse": {"wheels": []},
        "uv": {"version": "0.12.23", "artifact_sha256": tgz_sha if uv_sha_ok else "0" * 64,
               "source_url": "https://example.invalid/uv-x86_64-unknown-linux-gnu.tar.gz",
               "install_dir": ".toolchain/uv", "archive_strip_components": 1,
               "binary": ".toolchain/uv/uv", "binary_sha256": _sha(uv_bin)},
    }
    lp = tmp_path / "lock.json"
    lp.write_text(json.dumps(lock))
    return lp, art, log


def _run_main(tmp_path: Path, lp: Path, art: Path, *extra: str) -> tuple[int, dict, str]:
    rep = tmp_path / "report.json"
    env = {"PATH": "/usr/bin:/bin", "HOME": str(tmp_path / "home")}  # no uv on PATH, no ~/.local/bin
    r = subprocess.run([sys.executable, str(REPO / "scripts/bootstrap_locked.py"), "--offline", "--lock", str(lp),
                        "--artifact-dir", str(art), "--toolchain-dir", str(tmp_path / "tc"), "--skip-serena",
                        "--report", str(rep), *extra], env=env, capture_output=True, text=True, timeout=300)
    return r.returncode, (json.loads(rep.read_text()) if rep.exists() else {}), r.stdout + r.stderr


def test_pinned_uv_with_a_wrong_sha256_is_refused_before_anything_else(tmp_path: Path) -> None:
    lp, art, log = _fake_toolchain(tmp_path, uv_sha_ok=False)
    rc, rep, out = _run_main(tmp_path, lp, art, "--no-venv")
    assert rc == 2, out
    assert any("uv-uv-x86_64-unknown-linux-gnu.tar.gz" in e and "sha256" in e for e in rep["errors"]), rep
    tc = tmp_path / "tc"
    assert not (tc / "uv").exists(), "a tarball that failed verification was extracted"
    assert not (tc / "slang").exists(), "the run went on after the uv pin failed"
    assert not log.exists()


def test_pinned_uv_is_used_by_default_and_verified(tmp_path: Path) -> None:
    lp, art, log = _fake_toolchain(tmp_path)
    venv = tmp_path / "venv"
    _, rep, out = _run_main(tmp_path, lp, art, "--create-venv", "--venv-dir", str(venv))
    pinned = tmp_path / "tc" / "uv" / "uv"
    assert rep["components"]["uv"]["source"] == "pinned", out
    assert rep["components"]["uv"]["verified"] is True and rep["components"]["uv"]["binary_sha256_ok"] is True
    assert rep["warnings"] == []
    calls = log.read_text().splitlines()
    assert calls and all(c.startswith(f"{pinned} ") for c in calls), calls
    assert any(c.startswith(f"{pinned} venv ") for c in calls), "the installer creates the gateway venv itself"
    assert any(c.startswith(f"{pinned} pip install ") and "--require-hashes" in c for c in calls)
    assert (venv / "bin" / "python").exists()


def test_explicit_uv_override_is_used_but_reported_unverified(tmp_path: Path) -> None:
    lp, art, log = _fake_toolchain(tmp_path)
    other_log = tmp_path / "other.log"
    other = tmp_path / "bin" / "uv"
    other.parent.mkdir()
    other.write_text(FAKE_UV.format(log=other_log, py=sys.executable))
    other.chmod(0o755)
    _, rep, out = _run_main(tmp_path, lp, art, "--create-venv", "--venv-dir", str(tmp_path / "venv"),
                            "--uv", str(other))
    assert rep["components"]["uv"] == {"source": "--uv", "path": str(other), "verified": False}, out
    assert any("NOT verified" in w for w in rep["warnings"]), rep
    assert "NOT verified" in out  # also on stderr
    assert other_log.read_text().strip() and not log.exists(), "the override, not the pinned uv, must run"
    assert not (tmp_path / "tc" / "uv").exists()
    assert os.access(other, os.X_OK)


def test_relative_python_is_made_absolute_because_the_serena_sync_runs_in_its_checkout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bs = load()
    monkeypatch.chdir(tmp_path)
    assert bs.base_python("runtime/python/bin/python3") == str(tmp_path / "runtime/python/bin/python3")
    assert bs.base_python("python3.12") == "python3.12"  # a bare name stays a PATH lookup for uv

