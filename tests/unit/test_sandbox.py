"""Unit tests for the bwrap command builder, validation and the explicit degraded mode."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from svweave.domain.errors import ErrorCode, SvwError
from svweave.security.sandbox import (
    HostCapability,
    IsolationMode,
    SandboxRunner,
    SandboxSpec,
    build_bwrap_argv,
    probe_host,
    validate_spec,
)

UNUSABLE = HostCapability(False, "/usr/bin/bwrap", "bubblewrap 0.4.0", "setting up uid map: Permission denied")
USABLE = HostCapability(True, "/usr/bin/bwrap", "bubblewrap 0.4.0", None)


def argv_for(spec: SandboxSpec, inner: list[str] | None = None) -> list[str]:
    return build_bwrap_argv(spec, inner or ["/bin/true"], bwrap="/usr/bin/bwrap")


def pairs(argv: list[str], flag: str) -> list[tuple[str, str]]:
    return [(argv[i + 1], argv[i + 2]) for i, a in enumerate(argv) if a == flag]


def test_isolation_flags_and_no_network_by_default(tmp_path: Path) -> None:
    a = argv_for(SandboxSpec())
    for f in ("--die-with-parent", "--new-session", "--unshare-user", "--unshare-pid", "--unshare-ipc",
              "--unshare-uts", "--unshare-net"):
        assert f in a
    assert a[0] == "/usr/bin/bwrap" and a[a.index("--") + 1 :][:2] == ["/usr/bin/env", "-i"]
    assert "--unshare-net" not in argv_for(SandboxSpec(network=True))


def test_argv_is_a_list_of_strings_never_a_shell_string(tmp_path: Path) -> None:
    evil = tmp_path / "a b; rm -rf $(whoami)`id`"
    evil.mkdir()
    a = argv_for(SandboxSpec(rw_paths=(evil,)), ["/bin/echo", "x; y", "$(z)"])
    assert all(isinstance(x, str) for x in a)
    assert str(evil) in a  # passed as one argv element, untouched
    assert a[-2:] == ["x; y", "$(z)"]


def test_only_declared_paths_are_writable(tmp_path: Path) -> None:
    ro, rw, prot = tmp_path / "snap", tmp_path / "cand", tmp_path / "orig"
    for p in (ro, rw, prot):
        p.mkdir()
    a = argv_for(SandboxSpec(ro_paths=(ro,), rw_paths=(rw,), protected_paths=(prot,)))
    writable = {d for _, d in pairs(a, "--bind")}
    assert writable == {str(rw)}
    assert str(ro) in {d for _, d in pairs(a, "--ro-bind")}
    # the original source is simply not mounted
    assert str(prot) not in a
    assert ("/usr", "/usr") in pairs(a, "--ro-bind")
    assert pairs(a, "--bind")[0][0] == str(rw)


def test_home_and_etc_are_not_exposed_wholesale() -> None:
    a = argv_for(SandboxSpec())
    assert ("/etc", "/etc") not in pairs(a, "--ro-bind")
    assert str(Path.home()) not in a
    assert ("/etc", "/etc") in pairs(argv_for(SandboxSpec(etc="full")), "--ro-bind")


def test_nested_binds_are_ordered_shallow_to_deep(tmp_path: Path) -> None:
    tool = tmp_path / "tool"
    state = tool / "state"
    state.mkdir(parents=True)
    a = argv_for(SandboxSpec(ro_paths=(tool,), rw_paths=(state,)))
    i_ro = [i for i, x in enumerate(a) if x == str(tool)][0]
    i_rw = [i for i, x in enumerate(a) if x == str(state)][0]
    assert i_ro < i_rw


def test_env_is_cleared_and_explicit() -> None:
    a = argv_for(SandboxSpec(env={"HOME": "/h", "A": "b"}), ["/bin/x"])
    tail = a[a.index("--") + 1 :]
    assert tail == ["/usr/bin/env", "-i", "HOME=/h", "A=b", "/bin/x"]


def test_tmp_dirs_must_be_under_tmp() -> None:
    assert "--dir" in argv_for(SandboxSpec(tmp_dirs=("/tmp/cbm-rt",)))
    for bad in ("/etc/x", "/tmp/../etc", "relative"):
        with pytest.raises(SvwError):
            argv_for(SandboxSpec(tmp_dirs=(bad,)))


@pytest.mark.parametrize("bad", ["/", "/usr", "/etc", "/proc", str(Path.home()), str(Path.home().parent)])
def test_dangerous_rw_binds_refused(bad: str) -> None:
    with pytest.raises(SvwError) as ei:
        validate_spec(SandboxSpec(rw_paths=(Path(bad),)))
    assert ei.value.code is ErrorCode.POLICY_DENIED


def test_relative_and_conflicting_paths_refused(tmp_path: Path) -> None:
    with pytest.raises(SvwError) as ei:
        validate_spec(SandboxSpec(ro_paths=(Path("rel/path"),)))
    assert ei.value.code is ErrorCode.PATH_NOT_ALLOWED
    with pytest.raises(SvwError):
        validate_spec(SandboxSpec(ro_paths=(tmp_path,), rw_paths=(tmp_path,)))
    with pytest.raises(SvwError):
        validate_spec(SandboxSpec(ro_paths=(Path.home(),)))


def test_protected_original_cannot_be_exposed_writable(tmp_path: Path) -> None:
    orig = tmp_path / "orig"
    (orig / "sub").mkdir(parents=True)
    for rw in (orig, orig / "sub", tmp_path):
        with pytest.raises(SvwError) as ei:
            validate_spec(SandboxSpec(rw_paths=(rw,), protected_paths=(orig,)))
        assert ei.value.code is ErrorCode.POLICY_DENIED
    # a read-only mount of the original is allowed (explicit choice)
    validate_spec(SandboxSpec(ro_paths=(orig,), protected_paths=(orig,)))


def test_symlinked_rw_path_into_protected_is_caught(tmp_path: Path) -> None:
    orig = tmp_path / "orig"
    orig.mkdir()
    link = tmp_path / "innocent"
    os.symlink(orig, link)
    with pytest.raises(SvwError):
        validate_spec(SandboxSpec(rw_paths=(link,), protected_paths=(orig,)))


def test_runner_uses_bwrap_when_available() -> None:
    r = SandboxRunner(capability=USABLE)
    assert r.mode is IsolationMode.BWRAP
    w = r.wrap(SandboxSpec(), ["/bin/true"])
    assert w.isolation is IsolationMode.BWRAP and w.warnings == () and w.argv[0] == "/usr/bin/bwrap"


def test_unusable_bwrap_is_never_silent(tmp_path: Path) -> None:
    r = SandboxRunner(allow_degraded=False, capability=UNUSABLE)
    assert r.mode is IsolationMode.DEGRADED_NO_ISOLATION
    with pytest.raises(SvwError) as ei:
        r.wrap(SandboxSpec(), ["/bin/true"])
    assert ei.value.code is ErrorCode.BACKEND_UNAVAILABLE
    assert ei.value.details["isolation"] == "UNAVAILABLE" and "uid map" in ei.value.details["reason"]

    d = SandboxRunner(allow_degraded=True, capability=UNUSABLE)
    w = d.wrap(SandboxSpec(env={"A": "1"}), ["/bin/true"])
    assert w.isolation is IsolationMode.DEGRADED_NO_ISOLATION
    assert w.warnings and "DEGRADED ISOLATION" in w.warnings[0]
    assert "bwrap" not in w.argv[0] and w.argv[:3] == ["/usr/bin/env", "-i", "A=1"]


def test_degraded_mode_still_enforces_write_surface_rules(tmp_path: Path) -> None:
    d = SandboxRunner(allow_degraded=True, capability=UNUSABLE)
    with pytest.raises(SvwError):
        d.wrap(SandboxSpec(rw_paths=(Path("/usr"),)), ["/bin/true"])


def test_probe_host_reports_missing_bwrap_with_reason() -> None:
    cap = probe_host("/nonexistent/bwrap", refresh=True)
    assert not cap.available and cap.reason == "bwrap executable not found"
