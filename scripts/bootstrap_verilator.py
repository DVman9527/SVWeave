#!/usr/bin/env python3
"""Approved GHA-only installation boundary for the optional native static verifier.

Only this bootstrap downloads Verilator. Its official git commit, tree and git-archive
SHA256 are frozen separately from the unchanged mandatory backend toolchain lock.
Build commands run without network inside mandatory bwrap; no global install or simulation.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
from dataclasses import asdict
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src"))

from svweave.security.sandbox import SandboxRunner, SandboxSpec, probe_host  # noqa: E402

PIN = json.loads((REPO / "fixtures/verilator/native/pin.json").read_text())


def source_identity(root: Path) -> dict[str, str]:
    def git(*args: str) -> bytes:
        return subprocess.check_output(
            ["git", "-c", "core.hooksPath=/dev/null", "-C", str(root), *args],
            timeout=120,
        )

    identity = {
        "commit": git("rev-parse", "HEAD").decode().strip(),
        "tree": git("rev-parse", "HEAD^{tree}").decode().strip(),
        "git_archive_tar_sha256": hashlib.sha256(git("archive", "--format=tar", "HEAD")).hexdigest(),
    }
    for name, actual in identity.items():
        if actual != PIN[name]:
            raise RuntimeError(f"Verilator source {name} mismatch: {actual}")
    if git("status", "--porcelain", "--untracked-files=no").strip():
        raise RuntimeError("Verilator tracked source differs from the pinned commit")
    return identity


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--destination", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=False)
    report: dict = {"status": "BLOCKED", "pin": PIN, "commands": [], "simulation": "NOT_RUN"}
    try:
        if os.environ.get("GITHUB_ACTIONS") != "true" or os.environ.get("RUNNER_ENVIRONMENT") != "github-hosted":
            raise RuntimeError("Installation is authorized only on an ephemeral GitHub-hosted Actions runner")
        root = args.destination.resolve()
        runner_temp = Path(os.environ["RUNNER_TEMP"]).resolve()
        if runner_temp not in root.parents or root.exists():
            raise RuntimeError("Destination must be a new directory strictly below RUNNER_TEMP")
        cap = probe_host(refresh=True)
        report["sandbox"] = asdict(cap)
        if not cap.available:
            raise RuntimeError(f"Mandatory sandbox unavailable: {cap.reason}")
        report["status"] = "FAIL"  # a started installation/build failure is not a missing prerequisite
        # No mutable tag checkout or user hooks. Only this explicit installation step uses network.
        commands = [
            ["git", "-c", "core.hooksPath=/dev/null", "init", str(root)],
            ["git", "-c", "core.hooksPath=/dev/null", "-C", str(root), "fetch", "--depth=1", PIN["repository"], PIN["commit"]],
            ["git", "-c", "core.hooksPath=/dev/null", "-C", str(root), "checkout", "--detach", "FETCH_HEAD"],
        ]
        for index, command in enumerate(commands):
            report["commands"].append(command)
            with (output / f"fetch-{index}.log").open("w") as log:
                subprocess.run(command, stdout=log, stderr=subprocess.STDOUT, check=True, timeout=300)
        report["source"] = source_identity(root)
        sandbox = SandboxRunner(capability=cap)
        spec = SandboxSpec(
            rw_paths=(root,), protected_paths=(REPO,), chdir=root,
            env={"PATH": "/usr/bin:/bin", "HOME": "/tmp", "LC_ALL": "C"},
        )
        for index, command in enumerate([
            ["autoconf"],
            ["./configure", "--disable-tcmalloc", "--disable-jemalloc"],
            ["make", "-C", "src", "-j2", "opt"],
        ]):
            wrapped = sandbox.wrap(spec, command)
            report["commands"].append(wrapped.argv)
            with (output / f"build-{index}.log").open("w") as log:
                subprocess.run(wrapped.argv, stdout=log, stderr=subprocess.STDOUT, check=True,
                               timeout=1800, env={"PATH": "/usr/bin:/bin"})
        report["source_after_build"] = source_identity(root)
        report["binary_sha256"] = hashlib.sha256((root / "bin/verilator_bin").read_bytes()).hexdigest()
        report["wrapper_sha256"] = hashlib.sha256((root / "bin/verilator").read_bytes()).hexdigest()
        report["status"] = "PASS"
        report["destination"] = str(root)
    except Exception as exc:
        report["error"] = f"{type(exc).__name__}: {exc}"
    (output / "report.json").write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps({"status": report["status"], "report": str(output / "report.json"), "error": report.get("error")}))
    return 0 if report["status"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
