"""Shared by scripts/benchmark.py and scripts/qualify.py: source identity and the benchmark-evidence
contract (plan 22.3 mandatory targets, JSON schema, strict acceptance for G5)."""

from __future__ import annotations

import hashlib
import subprocess
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parents[1]

#: every plan 22.3 target a G5-eligible benchmark must carry, exactly once, with status PASS.
MANDATORY_TARGETS = (
    "cold semantic build (median of runs)",
    "cold semantic build (max of runs)",
    "HDL worker peak RSS (max)",
    "hot symbol query p95",
    "hot hierarchy query p95",
    "hot references query p95",
    "hot repository search p95 (text fallback)",
    "hot repository search p95 (graph)",
)

_ROW = {
    "type": "object",
    "required": ["target", "status"],
    "properties": {
        "target": {"type": "string"},
        "status": {"enum": ["PASS", "FAIL", "SKIPPED", "BLOCKED", "NOT_RUN"]},
        "measured": {"type": ["number", "null"]},
        "budget": {"type": "string"},
    },
}

BENCH_SCHEMA: dict[str, Any] = {
    "type": "object",
    "required": ["benchmark", "host", "corpus", "toolchain_lock_sha256", "source_identity",
                 "cold_build", "hot_queries", "hot_search", "disk_usage", "compare_plan_22_3"],
    "properties": {
        "benchmark": {"const": "PERF-01"},
        "toolchain_lock_sha256": {"type": "string", "pattern": "^[0-9a-f]{64}$"},
        "source_identity": {
            "type": "object",
            "required": ["git_head", "src_tree_sha256"],
            "properties": {"git_head": {"type": "string", "minLength": 1},
                           "src_tree_sha256": {"type": "string", "pattern": "^[0-9a-f]{64}$"}},
        },
        "corpus": {"type": "object", "required": ["scale", "files", "lines"],
                   "properties": {"scale": {"type": "number"}, "files": {"type": "integer"},
                                  "lines": {"type": "integer"}}},
        "cold_build": {"type": "object", "required": ["ok_runs"],
                       "properties": {"ok_runs": {"type": "integer"}}},
        "compare_plan_22_3": {
            "type": "object", "required": ["rows", "reference_corpus"],
            "properties": {"rows": {"type": "array", "items": _ROW},
                           "reference_corpus": {"type": "boolean"}},
        },
    },
}


def src_tree_sha256(root: Path = REPO) -> str:
    """Content hash of src/ (sorted relative path + file sha256; caches and bytecode excluded)."""
    h = hashlib.sha256()
    base = root / "src"
    for p in sorted(base.rglob("*")):
        if not p.is_file() or "__pycache__" in p.parts or p.suffix in (".pyc", ".pyo"):
            continue
        h.update(p.relative_to(base).as_posix().encode() + b"\0")
        h.update(hashlib.sha256(p.read_bytes()).hexdigest().encode() + b"\n")
    return h.hexdigest()


def git_head(root: Path = REPO) -> str:
    try:
        r = subprocess.run(["git", "rev-parse", "HEAD"], cwd=root, capture_output=True, text=True,
                           timeout=20)
    except (OSError, subprocess.SubprocessError):
        return "NONE"
    return r.stdout.strip() if r.returncode == 0 and r.stdout.strip() else "NONE"


def lock_sha256(root: Path = REPO) -> str:
    return hashlib.sha256((root / "toolchain.lock.json").read_bytes()).hexdigest()


def source_identity(root: Path = REPO) -> dict[str, str]:
    """git HEAD (or the literal "NONE" when there is no commit) plus the src/ tree content hash,
    which is what actually identifies uncommitted code."""
    return {"git_head": git_head(root), "src_tree_sha256": src_tree_sha256(root)}


def validate_benchmark(bj: Any, *, current: dict[str, str] | None = None,
                       current_lock_sha256: str | None = None) -> list[str]:
    """Reasons a benchmark JSON is NOT acceptable evidence for G5 (empty list = acceptable)."""
    import jsonschema

    if not isinstance(bj, dict):
        return ["benchmark JSON is not an object"]
    why: list[str] = []
    for e in sorted(jsonschema.Draft202012Validator(BENCH_SCHEMA).iter_errors(bj),
                    key=lambda e: list(e.absolute_path)):
        why.append("schema: " + ".".join(str(x) for x in e.absolute_path) + ": " + e.message[:120])
    if why:
        return why
    cmp_ = bj["compare_plan_22_3"]
    if not cmp_["reference_corpus"] or bj["corpus"]["scale"] < 1.0:
        why.append("not the plan 22.3 reference corpus (scale < 1.0)")
    if bj["cold_build"]["ok_runs"] < 5:
        why.append(f"cold build ok_runs {bj['cold_build']['ok_runs']} < 5 (plan 22.3)")
    by_target: dict[str, list[dict[str, Any]]] = {}
    for r in cmp_["rows"]:
        by_target.setdefault(r["target"], []).append(r)
    for t in MANDATORY_TARGETS:
        rows = by_target.get(t, [])
        if len(rows) != 1:
            why.append(f"mandatory target {t!r} appears {len(rows)} times (need exactly 1)")
        elif rows[0]["status"] != "PASS":
            why.append(f"target {t!r} is {rows[0]['status']}, only an explicit PASS is accepted"
                       + (f": {rows[0].get('reason')}" if rows[0].get("reason") else ""))
        elif not isinstance(rows[0].get("measured"), (int, float)):
            why.append(f"target {t!r} is PASS but carries no measured value")
    extra = sorted(set(by_target) - set(MANDATORY_TARGETS))
    for t in extra:
        if any(r["status"] != "PASS" for r in by_target[t]):
            why.append(f"extra target {t!r} is not PASS")
    if current is not None and bj["source_identity"] != current:
        why.append(f"benchmark source identity {bj['source_identity']} != current tree {current}")
    if current_lock_sha256 is not None and bj["toolchain_lock_sha256"] != current_lock_sha256:
        why.append("benchmark toolchain lock sha256 does not match the current toolchain.lock.json")
    return why
