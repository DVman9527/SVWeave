"""Versioned SQLite migrations. Never edit an applied migration; append a new one."""

from __future__ import annotations

import sqlite3

from svweave.domain.errors import ErrorCode, SvwError
from svweave.storage.edit_schema import MIGRATION_V4
from svweave.storage.gateway_schema import MIGRATION_V3
from svweave.storage.semantic import MIGRATION_V2

MIGRATIONS: list[tuple[int, str]] = [
    (
        1,
        """
CREATE TABLE workspaces (
    workspace_id TEXT PRIMARY KEY,
    root_mapping TEXT NOT NULL,           -- JSON: logical root id -> description (no secrets)
    policy_hash TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE snapshots (
    snapshot_id TEXT PRIMARY KEY,
    workspace_id TEXT NOT NULL REFERENCES workspaces(workspace_id),
    profile TEXT NOT NULL,
    config_id TEXT NOT NULL,
    inventory_hash TEXT NOT NULL,
    capture_metadata TEXT NOT NULL,       -- JSON
    retention_refs TEXT NOT NULL DEFAULT '[]',
    gc_state TEXT NOT NULL DEFAULT 'PRESENT' CHECK (gc_state IN ('PRESENT','DELETED')),
    created_at TEXT NOT NULL,
    seq INTEGER NOT NULL                  -- monotonic capture order
);
CREATE INDEX idx_snapshots_ws ON snapshots(workspace_id, seq);
CREATE TABLE source_files (
    snapshot_id TEXT NOT NULL REFERENCES snapshots(snapshot_id),
    logical_path TEXT NOT NULL,
    bytes_hash TEXT NOT NULL,
    size INTEGER NOT NULL,
    origin TEXT NOT NULL,                 -- original path
    role TEXT NOT NULL,
    ord INTEGER NOT NULL,
    symlink_target TEXT,
    PRIMARY KEY (snapshot_id, logical_path)
);
CREATE TABLE builds (
    build_id TEXT PRIMARY KEY,
    workspace_id TEXT NOT NULL REFERENCES workspaces(workspace_id),
    profile TEXT NOT NULL,
    snapshot_id TEXT NOT NULL REFERENCES snapshots(snapshot_id),
    config_id TEXT NOT NULL,
    toolchain_id TEXT NOT NULL,
    extractor_id TEXT,
    semantic_schema_version TEXT NOT NULL,
    completeness TEXT NOT NULL CHECK (completeness IN ('COMPLETE','PARTIAL','BLOCKED')),
    diagnostic_outcome TEXT NOT NULL
        CHECK (diagnostic_outcome IN ('NO_ERRORS','HAS_ERRORS','NOT_AVAILABLE')),
    created_at TEXT NOT NULL
);
CREATE INDEX idx_builds_snapshot ON builds(snapshot_id);
CREATE TABLE jobs (
    job_id TEXT PRIMARY KEY,
    build_id TEXT,                        -- deliberately not a FK: failed jobs outlive staging
    snapshot_id TEXT,
    lifecycle TEXT NOT NULL
        CHECK (lifecycle IN ('QUEUED','RUNNING','SUCCEEDED','FAILED','CANCELLED','TIMED_OUT')),
    started_at TEXT,
    finished_at TEXT,
    exit_code INTEGER,
    error TEXT
);
CREATE INDEX idx_jobs_build ON jobs(build_id);
CREATE TABLE declarations (
    declaration_id TEXT NOT NULL,
    build_id TEXT NOT NULL REFERENCES builds(build_id),
    symbol_id TEXT,
    scope TEXT NOT NULL,
    kind TEXT NOT NULL,
    source_id TEXT,
    span_start INTEGER,
    span_end INTEGER,
    type TEXT,
    compilation_unit TEXT,
    PRIMARY KEY (build_id, declaration_id)
);
CREATE TABLE "references" (
    reference_id TEXT NOT NULL,
    build_id TEXT NOT NULL REFERENCES builds(build_id),
    source_id TEXT,
    span_start INTEGER,
    span_end INTEGER,
    target_id TEXT,
    kind TEXT NOT NULL,
    resolution_status TEXT NOT NULL CHECK (resolution_status IN
        ('resolved_static','candidate_source','dynamic_unresolved','unsupported',
         'inactive_for_profile')),
    unresolved_reason TEXT,
    PRIMARY KEY (build_id, reference_id)
);
CREATE INDEX idx_references_target ON "references"(build_id, target_id);
CREATE TABLE instances (
    instance_id TEXT NOT NULL,
    build_id TEXT NOT NULL REFERENCES builds(build_id),
    parent_id TEXT,
    path TEXT NOT NULL,
    definition TEXT,
    specialization TEXT,
    PRIMARY KEY (build_id, instance_id)
);
CREATE INDEX idx_instances_path ON instances(build_id, path);
CREATE TABLE port_bindings (
    build_id TEXT NOT NULL REFERENCES builds(build_id),
    instance_id TEXT NOT NULL,
    formal_port TEXT NOT NULL,
    actual_expression TEXT,
    referenced_symbols TEXT,
    types TEXT,
    PRIMARY KEY (build_id, instance_id, formal_port)
);
CREATE TABLE assertions (
    assertion_id TEXT NOT NULL,
    build_id TEXT NOT NULL REFERENCES builds(build_id),
    scope TEXT,
    kind TEXT NOT NULL,
    metadata TEXT,
    PRIMARY KEY (build_id, assertion_id)
);
CREATE TABLE diagnostics (
    diagnostic_id INTEGER PRIMARY KEY AUTOINCREMENT,
    build_id TEXT,
    job_id TEXT,
    severity TEXT NOT NULL,
    code TEXT,
    locations TEXT,
    message TEXT NOT NULL,
    suppression TEXT
);
CREATE TABLE backend_indexes (
    index_id INTEGER PRIMARY KEY AUTOINCREMENT,
    snapshot_id TEXT NOT NULL REFERENCES snapshots(snapshot_id),
    backend TEXT NOT NULL,
    backend_version TEXT,
    upstream_project TEXT,
    index_state TEXT NOT NULL,
    schema_hash TEXT
);
CREATE TABLE crosswalks (
    crosswalk_id INTEGER PRIMARY KEY AUTOINCREMENT,
    snapshot_id TEXT REFERENCES snapshots(snapshot_id),
    upstream_node_id TEXT NOT NULL,
    candidates TEXT,
    match_method TEXT,
    ambiguity TEXT
);
CREATE TABLE edit_transactions (
    transaction_id TEXT PRIMARY KEY,
    base_snapshot_id TEXT REFERENCES snapshots(snapshot_id),
    candidate_snapshot_id TEXT REFERENCES snapshots(snapshot_id),
    patch_hash TEXT,
    state TEXT NOT NULL CHECK (state IN
        ('PREPARED','CANDIDATE_READY','VALIDATING','VALIDATED','AWAITING_APPROVAL','APPROVED',
         'COMMITTING','APPLIED','RECOVERY_REQUIRED','POST_APPLY_VERIFIED','ABORTED','EXPIRED',
         'CONFLICT','VALIDATION_FAILED')),
    approval_id TEXT,
    journal_location TEXT
);
CREATE TABLE evidence (
    evidence_id TEXT PRIMARY KEY,         -- content hash of the immutable record
    kind TEXT NOT NULL CHECK (kind IN
        ('SOURCE_CANDIDATE','LSP_RESOLVED','HDL_STATIC_RESOLVED','RUNTIME_OBSERVED',
         'EXTERNAL_DECLARED','UNRESOLVED')),
    producer TEXT NOT NULL,
    build_id TEXT,
    snapshot_id TEXT REFERENCES snapshots(snapshot_id),
    run_identity TEXT,
    artifacts TEXT,
    trust_class TEXT,
    outcome TEXT
);
CREATE TABLE published_pointer (
    workspace_id TEXT NOT NULL REFERENCES workspaces(workspace_id),
    profile TEXT NOT NULL,
    build_id TEXT NOT NULL REFERENCES builds(build_id),
    snapshot_id TEXT NOT NULL REFERENCES snapshots(snapshot_id),
    published_at TEXT NOT NULL,
    PRIMARY KEY (workspace_id, profile)
);
""",
    ),
    (2, MIGRATION_V2),
    (3, MIGRATION_V3),
    (4, MIGRATION_V4),
    (5, """
CREATE TABLE execution_attempt_refs (
    attempt_id TEXT PRIMARY KEY,
    job_id TEXT NOT NULL,
    snapshot_id TEXT NOT NULL,
    execution_fingerprint TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TRIGGER execution_attempt_refs_no_replace BEFORE INSERT ON execution_attempt_refs WHEN EXISTS (SELECT 1 FROM execution_attempt_refs WHERE attempt_id=NEW.attempt_id) BEGIN SELECT RAISE(ABORT, 'execution attempt references cannot be replaced'); END;
CREATE TRIGGER execution_attempt_refs_no_update BEFORE UPDATE ON execution_attempt_refs BEGIN SELECT RAISE(ABORT, 'execution attempt references are immutable'); END;
CREATE TRIGGER execution_attempt_refs_no_delete BEFORE DELETE ON execution_attempt_refs BEGIN SELECT RAISE(ABORT, 'execution attempt references must be retained'); END;
"""),
]

LATEST_VERSION = MIGRATIONS[-1][0]


def current_version(conn: sqlite3.Connection) -> int:
    return int(conn.execute("PRAGMA user_version").fetchone()[0])


def migrate(conn: sqlite3.Connection) -> int:
    """Apply pending migrations, each in its own transaction. Returns the resulting version."""
    version = current_version(conn)
    if version > LATEST_VERSION:
        raise SvwError(
            ErrorCode.CAPABILITY_MISMATCH,
            f"database schema v{version} is newer than this build (v{LATEST_VERSION})",
            {"db_version": version, "supported": LATEST_VERSION},
        )
    for ver, sql in MIGRATIONS:
        if ver <= version:
            continue
        conn.execute("BEGIN IMMEDIATE")
        try:
            # another process may have applied this migration while we waited for the lock
            if current_version(conn) >= ver:
                conn.execute("COMMIT")
                continue
            for stmt in _split(sql):
                conn.execute(stmt)
            conn.execute(f"PRAGMA user_version = {int(ver)}")
            conn.execute("COMMIT")
        except BaseException:
            conn.execute("ROLLBACK")
            raise
    return current_version(conn)


def _split(sql: str) -> list[str]:
    # Statements are separated by ';' at end of line; no semicolons appear inside literals.
    out = []
    for chunk in sql.split(";\n"):
        stmt = chunk.strip().rstrip(";")
        if stmt:
            out.append(stmt)
    return out
