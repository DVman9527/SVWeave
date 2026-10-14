"""Semantic-core storage: migration v2 and read helpers (plan 13.2).

Migration v2 extends the M1 skeleton tables with the columns the semantic queries need (by
``ALTER TABLE ... ADD COLUMN``; the applied v1 migration is never edited) and adds the
tables that carry build-level facts: specializations, inactive regions, coverage gaps,
unsupported-node inventory and the per-build report.

Every semantic row is keyed by ``build_id`` (immutable) and is written in ONE staging
transaction by ``Database.stage_build`` (plan 9.6).
"""

from __future__ import annotations

import json
import sqlite3
from typing import Any

MIGRATION_V2 = """
ALTER TABLE declarations ADD COLUMN name TEXT;
ALTER TABLE declarations ADD COLUMN qualified_name TEXT;
ALTER TABLE declarations ADD COLUMN file TEXT;
ALTER TABLE declarations ADD COLUMN line INTEGER;
ALTER TABLE declarations ADD COLUMN col INTEGER;
ALTER TABLE declarations ADD COLUMN full_start INTEGER;
ALTER TABLE declarations ADD COLUMN full_end INTEGER;
ALTER TABLE declarations ADD COLUMN library TEXT;
ALTER TABLE declarations ADD COLUMN macro TEXT;
ALTER TABLE declarations ADD COLUMN attrs TEXT;
ALTER TABLE declarations ADD COLUMN loc TEXT;
ALTER TABLE declarations ADD COLUMN ord INTEGER;
CREATE INDEX idx_decl_name ON declarations(build_id, name);
CREATE INDEX idx_decl_qname ON declarations(build_id, qualified_name);
CREATE INDEX idx_decl_file ON declarations(build_id, file, span_start);
ALTER TABLE "references" ADD COLUMN file TEXT;
ALTER TABLE "references" ADD COLUMN line INTEGER;
ALTER TABLE "references" ADD COLUMN col INTEGER;
ALTER TABLE "references" ADD COLUMN loc TEXT;
ALTER TABLE "references" ADD COLUMN target_name TEXT;
ALTER TABLE "references" ADD COLUMN context TEXT;
ALTER TABLE "references" ADD COLUMN container_id TEXT;
ALTER TABLE "references" ADD COLUMN ord INTEGER;
CREATE INDEX idx_references_file ON "references"(build_id, file, span_start);
ALTER TABLE instances ADD COLUMN hier_path TEXT;
ALTER TABLE instances ADD COLUMN components TEXT;
ALTER TABLE instances ADD COLUMN parameters TEXT;
ALTER TABLE instances ADD COLUMN generate_context TEXT;
ALTER TABLE instances ADD COLUMN is_top INTEGER;
ALTER TABLE instances ADD COLUMN is_interface INTEGER;
ALTER TABLE instances ADD COLUMN is_stub INTEGER;
ALTER TABLE instances ADD COLUMN stub_name TEXT;
ALTER TABLE instances ADD COLUMN blackbox_candidate INTEGER;
ALTER TABLE instances ADD COLUMN definition_declaration_id TEXT;
ALTER TABLE instances ADD COLUMN loc TEXT;
ALTER TABLE instances ADD COLUMN bind TEXT;
ALTER TABLE instances ADD COLUMN ord INTEGER;
CREATE INDEX idx_instances_parent ON instances(build_id, parent_id);
ALTER TABLE port_bindings ADD COLUMN direction TEXT;
ALTER TABLE port_bindings ADD COLUMN style TEXT;
ALTER TABLE port_bindings ADD COLUMN port_kind TEXT;
ALTER TABLE port_bindings ADD COLUMN port_declaration_id TEXT;
ALTER TABLE port_bindings ADD COLUMN actual_loc TEXT;
ALTER TABLE port_bindings ADD COLUMN actual_kind TEXT;
ALTER TABLE port_bindings ADD COLUMN bit_width INTEGER;
ALTER TABLE port_bindings ADD COLUMN is_constant INTEGER;
ALTER TABLE port_bindings ADD COLUMN implicit_actual INTEGER;
ALTER TABLE port_bindings ADD COLUMN parts TEXT;
ALTER TABLE port_bindings ADD COLUMN interface_instance_id TEXT;
ALTER TABLE port_bindings ADD COLUMN modport TEXT;
ALTER TABLE port_bindings ADD COLUMN supported INTEGER;
ALTER TABLE port_bindings ADD COLUMN note TEXT;
ALTER TABLE port_bindings ADD COLUMN ord INTEGER;
ALTER TABLE assertions ADD COLUMN entry_type TEXT;
ALTER TABLE assertions ADD COLUMN name TEXT;
ALTER TABLE assertions ADD COLUMN instance_id TEXT;
ALTER TABLE assertions ADD COLUMN file TEXT;
ALTER TABLE assertions ADD COLUMN span_start INTEGER;
ALTER TABLE assertions ADD COLUMN span_end INTEGER;
ALTER TABLE assertions ADD COLUMN execution_status TEXT;
ALTER TABLE assertions ADD COLUMN ord INTEGER;
CREATE INDEX idx_assertions_scope ON assertions(build_id, entry_type, instance_id);
ALTER TABLE diagnostics ADD COLUMN is_error INTEGER;
ALTER TABLE diagnostics ADD COLUMN file TEXT;
ALTER TABLE diagnostics ADD COLUMN span_start INTEGER;
ALTER TABLE diagnostics ADD COLUMN span_end INTEGER;
CREATE INDEX idx_diagnostics_build ON diagnostics(build_id);
CREATE TABLE specializations (
    build_id TEXT NOT NULL REFERENCES builds(build_id),
    symbol_id TEXT NOT NULL,
    class_declaration_id TEXT,
    text TEXT NOT NULL,
    parameters TEXT,
    base_class TEXT,
    base_declaration_id TEXT,
    methods TEXT,
    PRIMARY KEY (build_id, symbol_id)
);
CREATE INDEX idx_spec_class ON specializations(build_id, class_declaration_id);
CREATE TABLE inactive_regions (
    build_id TEXT NOT NULL REFERENCES builds(build_id),
    ord INTEGER NOT NULL,
    kind TEXT NOT NULL,
    name TEXT,
    file TEXT,
    span_start INTEGER,
    span_end INTEGER,
    status TEXT NOT NULL,
    data TEXT,
    PRIMARY KEY (build_id, ord)
);
CREATE TABLE coverage_gaps (
    build_id TEXT NOT NULL REFERENCES builds(build_id),
    ord INTEGER NOT NULL,
    kind TEXT NOT NULL,
    name TEXT,
    detail TEXT,
    data TEXT,
    PRIMARY KEY (build_id, ord)
);
CREATE TABLE unsupported_nodes (
    build_id TEXT NOT NULL REFERENCES builds(build_id),
    ord INTEGER NOT NULL,
    kind TEXT NOT NULL,
    count INTEGER NOT NULL,
    sample TEXT,
    detail TEXT,
    in_scope INTEGER NOT NULL,
    PRIMARY KEY (build_id, ord)
);
CREATE TABLE build_reports (
    build_id TEXT PRIMARY KEY REFERENCES builds(build_id),
    job_id TEXT,
    extractor_version TEXT,
    pyslang_version TEXT,
    scope TEXT,
    tops TEXT,
    unresolved_count INTEGER NOT NULL DEFAULT 0,
    dynamic_count INTEGER NOT NULL DEFAULT 0,
    reasons TEXT,
    limitations TEXT,
    stats TEXT,
    dependency_report TEXT,
    source_map TEXT
);
"""


def dumps(obj: Any) -> str:
    return json.dumps(obj, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


def loads(text: str | None) -> Any:
    return None if text is None else json.loads(text)


def row_dict(row: sqlite3.Row, json_cols: tuple[str, ...] = ()) -> dict[str, Any]:
    d = dict(row)
    for c in json_cols:
        if c in d:
            d[c] = loads(d[c])
    return d
