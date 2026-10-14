"""Apply the planned edits INSIDE the candidate directory (plan 12.2).

* ``function`` declarations (the qualified Serena subset, ADR-012) go through Serena's
  ``replace_symbol_body`` when the Serena capability is present, and the result is labelled
  ``serena_symbol``;
* everything else (tasks, constructors, explicit ranges, or no Serena) is a gateway exact-range
  patch labelled ``exact_range`` and NEVER counted as a Serena semantic edit;
* ``slang_symbol`` edits (ADR-013) are located by name by the planner and applied here as the same
  exact-range patch, labelled ``slang_symbol`` and NEVER counted as a Serena semantic edit;
* ambiguity is refused by the adapter (``AMBIGUOUS_SYMBOL``); this module never picks one.
"""

from __future__ import annotations

from collections.abc import Callable
from contextlib import AbstractContextManager
from pathlib import Path
from typing import Any, Protocol

from svweave.adapters.serena.guard import EditOutcome, apply_exact_range
from svweave.domain.errors import ErrorCode, SvwError
from svweave.domain.ids import sha256_hex
from svweave.editing.plan import ResolvedEdit, denied

SERENA_KIND = "function"


class SymbolEditor(Protocol):
    """The slice of the Serena adapter the edit loop needs (fakes implement it in unit tests)."""

    def plan_symbol_edit(self, candidate: Path, name_path: str, relative_path: str) -> Any: ...

    def replace_symbol_body(
        self,
        candidate: Path,
        name_path: str,
        relative_path: str,
        new_body: str,
        *,
        expected_body_sha256: str | None = None,
    ) -> EditOutcome: ...


SerenaFactory = Callable[[Path], AbstractContextManager[SymbolEditor]]


def name_paths(qualified_name: str) -> list[str]:
    """Serena name paths for ``pkg::cls::fn``: full path first, then shorter suffixes (at least
    two components when available, so a bare function name is never tried)."""
    parts = qualified_name.split("::")
    out = ["/".join(parts[i:]) for i in range(len(parts))]
    return [p for p in out if "/" in p] or out[:1]




def apply_edits(
    candidate: Path, edits: list[ResolvedEdit], serena_factory: SerenaFactory | None
) -> None:
    """Mutates ``edits`` (edit_path, reason, serena summary) and the candidate directory."""
    if serena_factory is not None:
        cands = [e for e in edits if e.kind == "symbol_replacement" and e.decl_kind == SERENA_KIND]
        if cands:
            _plan_serena(candidate, cands, serena_factory)
    elif any(e.kind == "symbol_replacement" and e.decl_kind == SERENA_KIND for e in edits):
        for e in edits:
            if e.kind == "symbol_replacement":
                e.reason = "Serena capability unavailable: exact-range patch on the declaration span"
    for e in edits:
        if e.kind == "symbol_replacement" and not e.reason:
            e.reason = (
                f"declaration kind {e.decl_kind!r} is outside the qualified Serena edit subset "
                "(ADR-012): exact-range patch on the declaration span"
            )
    # Serena edits and gateway edits must not share a file: Serena's symbol cache and the
    # exact-range offsets (base coordinates) would disagree.
    serena_files = {e.logical_path for e in edits if e.edit_path == "serena_symbol"}
    for e in edits:
        if e.edit_path != "serena_symbol" and e.logical_path in serena_files:
            raise denied(
                "mixed_edit_paths_same_file",
                "a Serena symbol edit and an exact-range edit target the same file; split them "
                "into separate transactions",
                path=e.logical_path,
            )
    # exact-range edits: descending offsets within a file keep base coordinates valid
    for e in sorted(
        (x for x in edits if x.edit_path in ("exact_range", "slang_symbol")),
        key=lambda x: (x.logical_path, -x.start),
    ):
        out = apply_exact_range(candidate, e.rel, e.start, e.end, e.expected_sha256, e.replacement)
        out.raise_if_surprising()


def _plan_serena(
    candidate: Path,
    cands: list[ResolvedEdit],
    factory: SerenaFactory,
) -> None:
    with factory(candidate) as ad:
        planned: list[tuple[ResolvedEdit, str, Any]] = []
        for e in cands:  # plan everything against the PRISTINE candidate first
            assert e.qualified_name is not None
            last_reason = ""
            hit: tuple[str, Any] | None = None
            for np in name_paths(e.qualified_name):
                plan = ad.plan_symbol_edit(candidate, np, e.rel)  # AMBIGUOUS_SYMBOL propagates
                if plan.edit_path == "serena_symbol":
                    hit = (np, plan)
                    break
                last_reason = plan.reason
            if hit is None:
                e.reason = (
                    f"Serena could not locate the symbol ({last_reason}); exact-range patch on "
                    "the declaration span"
                )
                continue
            np, plan = hit
            sym = plan.symbol
            # Serena's symbol must CONTAIN the chosen declaration (it may also cover leading
            # qualifiers such as ``virtual`` that slang's declaration span excludes). Anything
            # else is a different symbol.
            if (
                sym is None
                or sym.byte_start is None
                or sym.byte_end is None
                or not (sym.byte_start <= e.start and e.end <= sym.byte_end)
            ):
                raise SvwError(
                    ErrorCode.AMBIGUOUS_SYMBOL,
                    "Serena's symbol does not contain the declaration chosen in the semantic "
                    "database (different source span); refusing",
                    {
                        "reason": "serena_span_mismatch",
                        "name_path": np,
                        "declaration_span": [e.start, e.end],
                        "serena_span": [getattr(sym, "byte_start", None), getattr(sym, "byte_end", None)],
                    },
                )
            data = (candidate / e.rel).read_bytes()
            e.notes.append(
                "qualifiers outside the declaration span are preserved: "
                f"{data[sym.byte_start:e.start]!r} / {data[e.end:sym.byte_end]!r}"
            )
            e.serena_pre = data[sym.byte_start : e.start]
            e.serena_post = data[e.end : sym.byte_end]
            planned.append((e, np, plan))
        for e, np, plan in planned:
            sym = plan.symbol
            out = ad.replace_symbol_body(
                candidate,
                np,
                e.rel,
                (e.serena_pre + e.replacement + e.serena_post).decode("utf-8"),
                expected_body_sha256=sha256_hex((sym.body or "").encode()),
            )
            out.raise_if_surprising()
            e.edit_path = "serena_symbol"
            e.reason = "unique SystemVerilog function replaced by Serena replace_symbol_body"
            e.serena = {
                "name_path": np,
                "kind": sym.kind,
                "serena_span": [sym.byte_start, sym.byte_end],
                "old_span": list(out.old_span) if out.old_span else None,
                "new_span": list(out.new_span) if out.new_span else None,
                "within_target": out.within_target,
            }
