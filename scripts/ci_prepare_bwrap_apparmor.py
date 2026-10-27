"""Install only Ubuntu's reviewed bwrap profile on an ephemeral hosted CI runner.

User-authorized CI setup, not application runtime (plan 6.4, 23.1, 23.4 / SEC-05).
No policy is synthesized, existing policy replaced, or global restriction disabled.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shlex
import shutil
import stat
import subprocess
import sys
import tempfile
from pathlib import Path
from urllib.parse import ParseResult, urlparse

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


def official_ubuntu_uri(uri: ParseResult, *, mirror_base: bool = False) -> bool:
    # ParseResult is used by both package and mirror validation. No credentials or alternate ports.
    host = uri.hostname or ""
    return (
        uri.scheme in {"http", "https"}
        and uri.netloc == host
        and not uri.params and not uri.query and not uri.fragment
        and "%" not in uri.path and ".." not in uri.path.split("/")
        and (host in {"archive.ubuntu.com", "security.ubuntu.com", "ports.ubuntu.com"}
             or host.endswith(".archive.ubuntu.com"))
        and (uri.path in {"/ubuntu/", "/ubuntu-ports/"} if mirror_base
             else uri.path.startswith(("/ubuntu/pool/", "/ubuntu-ports/pool/")))
    )


def validate_mirror_entries(content: str) -> None:
    entries = 0
    for line in content.splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        fields = line.split("\t")
        require(len(fields) in {1, 2} and (
            len(fields) == 1 or re.fullmatch(r"priority:[1-9][0-9]*", fields[1]) is not None
        ), "Unsupported Ubuntu mirror-list metadata")
        require(official_ubuntu_uri(urlparse(fields[0]), mirror_base=True),
                "Mirror list contains a nonofficial or malformed Ubuntu origin")
        entries += 1
    require(1 <= entries <= 32, "Expected a bounded nonempty Ubuntu mirror list")


def validate_runner_mirror_file() -> None:
    # The only authorized file transport. Check every ancestor without following symlinks.
    for name in ("/etc", "/etc/apt", "/etc/apt/apt-mirrors.txt"):
        path = Path(name)
        metadata = path.lstat()
        expected_type = stat.S_ISREG if name.endswith(".txt") else stat.S_ISDIR
        require(expected_type(metadata.st_mode) and metadata.st_uid == 0
                and not metadata.st_mode & 0o022,
                "Runner mirror path must be root-owned, non-writable by others, and not a symlink")
        if name.endswith(".txt"):
            require(metadata.st_size <= 4096, "Runner mirror list exceeds inspection bound")
    with Path("/etc/apt/apt-mirrors.txt").open() as stream:
        content = stream.read(4097)
    require(len(content) <= 4096, "Runner mirror list changed beyond inspection bound")
    validate_mirror_entries(content)


def validate_download_uri(output: str) -> None:
    lines = [line for line in output.splitlines() if line.startswith("'")]
    require(len(lines) == 1, "Expected exactly one authenticated Ubuntu package URI")
    uri = urlparse(shlex.split(lines[0])[0])
    if uri.scheme == "mirror+file":
        require(not uri.netloc and not uri.params and not uri.query and not uri.fragment
                and re.fullmatch(
                    r"/etc/apt/apt-mirrors\.txt/pool/main/a/apparmor/"
                    r"apparmor-profiles_[A-Za-z0-9.+~:-]+_all\.deb", uri.path
                ) is not None,
                "Unsupported mirror-file reference or package path")
        validate_runner_mirror_file()
        return
    # No userinfo, query, fragment, or raw APT output in failure diagnostics.
    origin = json.dumps({"scheme": uri.scheme, "host": uri.hostname or "", "path": uri.path[:500]})
    require(official_ubuntu_uri(uri),
            f"Package download is not from the official Ubuntu archive: {origin}")


def describe_runner_mirror_list() -> None:
    """Read only the runner's fixed mirror-list path; this does not grant it trust."""
    path = Path("/etc/apt/apt-mirrors.txt")
    try:
        metadata = path.stat()
        with path.open() as stream:
            content = stream.read(4096)
        origins = []
        for line in content.splitlines()[:32]:
            if not line.strip() or line.lstrip().startswith("#"):
                continue
            uri = urlparse(line.split()[0])
            origins.append({"scheme": uri.scheme, "host": uri.hostname, "path": uri.path[:500]})
        print("Untrusted runner mirror-list evidence: " + json.dumps({
            "path": str(path), "uid": metadata.st_uid, "gid": metadata.st_gid,
            "mode": oct(metadata.st_mode & 0o777), "size": metadata.st_size,
            "origins": origins,
        }), flush=True)
    except (OSError, ValueError) as exc:
        print(f"Runner mirror-list evidence unavailable: {type(exc).__name__}", flush=True)


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
        try:
            validate_download_uri(run(*apt, "--print-uris", "download", "apparmor-profiles", cwd=staging))
        except RuntimeError:
            describe_runner_mirror_list()
            raise
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
        require(source.stat().st_size <= 16384, "Official profile exceeds review bound")
        policy = source.read_text()
        print(f"Official profile sha256={hashlib.sha256(policy.encode()).hexdigest()}\n{policy}", flush=True)
        validate_policy(policy)
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
