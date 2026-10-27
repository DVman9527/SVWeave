"""Hermetic Git configuration for tests, without replacing Git or repository policy."""

from __future__ import annotations

import shlex
import shutil
from pathlib import Path


def isolated_git_launcher(directory: Path) -> Path:
    real_git = shutil.which("git")
    if real_git is None:
        raise RuntimeError("real git is required for Git contract tests")
    real_git = str(Path(real_git).resolve())
    directory.mkdir()
    launcher = directory / "git"
    # Application subprocesses retain PATH but deliberately strip GIT_CONFIG_* overrides.
    # Apply isolation at the real Git process boundary; repository .git/config still applies.
    launcher.write_text(
        "#!/bin/sh\n"
        "export GIT_CONFIG_NOSYSTEM=1\n"
        "export GIT_CONFIG_GLOBAL=/dev/null\n"
        "unset GIT_CONFIG_COUNT GIT_CONFIG_PARAMETERS\n"
        f"exec {shlex.quote(real_git)} \"$@\"\n"
    )
    launcher.chmod(0o700)
    return launcher
