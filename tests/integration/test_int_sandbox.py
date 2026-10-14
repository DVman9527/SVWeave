"""SEC-05 with real bubblewrap: a backend-like process cannot write the original source nor read a
sibling repository. Tests the OS view, not tool-name hiding."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest

from svweave.adapters.codebase_memory.models import tree_fingerprint
from svweave.security.sandbox import SandboxRunner, SandboxSpec

pytestmark = pytest.mark.integration

PROBE = r"""
import json, os, socket, sys
orig, other, snap, cand, state, home = sys.argv[1:7]
res = {}
def w(label, path):
    try:
        with open(path, "w") as f:
            f.write("PWNED")
        res[label] = "WRITTEN"
    except OSError as e:
        res[label] = f"DENIED {type(e).__name__} errno={e.errno}"
def r(label, path):
    try:
        with open(path) as f:
            res[label] = "READ:" + f.read()[:20]
    except OSError as e:
        res[label] = f"DENIED {type(e).__name__} errno={e.errno}"
w("write_original_existing", orig + "/rtl.sv")
w("write_original_new", orig + "/new_file.sv")
r("read_original", orig + "/rtl.sv")
r("read_sibling_repo", other + "/secret.sv")
r("read_sibling_listing", other)  # a directory: must not be listable either
w("write_snapshot", snap + "/rtl.sv")
w("write_candidate", cand + "/edit.sv")
w("write_state", state + "/x.log")
r("read_home_ssh", home + "/.ssh/id_rsa")
res["home_listing"] = sorted(os.listdir(home)) if os.path.isdir(home) else None
try:
    socket.create_connection(("1.1.1.1", 443), timeout=2)
    res["network"] = "CONNECTED"
except OSError as e:
    res["network"] = f"BLOCKED errno={e.errno}"
print(json.dumps(res))
"""


@pytest.fixture
def world(tmp_path: Path) -> dict[str, Path]:
    w = {n: tmp_path / n for n in ("original", "sibling", "snapshot", "candidate", "state")}
    for p in w.values():
        p.mkdir()
    (w["original"] / "rtl.sv").write_text("module orig; endmodule\n")
    (w["sibling"] / "secret.sv").write_text("module another_customers_ip; endmodule\n")
    (w["snapshot"] / "rtl.sv").write_text("module snap; endmodule\n")
    return w


def python_and_ro() -> tuple[str, list[Path]]:
    py = os.path.realpath(sys.executable)
    ro: list[Path] = []
    if not py.startswith("/usr/bin/"):
        ro.append(Path(py).parents[1])
    return py, ro


def run_probe(world: dict[str, Path], sandbox: SandboxRunner, *, mount_original_ro: bool) -> dict[str, str]:
    py, ro = python_and_ro()
    spec = SandboxSpec(
        ro_paths=(*ro, world["snapshot"], *([world["original"]] if mount_original_ro else [])),
        rw_paths=(world["candidate"], world["state"]),
        protected_paths=(world["original"],),
        chdir=world["candidate"],
        env={"PATH": "/usr/bin:/bin"},
    )
    args = [str(world[k]) for k in ("original", "sibling", "snapshot", "candidate", "state")]
    p = sandbox.run_once(spec, [py, "-I", "-c", PROBE, *args, str(Path.home())], timeout=60)
    assert p.returncode == 0, p.stderr
    out: dict[str, str] = json.loads(p.stdout)
    return out


def host_unchanged(world: dict[str, Path]) -> None:
    assert (world["original"] / "rtl.sv").read_text() == "module orig; endmodule\n"
    assert sorted(os.listdir(world["original"])) == ["rtl.sv"]
    assert (world["sibling"] / "secret.sv").read_text() == "module another_customers_ip; endmodule\n"
    assert sorted(os.listdir(world["sibling"])) == ["secret.sv"]
    assert (world["snapshot"] / "rtl.sv").read_text() == "module snap; endmodule\n"


def test_sec05_original_not_mounted(world: dict[str, Path], sandbox: SandboxRunner) -> None:
    before = tree_fingerprint(world["original"]), tree_fingerprint(world["sibling"])
    res = run_probe(world, sandbox, mount_original_ro=False)
    assert res["write_original_existing"].startswith("DENIED")
    assert res["write_original_new"].startswith("DENIED")
    assert res["read_original"].startswith("DENIED")
    assert res["read_sibling_repo"].startswith("DENIED"), res
    assert res["read_sibling_listing"].startswith("DENIED"), res
    assert res["read_home_ssh"].startswith("DENIED")
    assert res["home_listing"] is None or ".ssh" not in res["home_listing"]
    assert res["write_snapshot"].startswith("DENIED") and "errno=30" in res["write_snapshot"]  # EROFS
    assert res["write_candidate"] == "WRITTEN" and res["write_state"] == "WRITTEN"
    assert res["network"].startswith("BLOCKED")
    host_unchanged(world)
    assert (tree_fingerprint(world["original"]), tree_fingerprint(world["sibling"])) == before
    assert (world["candidate"] / "edit.sv").read_text() == "PWNED"  # RW paths do reach the host


def test_sec05_original_mounted_read_only(world: dict[str, Path], sandbox: SandboxRunner) -> None:
    res = run_probe(world, sandbox, mount_original_ro=True)
    assert res["read_original"].startswith("READ:module orig")  # explicitly ro-bound: readable
    assert res["write_original_existing"].startswith("DENIED") and "errno=30" in res["write_original_existing"]
    assert res["write_original_new"].startswith("DENIED") and "errno=30" in res["write_original_new"]
    assert res["read_sibling_repo"].startswith("DENIED")
    host_unchanged(world)


def test_sec05_cannot_make_original_writable(world: dict[str, Path], sandbox: SandboxRunner) -> None:
    from svweave.domain.errors import ErrorCode, SvwError

    with pytest.raises(SvwError) as ei:
        sandbox.wrap(SandboxSpec(rw_paths=(world["original"],), protected_paths=(world["original"],)), ["/bin/true"])
    assert ei.value.code is ErrorCode.POLICY_DENIED
    with pytest.raises(SvwError):
        sandbox.wrap(SandboxSpec(rw_paths=(world["original"] / "sub",), protected_paths=(world["original"],)),
                     ["/bin/true"])
    with pytest.raises(SvwError):  # parent of the original would expose it writable
        sandbox.wrap(SandboxSpec(rw_paths=(world["original"].parent,), protected_paths=(world["original"],)),
                     ["/bin/true"])


def test_sandbox_dies_with_parent_and_kills_children(sandbox: SandboxRunner) -> None:
    """--die-with-parent + pid namespace: nothing survives the wrapper."""
    import subprocess
    import time

    cmd = sandbox.wrap(SandboxSpec(env={"PATH": "/usr/bin:/bin"}), ["/usr/bin/sleep", "300"])
    proc = subprocess.Popen(cmd.argv, env={"PATH": "/usr/bin:/bin"})
    time.sleep(1.0)
    proc.kill()
    proc.wait()
    time.sleep(1.0)
    leftovers = [
        d for d in os.listdir("/proc")
        if d.isdigit() and Path(f"/proc/{d}/cmdline").exists()
        and b"sleep\x00300" in Path(f"/proc/{d}/cmdline").read_bytes()
    ]
    assert not leftovers
