"""Synthetic source-derived Slurm22.05.9 field examples; NOT captured scheduler output.

No scheduler, subprocess, socket, ownership observation or cleanup action is involved.
"""
from __future__ import annotations

import json

import pytest
from pydantic import ValidationError

from svweave.execution.accounting import AccountingScope, decode_accounting

SCOPE = AccountingScope(cluster="cluster1", job_id="123.4")


def payload(**fields: object) -> bytes:
    row: dict[str, object] = {
        "contract": "svweave-sacct-fields-v1", "slurm_version": "22.05.9",
        "source": "sacct_step_fields", "scope": SCOPE.model_dump(), "job_id_raw": "123.4",
        "state": "RUNNING", "exit_code": "0:0",
    }
    row.update(fields)
    return json.dumps(row).encode()


@pytest.mark.parametrize("state,category", [
    ("PENDING", "pending"), ("RUNNING", "active"), ("SUSPENDED", "active"),
    ("COMPLETED", "completion_reported"), ("CANCELLED", "failure_reported"),
    ("FAILED", "failure_reported"), ("TIMEOUT", "failure_reported"),
    ("NODE_FAIL", "failure_reported"), ("PREEMPTED", "failure_reported"),
    ("BOOT_FAIL", "failure_reported"), ("DEADLINE", "failure_reported"),
    ("OUT_OF_MEMORY", "failure_reported"), ("COMPLETING", "transition"),
    ("STAGE_OUT", "transition"), ("CONFIGURING", "transition"), ("RESIZING", "transition"),
    ("REQUEUED", "transition"), ("REQUEUE_FED", "transition"), ("REQUEUE_HOLD", "transition"),
    ("SPECIAL_EXIT", "transition"), ("STOPPED", "transition"), ("REVOKED", "transition"),
    ("RESV_DEL_HOLD", "transition"), ("SIGNALING", "transition"),
])
def test_SRUN_12_all_version_pinned_states_are_diagnostic_only(state: str, category: str) -> None:
    evidence = decode_accounting(payload(state=state), expected_scope=SCOPE)
    assert evidence.state.normalized == state and evidence.state.category == category
    assert evidence.state.status == "PARSED"
    assert evidence.remote_cleanup == "UNKNOWN" and evidence.worker_success == "NOT_ESTABLISHED"
    assert evidence.evidence_kind == "EXTERNAL_DECLARED" and evidence.interpretation == "diagnostic_only"
    assert not hasattr(evidence, "ownership") and not hasattr(evidence, "terminal")


@pytest.mark.parametrize("state", ["?", "FUTURE_STATE", "completed", "CD", "FAILED\n", "RUNNING\x00", "x" * 129, "COMPLETED by 2", "CANCELLED by user", "CANCELLED by 4294967295", "CANCELLED by 001"])
def test_SRUN_12_unrecognized_or_malformed_states_remain_unknown(state: str) -> None:
    evidence = decode_accounting(payload(state=state), expected_scope=SCOPE)
    assert evidence.state.status == "UNKNOWN" and evidence.state.normalized is None
    assert evidence.parse_status == "INCOMPLETE"
    assert evidence.exit_code.status == "PARSED"  # independent field evidence retained
    assert "\n" not in (evidence.state.display or "") and "\x00" not in (evidence.state.display or "")


@pytest.mark.parametrize("state", ["CANCELLED+", "OUT_OF_ME+", "COMPLETED+", "CANCELLED by 10+"])
def test_SRUN_12_truncated_known_prefix_does_not_become_known_state(state: str) -> None:
    evidence = decode_accounting(payload(state=state), expected_scope=SCOPE)
    assert evidence.state.status == "TRUNCATED" and evidence.state.category == "unknown"
    assert evidence.state.normalized is None


@pytest.mark.parametrize("state,normalized,uid", [("CANCELLED by 0", "CANCELLED", 0), ("CANCELLED by 42", "CANCELLED", 42), ("COMPLETING by 42", "COMPLETING", 42)])
def test_SRUN_12_cancel_uid_is_attribution_not_ownership(state: str, normalized: str, uid: int) -> None:
    evidence = decode_accounting(payload(state=state), expected_scope=SCOPE)
    assert evidence.state.normalized == normalized and evidence.state.cancellation_uid_reported == uid
    assert evidence.remote_cleanup == "UNKNOWN"
    if normalized == "COMPLETING":
        assert evidence.state.category == "transition"


@pytest.mark.parametrize("value,status,signal", [("0:0", 0, 0), ("127:0", 127, 0), ("0:9", 0, 9), ("0:15", 0, 15), ("0:126", 0, 126), (" 1:0 ", 1, 0)])
def test_SRUN_12_printed_exit_components_are_not_wait_status(value: str, status: int, signal: int) -> None:
    evidence = decode_accounting(payload(exit_code=value), expected_scope=SCOPE)
    assert evidence.exit_code.status == "PARSED"
    assert (evidence.exit_code.printed_exit_status, evidence.exit_code.printed_signal) == (status, signal)
    assert evidence.worker_success == "NOT_ESTABLISHED"


@pytest.mark.parametrize("value", ["137:0", "128:0", "256:0", "0:128", "0:127", "1:9", "-1:0", "00:0", "0", "1:0:0", "0:0\n", "0:0+", "x" * 40])
def test_SRUN_12_malformed_exit_does_not_override_state_or_default_zero(value: str) -> None:
    evidence = decode_accounting(payload(state="COMPLETED", exit_code=value), expected_scope=SCOPE)
    assert evidence.exit_code.status == "INVALID" and evidence.exit_code.printed_exit_status is None
    assert evidence.state.normalized == "COMPLETED" and evidence.parse_status == "INCOMPLETE"
    assert evidence.worker_success == "NOT_ESTABLISHED"


def test_SRUN_12_kill_signal_is_not_oom_and_completed_is_not_success() -> None:
    failed = decode_accounting(payload(state="FAILED", exit_code="0:9"), expected_scope=SCOPE)
    oom = decode_accounting(payload(state="OUT_OF_MEMORY", exit_code=None), expected_scope=SCOPE)
    completed = decode_accounting(payload(state="COMPLETED", exit_code="0:0"), expected_scope=SCOPE)
    assert failed.state.reason == "scheduler_reported_failed"
    assert oom.state.reason == "scheduler_reported_oom" and oom.exit_code.printed_exit_status is None
    assert completed.worker_success == "NOT_ESTABLISHED" and completed.remote_cleanup == "UNKNOWN"
    assert any("SIGKILL" in text for text in failed.limitations)


@pytest.mark.parametrize("value", [None, "", "   "])
def test_SRUN_12_missing_fields_are_not_zero(value: str | None) -> None:
    evidence = decode_accounting(payload(state=value, exit_code=value), expected_scope=SCOPE)
    assert evidence.state.status == "MISSING" and evidence.exit_code.status == "MISSING"
    assert evidence.exit_code.printed_exit_status is None and evidence.exit_code.printed_signal is None
    assert evidence.derived_exit_code.printed_exit_status is None


@pytest.mark.parametrize("raw", [None, b""])
def test_SRUN_12_unavailable_accounting_is_not_absence_proof(raw: bytes | None) -> None:
    evidence = decode_accounting(raw, expected_scope=SCOPE)
    assert not evidence.accounting_available and evidence.recorded_scope is None
    assert evidence.parse_status == "INCOMPLETE" and evidence.remote_cleanup == "UNKNOWN"


def test_SRUN_12_derived_exit_has_distinct_unfolded_contract() -> None:
    scope = AccountingScope(cluster="cluster1", job_id="123")
    evidence = decode_accounting(payload(source="sacct_job_fields", scope=scope.model_dump(), job_id_raw="123",
                                         exit_code="9:0", derived_exit_code="137:0"), expected_scope=scope)
    assert evidence.exit_code.printed_exit_status == 9
    assert evidence.derived_exit_code.printed_exit_status == 137
    assert evidence.worker_success == "NOT_ESTABLISHED"
    with pytest.raises(ValueError):
        decode_accounting(payload(derived_exit_code="0:0"), expected_scope=SCOPE)


@pytest.mark.parametrize("identifier", ["123_4", "123_[1-2]", "123+1", "123.batch", "123.extern", "123.interactive", "123.4294967290", "123.4294967280", "123.4+1", "123-124", "0123", "0", "67108864", "123\n", "123."])
def test_SRUN_12_unsupported_id_shapes_are_rejected(identifier: str) -> None:
    with pytest.raises(ValidationError):
        AccountingScope(cluster="cluster1", job_id=identifier)


def test_SRUN_12_numeric_raw_id_does_not_launder_array_or_het_identity() -> None:
    for full in ("123_4", "123+1"):
        with pytest.raises(ValueError):
            decode_accounting(payload(source="sacct_job_fields", scope={"cluster": "cluster1", "job_id": full}, job_id_raw="123"), expected_scope=SCOPE)
    with pytest.raises(ValueError):
        decode_accounting(payload(job_id_raw="123.5"), expected_scope=SCOPE)
    with pytest.raises(ValueError):
        decode_accounting(payload(), expected_scope=AccountingScope(cluster="other", job_id="123.4"))
    with pytest.raises(ValueError):
        decode_accounting(payload(), expected_scope=AccountingScope(cluster="cluster1", job_id="123"))


@pytest.mark.parametrize("override", [{"slurm_version": "23.02"}, {"source": "scontrol"}, {"contract": "v2"}, {"ownership": {}}, {"state": True}, {"exit_code": 0}, {"source": "sacct_job_fields"}])
def test_SRUN_12_version_source_and_shape_are_strict(override: dict[str, object]) -> None:
    with pytest.raises(ValueError):
        decode_accounting(payload(**override), expected_scope=SCOPE)


@pytest.mark.parametrize("raw", [b"x" * 8193, b"banner\n{}", b"\xff", b"[]", b"{\"state\":\"RUNNING\",\"state\":\"COMPLETED\"}", b"{\"state\": NaN}", b"[" * 4096 + b"]" * 4096])
def test_SRUN_13_bounded_serialization_rejects_flood_banner_duplicates_and_bad_json(raw: bytes) -> None:
    with pytest.raises(ValueError):
        decode_accounting(raw, expected_scope=SCOPE)
