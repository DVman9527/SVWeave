"""Migration v4: controlled-edit transaction bookkeeping (plan 12.3, 13.2, M4).

Appended by ``ALTER TABLE ... ADD COLUMN`` on the v1 ``edit_transactions`` table; applied
migrations are never edited. ``record`` is the JSON transaction record (edits, plan, profiles,
validation result, commit result, audit trail); the columns next to it are what must be queried
or constrained: state, idempotency key and the base build.
"""

from __future__ import annotations

MIGRATION_V4 = """
ALTER TABLE edit_transactions ADD COLUMN request_key TEXT;
ALTER TABLE edit_transactions ADD COLUMN payload_hash TEXT;
ALTER TABLE edit_transactions ADD COLUMN base_build_id TEXT;
ALTER TABLE edit_transactions ADD COLUMN profile TEXT;
ALTER TABLE edit_transactions ADD COLUMN record TEXT;
ALTER TABLE edit_transactions ADD COLUMN created_at TEXT;
ALTER TABLE edit_transactions ADD COLUMN updated_at TEXT;
CREATE UNIQUE INDEX idx_edit_request_key ON edit_transactions(request_key)
    WHERE request_key IS NOT NULL AND state != 'ABORTED';
CREATE INDEX idx_edit_state ON edit_transactions(state)
"""
