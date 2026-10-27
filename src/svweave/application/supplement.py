"""External static inventories, isolated from authoritative semantic and edit data.

Plan 13.3/16.1: artifact integrity is NOT source/build identity or runtime evidence.
No source path mentioned by the producer is opened, and no backend is started.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from svweave.adapters.verilator.importer import parse_tree
from svweave.application.options import WorkspaceOptions
from svweave.config.context import WorkspaceContext
from svweave.domain.cursor import MAX_RESPONSE_BYTES
from svweave.domain.envelope import AnalysisCoverage, Envelope, Outcome, Pagination, Provenance
from svweave.domain.errors import ErrorCode as EC
from svweave.domain.errors import SvwError
from svweave.domain.ids import is_sha256_hex
from svweave.domain.states import Completeness, EvidenceKind
from svweave.security import safeio

TREE_LIMIT = 16 << 20
META_LIMIT = 2 << 20
RECORD_LIMIT = 32 << 20
STORE = ".svweave/supplements/verilator"
LIMITATIONS = [
    "External Verilator static inventory only; source/snapshot/build association is UNVERIFIED.",
    "Source filenames and locations are producer declarations, not verified original-source spans.",
    "Not authoritative slang references; excluded from rename and other edit decisions.",
    "No simulation, runtime factory/config_db resolution, formal or coverage validation performed.",
    "PARTIAL inventory: absence is not proof of absence.",
]


def _read(ctx: WorkspaceContext, path: Path, limit: int) -> bytes:
    # Policy checks do not replace fd-relative no-follow reads (including all parents).
    if ".." in path.parts:
        raise SvwError(EC.PATH_NOT_ALLOWED, "supplement paths may not contain '..'")
    lexical, _ = ctx.path_policy.check(str(path), ctx.project_root)
    try:
        data = safeio.read_regular_under(Path("/"), lexical.as_posix().lstrip("/"), limit + 1)
    except FileNotFoundError as exc:
        raise SvwError(EC.MISSING_INPUT, "supplement artifact not found") from exc
    if len(data) > limit:
        raise SvwError(EC.RESOURCE_LIMIT, "supplement artifact exceeds bounded input limit")
    return data


def _encode(data: dict[str, Any]) -> bytes:
    return json.dumps(data, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode()


def _envelope(ctx: WorkspaceContext, rid: str, data: dict[str, Any]) -> Envelope:
    return Envelope(
        request_id=rid,
        outcome=Outcome.PARTIAL,
        workspace_id=ctx.workspace_id,
        provenance=Provenance(backend="verilator", evidence_kind=EvidenceKind.EXTERNAL_DECLARED),
        analysis_coverage=AnalysisCoverage(
            completeness=Completeness.PARTIAL,
            scope="external_static_inventory",
            basis="externally_supplied_verilator_json",
            limitations=LIMITATIONS,
        ),
        data=data,
    )


def import_verilator(
    opts: WorkspaceOptions, tree: Path, meta: Path, tool_version: str, request_id: str
) -> Envelope:
    ctx = opts.load()
    tree_bytes = _read(ctx, tree, TREE_LIMIT)
    meta_bytes = _read(ctx, meta, META_LIMIT)
    inventory = parse_tree(tree_bytes, meta_bytes, tool_version)
    record = {
        "schema_version": "svw-external-static-1",
        "source_binding": "UNVERIFIED",
        "snapshot_id": None,
        "build_id": None,
        "inventory": inventory,
    }
    encoded = _encode(record)
    if len(encoded) > RECORD_LIMIT:
        raise SvwError(EC.RESOURCE_LIMIT, "supplement inventory exceeds storage limit")
    sid = hashlib.sha256(encoded).hexdigest()
    directory = f"{STORE}/{sid}"
    safeio.ensure_private_dir(ctx.project_root, directory)
    # Raw artifacts retained for audit, not execution. Record is published last.
    safeio.write_under(ctx.project_root, f"{directory}/tree.json", tree_bytes)
    safeio.write_under(ctx.project_root, f"{directory}/meta.json", meta_bytes)
    safeio.write_under(ctx.project_root, f"{directory}/record.json", encoded)
    return _envelope(
        ctx,
        request_id,
        {
            "supplement_id": sid,
            "source_binding": "UNVERIFIED",
            "tool_version": tool_version,
            "item_count": len(inventory["items"]),
            "tree_sha256": inventory["tree_sha256"],
            "meta_sha256": inventory["meta_sha256"],
            "limitations": inventory["limitations"],
        },
    )


def show_verilator(
    opts: WorkspaceOptions,
    supplement_id: str,
    request_id: str,
    *,
    name: str | None = None,
    node_type: str | None = None,
    source_file: str | None = None,
    offset: int = 0,
    limit: int = 20,
) -> Envelope:
    if not is_sha256_hex(supplement_id) or not 1 <= limit <= 100 or offset < 0:
        raise SvwError(EC.INVALID_CONFIG, "invalid supplement id or pagination bounds")
    ctx = opts.load()
    raw = _read(ctx, Path(f"{STORE}/{supplement_id}/record.json"), RECORD_LIMIT)
    if hashlib.sha256(raw).hexdigest() != supplement_id:
        raise SvwError(EC.INVALID_CONFIG, "supplement content hash mismatch")
    try:
        record = json.loads(raw)
        inv = record["inventory"]
        if record["schema_version"] != "svw-external-static-1" or not isinstance(
            inv["items"], list
        ):
            raise ValueError("invalid schema")
        version = inv["tool_version"]
        if not isinstance(version, str):
            raise ValueError("invalid version")
    except (ValueError, KeyError, TypeError, AttributeError, RecursionError) as exc:
        raise SvwError(EC.INVALID_CONFIG, "invalid stored supplement") from exc
    # Revalidate both retained artifacts and the derived inventory. A content hash is
    # integrity, not schema validation or producer authentication.
    artifacts: dict[str, bytes] = {}
    for filename, maximum, key in [
        ("tree.json", TREE_LIMIT, "tree_sha256"),
        ("meta.json", META_LIMIT, "meta_sha256"),
    ]:
        artifact = _read(ctx, Path(f"{STORE}/{supplement_id}/{filename}"), maximum)
        if hashlib.sha256(artifact).hexdigest() != inv.get(key):
            raise SvwError(EC.INVALID_CONFIG, "supplement audit artifact hash mismatch")
        artifacts[filename] = artifact
    validated = parse_tree(artifacts["tree.json"], artifacts["meta.json"], version)
    expected = {
        "schema_version": "svw-external-static-1",
        "source_binding": "UNVERIFIED",
        "snapshot_id": None,
        "build_id": None,
        "inventory": validated,
    }
    if raw != _encode(expected):
        raise SvwError(
            EC.INVALID_CONFIG, "stored supplement differs from validated artifact inventory"
        )
    inv = validated
    items = [
        item
        for item in inv["items"]
        if (name is None or name in str(item.get("name", "")))
        and (node_type is None or item.get("node_type") == node_type)
        and (source_file is None or item.get("source_file") == source_file)
    ]
    data = {
        "supplement_id": supplement_id,
        "source_binding": "UNVERIFIED",
        "tool_version": inv["tool_version"],
        "node_count": inv["node_count"],
        "omitted_node_count": inv["omitted_node_count"],
        "runtime_resolution": "not_computed",
        "tree_sha256": inv["tree_sha256"],
        "meta_sha256": inv["meta_sha256"],
        "limitations": inv["limitations"],
        "matched_count": len(items),
        "offset": offset,
        "next_offset": None,
        "items": items[offset : offset + limit],
    }
    env = _envelope(ctx, request_id, data)
    while True:
        next_offset = offset + len(env.data["items"])
        more = next_offset < len(items)
        env.data["next_offset"] = next_offset if more else None
        env.pagination = Pagination(limit=limit, truncated=more)
        if len(env.to_json(indent=2).encode()) <= MAX_RESPONSE_BYTES:
            return env
        if not env.data["items"]:
            raise SvwError(EC.OUTPUT_LIMIT, "supplement metadata exceeds response budget")
        env.data["items"].pop()
        if not env.data["items"]:
            raise SvwError(EC.OUTPUT_LIMIT, "supplement item exceeds response budget")
