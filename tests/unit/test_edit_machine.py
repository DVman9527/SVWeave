"""Transaction state machine (plan 12.3): the legal-transition table is enforced."""

from __future__ import annotations

import itertools
from pathlib import Path

import pytest

from svweave.domain.errors import ErrorCode, SvwError
from svweave.domain.states import TERMINAL_TRANSACTION_STATES
from svweave.domain.states import TransactionState as S
from svweave.editing.machine import LEGAL, check_transition
from svweave.editing.models import PrepareRequest
from svweave.editing.store import TxnStore, new_txn_id, valid_txn_id
from tests.unit._edit_fx import make_ws


def test_every_state_has_a_row_and_terminals_have_no_exit() -> None:
    assert set(LEGAL) == set(S)
    for t in (S.POST_APPLY_VERIFIED, S.ABORTED, S.EXPIRED, S.CONFLICT, S.VALIDATION_FAILED):
        assert LEGAL[t] == frozenset() and t in TERMINAL_TRANSACTION_STATES


def test_happy_path_is_exactly_the_plan_12_3_chain() -> None:
    chain = [S.PREPARED, S.CANDIDATE_READY, S.VALIDATING, S.VALIDATED, S.AWAITING_APPROVAL,
             S.APPROVED, S.COMMITTING, S.APPLIED, S.POST_APPLY_VERIFIED]
    for a, b in itertools.pairwise(chain):
        check_transition(a, b)


@pytest.mark.parametrize("frm", [S.PREPARED, S.CANDIDATE_READY, S.VALIDATING, S.VALIDATED,
                                 S.AWAITING_APPROVAL, S.APPROVED])
def test_any_unapplied_state_can_be_abandoned(frm: S) -> None:
    for to in (S.ABORTED, S.EXPIRED, S.CONFLICT, S.VALIDATION_FAILED):
        check_transition(frm, to)


def test_every_illegal_pair_is_an_internal_error() -> None:
    legal_pairs = {(a, b) for a, bs in LEGAL.items() for b in bs}
    bad = [(a, b) for a in S for b in S if (a, b) not in legal_pairs]
    assert len(bad) > 100
    for a, b in bad:
        with pytest.raises(SvwError) as ei:
            check_transition(a, b)
        assert ei.value.code is ErrorCode.INTERNAL


@pytest.mark.parametrize("frm,to", [
    (S.PREPARED, S.APPROVED),          # cannot skip validation
    (S.CANDIDATE_READY, S.COMMITTING),  # cannot commit an unvalidated candidate
    (S.AWAITING_APPROVAL, S.COMMITTING),  # cannot skip approval
    (S.COMMITTING, S.ABORTED),         # writes may exist: only recovery can close it
    (S.APPLIED, S.ABORTED),            # applied changes are never "aborted"
    (S.VALIDATION_FAILED, S.APPROVED),
    (S.ABORTED, S.CANDIDATE_READY),
])
def test_dangerous_shortcuts_are_illegal(frm: S, to: S) -> None:
    with pytest.raises(SvwError) as ei:
        check_transition(frm, to)
    assert ei.value.code is ErrorCode.INTERNAL


def test_store_refuses_illegal_transition_and_is_compare_and_set(tmp_path: Path) -> None:
    ws = make_ws(tmp_path)
    with ws.db() as db:
        st = TxnStore(db)
        tid = new_txn_id()
        assert valid_txn_id(tid) and not valid_txn_id("txn-../x") and not valid_txn_id("x")
        st.create(tid, base_snapshot_id=None, base_build_id="b", profile="p",  # type: ignore[arg-type]
                  request_key="k-12345678", payload_hash="h", record={})
        with pytest.raises(SvwError) as ei:
            st.update(tid, to=S.APPROVED)  # PREPARED -> APPROVED
        assert ei.value.code is ErrorCode.INTERNAL
        assert st.get(tid).state is S.PREPARED  # unchanged
        with pytest.raises(SvwError) as ei2:
            st.update(tid, expect=S.VALIDATED, to=S.AWAITING_APPROVAL)  # stale expectation
        assert ei2.value.code is ErrorCode.INTERNAL
        st.update(tid, to=S.CANDIDATE_READY)
        audit = st.get(tid).record["audit"]
        assert [a["to"] for a in audit] == ["PREPARED", "CANDIDATE_READY"]


def test_request_model_has_no_approval_field_and_rejects_unknown_fields() -> None:
    base = {"base_build_id": "a" * 64, "snapshot_id": "b" * 64, "request_key": "key-12345678",
            "edits": [{"kind": "exact_range", "logical_path": "x.sv", "start_byte": 0,
                       "end_byte": 0, "expected_sha256": "c" * 64, "replacement": ""}]}
    PrepareRequest.model_validate(base)
    for extra in ({"approved": True}, {"approval": "x"}, {"skip_validation": True}):
        with pytest.raises(ValueError):
            PrepareRequest.model_validate({**base, **extra})
    bad_edit = {**base, "edits": [{"kind": "symbol_replacement", "declaration_id": "x",
                                   "new_body": "y", "approved": True}]}
    with pytest.raises(ValueError):
        PrepareRequest.model_validate(bad_edit)
    with pytest.raises(ValueError):  # discriminator: unknown kind
        PrepareRequest.model_validate({**base, "edits": [{"kind": "rename", "from": "a", "to": "b"}]})
    with pytest.raises(ValueError):  # bad key
        PrepareRequest.model_validate({**base, "request_key": "x"})
