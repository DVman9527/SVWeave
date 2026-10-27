#!/usr/bin/env python3
"""Run bounded real Verilator lint/JSON and SVWeave import/query checks; never simulate.

Missing tool, failed isolation, wrong source/version, unexpected diagnostics or failed
semantic expectations are nonzero results. Upstream goldens are never substituted.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import platform
import re
import resource
import shutil
import subprocess
import sys
import time
import xml.etree.ElementTree as ET
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src"))
sys.path.insert(0, str(REPO / "scripts"))

from bootstrap_verilator import PIN, source_identity  # noqa: E402

from svweave.adapters.verilator.importer import parse_tree  # noqa: E402
from svweave.application import supplement  # noqa: E402
from svweave.application.init import init_workspace  # noqa: E402
from svweave.application.options import WorkspaceOptions  # noqa: E402
from svweave.security.sandbox import SandboxRunner, SandboxSpec, probe_host  # noqa: E402

FIXTURES = REPO / "fixtures/verilator/native"
CASES = (
    {"id": "HDL-VLT-01", "source": "native_legacy.v", "top": "native_legacy_top", "language": "1364-2005", "expect": "accept"},
    {"id": "HDL-VLT-02", "source": "native_class.sv", "top": "native_class_top", "language": "1800-2017", "expect": "accept"},
    {"id": "HDL-VLT-03", "source": "native_missing.sv", "top": "native_missing_top", "language": "1800-2017", "expect": "reject", "diagnostic": "missing_unit"},
    {"id": "HDL-VLT-04", "source": "native_syntax.sv", "top": "native_syntax_top", "language": "1800-2017", "expect": "reject", "diagnostic": "syntax error"},
    {"id": "HDL-VLT-05", "source": "native_plain.v", "top": "native_plain_top", "language": "1364-2005", "expect": "accept"},
)


def check(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(message)


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def source_hashes(root: Path) -> dict[str, str]:
    return {p.name: digest(p) for p in sorted(root.iterdir()) if p.suffix in {".v", ".vh", ".sv"}}


def static_command(root: Path, source: Path, out: Path, case: dict, mode: str) -> list[str]:
    check(mode in {"lint", "json"}, "Only static lint and JSON modes are permitted")
    command = [str(root / "bin/verilator"), "--language", case["language"],
               "--top-module", case["top"], "--Mdir", str(out), "--no-std"]
    if mode == "lint":
        command += ["--lint-only"]
    else:
        command += ["--json-only", "--json-only-output", str(out / "design.tree.json"),
                    "--json-only-meta-output", str(out / "design.tree.meta.json")]
    return [*command, str(source)]


def native_spec(root: Path, sources: Path, out: Path) -> SandboxSpec:
    return SandboxSpec(
        ro_paths=(root, sources), rw_paths=(out,), protected_paths=(REPO, root, sources),
        rw_exempt=(out,), chdir=sources, network=False,
        env={"PATH": "/usr/bin:/bin", "HOME": "/tmp", "TMPDIR": "/tmp", "LC_ALL": "C", "VERILATOR_ROOT": str(root)},
    )


def _limits() -> None:
    resource.setrlimit(resource.RLIMIT_AS, (2 << 30, 2 << 30))
    resource.setrlimit(resource.RLIMIT_CPU, (90, 90))
    resource.setrlimit(resource.RLIMIT_FSIZE, (32 << 20, 32 << 20))


def invoke(runner: SandboxRunner, spec: SandboxSpec, command: list[str], output: Path, name: str) -> dict:
    wrapped = runner.wrap(spec, command)
    started = time.monotonic()
    stdout, stderr = output / f"{name}.stdout.log", output / f"{name}.stderr.log"
    with stdout.open("w") as out_log, stderr.open("w") as err_log:
        proc = subprocess.run(wrapped.argv, stdout=out_log, stderr=err_log, timeout=120,
                              check=False, env={"PATH": "/usr/bin:/bin"}, preexec_fn=_limits)
    return {"argv": command, "sandbox_argv": wrapped.argv, "isolation": str(wrapped.isolation),
            "returncode": proc.returncode, "duration_seconds": round(time.monotonic() - started, 3),
            "stdout": str(stdout), "stderr": str(stderr),
            "stdout_sha256": digest(stdout), "stderr_sha256": digest(stderr)}


def verify_exit(result: dict, case: dict) -> None:
    diagnostic = Path(result["stderr"]).read_text(errors="replace") + Path(result["stdout"]).read_text(errors="replace")
    if case["expect"] == "accept":
        check(result["returncode"] == 0, f"Valid source rejected: {diagnostic[-3000:]}")
        check("%Error" not in diagnostic, "Error diagnostic on successful source")
    else:
        # A signal, sandbox failure, warning-only exit or arbitrary nonzero is NOT the expected rejection.
        check(result["returncode"] == 1, "Expected normal diagnostic rejection, not acceptance/signal/wrapper failure")
        check(not any(token in diagnostic for token in (
            "Internal Error", "internal fault", "threw signal", "Aborting", "bad_alloc",
            "Cannot allocate memory", "bwrap:",
        )), "Compiler crash/resource/isolation failure is not expected rejection")
        check("%Error" in diagnostic and case["diagnostic"] in diagnostic,
              f"Missing specific compiler diagnostic: {diagnostic[-3000:]}")


def verify_inventory(inventory: dict, case: dict, source: Path, *, source_label: str | None = None) -> dict:
    check(inventory["trust_class"] == "EXTERNAL_DECLARED", "Native run must not promote importer trust")
    check(inventory["completeness"] == "PARTIAL", "Native run must not promote completeness")
    check(inventory["runtime_resolution"] == "not_computed", "No runtime resolution allowed")
    check("references" not in inventory, "Inventory must not become authoritative references")
    items = inventory["items"]
    lines = source.read_text().splitlines()

    def find(kind: str, name: str, line: int | None) -> dict:
        found = [i for i in items if i["node_type"] == kind and i["verilog_name"] == name]
        if line is not None:
            found = [i for i in found if i["source_span"]["start_line"] == line]
        check(len(found) == 1, f"Expected exactly one {kind} {name} at line {line}: {len(found)}")
        item = found[0]
        # Explicit mapping to this known fixture, never open a producer-provided path.
        labels = {source_label} if source_label is not None else {source.name, str(source)}
        check(item["source_file"] in labels, "Unexpected producer source label")
        span = item["source_span"]
        check(span["start_line"] == span["end_line"], "Expected a single-line fixture location")
        check(1 <= span["start_line"] <= len(lines), "Location outside fixture")
        text = lines[span["start_line"] - 1]
        check(1 <= span["start_column"] < span["end_column"] <= len(text) + 1, "Column outside fixture")
        check("resolved_target" not in item, "No invented resolved target")
        return item

    def select(kind: str, name: str, *, token: str | None = None, line: int | None = None) -> dict:
        item = find(kind, name, line)
        span = item["source_span"]
        text = lines[span["start_line"] - 1]
        actual = text[span["start_column"] - 1:span["end_column"] - 1]
        check(actual == (token or name), f"Source token mismatch for {kind} {name}: {actual!r}")
        return item

    location_findings = []

    def macro_column(kind: str, name: str, line: int, start: int, end: int, original: str) -> dict:
        # Fixture-only negative oracle, observed in actual run 40471175289. This is NOT
        # a macro source mapper and never rewrites the imported producer location.
        item = find(kind, name, line)
        expected_span = {"start_line": line, "end_line": line, "start_column": start, "end_column": end}
        check(item["source_span"] == expected_span, f"Changed producer macro columns for {name}")
        text = lines[line - 1]
        check(text.count("`NATIVE_WIDTH") == 1, "Expected exactly this fixture's single macro")
        check(text[start - 1:end - 1] == original != name, "Macro location must not select the original identifier")
        expanded = text.replace("`NATIVE_WIDTH", "4")
        check(expanded[start - 1:end - 1] == name, "Producer column must match the known fixture expansion")
        location_findings.append({
            "node_type": kind, "name": name, "producer_span": item["source_span"],
            "original_source_slice": original, "known_expansion_slice": name,
            "source_binding": "UNVERIFIED", "interpretation": "macro_expanded_column_not_original_token",
        })
        return item

    assertions = []
    select("MODULE", case["top"])
    if case["id"] == "HDL-VLT-01":
        leaf = select("MODULE", "native_leaf", line=7)
        check("`define NATIVE_WIDTH 4" in (source.parent / "native_defs.vh").read_text().splitlines(), "Unexpected fixture macro value")
        cell = macro_column("CELL", "u_leaf", 4, 28, 34, "IVE_WI")
        macro_column("VAR", "d", 2, 51, 52, "_")
        macro_column("VAR", "q", 3, 41, 42, "_")
        check(cell["attributes"]["modp"] == leaf["node_id"], "Cell pointer must name this artifact's specialized leaf")
        data = select("VAR", "d", line=7)
        check(data["parent_node_id"] == leaf["node_id"], "Input belongs to specialized leaf")
        dtype = next(i for i in items if i["node_id"] == data["attributes"]["dtypep"])
        check(dtype["attributes"].get("range") == "3:0", "Expected macro/parameter-specialized 4-bit input")
        check("WIDTH" not in json.dumps(dtype), "Folded type must not invent a WIDTH source reference")
        select("VAR", "WIDTH", line=7)
        assertions += ["1364-2005 .v/.vh preprocessing and parameter specialization", "macro-free module/parameter/leaf-input original source tokens", "macro-shifted cell/top-port locations explicitly NOT original source tokens", "artifact-local cell pointer", "folded range is 3:0; no reconstructed WIDTH reference"]
    elif case["id"] == "HDL-VLT-05":
        leaf = select("MODULE", "native_plain_leaf", line=4)
        cell = select("CELL", "u_plain", line=2)
        data = select("VAR", "d", line=4)
        check(cell["attributes"]["modp"] == leaf["node_id"], "Macro-free cell pointer mismatch")
        check(data["parent_node_id"] == leaf["node_id"], "Macro-free leaf input containment mismatch")
        assertions += ["1364-2005 macro-free cell/module/input exact original source tokens", "artifact-local cell pointer"]
    else:
        cls = select("CLASS", "Packet", token="class", line=2)
        member = select("VAR", "payload", line=3)
        method = select("FUNC", "get_payload", line=4)
        select("TYPEDEF", "nibble_t", line=1)
        check(member["parent_node_id"] == cls["node_id"], "Member containment mismatch")
        check(method["parent_node_id"] == cls["node_id"], "Method containment mismatch")
        assertions += ["1800-2017 typedef/class/member/function inventory", "class keyword/member/function source tokens", "class containment (not runtime dispatch)"]
    return {"assertions": assertions, "item_count": len(items), "source_location_findings": location_findings,
            "source_binding": "UNVERIFIED", "location_limitations": ["Producer columns following macro expansion may not address original source tokens; no edit-location binding is established."],
            "methodcall_count": sum(i["node_type"] == "METHODCALL" for i in items)}


def roundtrip(output: Path, case: dict, tree: Path, meta: Path) -> dict:
    workspace = output / "import-workspaces" / case["id"]
    workspace.mkdir(parents=True)
    policy = output / "trusted-test-policies" / f"{case['id']}.json"
    init_workspace(workspace, "native-init", policy_path=policy)
    shutil.copyfile(tree, workspace / "tree.json")
    shutil.copyfile(meta, workspace / "meta.json")
    opts = WorkspaceOptions(workspace, policy_path=policy)
    imported = supplement.import_verilator(opts, Path("tree.json"), Path("meta.json"), PIN["version"], "native-import")
    sid = imported.data["supplement_id"]
    shown = supplement.show_verilator(opts, sid, "native-query", name=case["top"], node_type="MODULE")
    absent = supplement.show_verilator(opts, sid, "native-negative-query", name="not_present_fixture_name")
    for envelope in (imported, shown, absent):
        check(envelope.snapshot_id is None and envelope.build_id is None, "Supplement became build-bound")
        check(envelope.provenance.evidence_kind == "EXTERNAL_DECLARED", "Native data promoted trust")
        check(envelope.analysis_coverage.completeness == "PARTIAL", "Native data promoted completeness")
    check(shown.data["matched_count"] == 1, "Query did not retrieve exact top module")
    check(absent.data["items"] == [], "Negative name filter returned unexpected items")
    check(not (workspace / ".svweave/svweave.db").exists(), "Supplement mutated authoritative database")
    for name, envelope in (("import", imported), ("query", shown), ("negative-query", absent)):
        (tree.parent / f"{name}.json").write_text(envelope.to_json(indent=2) + "\n")
    return {"supplement_id": sid, "source_binding": "UNVERIFIED", "authoritative_database_created": False}


def write_junit(report: dict, path: Path) -> None:
    cases = report.get("cases", [])
    records = list(cases)
    if not records or (report["status"] != "PASS" and all(c["status"] == "PASS" for c in cases)):
        records.append({"id": "HDL-VLT-preflight-or-invariant", "status": report["status"], "error": report.get("error", "Not run")})
    suite = ET.Element("testsuite", name="native-verilator-static", tests=str(len(records)),
                       failures=str(sum(c["status"] != "PASS" for c in records)), skipped="0")
    for case in records:
        node = ET.SubElement(suite, "testcase", classname="native_verilator", name=case["id"])
        if case["status"] != "PASS":
            ET.SubElement(node, "failure", message=case.get("error", case["status"])).text = json.dumps(case)
    ET.ElementTree(suite).write(path, encoding="unicode", xml_declaration=True)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--verilator-root", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=False)
    report: dict = {
        "schema_version": "svw-native-verilator-evidence-1", "status": "BLOCKED", "cases": [],
        "recorded_at": datetime.now(UTC).isoformat(), "host": platform.platform(), "pin": PIN,
        "lock_sha256": digest(REPO / "toolchain.lock.json"), "fixture_sha256": source_hashes(FIXTURES),
        "simulation": "NOT_RUN", "formal": "NOT_RUN", "coverage": "NOT_RUN", "uvm_acceptance": "NOT_RUN",
        "limits": {"wall_seconds_per_command": 120, "address_space_bytes": 2 << 30, "cpu_seconds": 90, "file_bytes": 32 << 20},
    }
    try:
        report["source_commit"] = subprocess.check_output(["git", "-C", str(REPO), "rev-parse", "HEAD"], text=True).strip()
        report["source_dirty"] = subprocess.check_output(["git", "-C", str(REPO), "status", "--porcelain"], text=True).strip()
        report["harness_sha256"] = digest(Path(__file__))
        cap = probe_host(refresh=True)
        report["sandbox"] = asdict(cap)
        check(cap.available, f"Mandatory sandbox unavailable: {cap.reason}")
        root = args.verilator_root.resolve()
        check((root / "bin/verilator_bin").is_file(), "Pinned native executable is missing")
        report["status"] = "FAIL"
        report["verilator_source"] = source_identity(root)
        report["binary_sha256"] = digest(root / "bin/verilator_bin")
        report["wrapper_sha256"] = digest(root / "bin/verilator")
        sources = output / "sources"
        sources.mkdir()
        for name in report["fixture_sha256"]:
            shutil.copyfile(FIXTURES / name, sources / name)
        version_dir = output / "version"
        version_dir.mkdir()
        runner = SandboxRunner(capability=cap)
        result = invoke(runner, native_spec(root, sources, version_dir), [str(root / "bin/verilator"), "--version"], version_dir, "version")
        report["version_command"] = result
        version = Path(result["stdout"]).read_text().strip()
        check(result["returncode"] == 0 and re.match(r"^Verilator 5\.052(?:\s|$)", version) is not None, f"Wrong native tool version: {version}")
        report["native_version"] = version
        for case in CASES:
            entry = {**case, "status": "FAIL", "native_lint": "NOT_RUN", "native_json": "NOT_RUN", "import_query": "NOT_RUN", "commands": []}
            report["cases"].append(entry)
            out = output / case["id"]
            out.mkdir()
            try:
                for mode in ("lint", "json"):
                    result = invoke(runner, native_spec(root, sources, out), static_command(root, sources / case["source"], out, case, mode), out, mode)
                    entry["commands"].append(result)
                    entry[f"native_{mode}"] = "ACCEPTED" if result["returncode"] == 0 else "REJECTED"
                    verify_exit(result, case)
                if case["expect"] == "accept":
                    tree, meta = out / "design.tree.json", out / "design.tree.meta.json"
                    inventory = parse_tree(tree.read_bytes(), meta.read_bytes(), PIN["version"])
                    entry["tree_sha256"], entry["meta_sha256"] = digest(tree), digest(meta)
                    entry["expectations"] = verify_inventory(inventory, case, sources / case["source"])
                    (out / "inventory.json").write_text(json.dumps(inventory, indent=2) + "\n")
                    entry["roundtrip"] = roundtrip(output, case, tree, meta)
                    entry["import_query"] = "PASS"
                else:
                    entry["import_query"] = "NOT_RUN_EXPECTED_REJECTION"
                entry["status"] = "PASS"
            except Exception as exc:
                entry["error"] = f"{type(exc).__name__}: {exc}"
        check(source_hashes(sources) == report["fixture_sha256"], "Native process modified copied source")
        check(source_hashes(FIXTURES) == report["fixture_sha256"], "Original fixture source changed")
        check(all(c["status"] == "PASS" for c in report["cases"]), "One or more native verification cases failed")
        report["status"] = "PASS"
    except Exception as exc:
        report["error"] = f"{type(exc).__name__}: {exc}"
        if report["cases"]:
            report["status"] = "FAIL"
    (output / "report.json").write_text(json.dumps(report, indent=2) + "\n")
    write_junit(report, output / "junit.xml")
    print(json.dumps({"status": report["status"], "report": str(output / "report.json"), "error": report.get("error")}))
    return 0 if report["status"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
