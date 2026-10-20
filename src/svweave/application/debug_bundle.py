"""``svweave debug bundle --build <id>``: reproduction bundle (plan 23.3, 24.1, test OBS-01).

Contents are hashes, the normalized manifest, a diagnostics SUMMARY (codes, severities and
locations; message text and all source contents are omitted), the build's event excerpt and the
commands to reproduce. Every text is passed through the redactor with NO allowed host roots, so
absolute paths never appear. Including sources is a separate, human decision (not done here).
"""

from __future__ import annotations

import hashlib
import json
import os
import platform
import re
from collections import Counter
from pathlib import Path
from typing import Any

from svweave import __version__
from svweave.application.build import DB_FILENAME
from svweave.application.options import WorkspaceOptions
from svweave.application.report import _lock_hash
from svweave.application.semantic_common import build_envelope, report_of, require_build
from svweave.config.paths import is_within
from svweave.domain.envelope import Envelope
from svweave.domain.errors import ErrorCode, SvwError
from svweave.domain.ids import canonical_hash
from svweave.security import safeio
from svweave.security.redaction import Redactor
from svweave.storage.db import Database

BUNDLE_VERSION = "1.0"


def semantic_digest(db: Database, build_id: str) -> str:
    """Hash of the normalized semantic result (no job ids, timestamps or host paths)."""
    rows = db.conn.execute(
        "SELECT path, definition, specialization FROM instances WHERE build_id=? ORDER BY ord, path",
        (build_id,),
    ).fetchall()
    rep = report_of(db, build_id)
    counts = {k: v for k, v in (rep.get("stats") or {}).items() if isinstance(v, int)}
    return canonical_hash(
        {
            "instances": [[r["path"], r["definition"], r["specialization"]] for r in rows],
            "counts": counts,
        }
    )


_SECRET_NAME = re.compile(
    r"(?i)(key|token|secret|passw|credential|auth|cookie|license|licence|private)"
)
_LICENSE_ASSIGN = re.compile(r"(?i)\b(\w*(?:license|licence)\w*)(\s*[=:]\s*)(\"[^\"]*\"|'[^']*'|\S+)")
REDACTED = "<redacted>"


def scrub(obj: Any, red: Redactor) -> Any:
    """Redact at VALUE level by the NAME that governs it: `[name, value]` pairs,
    `{"name": n, "value": v}` objects and secret-named keys (e.g. defines), then free text."""
    if isinstance(obj, str):
        return _LICENSE_ASSIGN.sub(lambda m: f"{m.group(1)}{m.group(2)}{REDACTED}", red.text(obj))
    if isinstance(obj, dict):
        out: dict[Any, Any] = {}
        named_secret = isinstance(obj.get("name"), str) and _SECRET_NAME.search(obj["name"])
        for k, v in obj.items():
            if (isinstance(k, str) and _SECRET_NAME.search(k) and isinstance(v, str | bytes)) or (
                named_secret and k in ("value", "val", "default", "expr")
            ):
                out[k] = REDACTED
            else:
                out[k] = scrub(v, red)
        return out
    if isinstance(obj, list | tuple):
        if len(obj) == 2 and isinstance(obj[0], str) and _SECRET_NAME.search(obj[0]):
            return [scrub(obj[0], red), REDACTED]
        return [scrub(v, red) for v in obj]
    return obj


def _write(red: Redactor, out: Path, name: str, obj: Any) -> str:
    text = (
        red.text(
            json.dumps(
                scrub(obj, red), indent=2, sort_keys=True, ensure_ascii=False, default=str
            )
        )
        + "\n"
    )
    safeio.write_atomic(out / name, text.encode("utf-8"), 0o600)  # never follows a link
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def make_bundle(
    opts: WorkspaceOptions, build_id: str, request_id: str, out_dir: Path | None = None
) -> Envelope:
    ctx = opts.load()
    red = Redactor([])  # no allowed roots: every absolute host path is redacted
    with Database(ctx.state_dir / DB_FILENAME) as db:
        b = require_build(db, build_id)
        rep = report_of(db, build_id)
        if out_dir is None:
            out = ctx.state_dir / "bundles" / build_id[:16]
        else:
            ctx.path_policy.check(str(out_dir), Path.cwd())  # PATH_NOT_ALLOWED outside roots
            out = Path(out_dir)
            real = Path(os.path.realpath(out))
            in_project = is_within(real, ctx.project_root)
            if (in_project and not is_within(real, Path(os.path.realpath(ctx.state_dir)))) or (
                out.is_symlink()
            ):
                raise SvwError(
                    ErrorCode.PATH_NOT_ALLOWED,
                    "bundle output may not be inside the project source tree (or a symlink); "
                    "use a directory under .svweave/ or outside the project",
                    {"reason": "bundle_out_in_source_tree"},
                )
        out.mkdir(parents=True, exist_ok=True, mode=0o700)
        files: dict[str, str] = {}

        inv = [
            {
                "logical_path": r["logical_path"],
                "sha256": r["bytes_hash"],
                "size": r["size"],
                "role": r["role"],
            }
            for r in db.conn.execute(
                "SELECT * FROM source_files WHERE snapshot_id=? ORDER BY ord", (b["snapshot_id"],)
            )
        ]
        files["source_inventory.json"] = _write(red, out, "source_inventory.json", inv)

        job_id = rep.get("job_id")
        run_dir = ctx.state_dir / "runs" / str(job_id) if job_id else None
        nm = run_dir / "normalized_manifest.json" if run_dir else None
        if nm is not None and nm.is_file():
            files["normalized_manifest.json"] = _write(
                red, out, "normalized_manifest.json", json.loads(nm.read_text(encoding="utf-8"))
            )
        diags = db.conn.execute(
            "SELECT severity, code, locations FROM diagnostics WHERE build_id=?", (build_id,)
        ).fetchall()
        locs: list[dict[str, Any]] = []
        for d in diags[:200]:
            try:
                raw = json.loads(d["locations"] or "null")
            except ValueError:
                raw = None
            # stored by ingest as {"primary": {...}, "ranges": [...]}; a list is tolerated too
            if isinstance(raw, dict):
                first = raw.get("primary")
            elif isinstance(raw, list) and raw:
                first = raw[0]
            else:
                first = None
            loc = (
                {k: first.get(k) for k in ("file", "line", "col")}
                if isinstance(first, dict)
                else None
            )
            locs.append({"severity": d["severity"], "code": d["code"], "location": loc})
        files["diagnostics_summary.json"] = _write(
            red,
            out,
            "diagnostics_summary.json",
            {
                "total": len(diags),
                "by_severity": dict(Counter(d["severity"] for d in diags)),
                "by_code": dict(Counter(d["code"] for d in diags)),
                "first_200": locs,
                "message_text_omitted": True,
            },
        )
        ev = run_dir / "events.jsonl" if run_dir else None
        if ev is not None and ev.is_file():
            text = red.text(ev.read_text(encoding="utf-8"))
            safeio.write_atomic(out / "events.jsonl", text.encode("utf-8"), 0o600)
            files["events.jsonl"] = hashlib.sha256(text.encode("utf-8")).hexdigest()
        digest = semantic_digest(db, build_id)
        bundle = {
            "bundle_version": BUNDLE_VERSION,
            "build": {
                k: b[k]
                for k in (
                    "build_id",
                    "snapshot_id",
                    "profile",
                    "config_id",
                    "toolchain_id",
                    "semantic_schema_version",
                    "completeness",
                    "diagnostic_outcome",
                )
            },
            "toolchain_lock_hash": _lock_hash(ctx.project_root),
            "versions": {"svweave": __version__, "python": platform.python_version()},
            "semantic_digest": digest,
            "reproduce": [
                f"svweave snapshot check --snapshot {b['snapshot_id']}",
                f"svweave build --profile {b['profile']} --no-reuse",
                f"svweave query hierarchy --build {build_id}",
                f"svweave query diagnostics --build {build_id}",
            ],
            "files": dict(sorted(files.items())),
            "contains_source_text": False,
            "note": "Compare semantic_digest after a rebuild with the same lock and snapshot; "
            "a different digest means the semantic result changed.",
        }
        files["bundle.json"] = _write(red, out, "bundle.json", bundle)
        try:
            shown = out.relative_to(ctx.project_root).as_posix()
        except ValueError:
            shown = red.text(str(out))
        env = build_envelope(
            ctx,
            db,
            b,
            request_id,
            {
                "bundle_dir": shown,
                "files": dict(sorted(files.items())),
                "semantic_digest": digest,
                "contains_source_text": False,
            },
        )
        return env
