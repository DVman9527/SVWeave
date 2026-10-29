"""Inert, user-level MCP registration previews. Never read/write client configuration."""

from __future__ import annotations

import json
import os
import shlex
import sys
from pathlib import Path
from typing import Any

from svweave.config.discovery import PROJECT_ENV
from svweave.domain.errors import ErrorCode, SvwError
from svweave.orchestration.toolchain import ENV_ROOT, Toolchain


def _safe_argument(value: str) -> str:
    if any(ord(c) < 32 or ord(c) == 127 for c in value):
        raise SvwError(ErrorCode.INVALID_CONFIG, "registration arguments must not contain controls")
    return value


def registration_preview(
    client: str,
    *,
    executable: Path | None = None,
    toolchain_root: Path | None = None,
    execution_provider: str | None = None,
    execution_profile: str | None = None,
) -> dict[str, Any]:
    """Return argv and safely quoted config using a stable name and no fixed project root.

    The executable is never searched on PATH or relative to the current repository. This is a
    configuration preview, not a native/backend probe and not proof a client preserves cwd.
    """
    if client not in {"claude", "codex"}:
        raise SvwError(ErrorCode.INVALID_CONFIG, "unsupported MCP client")
    # Do NOT resolve sys.executable: venv/bin/python is usually a symlink to the host interpreter.
    launcher = executable or (Path(sys.executable).parent / "svweave-mcp")
    if not launcher.is_absolute():
        raise SvwError(ErrorCode.INVALID_CONFIG, "launcher executable must be an absolute path")
    _safe_argument(str(launcher))
    if not launcher.is_file() or not os.access(launcher, os.X_OK):
        raise SvwError(ErrorCode.MISSING_INPUT, "shared svweave-mcp executable is missing/not executable")
    # Resolving the script itself is safe; it does not execute code or select a workspace.
    launcher = launcher.resolve()
    if toolchain_root is not None and not toolchain_root.is_absolute():
        raise SvwError(ErrorCode.INVALID_CONFIG, "toolchain root must be an absolute path")
    env_root = os.environ.get(ENV_ROOT)
    if toolchain_root is None and env_root is not None and (
        not env_root or not Path(env_root).is_absolute()
    ):
        raise SvwError(ErrorCode.INVALID_CONFIG, f"{ENV_ROOT} must be absolute for shared registration")
    toolchain = Toolchain.discover(toolchain_root)
    args = ["--toolchain-root", str(toolchain.root)]
    if execution_provider is not None:
        if execution_provider not in {"local", "srun"}:
            raise SvwError(ErrorCode.INVALID_CONFIG, "invalid execution provider")
        args.extend(["--execution-provider", execution_provider])
    if execution_profile is not None:
        args.extend(["--execution-profile", execution_profile])
    command = _safe_argument(str(launcher))
    args = [_safe_argument(arg) for arg in args]
    if client == "claude":
        argv = ["claude", "mcp", "add", "--transport", "stdio", "--scope", "user", "svweave",
                "--", command, *args]
        config: str = json.dumps(
            {"mcpServers": {"svweave": {"type": "stdio", "command": command, "args": args}}},
            ensure_ascii=False, indent=2,
        )
    else:
        argv = ["codex", "mcp", "add", "svweave", "--", command, *args]
        # JSON strings/arrays are a valid subset of TOML basic strings/arrays here (controls
        # were rejected). Forward a caller-supplied override, do not freeze its current value.
        config = "\n".join([
            "[mcp_servers.svweave]",
            f"command = {json.dumps(command, ensure_ascii=False)}",
            f"args = {json.dumps(args, ensure_ascii=False)}",
            f"env_vars = {json.dumps([PROJECT_ENV])}",
        ]) + "\n"
    return {
        "client": client,
        "scope": "user",
        "server_name": "svweave",
        "command": command,
        "args": args,
        "registration_argv": argv,
        "registration_shell": shlex.join(argv),
        "client_config": config,
        "applied": False,
        "validation": "configuration_only; native backends and client connection not probed",
        "notes": [
            "Review before applying. This command never changes client configuration.",
            "No project root is baked in; each process selects and binds one authorized workspace.",
            "Client cwd is not guaranteed. Use a caller-supplied SVWEAVE_PROJECT_ROOT if needed.",
            *([
                "Codex CLI add does not set env_vars; the TOML preview includes forwarding for "
                "SVWEAVE_PROJECT_ROOT. Retain that line when applying the preview.",
            ] if client == "codex" else []),
        ],
    }
