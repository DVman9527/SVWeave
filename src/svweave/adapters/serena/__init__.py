"""Serena adapter: candidate-bound symbol navigation and the qualified function-edit subset."""

from svweave.adapters.serena.adapter import (
    ReferenceRecord,
    SerenaAdapter,
    SymbolEditPlan,
    SymbolRecord,
)
from svweave.adapters.serena.guard import (
    CandidateGuard,
    ChangeSet,
    EditOutcome,
    apply_exact_range,
)

__all__ = [
    "CandidateGuard",
    "ChangeSet",
    "EditOutcome",
    "ReferenceRecord",
    "SerenaAdapter",
    "SymbolEditPlan",
    "SymbolRecord",
    "apply_exact_range",
]
