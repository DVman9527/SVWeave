"""Capability comparison against the frozen M0 report (plan 7.4, test DEP-02) and the
probe-derived capability table (plan 12.1: never assumed from tool names).
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from svweave.adapters.mcp_client import ToolInfo
from svweave.domain.errors import ErrorCode, SvwError


class ToolStatus(StrEnum):
    MATCH = "match"
    CHANGED = "changed"  # present but schema hash differs from the pinned report
    MISSING = "missing"  # pinned tool absent from live tools/list
    NOT_PINNED = "not_pinned"  # live tool the report never saw: never exposed


#: logical feature -> upstream tools it needs. A feature is enabled only if EVERY tool matches.
CBM_FEATURES: dict[str, frozenset[str]] = {
    "index": frozenset({"index_repository", "index_status"}),
    "search": frozenset({"search_graph"}),
    "snippet": frozenset({"get_code_snippet"}),
    "schema": frozenset({"get_graph_schema"}),
    "coverage": frozenset({"check_index_coverage"}),
    "text_search": frozenset({"search_code"}),
    "list_projects": frozenset({"list_projects"}),
}
CBM_CORE_FEATURES = frozenset({"index", "search"})
#: The ONLY cbm tools the gateway ever calls (everything else, e.g. delete_project, stays hidden).
CBM_ALLOWLIST: frozenset[str] = frozenset().union(*CBM_FEATURES.values())

SERENA_FEATURES: dict[str, frozenset[str]] = {
    "find_symbol": frozenset({"find_symbol"}),
    "references": frozenset({"find_referencing_symbols"}),
    "overview": frozenset({"get_symbols_overview"}),
    "symbol_edit": frozenset({"find_symbol", "replace_symbol_body"}),
    "diagnostics": frozenset({"get_diagnostics_for_file"}),
}
SERENA_CORE_FEATURES = frozenset({"find_symbol"})
SERENA_ALLOWLIST: frozenset[str] = frozenset().union(*SERENA_FEATURES.values())
#: Must never appear in a Serena tools/list under the SVWeave context (shell, file create, ...).
SERENA_FORBIDDEN: frozenset[str] = frozenset(
    {
        "execute_shell_command",
        "create_text_file",
        "activate_project",
        "remove_project",
        "open_dashboard",
        "replace_content",
        "write_memory",
    }
)


@dataclass(frozen=True)
class CapabilityComparison:
    backend: str
    tools: dict[str, ToolStatus]
    expected: dict[str, str]
    actual: dict[str, str]
    features: dict[str, bool]
    core_ok: bool
    forbidden_present: tuple[str, ...] = ()
    reasons: tuple[str, ...] = field(default_factory=tuple)

    @property
    def ok(self) -> bool:
        return self.core_ok and all(self.features.values()) and not self.forbidden_present

    def mismatched(self) -> list[str]:
        return sorted(
            n for n, s in self.tools.items() if s in (ToolStatus.CHANGED, ToolStatus.MISSING)
        )

    def require(self, feature: str) -> None:
        """Raise CAPABILITY_MISMATCH if ``feature`` is disabled for this backend instance."""
        if not self.features.get(feature, False):
            raise SvwError(
                ErrorCode.CAPABILITY_MISMATCH,
                f"{self.backend}: feature {feature!r} disabled (tool schema differs from the pinned "
                "capability report or tool missing)",
                {
                    "backend": self.backend,
                    "feature": feature,
                    "mismatched_tools": self.mismatched(),
                    "reason": "capability_mismatch",
                },
            )


def compare_capabilities(
    backend: str,
    live: Mapping[str, ToolInfo],
    expected_hashes: Mapping[str, str],
    features: Mapping[str, frozenset[str]],
    core: frozenset[str],
    *,
    forbidden: frozenset[str] = frozenset(),
) -> CapabilityComparison:
    status: dict[str, ToolStatus] = {}
    needed = frozenset().union(*features.values())
    for name in sorted(needed):
        info = live.get(name)
        if info is None:
            status[name] = ToolStatus.MISSING
        elif expected_hashes.get(name) is None:
            status[name] = ToolStatus.NOT_PINNED
        elif info.schema_sha256 != expected_hashes[name]:
            status[name] = ToolStatus.CHANGED
        else:
            status[name] = ToolStatus.MATCH
    for name in sorted(set(live) - needed):
        status[name] = ToolStatus.NOT_PINNED if name not in expected_hashes else ToolStatus.MATCH
    enabled = {
        feat: all(status.get(t) is ToolStatus.MATCH for t in tools) for feat, tools in features.items()
    }
    reasons: list[str] = []
    for n, s in status.items():
        if n in needed and s in (ToolStatus.CHANGED, ToolStatus.MISSING, ToolStatus.NOT_PINNED):
            reasons.append(f"tool {n}: {s.value}")
    bad = tuple(sorted(set(live) & forbidden))
    if bad:
        reasons.append(f"forbidden tools exposed by backend: {', '.join(bad)}")
    return CapabilityComparison(
        backend=backend,
        tools=status,
        expected={k: v for k, v in expected_hashes.items() if k in needed},
        actual={k: v.schema_sha256 for k, v in live.items() if k in needed},
        features=enabled,
        core_ok=all(enabled.get(f, False) for f in core),
        forbidden_present=bad,
        reasons=tuple(reasons),
    )


@dataclass(frozen=True)
class SerenaEditSubset:
    """Which SV symbol kinds Serena may edit semantically. Derived from probe checks (ADR-012)."""

    function: bool
    task: bool
    source: str

    @property
    def kinds(self) -> frozenset[str]:
        out: set[str] = set()
        if self.function:
            out.add("Function")
        if self.task:
            out.add("Task")
        return frozenset(out)


def derive_serena_edit_subset(serena_report: Mapping[str, Any]) -> SerenaEditSubset:
    """Read the M0 probe's recorded fixture checks; anything not proven by a PASS is unqualified."""
    function = task = False
    for c in serena_report.get("checks", []):
        name = str(c.get("check", ""))
        if "(function method)" in name:
            function = bool(c.get("pass"))
        elif "(task method)" in name:
            task = bool(c.get("pass"))
    return SerenaEditSubset(function=function, task=task, source="capability_report.json checks")
