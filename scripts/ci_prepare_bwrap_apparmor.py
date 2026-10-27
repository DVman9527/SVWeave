"""Install only Ubuntu's reviewed bwrap profile on an ephemeral hosted CI runner.

User-authorized CI setup, not application runtime (plan 6.4, 23.1, 23.4 / SEC-05).
No policy is synthesized, existing policy replaced, or global restriction disabled.
"""

from __future__ import annotations

import hashlib
import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from urllib.parse import urlparse

# Reviewed upstream v4.0.1 (Ubuntu Noble's ABI4 profile); comments/spacing may differ.
# https://gitlab.com/apparmor/apparmor/-/blob/v4.0.1/profiles/apparmor/profiles/extras/bwrap-userns-restrict
COMMON_RULES = """
allow file rwlkm /{**,},
allow network,
allow unix,
allow ptrace,
allow signal,
allow mqueue,
allow io_uring,
allow userns,
allow mount,
allow umount,
allow pivot_root,
allow dbus,
"""
EXPECTED_POLICY = f"""
abi <abi/4.0>,
include <tunables/global>
profile bwrap /usr/bin/bwrap flags=(attach_disconnected) {{
allow capability,
{COMMON_RULES}
allow px /** -> bwrap//&unpriv_bwrap,
include if exists <local/bwrap-userns-restrict>
}}
profile unpriv_bwrap flags=(attach_disconnected) {{
{COMMON_RULES}
allow pix /** -> &unpriv_bwrap,
audit deny capability,
include if exists <local/unpriv_bwrap>
}}
"""
POLICY_NAME = "bwrap-userns-restrict"
RESTRICTION = Path("/proc/sys/kernel/apparmor_restrict_unprivileged_userns")


def require(condition: bool, message: str) -> None:
    if not condition:
        raise RuntimeError(message)


def normalize_policy(text: str) -> str:
    return "".join(re.sub(r"#.*", "", text).split())


def validate_policy(text: str) -> None:
    require(normalize_policy(text) == normalize_policy(EXPECTED_POLICY),
            "Official package profile differs from reviewed ABI4 policy; stop for review")


def validate_download_uri(output: str) -> None:
    lines = [line for line in output.splitlines() if line.startswith("'")]
    require(len(lines) == 1, "Expected exactly one authenticated Ubuntu package URI")
    uri = urlparse(shlex.split(lines[0])[0])
    host = uri.hostname or ""
    require(uri.scheme in {"http", "https"} and (
        host in {"archive.ubuntu.com", "security.ubuntu.com", "ports.ubuntu.com"}
        or host.endswith(".archive.ubuntu.com")
    ) and uri.path.startswith(("/ubuntu/pool/", "/ubuntu-ports/pool/")),
        "Package download is not from the official Ubuntu archive")


def check_conflicts(policy_dir: Path, loaded_profiles: str) -> None:
    require(not re.search(r"bwrap|bubblewrap", loaded_profiles, re.IGNORECASE),
            "Existing loaded bwrap profile; stop without replacing it")
    for name in (POLICY_NAME, "local/bwrap-userns-restrict", "local/unpriv_bwrap"):
        path = policy_dir / name
        require(not path.exists() and not path.is_symlink(), f"Existing policy/override: {path}")
    for path in policy_dir.rglob("*"):
        if path.is_file():
            text = path.read_text(errors="replace")
            require(not re.search(r"/usr/bin/bwrap|profile\s+(?:bwrap|unpriv_bwrap)\b", text),
                    f"Existing bwrap policy attachment: {path}")


def run(*args: str, cwd: Path | None = None) -> str:
    try:
        result = subprocess.run(args, cwd=cwd, check=True, capture_output=True, text=True, timeout=120)
    except subprocess.CalledProcessError as exc:
        print(exc.stdout[-4000:], file=sys.stderr, flush=True)
        print(exc.stderr[-4000:], file=sys.stderr, flush=True)
        raise
    return result.stdout


def main() -> None:
    require(os.environ.get("GITHUB_ACTIONS") == "true"
            and os.environ.get("RUNNER_ENVIRONMENT") == "github-hosted",
            "Policy setup is authorized only on ephemeral GitHub-hosted CI")
    release = dict(line.split("=", 1) for line in Path("/etc/os-release").read_text().splitlines()
                   if "=" in line)
    require(release.get("ID", "").strip('"') == "ubuntu"
            and release.get("VERSION_ID", "").strip('"') == "24.04", "Expected Ubuntu 24.04")
    require(RESTRICTION.read_text().strip() == "1", "Expected user namespace restriction enabled")
    parser = shutil.which("apparmor_parser")
    require(parser is not None, "AppArmor parser unavailable; no automatic policy fallback")
    policy_dir = Path("/etc/apparmor.d")
    loaded = run("sudo", "-n", "cat", "/sys/kernel/security/apparmor/profiles")
    check_conflicts(policy_dir, loaded)
    apt = ("apt-get", "-o", "APT::Get::AllowUnauthenticated=false",
           "-o", "Acquire::AllowInsecureRepositories=false")
    with tempfile.TemporaryDirectory(prefix="svweave-ubuntu-profile-") as directory:
        staging = Path(directory)
        validate_download_uri(run(*apt, "--print-uris", "download", "apparmor-profiles", cwd=staging))
        run(*apt, "download", "apparmor-profiles", cwd=staging)
        packages = list(staging.glob("*.deb"))
        require(len(packages) == 1, "Expected exactly one Ubuntu package")
        package = packages[0]
        require(run("dpkg-deb", "-f", str(package), "Package").strip() == "apparmor-profiles",
                "Wrong package identity")
        version = run("dpkg-deb", "-f", str(package), "Version").strip()
        require("ubuntu" in version, "Expected Ubuntu package version")
        print(f"Official Ubuntu apparmor-profiles {version}; sha256={hashlib.sha256(package.read_bytes()).hexdigest()}", flush=True)
        extracted = staging / "extracted"
        run("dpkg-deb", "--extract", str(package), str(extracted))
        source = extracted / "usr/share/apparmor/extra-profiles" / POLICY_NAME
        require(source.is_file() and not source.is_symlink(), "Official bwrap profile absent")
        policy = source.read_text()
        validate_policy(policy)
        print(policy, flush=True)
        # Check again immediately before the only policy writes; never overwrite a destination.
        check_conflicts(policy_dir, run("sudo", "-n", "cat", "/sys/kernel/security/apparmor/profiles"))
        target = policy_dir / POLICY_NAME
        run("sudo", "-n", "cp", "--no-clobber", str(source), str(target))
        require(target.read_bytes() == policy.encode(), "Destination conflicts with official policy")
        run("sudo", "-n", str(parser), "-a", str(target))
        require(RESTRICTION.read_text().strip() == "1", "Global namespace restriction changed")
        loaded = run("sudo", "-n", "cat", "/sys/kernel/security/apparmor/profiles")
        for name in ("bwrap", "unpriv_bwrap"):
            require(f"{name} (enforce)" in loaded.splitlines(), f"Expected enforced {name} profile")
        print("Official bwrap profiles loaded; global user namespace restriction remains enabled", flush=True)


if __name__ == "__main__":
    main()
