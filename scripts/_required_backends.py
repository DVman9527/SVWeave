"""Pure required-backend status gate, shared by probe and qualification."""

from __future__ import annotations


def required_backend_errors(summary: dict[str, str]) -> list[str]:
    """All mandatory backends must be present; slang/UVM probes must be READY.

CBM/Serena's declared subset can be DEGRADED, but qualification still checks the
exact failed checks and owner waiver. This availability gate is not qualification.
"""
    errors = []
    for name in ("pyslang", "slang_cli", "cbm", "serena"):
        allowed = {"READY"} if name in {"pyslang", "slang_cli"} else {"READY", "DEGRADED"}
        if summary.get(name) not in allowed:
            errors.append(f"{name} is {summary.get(name)}, need {' or '.join(sorted(allowed))}")
    return errors
