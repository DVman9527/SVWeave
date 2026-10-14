"""Migration v3: gateway job bookkeeping and the evidence store (plan 13.2, 14.3, 16.2).

Appended by ``ALTER TABLE ... ADD COLUMN``; migrations v1/v2 are never edited.
"""

from __future__ import annotations

MIGRATION_V3 = """
ALTER TABLE jobs ADD COLUMN kind TEXT;
ALTER TABLE jobs ADD COLUMN profile TEXT;
ALTER TABLE jobs ADD COLUMN request_id TEXT;
ALTER TABLE jobs ADD COLUMN owner_pid INTEGER;
ALTER TABLE jobs ADD COLUMN created_at TEXT;
ALTER TABLE jobs ADD COLUMN result TEXT;
ALTER TABLE jobs ADD COLUMN cancel_requested INTEGER NOT NULL DEFAULT 0;
ALTER TABLE evidence ADD COLUMN evidence_type TEXT;
ALTER TABLE evidence ADD COLUMN profile TEXT;
ALTER TABLE evidence ADD COLUMN test_name TEXT;
ALTER TABLE evidence ADD COLUMN seed TEXT;
ALTER TABLE evidence ADD COLUMN run_id TEXT;
ALTER TABLE evidence ADD COLUMN tool_version TEXT;
ALTER TABLE evidence ADD COLUMN link_state TEXT;
ALTER TABLE evidence ADD COLUMN link_reason TEXT;
ALTER TABLE evidence ADD COLUMN claimed_snapshot_id TEXT;
ALTER TABLE evidence ADD COLUMN claimed_build_id TEXT;
ALTER TABLE evidence ADD COLUMN record TEXT;
ALTER TABLE evidence ADD COLUMN imported_at TEXT;
CREATE INDEX idx_evidence_build ON evidence(build_id);
CREATE INDEX idx_jobs_active ON jobs(lifecycle)
"""
