"""Persistence of edit transactions in ``edit_transactions`` (state is compare-and-set)."""

from __future__ import annotations

import json
import sqlite3
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from svweave.domain.errors import ErrorCode, SvwError
from svweave.domain.states import TransactionState as S
from svweave.editing.machine import BLOCKING_STATES, check_transition
from svweave.storage.db import Database

TXN_PREFIX = "txn-"


def now_iso() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def new_txn_id() -> str:
    return TXN_PREFIX + uuid.uuid4().hex[:20]


def valid_txn_id(txn_id: str) -> bool:
    body = txn_id[len(TXN_PREFIX) :]
    return txn_id.startswith(TXN_PREFIX) and len(body) == 20 and all(c in "0123456789abcdef" for c in body)


@dataclass
class Txn:
    transaction_id: str
    state: S
    base_snapshot_id: str | None
    candidate_snapshot_id: str | None
    patch_hash: str | None
    request_key: str | None
    payload_hash: str | None
    base_build_id: str | None
    profile: str | None
    record: dict[str, Any]
    created_at: str
    updated_at: str

    @classmethod
    def from_row(cls, r: sqlite3.Row) -> Txn:
        return cls(
            transaction_id=r["transaction_id"],
            state=S(r["state"]),
            base_snapshot_id=r["base_snapshot_id"],
            candidate_snapshot_id=r["candidate_snapshot_id"],
            patch_hash=r["patch_hash"],
            request_key=r["request_key"],
            payload_hash=r["payload_hash"],
            base_build_id=r["base_build_id"],
            profile=r["profile"],
            record=json.loads(r["record"] or "{}"),
            created_at=r["created_at"] or "",
            updated_at=r["updated_at"] or "",
        )


class TxnStore:
    def __init__(self, db: Database) -> None:
        self.db = db

    # ------------------------------------------------------------------ reads
    def get(self, txn_id: str) -> Txn:
        row = self.db.conn.execute(
            "SELECT * FROM edit_transactions WHERE transaction_id=?", (txn_id,)
        ).fetchone()
        if row is None:
            raise SvwError(
                ErrorCode.MISSING_INPUT,
                f"unknown edit transaction {txn_id}",
                {"reason": "transaction_missing", "transaction_id": txn_id},
            )
        return Txn.from_row(row)

    def by_request_key(self, key: str) -> Txn | None:
        row = self.db.conn.execute(
            "SELECT * FROM edit_transactions WHERE request_key=? AND state != 'ABORTED'", (key,)
        ).fetchone()
        return Txn.from_row(row) if row is not None else None

    def all_by_request_key(self, key: str) -> list[Txn]:
        """Every transaction that used this prepare key, ABORTED ones included (F7)."""
        rows = self.db.conn.execute(
            "SELECT * FROM edit_transactions WHERE request_key=?", (key,)
        ).fetchall()
        return [Txn.from_row(r) for r in rows]

    def commit_key_owner(self, key: str) -> list[Txn]:
        """Transactions whose commit used this request key (scoped per workspace, F7)."""
        rows = self.db.conn.execute(
            "SELECT * FROM edit_transactions WHERE record LIKE ?", (f'%"request_key": "{key}"%',)
        ).fetchall()
        out = [Txn.from_row(r) for r in rows]
        return [t for t in out if (t.record.get("commit") or {}).get("request_key") == key]

    def blocking(self) -> list[Txn]:
        marks = ",".join("?" for _ in BLOCKING_STATES)
        rows = self.db.conn.execute(
            f"SELECT * FROM edit_transactions WHERE state IN ({marks})",
            tuple(s.value for s in BLOCKING_STATES),
        ).fetchall()
        return [Txn.from_row(r) for r in rows]

    # ------------------------------------------------------------------ writes
    def create(
        self,
        txn_id: str,
        *,
        base_snapshot_id: str,
        base_build_id: str,
        profile: str,
        request_key: str,
        payload_hash: str,
        record: dict[str, Any],
    ) -> Txn:
        ts = now_iso()
        record = {**record, "audit": [{"ts": ts, "from": None, "to": S.PREPARED.value}]}
        with self.db.transaction() as c:
            c.execute(
                "INSERT INTO edit_transactions(transaction_id, base_snapshot_id, state, "
                "request_key, payload_hash, base_build_id, profile, record, created_at, "
                "updated_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
                (
                    txn_id,
                    base_snapshot_id,
                    S.PREPARED.value,
                    request_key,
                    payload_hash,
                    base_build_id,
                    profile,
                    json.dumps(record, sort_keys=True),
                    ts,
                    ts,
                ),
            )
        return self.get(txn_id)

    def update(
        self,
        txn_id: str,
        mutate: Callable[[dict[str, Any]], None] | None = None,
        *,
        expect: S | None = None,
        to: S | None = None,
        note: str | None = None,
        patch_hash: str | None = None,
        candidate_snapshot_id: str | None = None,
    ) -> Txn:
        """Compare-and-set update: optional state transition (legality enforced), record
        mutation and column updates in ONE database transaction."""
        with self.db.transaction() as c:
            row = c.execute(
                "SELECT * FROM edit_transactions WHERE transaction_id=?", (txn_id,)
            ).fetchone()
            if row is None:
                raise SvwError(ErrorCode.MISSING_INPUT, f"unknown edit transaction {txn_id}")
            cur = S(row["state"])
            if expect is not None and cur is not expect:
                raise SvwError(
                    ErrorCode.INTERNAL,
                    f"transaction {txn_id} is {cur.value}, expected {expect.value}",
                    {"state": cur.value, "expected": expect.value},
                )
            rec = json.loads(row["record"] or "{}")
            ts = now_iso()
            if to is not None:
                check_transition(cur, to)
                rec.setdefault("audit", []).append(
                    {"ts": ts, "from": cur.value, "to": to.value, **({"note": note} if note else {})}
                )
            if mutate is not None:
                mutate(rec)
            sets = ["record=?", "updated_at=?"]
            args: list[Any] = [json.dumps(rec, sort_keys=True), ts]
            if to is not None:
                sets.append("state=?")
                args.append(to.value)
            if patch_hash is not None:
                sets.append("patch_hash=?")
                args.append(patch_hash)
            if candidate_snapshot_id is not None:
                sets.append("candidate_snapshot_id=?")
                args.append(candidate_snapshot_id)
            c.execute(
                f"UPDATE edit_transactions SET {','.join(sets)} WHERE transaction_id=? AND state=?",
                (*args, txn_id, cur.value),
            )
        return self.get(txn_id)
