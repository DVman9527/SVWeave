"""Read-only Python runtime mounts, shared by native probes and the Serena adapter."""
from __future__ import annotations

import os
import re
from pathlib import Path

from svweave.security.sandbox import SandboxSpec, mount_providing


def python_prefix_to_bind(venv: Path) -> Path | None:
    """The interpreter prefix a venv python symlinks into, if outside the default system view."""
    real = Path(os.path.realpath(venv / "bin" / "python"))
    if str(real).startswith(("/usr/bin/", "/usr/lib/", "/usr/lib64/")):
        return None
    return real.parents[1] if len(real.parents) >= 2 else None


def python_binds(venv: Path) -> list[Path]:
    """Narrow read-only binds for the venv interpreter: the executable, its stdlib directory and
    libpython, instead of the whole prefix (``/usr/local`` holds unrelated software).
    Falls back to the prefix when the layout is not the expected one."""
    real = Path(os.path.realpath(venv / "bin" / "python"))
    if str(real).startswith(("/usr/bin/", "/usr/lib/", "/usr/lib64/")) or len(real.parents) < 2:
        return []
    prefix = real.parents[1]
    m = re.match(r"python(\d+\.\d+)", real.name)
    stdlib = prefix / "lib" / f"python{m.group(1)}" if m else None
    if stdlib is None or not stdlib.is_dir():
        return [prefix]
    return [real, stdlib, *sorted((prefix / "lib").glob("libpython*"))]


def python_links(venv: Path, mounts: SandboxSpec | None = None) -> list[tuple[str, Path]]:
    """The symlinks between the venv's ``bin/python`` and the real interpreter, as
    ``(target, link path)``: the shebang of every venv script goes through them, and
    :func:`python_binds` binds only the final file (a python-build-standalone runtime has
    ``bin/python3 -> python3.12``; without it Serena dies with "/usr/bin/env: ...: No such file").

    With ``mounts`` (the spec the links are for), a link the sandbox already shows is left out: one
    inside a bind such as the venv itself (``python -m venv``: ``bin/python -> python3``), under
    ``/usr`` or in the ``/etc`` view (``/usr/bin/python3 -> /etc/alternatives/python3``). Recreating
    it would land on the host, and the spec would be refused."""
    links: list[tuple[str, Path]] = []
    cur = venv / "bin" / "python"
    for _ in range(40):  # the kernel's own symlink-chain limit
        if not cur.is_symlink():
            break
        target = os.readlink(cur)
        nxt = Path(target) if os.path.isabs(target) else cur.parent / target
        link = Path(os.path.normpath(cur))
        if cur != venv / "bin" / "python" and (mounts is None or mount_providing(mounts, link) is None):
            links.append((target, link))
        cur = nxt
    return links
