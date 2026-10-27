from __future__ import annotations

import re
import uuid

import pytest
from jsonschema import Draft202012Validator
from pydantic import ValidationError

from svweave.domain import ids
from svweave.domain.envelope import (
    AnalysisCoverage,
    Envelope,
    Outcome,
    error_envelope,
    new_request_id,
)
from svweave.domain.errors import (
    EXIT_BACKEND,
    EXIT_BUILD,
    EXIT_CONFIG,
    EXIT_OK,
    EXIT_POLICY,
    EXIT_STALE,
    ErrorCode,
    SvwError,
    exit_code_for,
)
from svweave.domain.states import (
    TERMINAL_TRANSACTION_STATES,
    BackendAvailability,
    Completeness,
    DiagnosticOutcome,
    EvidenceKind,
    Freshness,
    JobLifecycle,
    ResolutionStatus,
    TransactionState,
)


def test_states_exact_per_plan_9_5() -> None:
    assert [e.value for e in JobLifecycle] == [
        "QUEUED",
        "RUNNING",
        "SUCCEEDED",
        "FAILED",
        "CANCELLED",
        "TIMED_OUT",
    ]
    assert {e.value for e in Completeness} == {"COMPLETE", "PARTIAL", "BLOCKED"}
    assert {e.value for e in Freshness} == {"CURRENT", "STALE", "HISTORICAL", "UNKNOWN"}
    assert {e.value for e in DiagnosticOutcome} == {"NO_ERRORS", "HAS_ERRORS", "NOT_AVAILABLE"}
    assert {e.value for e in BackendAvailability} == {"READY", "DEGRADED", "UNAVAILABLE"}


def test_evidence_resolution_transaction_enums() -> None:
    assert {e.value for e in EvidenceKind} == {
        "SOURCE_CANDIDATE",
        "LSP_RESOLVED",
        "HDL_STATIC_RESOLVED",
        "RUNTIME_OBSERVED",
        "EXTERNAL_DECLARED",
        "UNRESOLVED",
    }
    assert {e.value for e in ResolutionStatus} == {
        "resolved_static",
        "candidate_source",
        "dynamic_unresolved",
        "unsupported",
        "inactive_for_profile",
    }
    assert TransactionState.COMMITTING.value == "COMMITTING"
    assert TransactionState.APPLIED not in TERMINAL_TRANSACTION_STATES
    assert TransactionState.RECOVERY_REQUIRED not in TERMINAL_TRANSACTION_STATES
    assert TransactionState.ABORTED in TERMINAL_TRANSACTION_STATES


def test_error_catalog_and_exit_codes_plan_14_6_15_4() -> None:
    catalog = {
        "INVALID_CONFIG",
        "MISSING_INPUT",
        "UNSUPPORTED_OPTION",
        "PATH_NOT_ALLOWED",
        "WORKSPACE_UNSTABLE",
        "STALE_SNAPSHOT",
        "AMBIGUOUS_SYMBOL",
        "PARTIAL_ANALYSIS",
        "BACKEND_UNAVAILABLE",
        "CAPABILITY_MISMATCH",
        "BUILD_TIMEOUT",
        "RESOURCE_LIMIT",
        "EDIT_CONFLICT",
        "APPROVAL_REQUIRED",
        "APPROVAL_EXPIRED",
        "POLICY_DENIED",
        "RECOVERY_REQUIRED",
        "OUTPUT_LIMIT",
    }
    assert catalog <= {e.value for e in ErrorCode}
    assert {e.value for e in ErrorCode} - catalog == {"IDEMPOTENCY_CONFLICT", "INTERNAL"}
    for code in ErrorCode:  # every code is mapped to a documented non-zero exit code
        assert exit_code_for(code) in {2, 3, 4, 5, 6}
    assert exit_code_for(ErrorCode.INVALID_CONFIG) == EXIT_CONFIG == 2
    assert exit_code_for(ErrorCode.MISSING_INPUT) == EXIT_BUILD == 3
    assert exit_code_for(ErrorCode.BUILD_TIMEOUT) == EXIT_BACKEND == 4
    assert exit_code_for(ErrorCode.STALE_SNAPSHOT) == EXIT_STALE == 5
    assert exit_code_for(ErrorCode.EDIT_CONFLICT) == 5
    assert exit_code_for(ErrorCode.POLICY_DENIED) == EXIT_POLICY == 6
    assert exit_code_for(ErrorCode.PATH_NOT_ALLOWED) == 6
    assert EXIT_OK == 0


def test_svw_error_carries_code_message_details() -> None:
    e = SvwError(ErrorCode.MISSING_INPUT, "nope", {"path": "x"})
    assert (e.code, e.message, e.details, e.exit_code) == (
        ErrorCode.MISSING_INPUT,
        "nope",
        {"path": "x"},
        3,
    )
    assert "MISSING_INPUT" in str(e)
    assert SvwError(ErrorCode.INVALID_CONFIG, "m").details == {}


def test_canonical_json_sorts_keys_but_never_arrays() -> None:
    a = ids.canonical_json({"b": [3, 1, 2], "a": {"y": 1, "x": 2}})
    assert a == '{"a":{"x":2,"y":1},"b":[3,1,2]}'
    assert ids.canonical_json({"a": 1, "b": 2}) == ids.canonical_json({"b": 2, "a": 1})
    assert ids.canonical_hash({"inc": ["a", "b"]}) != ids.canonical_hash({"inc": ["b", "a"]})
    assert ids.canonical_json({"s": "中文"}) == '{"s":"中文"}'
    with pytest.raises(ValueError):
        ids.canonical_json({"x": float("nan")})


def test_sha256_helpers() -> None:
    assert ids.sha256_hex("abc") == ids.sha256_hex(b"abc")
    assert ids.is_sha256_hex(ids.sha256_hex("x"))
    assert not ids.is_sha256_hex("XYZ")
    assert not ids.is_sha256_hex("A" * 64)


def test_SNP_04_build_identity_dimensions_not_confused() -> None:
    s, c, t = ids.sha256_hex("s"), ids.sha256_hex("c"), ids.sha256_hex("t")
    base = ids.compute_build_id(s, c, t)
    assert re.fullmatch(r"[0-9a-f]{64}", base)
    assert base == ids.compute_build_id(s, c, t)
    seen = {base}
    for args in (
        (ids.sha256_hex("s2"), c, t),
        (s, ids.sha256_hex("c2"), t),
        (s, c, ids.sha256_hex("t2")),
        (s, c, t, "2"),
    ):
        seen.add(ids.compute_build_id(*args))
    assert len(seen) == 5
    # swapping two components must not collide either
    assert ids.compute_build_id(c, s, t) != base
    old = {"snapshot_id": s, "config_id": c, "toolchain_id": t, "semantic_schema_version": "1"}
    assert ids.explain_build_change(old, {**old, "config_id": "x"}) == ["config_id"]
    assert ids.explain_build_change(old, {**old, "toolchain_id": "x"}) == ["toolchain_id"]
    assert ids.explain_build_change(old, dict(old)) == []
    assert ids.compute_toolchain_id(["a"]) != ids.compute_toolchain_id(["b"])


def test_job_id_is_uuid_and_distinct_from_build_id() -> None:
    j1, j2 = ids.new_job_id(), ids.new_job_id()
    assert j1 != j2
    assert str(uuid.UUID(j1)) == j1
    assert not ids.is_sha256_hex(j1)


def test_API_01_envelope_shape_and_strictness() -> None:
    env = Envelope(request_id="r", outcome=Outcome.OK)
    d = env.model_dump(mode="json")
    assert list(d) == [
        "schema_version",
        "request_id",
        "outcome",
        "workspace_id",
        "snapshot_id",
        "build_id",
        "profile",
        "freshness",
        "analysis_coverage",
        "provenance",
        "data",
        "pagination",
        "warnings",
        "error",
    ]
    assert d["schema_version"] == "1.1" and d["freshness"] == "UNKNOWN"
    assert d["pagination"] == {"limit": 100, "next_cursor": None, "truncated": False}
    with pytest.raises(ValidationError):
        Envelope.model_validate({"request_id": "r", "outcome": "OK", "surprise": 1})
    with pytest.raises(ValidationError):
        Envelope.model_validate({"request_id": "r", "outcome": "MAYBE"})
    assert new_request_id().startswith("req-")
    assert '"outcome": "OK"' in env.to_json()


def test_HDL_COV_01_output_version_requires_strict_schema_upgrade() -> None:
    # Frozen AnalysisCoverage shape from envelope 1.0 (e7684ad), not today's schema:
    # even reference_coverage=null was forbidden by its additionalProperties:false.
    old_schema = {
        "type": "object", "additionalProperties": False, "required": ["completeness"],
        "properties": {
            "completeness": {"enum": ["COMPLETE", "PARTIAL", "BLOCKED"]},
            "scope": {"type": ["string", "null"]},
            "basis": {"type": ["string", "null"]},
            "unresolved_count": {"type": "integer"},
            "limitations": {"type": "array", "items": {"type": "string"}},
        },
    }
    env = Envelope(request_id="r", outcome=Outcome.OK,
                   analysis_coverage=AnalysisCoverage(completeness=Completeness.COMPLETE))
    payload = env.model_dump(mode="json")
    assert payload["schema_version"] == "1.1"
    assert not Draft202012Validator(old_schema).is_valid(payload["analysis_coverage"])
    assert Draft202012Validator(Envelope.model_json_schema()).is_valid(payload)
    # New readers can still read historical 1.0 payloads; this is NOT the reverse guarantee.
    payload["schema_version"] = "1.0"
    del payload["analysis_coverage"]["reference_coverage"]
    assert Draft202012Validator(old_schema).is_valid(payload["analysis_coverage"])
    assert Envelope.model_validate(payload).schema_version == "1.0"


def test_error_envelope_blocked_vs_error() -> None:
    blocked = error_envelope(SvwError(ErrorCode.MISSING_INPUT, "m"), request_id="r", profile="p")
    assert blocked.outcome == Outcome.BLOCKED
    assert blocked.analysis_coverage is not None
    assert blocked.analysis_coverage.completeness == Completeness.BLOCKED
    err = error_envelope(SvwError(ErrorCode.INVALID_CONFIG, "m"), request_id="r")
    assert err.outcome == Outcome.ERROR and err.analysis_coverage is None
    assert err.error is not None and err.error.code == "INVALID_CONFIG"
