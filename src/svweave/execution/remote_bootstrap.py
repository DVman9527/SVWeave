"""Remote bootstrap verification primitives, not an enabled remote launcher.

Trusted deployment must provide expected hashes independently of repository content.
The future entry point must freshly probe compute-node bwrap, set remote rlimits, and
supervise descendants before exposing any backend. This module does not start a worker.
"""

from __future__ import annotations

import hashlib
import os
import stat
from pathlib import Path

from svweave.domain.errors import ErrorCode, SvwError


def verify_immutable_files(root: Path, expected: dict[str, str]) -> dict[str, str]:
    """Hash canonical shared inputs using no-follow directory-relative opens.

    Expected keys are relative paths. Only regular read-only files are accepted;
    symlinks, sockets/FIFOs, traversal, and unexpected hashes fail before execution.
    The caller must separately validate that this is the complete dependency closure.
    """
    if not root.is_absolute() or ".." in root.parts or not expected:
        raise ValueError("canonical shared root and nonempty hash manifest required")
    rootfd = os.open("/", os.O_RDONLY | os.O_DIRECTORY)
    try:
        for part in root.parts[1:]:
            nextfd = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=rootfd)
            os.close(rootfd)
            rootfd = nextfd
        verified = {}
        for relative, digest in expected.items():
            path = Path(relative)
            if (path.is_absolute() or ".." in path.parts or str(path) != relative
                    or not path.parts or len(digest) != 64
                    or any(c not in "0123456789abcdef" for c in digest)):
                raise ValueError("invalid manifest entry")
            fd = os.dup(rootfd)
            try:
                for part in path.parts[:-1]:
                    nextfd = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
                    os.close(fd)
                    fd = nextfd
                src = os.open(path.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=fd)
                with os.fdopen(src, "rb") as stream:
                    before = os.fstat(stream.fileno())
                    if not stat.S_ISREG(before.st_mode) or before.st_mode & 0o222:
                        raise ValueError("input is not an immutable regular file")
                    hasher = hashlib.sha256()
                    while chunk := stream.read(1024 * 1024):
                        hasher.update(chunk)
                    after = os.fstat(stream.fileno())
                    if (before.st_size, before.st_mtime_ns, before.st_ctime_ns) != (
                        after.st_size, after.st_mtime_ns, after.st_ctime_ns
                    ) or hasher.hexdigest() != digest:
                        raise ValueError("input hash changed or differs from manifest")
                    verified[relative] = digest
            finally:
                os.close(fd)
        return verified
    except (OSError, ValueError) as exc:
        raise SvwError(
            ErrorCode.BACKEND_UNAVAILABLE, "shared immutable input verification failed",
            {"reason": "remote_input_verification", "worker_started": False},
        ) from exc
    finally:
        os.close(rootfd)


# Preparation is deliberately separate from process execution, so a deployment cannot
# accidentally turn offline input validation into a submission or a sandbox bypass.
def _prepare_worker(
    request: BootstrapRequest, deployment: BootstrapDeployment,
    *, probe: Callable[[], HostCapability] | None = None,
) -> tuple[WrappedCommand, SandboxRunner]:
    """Compute-node validation and fixed no-network/no-degraded worker command.

    The deployment object is trusted immutable operator data, never loaded from the
    work request. A fresh remote capability is required; submit-host cache is not used.
    This does not execute a worker or qualify the scheduler's ownership contract.
    """
    import sys

    from svweave.adapters.slang.protocol import DTO_SCHEMA
    from svweave.adapters.slang.runner import worker_sandbox_spec
    from svweave.security.sandbox import SandboxRunner, probe_host

    if request.worker.schema_version != DTO_SCHEMA:
        raise SvwError(ErrorCode.BACKEND_UNAVAILABLE, "unsupported remote worker protocol")
    if request.worker.test_fault is not None:
        raise SvwError(ErrorCode.POLICY_DENIED, "remote production requests cannot inject test faults")
    root = Path(deployment.shared_root)
    snapshot = Path(request.worker.snapshot_root)
    if root not in snapshot.parents or ".." in snapshot.parts:
        raise SvwError(ErrorCode.PATH_NOT_ALLOWED, "remote snapshot is outside approved staging root")
    if not snapshot.is_dir() or snapshot.is_symlink():
        raise SvwError(ErrorCode.BACKEND_UNAVAILABLE, "remote snapshot directory is unavailable")
    for group in request.worker.group_files:
        path = Path(group.path)
        if root not in path.parents or ".." in path.parts:
            raise SvwError(ErrorCode.PATH_NOT_ALLOWED, "group file is outside approved staging root")
        if request.input_hashes.get(str(path.relative_to(root))) != group.sha256:
            raise SvwError(ErrorCode.BACKEND_UNAVAILABLE, "group hash differs from input hash manifest")
    for directory, dirs, files in os.walk(snapshot, followlinks=False):
        for name in dirs:
            if (Path(directory) / name).is_symlink():
                raise SvwError(ErrorCode.PATH_NOT_ALLOWED, "snapshot contains a symlink directory")
        for name in files:
            relative = str((Path(directory) / name).relative_to(root))
            if relative not in request.input_hashes:
                raise SvwError(ErrorCode.BACKEND_UNAVAILABLE, "snapshot file is absent from hash manifest")
    verify_immutable_files(root, request.input_hashes)
    # The runtime must be the interpreter actually executing the pinned bootstrap.
    if os.path.realpath(sys.executable) != deployment.python_path:
        raise SvwError(ErrorCode.BACKEND_UNAVAILABLE, "remote interpreter differs from pinned deployment")
    verify_immutable_files(Path(deployment.runtime_root), deployment.runtime_hashes)
    spec = worker_sandbox_spec(request.worker, deployment.python_path)
    import svweave

    inputs = {snapshot.resolve(), *(Path(g.path).resolve() for g in request.worker.group_files)}
    runtime_roots = {Path(sys.prefix).resolve(), Path(sys.base_prefix).resolve(),
                     Path(svweave.__file__).resolve().parent.parent}
    if any(source == runtime or source in runtime.parents or runtime in source.parents
           for source in inputs for runtime in runtime_roots):
        raise SvwError(ErrorCode.POLICY_DENIED, "input mounts overlap trusted runtime")
    _verify_runtime_closure(spec, root, deployment.runtime_tree_hashes, input_mounts=inputs)
    # No local _PROBE_CACHE evidence is accepted: force a new compute-node probe.
    capability = probe() if probe is not None else probe_host(deployment.bwrap_path, refresh=True)
    if not capability.available or capability.bwrap_path != deployment.bwrap_path:
        raise SvwError(ErrorCode.BACKEND_UNAVAILABLE, "compute-node bwrap verification failed")
    if spec.network or spec.rw_paths:
        raise SvwError(ErrorCode.POLICY_DENIED, "remote worker must have read-only mounts and no network")
    sandbox = SandboxRunner(capability=capability, allow_degraded=False)
    command = sandbox.wrap(spec, [deployment.python_path, "-I", "-m", "svweave.adapters.slang.worker"])
    return command, sandbox


from collections.abc import Callable  # noqa: E402

from pydantic import Field  # noqa: E402

from svweave.adapters.slang.protocol import WorkerRequest  # noqa: E402
from svweave.adapters.slang.runner import WorkerHandle, WorkerOutcome  # noqa: E402
from svweave.config.execution import ResolvedExecution  # noqa: E402
from svweave.execution.models import Digest, Ownership, StrictModel, Token  # noqa: E402
from svweave.security.sandbox import HostCapability, SandboxRunner, SandboxSpec, WrappedCommand  # noqa: E402


class BootstrapRequest(StrictModel):
    attempt_id: Token
    nonce: Token
    worker: WorkerRequest
    input_hashes: dict[str, Digest]
    memory_mib: int = Field(ge=64, le=1048576)
    execution_seconds: int = Field(ge=1, le=604800)


class BootstrapDeployment(StrictModel):
    shared_root: str
    runtime_root: str
    python_path: str
    bwrap_path: str
    runtime_hashes: dict[str, Digest]
    runtime_tree_hashes: dict[str, Digest] = Field(default_factory=dict)


class ControlRecord(StrictModel):
    protocol_version: str = Field(pattern=r"^svweave-slurm-1$")
    attempt_id: Token
    nonce: Token
    request_sha256: Digest
    ownership: Ownership
    bootstrap_sha256: Digest
    toolchain_sha256: Digest
    sandbox_verified: bool
    remote_ready: bool


def validate_control(
    control: ControlRecord, *, attempt_id: str, nonce: str, request_sha256: str,
    ownership: Ownership, bootstrap_sha256: str, toolchain_sha256: str,
) -> None:
    """Sidecar is correlation evidence only; ownership must come from a scheduler query."""
    if (control.attempt_id != attempt_id or control.nonce != nonce
            or control.request_sha256 != request_sha256 or control.ownership != ownership
            or control.bootstrap_sha256 != bootstrap_sha256
            or control.toolchain_sha256 != toolchain_sha256
            or not control.sandbox_verified or not control.remote_ready):
        raise SvwError(ErrorCode.BACKEND_UNAVAILABLE, "remote control record does not match verified request")


def prepare_worker(
    request: BootstrapRequest, deployment: BootstrapDeployment,
    *, probe: Callable[[], HostCapability] | None = None,
) -> WrappedCommand:
    """Offline/fresh-node preparation API; does not execute the prepared worker."""
    return _prepare_worker(request, deployment, probe=probe)[0]


def run_bootstrap(
    request: BootstrapRequest, deployment: BootstrapDeployment,
    *, execution: ResolvedExecution, handle: WorkerHandle | None = None,
    max_stdout_bytes: int | None = None,
    on_ready: Callable[[], None] | None = None,
) -> WorkerOutcome:
    """Remote lifecycle implementation behind the still-closed site authorization gate.

    Runs only the fixed worker with compute-host RLIMIT_AS/CORE, bounded stream drains,
    exact wall timeout and a private process group/PID namespace. The existing local
    runner's proven child lifecycle is reused *on the compute host*, not the gateway.
    Control publication and scheduler reconciliation remain the trusted site adapter's
    responsibilities; this helper never declares scheduler cleanup confirmed.
    """
    from svweave.adapters.slang.runner import run_worker

    execution.require_srun_authorized()
    site = execution.site_profile
    if site is None or (site.shared_root != deployment.shared_root
                        or os.path.realpath(site.python_path) != deployment.python_path):
        raise SvwError(ErrorCode.POLICY_DENIED, "bootstrap deployment differs from trusted site")
    runtime_root = Path(deployment.runtime_root)
    for executable in (deployment.python_path, deployment.bwrap_path, site.bootstrap_path):
        path = Path(executable)
        if runtime_root not in path.parents:
            raise SvwError(ErrorCode.POLICY_DENIED, "pinned executable is outside runtime inventory")
        expected_hash = site.executable_hashes.get(executable)
        if expected_hash is None or deployment.runtime_hashes.get(str(path.relative_to(runtime_root))) != expected_hash:
            raise SvwError(ErrorCode.POLICY_DENIED, "executable hash is not bound to trusted site policy")
    if request.memory_mib >= site.resources.memory_mib:
        raise SvwError(ErrorCode.POLICY_DENIED, "scheduler memory must leave bootstrap overhead")
    if request.execution_seconds >= site.resources.walltime_seconds:
        raise SvwError(ErrorCode.POLICY_DENIED, "scheduler walltime must leave bootstrap cleanup margin")
    _command, sandbox = _prepare_worker(request, deployment)
    if on_ready is not None:
        on_ready()
    outcome = run_worker(
        request.worker, timeout_s=request.execution_seconds,
        memory_mib=request.memory_mib, handle=handle, python=deployment.python_path,
        sandbox=sandbox, allow_degraded=False, max_stdout_bytes=max_stdout_bytes,
    )
    # These statistics were collected on the compute host; never label them as
    # the submission host's launcher usage or as scheduler accounting.
    if outcome.resource_usage.get("rusage_source") == "launcher":
        outcome.resource_usage["rusage_source"] = "remote_launcher"
    outcome.resource_usage.update({"execution_host": "compute", "scheduler_accounting": None})
    # Local runner's SIGKILL heuristic is not valid for scheduler termination.
    if outcome.signal == 9 and outcome.kind == "resource_limit":
        outcome.kind = "crashed"
        outcome.error_code = ErrorCode.BACKEND_UNAVAILABLE
        outcome.message = "remote worker received SIGKILL; scheduler OOM/cancel/node cause is UNKNOWN"
    return outcome


def runtime_tree_digest(root: Path) -> str:
    """Inventory a mounted runtime tree without following links (including link targets).

    This is an offline deployment helper. It is deliberately exhaustive rather than
    assuming a lockfile hash proves the interpreter, shared libraries or editable code.
    Metadata and content changes during hashing are rejected. Device/socket/FIFO assets
    are not runtime inputs. The digest is not a substitute for a read-only deployment.
    """
    from svweave.domain.ids import canonical_hash

    entries: list[dict[str, object]] = []
    def visit(path: Path, relative: str) -> None:
        before = path.lstat()
        entry: dict[str, object] = {
            "path": relative, "mode": before.st_mode, "uid": before.st_uid,
        }
        if stat.S_ISLNK(before.st_mode):
            entry.update({"kind": "symlink", "target": os.readlink(path)})
        elif stat.S_ISDIR(before.st_mode):
            entry["kind"] = "directory"
            for child in sorted(path.iterdir()):
                visit(child, f"{relative}/{child.name}")
        elif stat.S_ISREG(before.st_mode):
            fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
            hasher = hashlib.sha256()
            with os.fdopen(fd, "rb") as stream:
                if os.fstat(stream.fileno()).st_ino != before.st_ino:
                    raise ValueError("runtime asset replaced during inventory")
                while chunk := stream.read(1024 * 1024):
                    hasher.update(chunk)
            entry.update({"kind": "file", "size": before.st_size, "sha256": hasher.hexdigest()})
        else:
            raise ValueError("unsupported runtime asset type")
        after = path.lstat()
        if (before.st_ino, before.st_size, before.st_mtime_ns, before.st_ctime_ns) != (
            after.st_ino, after.st_size, after.st_mtime_ns, after.st_ctime_ns
        ):
            raise ValueError("runtime asset changed during inventory")
        entries.append(entry)
    visit(root, ".")
    return canonical_hash({"schema": "runtime-tree-1", "entries": entries})


def _verify_runtime_closure(
    spec: SandboxSpec, shared_root: Path, expected: dict[str, str],
    *, input_mounts: set[Path] | None = None,
) -> None:
    from svweave.security.sandbox import MINIMAL_ETC

    verified_inputs = input_mounts or set()
    roots = {Path("/usr"), *(Path(p).resolve() for p in spec.ro_paths
                             if Path(p).resolve() not in verified_inputs),
             *(Path(p).resolve() for p in MINIMAL_ETC if Path(p).exists())}
    # Nested mounts are already covered by their outer read-only runtime inventory.
    roots = {p for p in roots if not any(parent in roots for parent in p.parents)}
    if set(expected) != {str(p) for p in roots}:
        raise SvwError(ErrorCode.BACKEND_UNAVAILABLE, "runtime inventory does not cover exact sandbox mounts")
    try:
        for root in sorted(roots):
            if runtime_tree_digest(root) != expected[str(root)]:
                raise ValueError("runtime tree digest mismatch")
    except (OSError, ValueError) as exc:
        raise SvwError(ErrorCode.BACKEND_UNAVAILABLE, "runtime closure verification failed") from exc


def bootstrap_once(
    payload: bytes, deployment: BootstrapDeployment, *, execution: ResolvedExecution,
    ownership: Ownership, control_directory: Path, bootstrap_sha256: str,
    toolchain_sha256: str, max_request_bytes: int = 16 * 1024 * 1024,
    max_stdout_bytes: int = 256 * 1024 * 1024,
) -> WorkerOutcome:
    """One bounded request -> verified sidecar -> remote worker lifecycle.

    This is the reusable entrypoint API, not an enabled CLI. Trusted site deployment
    must supply policy/deployment/expected identities independently from the request.
    A scheduler query on the coordinator still has to corroborate the sidecar; matching
    SLURM variables and a nonce alone cannot establish cancellation authority.
    """
    from svweave.adapters.slang.runner import worker_sandbox_spec
    from svweave.execution.control import ControlStore
    from svweave.security.sandbox import mount_providing

    execution.require_srun_authorized()
    if max_request_bytes <= 0 or len(payload) > max_request_bytes:
        raise SvwError(ErrorCode.OUTPUT_LIMIT, "bootstrap request exceeds bounded input limit")
    request = BootstrapRequest.model_validate_json(payload)
    site = execution.site_profile
    if (site is None or ownership.cluster != site.cluster or ownership.uid != os.getuid()
            or ownership.attempt_id != request.attempt_id or ownership.nonce != request.nonce
            or os.environ.get("SLURM_JOB_ID") != ownership.job_id
            or os.environ.get("SLURM_STEP_ID") != ownership.step_id):
        raise SvwError(ErrorCode.POLICY_DENIED, "remote allocation identity does not match request")
    if ownership.allocation_owned and execution.selection.allocation_mode != "new_allocation":
        raise SvwError(ErrorCode.POLICY_DENIED, "borrowed allocation cannot be owned by bootstrap")
    if execution.selection.allocation_id is not None and ownership.job_id != execution.selection.allocation_id:
        raise SvwError(ErrorCode.POLICY_DENIED, "remote job differs from explicit borrowed allocation")
    control_path = control_directory.resolve(strict=True)
    spec = worker_sandbox_spec(request.worker, deployment.python_path)
    if mount_providing(spec, control_path) is not None:
        raise SvwError(ErrorCode.POLICY_DENIED, "control directory is visible inside backend sandbox")
    if site.executable_hashes.get(site.bootstrap_path) != bootstrap_sha256:
        raise SvwError(ErrorCode.POLICY_DENIED, "bootstrap hash differs from trusted site policy")
    control = ControlRecord(
        protocol_version="svweave-slurm-1", attempt_id=request.attempt_id, nonce=request.nonce,
        request_sha256=hashlib.sha256(payload).hexdigest(), ownership=ownership,
        bootstrap_sha256=bootstrap_sha256, toolchain_sha256=toolchain_sha256,
        sandbox_verified=True, remote_ready=True,
    )
    store = ControlStore(control_directory)
    return run_bootstrap(
        request, deployment, execution=execution, max_stdout_bytes=max_stdout_bytes,
        on_ready=lambda: store.publish(control),
    )
