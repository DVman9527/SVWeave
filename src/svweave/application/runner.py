"""Run a use case and always return an Envelope (domain errors become error envelopes)."""

from __future__ import annotations

import sys
import traceback
from collections.abc import Callable

from svweave.domain.envelope import Envelope, ErrorInfo, Outcome, error_envelope, new_request_id
from svweave.domain.errors import EXIT_OK, ErrorCode, SvwError, exit_code_for


def execute(
    fn: Callable[[str], Envelope],
    *,
    request_id: str | None = None,
    workspace_id: str | None = None,
    profile: str | None = None,
) -> Envelope:
    rid = request_id or new_request_id()
    try:
        return fn(rid)
    except SvwError as e:
        return error_envelope(e, request_id=rid, workspace_id=workspace_id, profile=profile)
    except Exception as e:  # noqa: BLE001 - last-resort boundary: never leak a traceback on stdout
        traceback.print_exc(file=sys.stderr)
        return Envelope(
            request_id=rid,
            outcome=Outcome.ERROR,
            workspace_id=workspace_id,
            profile=profile,
            error=ErrorInfo(
                code=ErrorCode.INTERNAL.value,
                message=f"internal error ({type(e).__name__}); see stderr",
            ),
        )


def exit_code(env: Envelope) -> int:
    """CLI exit code for an envelope (plan 15.4). Stale snapshots map to 5 via the error code."""
    if env.error is None:
        return EXIT_OK
    return exit_code_for(ErrorCode(env.error.code))
