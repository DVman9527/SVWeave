"""Workspace secret used to sign pagination cursors (plan 14.2).

Stored in the trusted per-workspace directory (``config/trusted.py``), never inside the repository,
so repository content cannot read or forge cursors. The key is >= 32 random bytes, mode 0600, in a
0700 directory; a weak or foreign file is refused instead of being used.
"""

from __future__ import annotations

import os
import secrets
import stat

from svweave.config.context import WorkspaceContext
from svweave.domain.errors import ErrorCode, SvwError

KEY_FILENAME = "cursor.key"
KEY_BYTES = 32


def _read_checked(p: os.PathLike[str]) -> bytes:
    st = os.lstat(p)
    if not stat.S_ISREG(st.st_mode) or st.st_uid != os.geteuid():
        raise SvwError(
            ErrorCode.POLICY_DENIED,
            "cursor.key is not a regular file owned by the current user",
            {"reason": "cursor_key_untrusted"},
        )
    if st.st_mode & 0o077:
        os.chmod(p, 0o600)  # owned by us: tighten rather than trust an open mode
    key = open(p, "rb").read()  # noqa: SIM115
    if len(key) < KEY_BYTES:
        raise SvwError(
            ErrorCode.POLICY_DENIED,
            f"cursor.key is shorter than {KEY_BYTES} bytes; delete it to generate a new one",
            {"reason": "cursor_key_weak"},
        )
    return key


def cursor_key(ctx: WorkspaceContext) -> bytes:
    d = ctx.trusted_dir
    p = d / KEY_FILENAME
    if p.exists() or p.is_symlink():
        return _read_checked(p)
    d.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(d, 0o700)
    key = secrets.token_bytes(KEY_BYTES)
    tmp = d / f".{KEY_FILENAME}.{os.getpid()}.{secrets.token_hex(4)}"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "wb") as f:
        f.write(key)
    try:
        os.link(tmp, p)  # atomic publish; loses gracefully to a concurrent creator
    except FileExistsError:
        return _read_checked(p)
    finally:
        os.unlink(tmp)
    return key
