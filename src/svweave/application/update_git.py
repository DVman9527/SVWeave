"""Bounded, non-authoritative Git hints for update (plan 9.2/9.6, 23.1).

Git's baseline is NOT the previous snapshot: ignored files, external roots, index flags,
and staged/unstaged cancellation make Git unsuitable for deciding whether to reuse a build.
Every caller must still capture and verify the complete relevant input inventory/bytes.
Unsupported repository layouts/configuration fail closed to an UNAVAILABLE advisory only.
"""

from __future__ import annotations

import os
import re
import selectors
import shutil
import signal
import stat
import subprocess
import tempfile
import time
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from svweave.config.context import WorkspaceContext
from svweave.domain.errors import SvwError
from svweave.security import safeio

GIT_TIMEOUT_S = 10.0
GIT_TERMINATION_GRACE_S = 0.5
MAX_GIT_BYTES = 8 * 1024 * 1024
MAX_GIT_PATHS = 500
MAX_METADATA_ENTRIES = 100_000
MAX_OBJECT_FILES = 256
MAX_WORKTREE_ENTRIES = 100_000
MAX_DIRECTORY_DEPTH = 64
_REF = re.compile(r"[A-Za-z0-9_][A-Za-z0-9._/@^~-]{0,199}\Z")
_OID = re.compile(rb"(?:[0-9a-f]{40}|[0-9a-f]{64})\n?\Z")
_FILTER = re.compile(r"filter\..*\.(?:clean|smudge|process)\Z")
_OBJECT_PATH = re.compile(
    r"objects/(?:[0-9a-f]{2}/(?:[0-9a-f]{38}|[0-9a-f]{62})|"
    r"pack/pack-(?:[0-9a-f]{40}|[0-9a-f]{64})\.(?:pack|idx|rev|bitmap))\Z"
)
_STAT = re.compile(
    rb"  ctime: (\d+):(\d+)\n  mtime: (\d+):(\d+)\n"
    rb"  dev: (\d+)\tino: (\d+)\n  uid: (\d+)\tgid: (\d+)\n"
    rb"  size: (\d+)\tflags: ([0-9a-f]+)\n"
)
_LIMITATIONS = [
    "Git is advisory only; complete snapshot inventory and content verification remain authoritative.",
    "The Git baseline is informational, not necessarily the previous built snapshot.",
    "Ignored files and all relevant inputs are still captured independently; external source roots "
    "and submodule contents are not covered by this advisory.",
    "Untracked hints are a bounded no-follow filesystem inventory minus the index, not Git "
    "porcelain status; ignore rules are not applied and ignored paths may be included.",
    "Only project-root repositories with at most 256 regular Git object files are supported; "
    "gitfiles/linked worktrees, metadata indirection and non-UTF8 names are unavailable.",
    "Tracked hints are the union of staged changes versus base and unstaged changes versus index; "
    "renames are reported as deletion plus addition, not semantic dependencies.",
    "Unstaged hints compare index metadata with no-follow worktree stat only: they can include "
    "false positives and miss metadata-preserving changes; no Git content filters are used.",
]


class _Unavailable(Exception):
    """Internal reason codes only: never return untrusted Git stderr/config values."""


@dataclass
class _Budget:
    deadline: float
    remaining: int

    def check(self) -> float:
        remaining = self.deadline - time.monotonic()
        if remaining <= 0:
            raise _Unavailable("git_timeout")
        return remaining


def _stop(proc: subprocess.Popen[bytes]) -> None:
    # Kill the session, including any unexpected descendant holding a pipe open. No shell.
    with suppress(ProcessLookupError):
        os.killpg(proc.pid, signal.SIGKILL)
    with suppress(subprocess.TimeoutExpired):
        proc.wait(timeout=GIT_TERMINATION_GRACE_S)


def _bounded_run(
    argv: list[str], env: dict[str, str], budget: _Budget, *, pass_fds: tuple[int, ...] = ()
) -> tuple[int, bytes]:
    """Drain BOTH pipes without blocking; bound cumulative bytes and total advisory wall time."""
    budget.check()
    try:
        proc = subprocess.Popen(
            argv,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=env,
            start_new_session=True,
            pass_fds=pass_fds,
        )
    except FileNotFoundError as exc:
        raise _Unavailable("git_missing") from exc
    chunks: list[bytes] = []
    try:
        assert proc.stdout is not None and proc.stderr is not None
        with selectors.DefaultSelector() as selector:
            for pipe in (proc.stdout, proc.stderr):
                os.set_blocking(pipe.fileno(), False)
                selector.register(pipe, selectors.EVENT_READ)
            while selector.get_map():
                for key, _ in selector.select(timeout=budget.check()):
                    chunk = os.read(key.fd, 64 * 1024)
                    if not chunk:
                        selector.unregister(key.fileobj)
                        continue
                    budget.remaining -= len(chunk)
                    if budget.remaining < 0:
                        raise _Unavailable("git_output_limit")
                    if key.fileobj is proc.stdout:
                        chunks.append(chunk)
                budget.check()
            try:
                rc = proc.wait(timeout=budget.check())
            except subprocess.TimeoutExpired as exc:
                raise _Unavailable("git_timeout") from exc
        return rc, b"".join(chunks)
    finally:
        if proc.poll() is None:
            _stop(proc)
        # A child may outlive Git and keep pipes open even after the parent exited.
        else:
            with suppress(ProcessLookupError):
                os.killpg(proc.pid, signal.SIGKILL)
        for output_pipe in (proc.stdout, proc.stderr):
            if output_pipe is not None:
                output_pipe.close()


def _environment() -> dict[str, str]:
    return {
        "PATH": os.defpath,
        "HOME": "/nonexistent",
        "XDG_CONFIG_HOME": "/nonexistent",
        "LC_ALL": "C",
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_SYSTEM": os.devnull,
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_ATTR_NOSYSTEM": "1",
        "GIT_OPTIONAL_LOCKS": "0",
        "GIT_TERMINAL_PROMPT": "0",
        "GIT_NO_LAZY_FETCH": "1",
        "GIT_ALLOW_PROTOCOL": "",
    }


def _metadata(ctx: WorkspaceContext, budget: _Budget) -> tuple[Path, list[str], list[str]]:
    """Do not discover ancestor repositories or follow gitfiles/linked metadata outside policy."""
    root = ctx.project_root
    ctx.path_policy.check(str(root), root)
    gitdir = root / ".git"
    try:
        st = gitdir.lstat()
    except FileNotFoundError as exc:
        raise _Unavailable("not_git_repository") from exc
    if not stat.S_ISDIR(st.st_mode):
        raise _Unavailable("git_metadata_indirection_unsupported")
    # Git can follow refs/object/config symlinks and object alternates itself. Refuse those
    # layouts before invoking it, rather than attempting to validate a path after its read.
    control_files: list[str] = []
    object_files: list[str] = []
    count = 0

    def scan(directory: int, prefix: str, depth: int) -> None:
        nonlocal count
        if depth > MAX_DIRECTORY_DEPTH:
            raise _Unavailable("git_metadata_limit")
        with os.scandir(directory) as entries:
            for entry in entries:
                budget.check()
                count += 1
                if count > MAX_METADATA_ENTRIES:
                    raise _Unavailable("git_metadata_limit")
                rel = prefix + entry.name
                budget.remaining -= len(os.fsencode(rel)) + 32
                if budget.remaining < 0:
                    raise _Unavailable("git_output_limit")
                if (
                    not prefix
                    and rel
                    not in (
                        "HEAD",
                        "index",
                        "packed-refs",
                        "shallow",
                        "config",
                        "refs",
                        "objects",
                        "commondir",
                    )
                    and not rel.startswith("sharedindex.")
                ):
                    continue  # logs/hooks and unrelated metadata are never exposed to Git
                if (
                    rel in ("commondir", "objects/info/alternates", "objects/info/http-alternates")
                    or entry.is_symlink()
                ):
                    raise _Unavailable("git_metadata_indirection_unsupported")
                if rel.startswith("sharedindex."):
                    raise _Unavailable("git_split_index_unsupported")
                if entry.is_dir(follow_symlinks=False):
                    child = os.open(
                        entry.name,
                        os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC,
                        dir_fd=directory,
                    )
                    try:
                        scan(child, rel + "/", depth + 1)
                    finally:
                        os.close(child)
                elif not entry.is_file(follow_symlinks=False):
                    raise _Unavailable("git_metadata_special_file")
                elif rel in ("HEAD", "index", "packed-refs", "shallow", "config") or rel.startswith(
                    "refs/"
                ):
                    control_files.append(rel)
                elif _OBJECT_PATH.fullmatch(rel):
                    object_files.append(rel)
                    if len(object_files) > MAX_OBJECT_FILES:
                        raise _Unavailable("git_object_file_limit")
                elif rel.startswith("objects/") and rel.endswith(".promisor"):
                    raise _Unavailable("git_promisor_unsupported")

    fd = os.open(gitdir, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
    try:
        scan(fd, "", 0)
    finally:
        os.close(fd)
    return gitdir, control_files, object_files


def _copy_control(gitdir: Path, private: Path, files: list[str], budget: _Budget) -> None:
    """Freeze control files without following links; original config is never read by Git.

    This is not a transactional snapshot of Git history; object/ref races may make the
    advisory unavailable and cannot affect source capture.
    """
    (private / "objects").mkdir()
    (private / "refs").mkdir()
    for rel in files:
        budget.check()
        data = safeio.read_regular_under(gitdir, rel, budget.remaining + 1)
        budget.remaining -= len(data)
        if budget.remaining < 0:
            raise _Unavailable("git_output_limit")
        if rel == "HEAD" and not (
            _OID.fullmatch(data)
            or (
                re.fullmatch(rb"ref: refs/[A-Za-z0-9_][A-Za-z0-9._/-]{0,500}\n?", data)
                and b".." not in data
                and b"//" not in data
            )
        ):
            raise _Unavailable("head_unavailable")
        destination = "source-config" if rel == "config" else rel
        (private / destination).parent.mkdir(parents=True, exist_ok=True)
        (private / destination).write_bytes(data)
    # A missing config is valid for some repositories; make inspection a deterministic read.
    if not (private / "source-config").exists():
        (private / "source-config").write_bytes(b"")


def _object_view(
    gitdir: Path, private: Path, files: list[str], budget: _Budget, fds: list[int]
) -> None:
    """Expose only opened regular object inodes, never mutable object-directory paths.

    The Linux /proc descriptor view avoids copying huge packs or hardlinking the original.
    No alternates, promisor flags, commit graphs or other object metadata are exposed.
    The file count is bounded before any descriptors are opened; exhaustion fails unavailable.
    """
    for rel in files:
        budget.check()
        parts = rel.split("/")
        directory = os.open(gitdir, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
        try:
            for part in parts[:-1]:
                next_fd = os.open(
                    part,
                    os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC,
                    dir_fd=directory,
                )
                os.close(directory)
                directory = next_fd
            fd = os.open(
                parts[-1],
                os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK,
                dir_fd=directory,
            )
            fds.append(fd)
            if not stat.S_ISREG(os.fstat(fd).st_mode):
                raise _Unavailable("git_metadata_special_file")
        finally:
            os.close(directory)
        destination = private / rel
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.symlink_to(f"/proc/self/fd/{fd}")


def _stat_under(root: Path, path: str) -> os.stat_result | None:
    """lstat with no-follow directory traversal (never enter a swapped directory symlink)."""
    parts = path.split("/")
    fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
    try:
        for part in parts[:-1]:
            next_fd = os.open(
                part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=fd
            )
            os.close(fd)
            fd = next_fd
        return os.stat(parts[-1], dir_fd=fd, follow_symlinks=False)
    except (FileNotFoundError, NotADirectoryError):
        return None
    finally:
        os.close(fd)


def _stat_changes(
    ctx: WorkspaceContext, data: bytes, budget: _Budget
) -> tuple[list[dict[str, str]], set[str]]:
    """Parse explicitly bounded ls-files debug metadata; unknown formats fail unavailable."""
    result = []
    tracked: set[str] = set()
    offset = 0
    while offset < len(data):
        budget.check()
        end = data.find(b"\0", offset)
        if end < 0:
            raise _Unavailable("git_malformed_output")
        header, sep, raw_path = data[offset:end].partition(b"\t")
        fields = header.split(b" ")
        match = _STAT.match(data, end + 1)
        if not sep or len(fields) != 3 or not match:
            raise _Unavailable("git_malformed_output")
        mode, _, stage = fields
        path = _safe_path(ctx, raw_path)
        tracked.add(path)
        stored = tuple(int(value) for value in match.groups()[:-1])
        offset = match.end()
        if mode == b"160000":  # submodules are explicitly outside the advisory coverage
            continue
        st = _stat_under(ctx.project_root, path)
        status = None
        if stage != b"0":
            status = "U"
        elif st is None:
            status = "D"
        else:
            actual = (
                st.st_ctime_ns // 10**9,
                st.st_ctime_ns % 10**9,
                st.st_mtime_ns // 10**9,
                st.st_mtime_ns % 10**9,
                st.st_dev & 0xFFFFFFFF,
                st.st_ino & 0xFFFFFFFF,
                st.st_uid,
                st.st_gid,
                st.st_size,
            )
            actual_mode = (
                b"120000"
                if stat.S_ISLNK(st.st_mode)
                else (b"100755" if st.st_mode & stat.S_IXUSR else b"100644")
            )
            if not (stat.S_ISREG(st.st_mode) or stat.S_ISLNK(st.st_mode)) or mode != actual_mode:
                status = "T"
            elif actual != stored:
                status = "M"
        if status is not None:
            result.append({"path": path, "status": status, "source": "tracked"})
    return result, tracked


def _nul(data: bytes) -> list[bytes]:
    if not data:
        return []
    if not data.endswith(b"\0"):
        raise _Unavailable("git_malformed_output")
    return data[:-1].split(b"\0")


def _untracked(ctx: WorkspaceContext, tracked: set[str], budget: _Budget) -> list[dict[str, str]]:
    """Bounded no-follow inventory; do not interpret/read repository ignore files."""
    result: list[dict[str, str]] = []
    count = 0

    def scan(directory: int, prefix: str, depth: int) -> None:
        nonlocal count
        if depth > MAX_DIRECTORY_DEPTH:
            raise _Unavailable("git_worktree_inventory_limit")
        with os.scandir(directory) as entries:
            for entry in entries:
                budget.check()
                count += 1
                if count > MAX_WORKTREE_ENTRIES:
                    raise _Unavailable("git_worktree_inventory_limit")
                if entry.name in (".git", ".svweave"):
                    continue
                path = _safe_path(ctx, os.fsencode(prefix + entry.name))
                if path in tracked:  # includes gitlinks: never enter submodule contents
                    continue
                if entry.is_dir(follow_symlinks=False):
                    child = os.open(
                        entry.name,
                        os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC,
                        dir_fd=directory,
                    )
                    try:
                        scan(child, path + "/", depth + 1)
                    finally:
                        os.close(child)
                else:
                    budget.remaining -= len(path.encode("utf-8")) + 32
                    if budget.remaining < 0:
                        raise _Unavailable("git_output_limit")
                    result.append({"path": path, "status": "?", "source": "untracked"})

    fd = os.open(ctx.project_root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
    try:
        scan(fd, "", 0)
    finally:
        os.close(fd)
    return result


def _safe_path(ctx: WorkspaceContext, raw: bytes) -> str:
    # Surrogates cannot safely pass the JSON-envelope UTF-8 encoder. Refuse, never rename.
    try:
        path = raw.decode("utf-8", "strict")
    except UnicodeError as exc:
        raise _Unavailable("git_filename_encoding_unsupported") from exc
    parts = path.split("/")
    if not path or path.startswith("/") or any(p in ("", ".", "..", ".git") for p in parts):
        raise _Unavailable("git_unsafe_path")
    ctx.path_policy.check(path, ctx.project_root)
    return path


def _changes(ctx: WorkspaceContext, data: bytes) -> list[dict[str, str]]:
    tokens = _nul(data)
    if len(tokens) % 2:
        raise _Unavailable("git_malformed_output")
    result = []
    for index in range(0, len(tokens), 2):
        status = tokens[index].decode("ascii", "strict")
        if status not in ("A", "C", "D", "M", "T", "U", "X", "B"):
            raise _Unavailable("git_malformed_output")
        result.append(
            {"path": _safe_path(ctx, tokens[index + 1]), "status": status, "source": "tracked"}
        )
    return result


def git_advisory(ctx: WorkspaceContext, *, base: str | None = None) -> dict[str, Any]:
    """Return bounded Git hints, or explicit UNAVAILABLE; never authorize cache reuse.

    Supports normal repositories rooted exactly at project_root. A ref is verified as a
    commit and replaced with its OID before diff. No hooks, filters, external diff/textconv,
    fsmonitor, submodule recursion, host config or inherited Git environment is executed.
    """
    report: dict[str, Any] = {
        "status": "UNAVAILABLE",
        "advisory_only": True,
        "paths": [],
        "counts": None,
        "truncated": False,
        "limitations": list(_LIMITATIONS),
    }
    started = time.monotonic()
    budget = _Budget(started + GIT_TIMEOUT_S, MAX_GIT_BYTES)
    report["caps"] = {
        "wall_time_s": GIT_TIMEOUT_S,
        "termination_grace_s": GIT_TERMINATION_GRACE_S,
        "metadata_and_output_bytes": MAX_GIT_BYTES,
        "paths": MAX_GIT_PATHS,
        "object_files": MAX_OBJECT_FILES,
        "metadata_entries": MAX_METADATA_ENTRIES,
        "worktree_entries": MAX_WORKTREE_ENTRIES,
        "directory_depth": MAX_DIRECTORY_DEPTH,
    }
    try:
        ref = "HEAD" if base is None else base
        if not _REF.fullmatch(ref) or ".." in ref or "@{" in ref:
            raise _Unavailable("invalid_base")
        executable = shutil.which("git", path=os.defpath)
        if executable is None:
            raise _Unavailable("git_missing")
        gitdir, control_files, object_files = _metadata(ctx, budget)
        state = safeio.ensure_private_dir(ctx.project_root, ".svweave")
        with tempfile.TemporaryDirectory(prefix="git-advisory-", dir=state) as directory:
            private = Path(directory)
            fds: list[int] = []
            try:
                _copy_control(gitdir, private, control_files, budget)
                _object_view(gitdir, private, object_files, budget, fds)
                _collect(ctx, ref, executable, private, tuple(fds), budget, report)
            finally:
                for fd in fds:
                    os.close(fd)
    except _Unavailable as exc:
        report["reason"] = str(exc)
    except SvwError:
        report["reason"] = "git_path_not_allowed"
    except (OSError, ValueError, UnicodeError):
        report["reason"] = "git_unavailable"
    report["elapsed_s"] = round(time.monotonic() - started, 6)
    return report


def _collect(
    ctx: WorkspaceContext,
    ref: str,
    executable: str,
    private: Path,
    fds: tuple[int, ...],
    budget: _Budget,
    report: dict[str, Any],
) -> None:
    argv = [
        executable,
        "-C",
        str(private),
        "--no-pager",
        "--no-replace-objects",
        f"--git-dir={private}",
        f"--work-tree={private}",
    ]
    for setting in (
        "core.hooksPath=" + os.devnull,
        "core.fsmonitor=false",
        "core.untrackedCache=false",
        "core.attributesFile=" + os.devnull,
        "core.excludesFile=" + os.devnull,
        "core.bare=false",
        "core.sparseCheckout=false",
        "diff.external=",
        "diff.renames=false",
        "submodule.recurse=false",
        "fetch.recurseSubmodules=false",
        "protocol.allow=never",
    ):
        argv.extend(("-c", setting))

    def git(args: list[str], reason: str = "git_failed") -> bytes:
        rc, output = _bounded_run([*argv, *args], _environment(), budget, pass_fds=fds)
        if rc:
            raise _Unavailable(reason)
        return output

    # --no-includes is essential: inspecting config must not first follow a repo-controlled
    # include outside policy. Executable filters are refused, not merely overridden.
    config = git(
        ["config", "--file", str(private / "source-config"), "--no-includes", "--null", "--list"]
    )
    object_format = "sha1"
    for item in _nul(config):
        raw_key, _, value = item.partition(b"\n")
        key = raw_key.decode("utf-8", "strict").lower()
        if key == "extensions.objectformat":
            object_format = value.decode("ascii", "strict")
        if _FILTER.fullmatch(key):
            raise _Unavailable("git_filters_configured")
        if key == "include.path" or (key.startswith("includeif.") and key.endswith(".path")):
            raise _Unavailable("git_config_include_unsupported")
        if (
            (key.startswith("extensions.") and key != "extensions.objectformat")
            or key == "core.alternaterefscommand"
            or (key.startswith("remote.") and key.endswith(".promisor"))
        ):
            raise _Unavailable("git_config_indirection_unsupported")
    if object_format not in ("sha1", "sha256"):
        raise _Unavailable("git_object_format_unsupported")
    (private / "config").write_text(
        "[core]\nrepositoryFormatVersion = 1\n[extensions]\nobjectFormat = " + object_format + "\n"
    )
    head = git(["rev-parse", "--verify", "--end-of-options", "HEAD^{commit}"], "head_unavailable")
    baseline = (
        head
        if ref == "HEAD"
        else git(["rev-parse", "--verify", "--end-of-options", ref + "^{commit}"], "invalid_base")
    )
    if not _OID.fullmatch(head) or not _OID.fullmatch(baseline):
        raise _Unavailable("git_malformed_output")
    oid = baseline.strip().decode("ascii")
    # Validate tree paths; the cached diff never reads source content. Worktree hints use
    # only no-follow lstat, so attributes/filters cannot convert any worktree bytes.
    for inventory in (git(["ls-tree", "-r", "--name-only", "-z", oid, "--"]),):
        for path in _nul(inventory):
            budget.check()
            _safe_path(ctx, path)
    flags = [
        "--name-status",
        "-z",
        "--no-renames",
        "--no-ext-diff",
        "--no-textconv",
        "--ignore-submodules=all",
    ]
    staged = _changes(ctx, git(["diff", *flags, "--cached", oid, "--"]))
    unstaged, tracked = _stat_changes(
        ctx, git(["ls-files", "--cached", "--stage", "--debug", "--full-name", "-z"]), budget
    )
    untracked = _untracked(ctx, tracked, budget)
    records = {(r["path"], r["status"], r["source"]): r for r in (*staged, *unstaged, *untracked)}
    paths = [records[key] for key in sorted(records)]
    budget.check()
    report.update(
        status="AVAILABLE",
        base=oid,
        head=head.strip().decode("ascii"),
        comparison="staged_vs_base_and_unstaged_metadata_vs_index",
        paths=paths[:MAX_GIT_PATHS],
        truncated=len(paths) > MAX_GIT_PATHS,
        counts={
            "tracked": len({r["path"] for r in (*staged, *unstaged)}),
            "untracked": len(untracked),
            "records": len(paths),
        },
    )
    if report["truncated"]:
        report["limitations"].append(
            "Git path hints are truncated; absence is not proof of absence."
        )
