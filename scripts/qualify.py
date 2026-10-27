#!/usr/bin/env python3
"""Release-gate runner (plan 18, 22.2, 26, 28.2).

Runs the qualification steps, computes G0-G5 from the ACTUAL results and writes

* ``.svweave/qualification/report.json``  (machine readable, raw numbers and paths)
* ``.svweave/qualification/report.md``    (plan 28.2 template, filled)
* ``.svweave/qualification/logs/*.log``   (full output of every step)

Rules that cannot be configured away:

* SKIP is never PASS. Every test suite runs with ``SVWEAVE_REQUIRE_BACKENDS=1`` so a missing backend
  is a failure, and a suite whose junit file shows skipped tests makes its gates FAIL.
* A gate computed from a step that did not run is ``NOT_EVALUATED`` (not PASS).
* EDA compile / simulation / formal are ``NOT_RUN`` unless a valid evidence manifest exists under
  ``evidence/eda/``; declared evidence is reported as ``EXTERNAL_DECLARED``, never as verified.
* ``ACCEPTED_STATIC_PREVIEW`` only when every gate is PASS and no release blocker is open;
  otherwise ``BLOCKED`` with the reasons.

Gate definitions (see docs/qualification_report_template.md):

G0 lock verified + lock fields non-null + backend probe (pyslang/slang READY, cbm/Serena not
   UNAVAILABLE) + sandbox isolation tests.
G1 manifest/snapshot/config/path-policy suites (unit + integration) with no FAIL/SKIP.
G2 semantic suites (SEM/LOC/SVA/UVM/build identity, real pyslang, real UVM) with no FAIL/SKIP.
G3 backend suites (cbm, Serena, registry, stdio gateway, API) + MCP contract tests.
G4 edit safety (unit, fault injection, real-backend edit flow) + all 22 critical mutations (10 plan classes) killed.
G5 every P0 suite has no FAIL/BLOCKED/SKIP, acceptance flow, statement >= 85 %, branch >= 80 %,
   docs check, benchmark within the plan 22.3 budgets on the reference corpus.

Usage::

    .venv/bin/python scripts/qualify.py [--skip tests,mutation,benchmark,...] [--benchmark]
        [--benchmark-scale 1.0] [--out-dir .svweave/qualification] [--no-coverage]
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
import platform
import re
import shutil
import subprocess
import sys
import time
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _bench_common import source_identity, validate_benchmark  # noqa: E402
from _required_backends import required_backend_errors  # noqa: E402

REPO = Path(__file__).resolve().parents[1]
PY = str(REPO / ".venv" / "bin" / "python")
SVW = str(REPO / ".venv" / "bin" / "svweave")
OUT = REPO / ".svweave" / "qualification"

SUITES = ("unit", "contract", "fault_injection", "integration", "acceptance")
STEPS = ("lock", "probe", "tests", "coverage", "mutation", "benchmark", "docs")

#: DEGRADED backends are only acceptable for G0 when EVERY failed probe check lies inside the
#: DECLARED qualified subset (ADR-012 for Serena, recorded extraction gaps for cbm). Any other failed
#: check is DEGRADED for a different reason and fails G0.
DECLARED_SUBSET: dict[str, dict[str, Any]] = {
    "serena": {
        "declared_in": "docs/adr/ADR-012-serena-sv-edit-subset.md",
        "scope": "ADR-012: Serena symbol edits are qualified for SystemVerilog `function` bodies only",
        "allowed_failed_checks": ["find_symbol svw_driver/run_phase (task method)"],
    },
    "cbm": {
        "declared_in": "docs/limitations.md",
        "scope": "recorded cbm SystemVerilog extraction gaps (docs/limitations.md, graph backend)",
        "allowed_failed_checks": [
            "search_graph finds class svw_driver",
            "two run_phase methods extracted",
            "no fixture file parse_unusable",
        ],
    },
}

#: owner sign-offs that no agent can give. Recorded in evidence/owner_signoff.json as
#: {"<id>": {"by": "...", "date": "YYYY-MM-DD", "ref": "..."}}; absent/incomplete = OPEN.
OWNER_ITEMS = {
    "g0_waiver": "G0 waiver: cbm/Serena are DEGRADED (not READY) within the declared qualified subset",
    "adr_012": "ADR-012 owner acceptance (Serena symbol edits limited to function bodies)",
    "license_review": "license / third-party notices legal review (license_manifest.json legal_review_status)",
}

# test module -> gates it belongs to (a module may feed several gates)
GATE_MODULES: dict[str, list[str]] = {
    "G1": ["test_manifest", "test_snapshot", "test_config", "test_slang_args", "test_locations",
           "test_repo_profiles", "test_run_artifacts_freshness"],
    "G2": ["test_sem", "test_loc", "test_sva", "test_uvm", "test_build_identity",
           "test_extractor_dto", "test_domain", "test_semantic_envelope", "test_cursor",
           "test_cli_semantic", "test_api_semantic", "test_reference_coverage", "test_uvm_call_metadata"],
    "G3": ["test_int_cbm", "test_int_serena", "test_int_registry", "test_int_leaks",
           "test_gateway_stdio", "test_adapters_cbm", "test_adapters_serena",
           "test_adapters_mcp_client", "test_adapters_orchestration", "test_adapters_search",
           "test_mcp_api", "test_mcp_schemas", "test_stdio_server", "test_cli_parity"],
    "G4": ["test_edit_commit", "test_edit_machine", "test_edit_prepare", "test_edit_validate",
           "test_edit_crash", "test_edt_flow", "test_int_hardening", "test_adapters_hardening",
           "test_adapters_redaction", "test_sandbox", "test_int_sandbox"],
    "G0": ["test_int_sandbox", "test_sandbox", "test_ci_sandbox_gate", "test_ci_bwrap_apparmor",
           "test_ci_qualification", "test_ci_required_backends"],
}


# ------------------------------------------------------------------------------------ helpers
def sh(cmd: list[str], log: Path, env_extra: dict[str, str] | None = None,
       timeout: float = 7200) -> dict[str, Any]:
    env = dict(os.environ)
    env.update(env_extra or {})
    log.parent.mkdir(parents=True, exist_ok=True)
    t0 = time.monotonic()
    try:
        p = subprocess.run(cmd, cwd=REPO, env=env, capture_output=True, text=True, timeout=timeout)
        rc, out = p.returncode, p.stdout + p.stderr
    except subprocess.TimeoutExpired as e:
        rc, out = 124, f"[TIMEOUT after {timeout}s]\n{e.stdout or ''}"
    dur = time.monotonic() - t0
    log.write_text(out, encoding="utf-8")
    return {"cmd": " ".join(cmd[:8]) + (" ..." if len(cmd) > 8 else ""), "rc": rc,
            "seconds": round(dur, 1), "log": str(log.relative_to(REPO)), "tail": out.strip()[-1200:]}


def sha256(p: Path) -> str | None:
    try:
        return hashlib.sha256(p.read_bytes()).hexdigest()
    except OSError:
        return None


def git(*args: str) -> str | None:
    try:
        r = subprocess.run(["git", *args], cwd=REPO, capture_output=True, text=True, timeout=20)
    except (OSError, subprocess.SubprocessError):
        return None
    return r.stdout.strip() if r.returncode == 0 else None


def parse_junit(path: Path) -> dict[str, Any]:
    """Counts and per-module results from a pytest junit xml."""
    res: dict[str, Any] = {"passed": 0, "failed": 0, "errors": 0, "skipped": 0, "modules": {},
                           "failed_tests": [], "skipped_tests": []}
    if not path.is_file():
        return {**res, "missing": True}
    try:
        root = ET.parse(path).getroot()
    except (ET.ParseError, OSError) as exc:
        return {**res, "invalid": f"{type(exc).__name__}: {exc}"}
    if root.tag not in {"testsuite", "testsuites"}:
        return {**res, "invalid": f"unexpected JUnit root: {root.tag}"}
    for suite in root.iter():
        if suite.tag not in {"testsuite", "testsuites"}:
            continue
        cases = list(suite.iter("testcase"))
        observed = {"tests": len(cases),
                    "failures": sum(tc.find("failure") is not None for tc in cases),
                    "errors": sum(tc.find("error") is not None for tc in cases),
                    "skipped": sum(tc.find("skipped") is not None for tc in cases)}
        for key, count in observed.items():
            if key not in suite.attrib:
                continue
            try:
                declared = int(suite.attrib[key])
            except ValueError:
                return {**res, "invalid": f"invalid JUnit {key} count"}
            if declared != count:
                return {**res, "invalid": f"JUnit {key} count mismatch: {declared} != {count}"}
    for tc in root.iter("testcase"):
        mod = (tc.get("classname") or "").split(".")
        mod = [m for m in mod if m.startswith("test_")]
        name = mod[0] if mod else (tc.get("classname") or "?")
        m = res["modules"].setdefault(name, {"passed": 0, "failed": 0, "errors": 0, "skipped": 0})
        tid = f"{tc.get('classname')}::{tc.get('name')}"
        if tc.find("failure") is not None:
            res["failed"] += 1
            m["failed"] += 1
            res["failed_tests"].append(tid)
        elif tc.find("error") is not None:
            res["errors"] += 1
            m["errors"] += 1
            res["failed_tests"].append(tid)
        elif tc.find("skipped") is not None:
            res["skipped"] += 1
            m["skipped"] += 1
            res["skipped_tests"].append(tid)
        else:
            res["passed"] += 1
            m["passed"] += 1
    return res


def suite_result_errors(result: dict[str, Any]) -> list[str]:
    """A report must be present, valid, nonempty and entirely passing."""
    why = []
    if result.get("rc") != 0:
        why.append(f"pytest exit {result.get('rc')}")
    if result.get("missing"):
        why.append("JUnit report missing")
    if result.get("invalid"):
        why.append(f"invalid JUnit report: {result['invalid']}")
    for key in ("failed", "errors", "skipped"):
        if result.get(key):
            why.append(f"{result[key]} {key}")
    if result.get("passed", 0) == 0:
        why.append("zero tests passed")
    return why


def gate_from_modules(suites: dict[str, dict[str, Any]], modules: list[str]) -> dict[str, Any]:
    tot = {"passed": 0, "failed": 0, "errors": 0, "skipped": 0}
    seen: list[str] = []
    for s in suites.values():
        for mod in modules:
            mm = s.get("modules", {}).get(mod)
            if mm:
                seen.append(mod)
                for k in tot:
                    tot[k] += mm[k]
    return {**tot, "modules_seen": sorted(set(seen)),
            "modules_missing": sorted(set(modules) - set(seen))}


def detail(reasons: list[str], ok: bool, evaluated: bool = True) -> dict[str, Any]:
    status = "NOT_EVALUATED" if not evaluated else ("PASS" if ok else "FAIL")
    return {"status": status, "reasons": reasons}


# ------------------------------------------------------------------------------------- steps
def step_lock(out: Path) -> dict[str, Any]:
    r = sh([PY, "scripts/bootstrap_locked.py", "--verify-only", "--report",
            str(out / "lock_verify.json")], out / "logs" / "lock.log")
    lock = json.loads((REPO / "toolchain.lock.json").read_text())
    nulls: list[str] = []

    def walk(o: Any, path: str) -> None:
        if isinstance(o, dict):
            for k, v in o.items():
                walk(v, f"{path}.{k}")
        elif isinstance(o, list):
            for i, v in enumerate(o):
                walk(v, f"{path}[{i}]")
        elif o is None or o == "":
            nulls.append(path)

    walk(lock.get("components", {}), "components")
    r["lock_sha256"] = sha256(REPO / "toolchain.lock.json")
    r["null_or_empty_fields"] = nulls
    rep = out / "lock_verify.json"
    r["verify_report"] = json.loads(rep.read_text()) if rep.is_file() else None
    return r


def step_probe(out: Path) -> dict[str, Any]:
    dest = out / "capability_report.json"
    r = sh([PY, "scripts/probe_backends.py", "--out", str(dest)], out / "logs" / "probe.log",
           {"SVWEAVE_REQUIRE_BACKENDS": "1"}, timeout=1800)
    summary: dict[str, str] = {}
    if dest.is_file():
        summary = json.loads(dest.read_text()).get("summary", {})
    r["summary"] = summary
    failed: dict[str, list[str]] = {}
    if dest.is_file():
        for b, v in json.loads(dest.read_text()).get("backends", {}).items():
            failed[b] = [c["check"] for c in v.get("checks", []) if not c["pass"]]
            if v.get("error"):
                failed[b].append(f"error: {str(v['error'])[:200]}")
    r["failed_checks"] = failed
    return r


def owner_items() -> list[dict[str, Any]]:
    """State of every owner sign-off: SIGNED only with a complete record (by, date, ref)."""
    f = REPO / "evidence" / "owner_signoff.json"
    rec: dict[str, Any] = {}
    if f.is_file():
        try:
            rec = json.loads(f.read_text())
        except ValueError:
            rec = {}
    lm = json.loads((REPO / "license_manifest.json").read_text())
    out = []
    for oid, title in OWNER_ITEMS.items():
        r = rec.get(oid) if isinstance(rec, dict) else None
        signed = isinstance(r, dict) and all(r.get(k) for k in ("by", "date", "ref"))
        if oid == "license_review":
            signed = signed and lm.get("legal_review_status") == "REVIEWED"
        out.append({"id": oid, "title": title, "state": "SIGNED" if signed else "OPEN",
                    "detail": (f"signed by {r['by']} on {r['date']} ({r['ref']})" if signed and r else
                               "not signed in evidence/owner_signoff.json"
                               + (f"; license_manifest legal_review_status={lm.get('legal_review_status')}"
                                  if oid == "license_review" else ""))})
    return out


def step_tests(out: Path, coverage: bool) -> dict[str, Any]:
    res: dict[str, Any] = {}
    cov_data = out / ".coverage"
    if coverage and cov_data.exists():
        cov_data.unlink()
    for suite in SUITES:
        junit = out / f"junit_{suite}.xml"
        if not (REPO / "tests" / suite).is_dir():
            res[suite] = {"missing_dir": True}
            continue
        cmd = [PY, "-m", "pytest", f"tests/{suite}", "-p", "no:cacheprovider", f"--junitxml={junit}",
               "-q", "-rs", "--tb=short"]
        if coverage:
            cmd += ["--cov=svweave", "--cov-branch", "--cov-report=", "--cov-append"]
        else:
            cmd += ["--no-cov"]
        env = {"SVWEAVE_REQUIRE_BACKENDS": "1", "COVERAGE_FILE": str(cov_data),
               "PYTHONDONTWRITEBYTECODE": "1"}
        r = sh(cmd, out / "logs" / f"tests_{suite}.log", env)
        r.update(parse_junit(junit))
        res[suite] = r
        print(f"[qualify] {suite}: rc={r['rc']} passed={r.get('passed')} failed={r.get('failed')} "
              f"errors={r.get('errors')} skipped={r.get('skipped')} ({r['seconds']}s)", flush=True)
    return res


def step_coverage(out: Path) -> dict[str, Any]:
    data = out / ".coverage"
    if not data.is_file():
        return {"error": "no coverage data (tests step did not run with coverage)"}
    js = out / "coverage.json"
    r = sh([PY, "-m", "coverage", "json", "--data-file", str(data), "-o", str(js)],
           out / "logs" / "coverage.log")
    if not js.is_file():
        return {**r, "error": "coverage json failed"}
    t = json.loads(js.read_text())["totals"]
    stmts, miss = t["num_statements"], t["missing_lines"]
    br, brm = t.get("num_branches", 0), t.get("missing_branches", 0)
    part = t.get("num_partial_branches", 0)
    stmt_pct = 100.0 * (stmts - miss) / stmts if stmts else 0.0
    # branch coverage as the plan defines it: covered branches / all branches
    br_pct = 100.0 * (br - brm) / br if br else 0.0
    files = json.loads(js.read_text())["files"]
    worst = sorted(((v["summary"]["percent_covered"], k) for k, v in files.items()))[:10]
    return {"statements": stmts, "missing_statements": miss, "statement_percent": round(stmt_pct, 2),
            "branches": br, "missing_branches": brm, "partial_branches": part,
            "branch_percent": round(br_pct, 2), "combined_percent": t.get("percent_covered"),
            "worst_files": [{"file": k, "percent": round(p, 1)} for p, k in worst],
            "scope": "src/svweave (own Python only; no exclusions added)",
            "note": "the HDL worker runs in a subprocess with a clean environment, so its lines "
                    "count only where tests import the extractor in-process"}


def step_mutation(out: Path) -> dict[str, Any]:
    dest = out / "mutation.json"
    r = sh([PY, "scripts/mutation_suite.py", "--out", str(dest)], out / "logs" / "mutation.log",
           timeout=7200)
    r["result"] = json.loads(dest.read_text()) if dest.is_file() else None
    return r


def step_benchmark(out: Path, scale: float) -> dict[str, Any]:
    bdir = out / "bench"
    r = sh([PY, "scripts/benchmark.py", "--scale", str(scale), "--out-dir", str(bdir)],
           out / "logs" / "benchmark.log", timeout=7200)
    files = sorted(bdir.glob("*.json"), key=lambda p: p.stat().st_mtime)
    r["result_file"] = str(files[-1].relative_to(REPO)) if files else None
    r["result"] = json.loads(files[-1].read_text()) if files else None
    return r


def latest_benchmark_evidence() -> tuple[str | None, dict[str, Any] | None]:
    """Newest reference-scale benchmark whose identity matches the current tree + lock; if none
    matches, the newest file (it will then be rejected by validate_benchmark with the reason)."""
    files = sorted((REPO / "evidence" / "bench").glob("*-s1.0.json"))
    if not files:
        return None, None
    cur, lk = source_identity(), sha256(REPO / "toolchain.lock.json")
    loaded: list[tuple[Path, Any]] = []
    for f in reversed(files):
        try:
            loaded.append((f, json.loads(f.read_text())))
        except ValueError:
            continue
    for f, bj in loaded:
        if not validate_benchmark(bj, current=cur, current_lock_sha256=lk):
            return str(f.relative_to(REPO)), bj
    if not loaded:
        return None, None
    return str(loaded[0][0].relative_to(REPO)), loaded[0][1]


def step_docs(out: Path) -> dict[str, Any]:
    checks: dict[str, Any] = {}
    checks["schema_emit_check"] = sh([SVW, "schema", "emit", "--check"], out / "logs" / "schema.log")
    junit = out / "junit_docs.xml"
    checks["doc_tests"] = sh(
        [PY, "-m", "pytest", "tests/unit/test_gateway_cli.py", "tests/unit/test_gateway_evidence.py",
         "-k", "DOC or docs", "--no-cov", "-q", "-p", "no:cacheprovider", f"--junitxml={junit}"],
        out / "logs" / "docs.log", {"SVWEAVE_REQUIRE_BACKENDS": "1"})
    checks["doc_tests"].update(parse_junit(junit))
    required = ["README.md", "AGENTS.md", "CHANGELOG.md", "docs/limitations.md", "docs/security.md",
                "docs/install.md", "docs/operations.md", "docs/architecture.md",
                "docs/manifest.md", "docs/mcp_contract.md", "docs/supported_features.md",
                "docs/editing.md", "docs/qualification_report_template.md",
                "THIRD_PARTY_NOTICES.md", "license_manifest.json", "toolchain.lock.json"]
    checks["required_files_missing"] = [f for f in required if not (REPO / f).is_file()]
    readme = (REPO / "README.md").read_text() if (REPO / "README.md").is_file() else ""
    checks["readme_claims_mit_for_svweave"] = bool(re.search(r"(?i)svweave[^\n]{0,40}\bMIT\b.{0,10}licen", readme)) \
        and "TBD by owner" not in readme
    checks["readme_mentions_license_tbd"] = "TBD by owner" in readme
    return checks


def eda_states() -> dict[str, Any]:
    """NOT_RUN unless declared evidence exists (never verified by SVWeave)."""
    out = {k: {"state": "NOT_RUN", "records": []} for k in ("eda_compile", "simulation", "formal")}
    d = REPO / "evidence" / "eda"
    if not d.is_dir():
        return out
    for f in sorted(d.glob("*.json")):
        try:
            recs = json.loads(f.read_text())
        except ValueError:
            continue
        for r in recs if isinstance(recs, list) else [recs]:
            kind = r.get("kind") if isinstance(r, dict) else None
            key = {"eda_compile": "eda_compile", "simulation": "simulation", "formal": "formal"}.get(kind or "")
            if key:
                out[key]["state"] = "EXTERNAL_DECLARED"
                out[key]["records"].append({"file": str(f.relative_to(REPO)),
                                            "result": r.get("result"), "run_id": r.get("run_id")})
    return out


# ---------------------------------------------------------------------------------- the gates
def compute_gates(R: dict[str, Any]) -> dict[str, Any]:
    gates: dict[str, Any] = {}
    suites = {k: v for k, v in R.get("tests", {}).items() if isinstance(v, dict)}
    tests_ran = bool(suites)

    def suite_ok(name: str) -> tuple[bool, list[str]]:
        s = suites.get(name)
        if not s or s.get("missing_dir"):
            return False, [f"suite {name}: directory missing or not run"]
        why = [f"suite {name}: {message}" for message in suite_result_errors(s)]
        return not why, why

    def modules_gate(g: str) -> tuple[bool, list[str]]:
        c = gate_from_modules(suites, GATE_MODULES[g])
        why = []
        if c["failed"] or c["errors"]:
            why.append(f"{c['failed']} failed / {c['errors']} errors in {g} modules")
        if c["skipped"]:
            why.append(f"{c['skipped']} SKIPPED in {g} modules (skip is not pass)")
        if c["modules_missing"]:
            why.append("no results for modules: " + ", ".join(c["modules_missing"]))
        if c["passed"] == 0:
            why.append(f"no passing test for {g}")
        for name, suite in suites.items():
            if set(suite.get("modules", {})) & set(GATE_MODULES[g]):
                why += [f"suite {name}: {message}" for message in suite_result_errors(suite)]
        return not why, why

    # G0
    lock, probe = R.get("lock"), R.get("probe")
    if lock is None or probe is None:
        gates["G0"] = detail(["lock verify and/or probe step not run"], False, False)
    else:
        why = []
        if lock["rc"] != 0:
            why.append("scripts/bootstrap_locked.py --verify-only failed")
        if lock["null_or_empty_fields"]:
            why.append("null/empty lock fields: " + ", ".join(lock["null_or_empty_fields"][:5]))
        sm = probe.get("summary", {})
        if probe.get("rc") != 0:
            why.append(f"backend probe exit {probe.get('rc')}")
        why += required_backend_errors(sm)
        ok, w = modules_gate("G0") if tests_ran else (False, ["isolation tests not run"])
        why += w
        degraded: dict[str, Any] = {}
        for b in ("cbm", "serena"):
            if sm.get(b) != "DEGRADED":
                continue
            decl = DECLARED_SUBSET[b]
            failed = probe.get("failed_checks", {}).get(b, [])
            outside = [c for c in failed if c not in decl["allowed_failed_checks"]]
            declared_ok = (REPO / decl["declared_in"]).is_file()
            within = bool(failed) and not outside and declared_ok
            degraded[b] = {"classification": "DEGRADED_WITHIN_DECLARED_SUBSET" if within
                           else "DEGRADED_OTHER", "failed_checks": failed, "scope": decl["scope"],
                           "declared_in": decl["declared_in"]}
            if not within:
                reason = (", ".join(outside) if outside else
                          "no failed check recorded" if not failed else
                          f"{decl['declared_in']} (the declaration) is missing")
                why.append(f"{b} DEGRADED for reasons outside the declared qualified subset: {reason}")
        waiver_open = bool(degraded) and next(
            i for i in owner_items() if i["id"] == "g0_waiver")["state"] != "SIGNED"
        gates["G0"] = detail(why, not why, evaluated=tests_ran)
        if gates["G0"]["status"] == "PASS" and waiver_open:
            gates["G0"]["status"] = "PASS_WITH_OWNER_WAIVER_OPEN"
            gates["G0"]["reasons"].append(
                "cbm/Serena are DEGRADED within the declared qualified subset; the G0 waiver "
                "(owner item g0_waiver) is not signed, so G0 is not a plain PASS")
        gates["G0"]["probe_summary"] = sm
        gates["G0"]["degraded"] = degraded
    # G1..G3
    for g in ("G1", "G2", "G3"):
        if not tests_ran:
            gates[g] = detail(["tests step not run"], False, False)
            continue
        ok, why = modules_gate(g)
        if g == "G3":
            okc, wc = suite_ok("contract")
            why += wc
            ok = ok and okc
        gates[g] = detail(why, ok)
    # G4
    mut = R.get("mutation")
    if not tests_ran or mut is None:
        gates["G4"] = detail(["tests and/or mutation step not run"], False, False)
    else:
        ok, why = modules_gate("G4")
        okf, wf = suite_ok("fault_injection")
        why += wf
        res = mut.get("result") or {}
        if not res.get("all_killed"):
            why.append(f"critical mutations: killed {res.get('killed')}/{res.get('total')}; "
                       f"survived={res.get('survived')} errors={res.get('errors')}")
        gates["G4"] = detail(why, not why)
    # G5
    if not tests_ran:
        gates["G5"] = detail(["tests step not run"], False, False)
    else:
        why = []
        p0 = {"passed": 0, "failed": 0, "errors": 0, "skipped": 0}
        for name in SUITES:
            ok, w = suite_ok(name)
            why += w
            for k in p0:
                p0[k] += suites.get(name, {}).get(k, 0)
        cov = R.get("coverage") or {}
        if "statement_percent" not in cov:
            why.append("coverage not measured")
        else:
            if cov["statement_percent"] < 85.0:
                why.append(f"statement coverage {cov['statement_percent']}% < 85%")
            if cov["branch_percent"] < 80.0:
                why.append(f"branch coverage {cov['branch_percent']}% < 80%")
        docs = R.get("docs")
        if docs is None:
            why.append("docs check not run")
        else:
            if docs["schema_emit_check"]["rc"] != 0:
                why.append("schema emit --check failed")
            dt_ = docs["doc_tests"]
            why += [f"docs command/sample tests: {message}" for message in suite_result_errors(dt_)]
            if docs["required_files_missing"]:
                why.append("missing docs: " + ", ".join(docs["required_files_missing"]))
            if docs["readme_claims_mit_for_svweave"]:
                why.append("README claims an MIT license for SVWeave itself")
        bpath, bj = R.get("benchmark_ref")
        if bj is None:
            why.append("no reference-scale (scale 1.0) benchmark JSON")
        else:
            why += [f"benchmark {bpath}: {w}" for w in validate_benchmark(
                bj, current=R["source_identity"], current_lock_sha256=R["lock_sha256"])]
        gates["G5"] = detail(why, not why)
        gates["G5"]["p0_counts"] = p0
    return gates


def blockers(R: dict[str, Any]) -> list[str]:
    """Open owner sign-offs: nothing an agent can close. Never silently passed."""
    # toolchain.lock.json / capability_report.json carry qualification_state UNQUALIFIED; that flag
    # is the OUTPUT of this report (the owner flips it on acceptance), so it is not an input gate.
    return [f"OPEN owner item {i['id']}: {i['title']} ({i['detail']})"
            for i in R["owner_items"] if i["state"] != "SIGNED"]


def markdown(R: dict[str, Any]) -> str:
    g = R["gates"]
    c = R.get("coverage") or {}
    p0 = g.get("G5", {}).get("p0_counts", {})
    mut = (R.get("mutation") or {}).get("result") or {}
    bpath, bj = R["benchmark_ref"]
    sm = R.get("probe", {}).get("summary", {})
    eda = R["eda"]

    def line(label: str, value: Any) -> str:
        return f"{label} {value}"

    gl = " / ".join(f"{k}={g[k]['status']}" for k in ("G0", "G1", "G2", "G3", "G4", "G5"))
    suites = R.get("tests", {})
    graph_ok = _mods_ok(suites, ["test_int_cbm", "test_int_registry"])
    serena_ok = _mods_ok(suites, ["test_int_serena", "test_edt_flow"])
    slang_ok = _mods_ok(suites, ["test_sem", "test_uvm"])
    edit_ok = _mods_ok(suites, ["test_edit_commit", "test_edit_crash", "test_edt_flow"])
    lines = [
        "# SVWeave qualification report", "",
        "```text",
        line("SVWeave version:", R["version"]),
        line("Source commit:", R["source_commit"]),
        line("Source identity:", f"git HEAD {R['source_identity']['git_head']}; "
             f"src/ tree sha256 {R['source_identity']['src_tree_sha256']}"),
        line("Toolchain lock hash:", R["lock_sha256"]),
        line("Qualified platform:", R["platform"] + " (this host only)"),
        line("Qualified feature subset:", "static preview subset in docs/supported_features.md"),
        "",
        line("G0 / G1 / G2 / G3 / G4 / G5:", gl),
        line("P0 passed / failed / blocked / skipped:",
             f"{p0.get('passed')} / {p0.get('failed')} / {p0.get('errors')} / {p0.get('skipped')}"
             if p0 else "NOT_EVALUATED"),
        line("Real graph integration:", graph_ok + f" (probe: {sm.get('cbm')})"),
        line("Real Serena integration:", serena_ok + f" (probe: {sm.get('serena')})"),
        line("Real slang integration:", slang_ok + f" (probe: {sm.get('pyslang')})"),
        line("Editing isolation / approval / recovery:", edit_ok),
        line("Statement / branch coverage:",
             f"{c.get('statement_percent')}% / {c.get('branch_percent')}%" if c
             and "statement_percent" in c else "NOT_EVALUATED"),
        line("Critical mutation results:",
             f"{mut.get('killed')}/{mut.get('total')} killed; survived={mut.get('survived')} "
             f"errors={mut.get('errors')}" if mut else "NOT_EVALUATED"),
        line("Benchmark report:",
             f"{bpath} (scale {bj['corpus']['scale']}, {bj['corpus']['files']} files, "
             f"{bj['corpus']['lines']} lines; identity {bj.get('source_identity')}; "
             f"lock {bj.get('toolchain_lock_sha256')})" if bj else "NOT_EVALUATED (no scale-1.0 JSON)"),
        "",
        line("EDA compile:", eda["eda_compile"]["state"]),
        line("Simulation: ", eda["simulation"]["state"]),
        line("Formal:     ", eda["formal"]["state"]),
        line("Coverage:   ", "MEASURED (SVWeave software QA only, not RTL coverage)"
             if c and "statement_percent" in c else "NOT_RUN"),
        "",
        "Known limitations: docs/limitations.md",
        "Semantic deviations: docs/supported_features.md section 4",
        "Security / license review: docs/security.md (client boundary NOT qualified; editing off "
        "by default); license legal review " + R["license_review"],
        "Evidence paths: " + ", ".join(R["evidence_paths"]),
        f"Release decision: {R['release_decision']}",
        "```", "",
    ]
    lines += ["## Gate details", ""]
    for k in ("G0", "G1", "G2", "G3", "G4", "G5"):
        lines.append(f"* **{k}: {g[k]['status']}**")
        for r in g[k]["reasons"]:
            lines.append(f"  * {r}")
    deg = g["G0"].get("degraded") or {}
    if deg:
        lines += ["", "## G0: DEGRADED backends", ""]
        for b, d in deg.items():
            lines.append(f"* **{b}: {d['classification']}** ({d['scope']}; declared in "
                         f"`{d['declared_in']}`); failed probe checks: "
                         + ("; ".join(d["failed_checks"]) or "none recorded"))
    lines += ["", "## Open owner sign-offs", ""]
    lines += [f"* [{i['state']}] `{i['id']}`: {i['title']} - {i['detail']}" for i in R["owner_items"]]
    lines += ["", "## Release blockers outside G0-G5", ""]
    lines += [f"* {b}" for b in R["blockers"]] or ["* none"]
    if R["release_decision"] == "BLOCKED":
        lines += ["", "## Why BLOCKED", ""]
        lines += [f"* {r}" for r in R["decision_reasons"]]
    lines += ["", "## Steps", "", "| Step | Result |", "|---|---|"]
    for k, v in R["step_summary"].items():
        lines.append(f"| {k} | {v} |")
    lines += ["", "Generated by `scripts/qualify.py` from the actual step results. SKIP, BLOCKED and "
              "NOT_RUN are never counted as PASS.", ""]
    return "\n".join(lines)


def _mods_ok(suites: dict[str, Any], mods: list[str]) -> str:
    c = gate_from_modules({k: v for k, v in suites.items() if isinstance(v, dict)}, mods)
    if c["passed"] == 0 and not c["failed"] and not c["errors"]:
        return "NOT_EVALUATED"
    if c["failed"] or c["errors"] or c["skipped"] or c["modules_missing"]:
        return f"FAIL/INCOMPLETE ({c['passed']} passed, {c['failed']} failed, {c['errors']} errors, " \
               f"{c['skipped']} skipped, missing {c['modules_missing']})"
    for name, suite in suites.items():
        if isinstance(suite, dict) and set(suite.get("modules", {})) & set(mods):
            reasons = suite_result_errors(suite)
            if reasons:
                return f"FAIL/INCOMPLETE (suite {name}: {'; '.join(reasons)})"
    return f"PASS ({c['passed']} tests, real backends, no skips)"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    ap.add_argument("--out-dir", type=Path, default=OUT)
    ap.add_argument("--skip", default="", help="comma list of steps to skip: " + ",".join(STEPS))
    ap.add_argument("--benchmark", action="store_true", help="also run the benchmark now")
    ap.add_argument("--benchmark-scale", type=float, default=1.0)
    ap.add_argument("--no-coverage", action="store_true")
    a = ap.parse_args(argv)
    skip = {s for s in a.skip.split(",") if s}
    bad = skip - set(STEPS)
    if bad:
        raise SystemExit(f"unknown steps {bad}")
    out: Path = a.out_dir.resolve()
    if out.exists():
        # benchmark corpora hold immutable (read-only) snapshots; make dirs writable first
        for root, dirs, _files in os.walk(out):
            os.chmod(root, 0o755)
            for d in dirs:
                p = os.path.join(root, d)
                if not os.path.islink(p):
                    os.chmod(p, 0o755)
        shutil.rmtree(out)
    (out / "logs").mkdir(parents=True)

    R: dict[str, Any] = {
        "version": "0.1.0-dev",
        "generated_utc": dt.datetime.now(dt.UTC).isoformat(timespec="seconds"),
        "platform": f"{platform.system()} {platform.machine()} {platform.release()} "
                    f"py{platform.python_version()}",
        "source_commit": (git("rev-parse", "HEAD") or "NONE (no commit in this repository)")
        + (" +dirty" if git("status", "--porcelain") else ""),
        "lock_sha256": sha256(REPO / "toolchain.lock.json"),
        "source_identity": source_identity(),
        "skipped_steps": sorted(skip),
        "step_summary": {},
    }
    ss = R["step_summary"]
    if "lock" not in skip:
        print("[qualify] lock verify", flush=True)
        R["lock"] = step_lock(out)
        ss["lock verify"] = f"rc={R['lock']['rc']}"
    if "probe" not in skip:
        print("[qualify] probe backends", flush=True)
        R["probe"] = step_probe(out)
        ss["probe backends"] = f"rc={R['probe']['rc']} {R['probe'].get('summary')}"
    if "tests" not in skip:
        print("[qualify] test suites (SVWEAVE_REQUIRE_BACKENDS=1)", flush=True)
        R["tests"] = step_tests(out, coverage=not a.no_coverage and "coverage" not in skip)
        for s, v in R["tests"].items():
            ss[f"tests/{s}"] = (f"rc={v.get('rc')} passed={v.get('passed')} failed={v.get('failed')} "
                                f"errors={v.get('errors')} skipped={v.get('skipped')}")
    if "coverage" not in skip and "tests" not in skip and not a.no_coverage:
        R["coverage"] = step_coverage(out)
        ss["coverage"] = (f"stmt {R['coverage'].get('statement_percent')}% / "
                          f"branch {R['coverage'].get('branch_percent')}%")
    if "mutation" not in skip:
        print("[qualify] mutation suite", flush=True)
        R["mutation"] = step_mutation(out)
        res = R["mutation"].get("result") or {}
        ss["mutation"] = f"killed {res.get('killed')}/{res.get('total')}"
    if a.benchmark and "benchmark" not in skip:
        print("[qualify] benchmark", flush=True)
        R["benchmark"] = step_benchmark(out, a.benchmark_scale)
        ss["benchmark (this run)"] = f"rc={R['benchmark']['rc']} {R['benchmark']['result_file']}"
    if "docs" not in skip:
        print("[qualify] docs check", flush=True)
        R["docs"] = step_docs(out)
        ss["docs"] = (f"schema rc={R['docs']['schema_emit_check']['rc']}, "
                      f"doc tests rc={R['docs']['doc_tests']['rc']}")
    # benchmark used for G5: this run's scale-1.0 JSON, else the newest scale-1.0 evidence file
    br = R.get("benchmark", {}).get("result")
    if br and br.get("corpus", {}).get("scale", 0) >= 1.0:
        R["benchmark_ref"] = (R["benchmark"]["result_file"], br)
    else:
        R["benchmark_ref"] = latest_benchmark_evidence()
    R["eda"] = eda_states()
    lm = json.loads((REPO / "license_manifest.json").read_text())
    R["license_review"] = lm.get("legal_review_status", "UNKNOWN")
    R["owner_items"] = owner_items()
    R["gates"] = compute_gates(R)
    R["blockers"] = blockers(R)
    reasons = [f"{k} {v['status']}: " + "; ".join(v["reasons"] or ["(no detail)"])
               for k, v in R["gates"].items() if v["status"] != "PASS"] + R["blockers"]
    R["decision_reasons"] = reasons
    R["release_decision"] = "ACCEPTED_STATIC_PREVIEW" if not reasons else "BLOCKED"
    R["evidence_paths"] = [str(p.relative_to(REPO)) for p in
                           [out / "report.json", out / "report.md", out / "coverage.json",
                            out / "mutation.json", out / "capability_report.json"] if True] \
        + ([R["benchmark_ref"][0]] if R["benchmark_ref"][0] else []) + ["evidence/m0/"]
    (out / "report.json").write_text(json.dumps(R, indent=2, default=str) + "\n")
    (out / "report.md").write_text(markdown(R))
    print((out / "report.md").read_text())
    print(f"[qualify] release decision: {R['release_decision']}")
    return 0 if R["release_decision"] == "ACCEPTED_STATIC_PREVIEW" else 1


if __name__ == "__main__":
    sys.exit(main())
