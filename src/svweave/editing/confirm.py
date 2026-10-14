"""Human confirmation for ``svweave edit approve`` (plan 12.4).

Qualified path: an interactive TTY on stdin; the diff is shown, the human types the first 12
characters of the patch hash. Test-harness path: the environment variable
``SVWEAVE_TEST_CONFIRM_TOKEN`` supplies the typed value. That path is NOT qualified (a process
that can set environment variables is not a human at a terminal); approvals made through it are
recorded with ``method = "test_token"`` and flagged in the result.
"""

from __future__ import annotations

import os
import sys
from typing import Any

from svweave.application.edit import Confirmation
from svweave.domain.errors import ErrorCode, SvwError
from svweave.editing.diffing import visible

TEST_TOKEN_ENV = "SVWEAVE_TEST_CONFIRM_TOKEN"


def tty_confirm(info: dict[str, Any]) -> Confirmation:
    if not (os.isatty(0) and sys.stdin.isatty()):
        raise SvwError(
            ErrorCode.APPROVAL_REQUIRED,
            "approval needs an interactive terminal on stdin (a human must type the confirmation)",
            {"reason": "no_tty"},
        )
    err = sys.stderr
    # A1: nothing taken from the transaction reaches the terminal raw
    err.write(f"\n=== edit transaction {visible(str(info['transaction_id']))} ===\n")
    profiles = visible(", ".join(info["profiles"]))
    paths = visible(", ".join(info["edit_paths"]))
    err.write(f"profiles: {profiles}   edit paths: {paths}\n")
    for rs in info.get("resolved_symbols", []):
        err.write(visible(
            f"resolved by name: {rs['qualified_name']} ({rs['kind']}"
            f"{', constructor' if rs.get('constructor') else ''}) "
            f"{rs['file']} bytes [{rs['start_byte']}, {rs['end_byte']})") + "\n")
    err.write(visible(info["diff"]) + "\n")
    err.write("NOT RUN: EDA compile, simulation, formal. Static slang check only.\n")
    err.write(f"patch hash: {visible(str(info['patch_hash']))}\n")
    err.write("Type the first 12 characters of the patch hash to approve: ")
    err.flush()
    typed = sys.stdin.readline().strip()
    return Confirmation(typed, "tty")


def env_token_confirm(_info: dict[str, Any]) -> Confirmation:
    tok = os.environ.get(TEST_TOKEN_ENV)
    if not tok:
        raise SvwError(
            ErrorCode.APPROVAL_REQUIRED,
            f"no interactive terminal and {TEST_TOKEN_ENV} is not set",
            {"reason": "no_tty"},
        )
    return Confirmation(tok, "test_token")


def choose_confirmer() -> Any:
    """TTY when stdin is a terminal, else the (non-qualified) environment token."""
    return tty_confirm if os.isatty(0) else env_token_confirm
