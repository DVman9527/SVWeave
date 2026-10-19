#!/usr/bin/env python3
"""M0 isolation probe (SVW-003): bubblewrap (bwrap) process/filesystem/network sandbox.

1. write-attempt test inside the sandbox (original source RO, candidate + state RW, $HOME hidden,
   network unshared)
2. re-run cbm_spike.py and serena_spike.py with their backend process wrapped in bwrap
   (`--wrap` = JSON argv prefix), so the REAL backends run inside the sandbox.

Usage: .venv/bin/python scripts/spikes/isolation_probe.py [--skip-backends]
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
TOOLCHAIN = REPO / ".toolchain"
BWRAP = "/usr/bin/bwrap"


def bwrap_prefix(ro: list[Path], rw: list[Path], chdir: Path | None = None) -> list[str]:
    """Minimal-host-view template. Later binds stack on top of earlier ones (rw state inside ro toolchain)."""
    argv = [BWRAP, "--die-with-parent", "--unshare-user", "--unshare-pid", "--unshare-ipc", "--unshare-uts",
            "--unshare-cgroup-try", "--unshare-net",
            "--ro-bind", "/usr", "/usr", "--ro-bind", "/etc", "/etc",
            "--symlink", "usr/bin", "/bin", "--symlink", "usr/sbin", "/sbin",
            "--symlink", "usr/lib", "/lib", "--symlink", "usr/lib64", "/lib64",
            "--proc", "/proc", "--dev", "/dev", "--tmpfs", "/tmp"]
    for p in ro:
        argv += ["--ro-bind", str(p), str(p)]
    for p in rw:
        p.mkdir(parents=True, exist_ok=True)
        argv += ["--bind", str(p), str(p)]
    if chdir:
        argv += ["--chdir", str(chdir)]
    argv += ["--"]
    return argv


WRITE_TEST = r"""
import json, os, socket, sys
res = {}
def tryw(label, path):
    try:
        with open(path, "w") as f: f.write("x")
        os.remove(path); res[label] = "WRITABLE"
    except Exception as e:
        res[label] = f"DENIED {type(e).__name__}: {e}"
tryw("original_fixtures", sys.argv[1] + "/demo/rtl/__svw_probe")
# repo root: write and LEAVE the file; the host checks afterwards whether it reached the real disk
try:
    with open(sys.argv[2] + "/__svw_probe_leftover", "w") as f: f.write("x")
    res["repo_root"] = "WRITE_SUCCEEDED_IN_SANDBOX_VIEW"
except Exception as e:
    res["repo_root"] = f"DENIED {type(e).__name__}: {e}"
tryw("candidate", sys.argv[3] + "/__svw_probe")
tryw("state", sys.argv[4] + "/__svw_probe")
tryw("toolchain_bin", sys.argv[5] + "/__svw_probe")
res["real_home_visible"] = os.path.exists(sys.argv[6])
res["real_home_entries"] = sorted(os.listdir(sys.argv[6])) if os.path.isdir(sys.argv[6]) else None
res["real_home_ssh_visible"] = os.path.exists(sys.argv[6] + "/.ssh")
try:
    socket.create_connection(("1.1.1.1", 443), timeout=3); res["network"] = "CONNECTED"
except Exception as e:
    res["network"] = f"BLOCKED {type(e).__name__}: {e}"
try:
    socket.getaddrinfo("pypi.org", 443); res["dns"] = "RESOLVED"
except Exception as e:
    res["dns"] = f"BLOCKED {type(e).__name__}: {e}"
res["netifs"] = sorted(os.listdir("/sys/class/net")) if os.path.isdir("/sys/class/net") else None
res["pid1_cmdline"] = open("/proc/1/cmdline","rb").read().replace(b"\0", b" ").decode()
res["uid"] = os.getuid()
print(json.dumps(res))
"""


def write_attempt_test(out: Path) -> dict:
    cand = REPO / ".svweave/spike/iso_candidate"
    state = TOOLCHAIN / "state/iso_probe"
    cand.mkdir(parents=True, exist_ok=True)
    py = "/usr/local/bin/python3.12"
    prefix = bwrap_prefix(ro=[Path("/usr/local"), REPO / "fixtures", TOOLCHAIN], rw=[cand, state], chdir=cand)
    argv = prefix + [py, "-I", "-c", WRITE_TEST, str(REPO / "fixtures"), str(REPO), str(cand), str(state),
                     str(TOOLCHAIN / "verible/bin"), os.path.expanduser("~")]
    p = subprocess.run(argv, capture_output=True, text=True, timeout=30)
    res = {"argv": argv, "returncode": p.returncode, "stderr": p.stderr[-2000:]}
    leftover = REPO / "__svw_probe_leftover"
    res["repo_root_write_reached_host_disk"] = leftover.exists()
    if leftover.exists():
        leftover.unlink()
    try:
        res["result"] = json.loads(p.stdout)
    except Exception:
        res["stdout"] = p.stdout[-2000:]
    # baseline (no sandbox) for comparison of the same checks
    q = subprocess.run([py, "-I", "-c", WRITE_TEST, str(REPO / "fixtures"), str(REPO), str(cand), str(state),
                        str(TOOLCHAIN / "verible/bin"), os.path.expanduser("~")],
                       capture_output=True, text=True, timeout=30)
    if leftover.exists():
        leftover.unlink()
    try:
        res["baseline_without_bwrap"] = json.loads(q.stdout)
    except Exception:
        res["baseline_without_bwrap"] = {"stdout": q.stdout, "stderr": q.stderr}
    return res


def run_backend_spike(script: str, wrap: list[str], extra: list[str], timeout: int) -> dict:
    cmd = [sys.executable, "-I", str(REPO / "scripts/spikes" / script), "--wrap", json.dumps(wrap), *extra]
    t0 = time.monotonic()
    p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    return {"cmd": cmd, "returncode": p.returncode, "wall_s": round(time.monotonic() - t0, 3),
            "stdout_tail": p.stdout[-4000:], "stderr_tail": p.stderr[-2000:]}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=str(REPO / "evidence/m0/isolation_probe.json"))
    ap.add_argument("--skip-backends", action="store_true")
    a = ap.parse_args()
    ver = subprocess.run([BWRAP, "--version"], capture_output=True, text=True)
    res: dict = {"bwrap": BWRAP, "bwrap_version": ver.stdout.strip(),
                 "max_user_namespaces": Path("/proc/sys/user/max_user_namespaces").read_text().strip(),
                 "write_attempt_test": write_attempt_test(Path(a.out))}
    if not a.skip_backends:
        snap = REPO / ".svweave/spike/iso_snapshot"
        # cbm: snapshot RO (copied by the spike BEFORE launching the wrapped server), cbm state RW
        cbm_wrap = bwrap_prefix(ro=[TOOLCHAIN, REPO / "fixtures"], rw=[TOOLCHAIN / "state/cbm_iso"])
        # make the snapshot read-only inside the sandbox: bind it RO after it exists
        snap.mkdir(parents=True, exist_ok=True)
        cbm_wrap = cbm_wrap[:-1] + ["--ro-bind", str(snap), str(snap), "--"]
        res["cbm_under_bwrap"] = run_backend_spike(
            "cbm_spike.py", cbm_wrap,
            ["--snapshot", str(snap), "--out", str(REPO / ".svweave/spike/cbm_bwrap"),
             "--evidence", str(REPO / "evidence/m0/cbm_spike_bwrap.json"), "--state", str(TOOLCHAIN / "state/cbm_iso")],
            300)
        cand = REPO / ".svweave/spike/iso_serena_candidate"
        cand.mkdir(parents=True, exist_ok=True)
        ser_wrap = bwrap_prefix(ro=[Path("/usr/local"), TOOLCHAIN, REPO / "fixtures"],
                                rw=[cand, TOOLCHAIN / "state/serena_iso"])
        res["serena_under_bwrap"] = run_backend_spike(
            "serena_spike.py", ser_wrap,
            ["--candidate", str(cand), "--state", str(TOOLCHAIN / "state/serena_iso"),
             "--out", str(REPO / ".svweave/spike/serena_bwrap"),
             "--evidence", str(REPO / "evidence/m0/serena_spike_bwrap.json")], 600)
    Path(a.out).parent.mkdir(parents=True, exist_ok=True)
    Path(a.out).write_text(json.dumps(res, indent=2) + "\n")
    print(json.dumps({"write_attempt": res["write_attempt_test"].get("result"),
                      "cbm_rc": res.get("cbm_under_bwrap", {}).get("returncode"),
                      "serena_rc": res.get("serena_under_bwrap", {}).get("returncode")}, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
