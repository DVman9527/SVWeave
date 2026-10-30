"""Trusted, fail-closed G0 controller. Native commands always use mandatory bwrap.

No installation or network code lives here. Source/dependency trees are immutable for
compilation, binaries/fixtures immutable for tests. Only dedicated build state is writable.
This experiment is not SVWeave integration, qualification, or a production partial path.
"""
from __future__ import annotations

import argparse
import contextlib
import hashlib
import json
import os
import platform
import shutil
import signal
import subprocess
import sys
import threading
import time
from collections import Counter
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[2]
sys.path.insert(0, str(HERE.parent))
from _sandbox import LAUNCH_ENV, probe_spec, wrap  # noqa: E402

BUILD_FLAGS = [
    "-G", "Ninja", "-DCMAKE_BUILD_TYPE=Debug", "-DCMAKE_CXX_COMPILER=/usr/bin/g++",
    "-DPython_EXECUTABLE=/usr/bin/python3", "-DCMAKE_CXX_FLAGS_DEBUG=-O0 -g0",
    "-DCMAKE_CXX_FLAGS=-fsanitize=address,undefined -fno-sanitize-recover=all -fno-omit-frame-pointer",
    "-DCMAKE_EXE_LINKER_FLAGS=-fsanitize=address,undefined",
    "-DSLANG_INCLUDE_TOOLS=OFF", "-DSLANG_INCLUDE_TESTS=OFF",
    "-DSLANG_INCLUDE_INSTALL=OFF", "-DSLANG_USE_MIMALLOC=OFF",
    "-DSLANG_INCLUDE_PYLIB=OFF", "-DFETCHCONTENT_FULLY_DISCONNECTED=ON",
    "-DFETCHCONTENT_UPDATES_DISCONNECTED=ON",
]


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def tree_rss(root: int) -> tuple[int, int]:
    """Count current host-visible descendants, including children in a new session."""
    parents = {}
    sizes = {}
    for path in Path("/proc").glob("[0-9]*/stat"):
        try:
            fields = path.read_text().rsplit(")", 1)[1].split()
            pid = int(path.parent.name)
            parents[pid] = int(fields[1])
            sizes[pid] = int(fields[21]) * os.sysconf("SC_PAGE_SIZE")
        except (OSError, ValueError, IndexError):
            continue
    descendants = {root}
    while True:
        added = {pid for pid, parent in parents.items() if parent in descendants} - descendants
        if not added:
            break
        descendants.update(added)
    return sum(sizes.get(pid, 0) for pid in descendants), len(descendants)


def progress(stage: str, event: str, **fields) -> None:
    """Bounded controller metadata only, never streamed native output or environment."""
    print(json.dumps({"stage": stage, "event": event, **fields}), flush=True)


def execute(spec, argv: list[str], *, timeout: int, rss_limit: int,
            output_limit: int = 8 * 1024 * 1024, stage: str | None = None) -> dict:
    command = wrap(spec, argv)  # No subprocess exists until mandatory isolation succeeds.
    start = time.monotonic()
    proc = subprocess.Popen(command.argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, env=LAUNCH_ENV, start_new_session=True)
    buffers = [bytearray(), bytearray()]
    failures = []
    peak_rss = 0
    next_progress = start + 30

    def kill():
        with contextlib.suppress(ProcessLookupError):
            os.killpg(proc.pid, signal.SIGKILL)

    def read(stream, buffer):
        while block := stream.read(65536):
            remaining = max(0, output_limit - len(buffer))
            buffer.extend(block[:remaining])
            if len(block) > remaining:
                failures.append("OUTPUT_LIMIT")
                kill()

    readers = [threading.Thread(target=read, args=pair, daemon=True)
               for pair in zip((proc.stdout, proc.stderr), buffers, strict=True)]
    try:
        for reader in readers:
            reader.start()
        while proc.poll() is None:
            rss, count = tree_rss(proc.pid)
            peak_rss = max(peak_rss, rss)
            now = time.monotonic()
            if stage is not None and now >= next_progress:
                progress(stage, "running", elapsed_seconds=round(now - start, 1),
                         sampled_rss_bytes=rss, process_count=count)
                next_progress = now + 30
            if rss > rss_limit or count > 128:
                failures.append("RESOURCE_LIMIT")
                kill()
            if time.monotonic() - start > timeout:
                failures.append("TIMEOUT")
                kill()
            time.sleep(0.05)
    finally:
        kill()
        proc.wait()
        for reader in readers:
            reader.join(timeout=5)
        if any(reader.is_alive() for reader in readers):
            failures.append("PIPE_NOT_REAPED")
        proc.stdout.close()
        proc.stderr.close()
    return {"argv": argv, "isolation": str(command.isolation), "returncode": proc.returncode,
            "failures": failures, "wall_seconds": time.monotonic() - start,
            "peak_sampled_process_tree_rss_bytes": peak_rss, "timeout_seconds": timeout,
            "rss_limit_bytes": rss_limit, "output_limit_per_pipe_bytes": output_limit,
            "stdout": buffers[0].decode("utf-8", "replace"),
            "stderr": buffers[1].decode("utf-8", "replace")}



ASSERTIONS = {
    "full_equivalence", "stale_handle_rejected", "unsupported_edits_rejected",
    "rejection_atomic", "control_identity_retained", "control_native_work_unchanged",
    "changed_native_work_observed", "generation_limit_enforced",
    "all_changed_occurrences_updated", "source_provenance_verified",
    "unaffected_native_work_unchanged",
}
REFERENCE_ASSERTIONS = ASSERTIONS | {
    "same_bytes_noop", "native_lookup_rebound", "shared_contributions_preserved",
    "last_reference_withdrawn", "reference_event_ledger_equivalent",
}
COUNTERS = {"control_before", "control_after", "changed_before", "changed_after_b", "changed_after_a"}
WORK = {"expressions", "lookups", "assignments", "conversions"}


def strict_json(text: str):
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ValueError("duplicate JSON field")
            result[key] = value
        return result

    def bad_constant(value):
        raise ValueError(f"nonfinite JSON constant: {value}")

    return json.loads(text, object_pairs_hook=pairs, parse_constant=bad_constant)


def validate_incremental(value, full_a, full_b, *, reference: bool = False,
                         full_ledgers=None) -> None:
    fields = {"status", "general_g0", "generations", "assertions", "counters"}
    if reference:
        fields.add("reference_event_ledgers")
    if not isinstance(value, dict) or set(value) != fields:
        raise ValueError("unknown or missing native oracle fields")
    status = "REFERENCE_MICRO_PROTOTYPE_PASS" if reference else "MICRO_PROTOTYPE_PASS"
    if value["status"] != status or value["general_g0"] != "NOT_ESTABLISHED":
        raise ValueError("native oracle status/scope invalid")
    def canonical(obj):
        return json.dumps(obj, sort_keys=True, separators=(",", ":"), allow_nan=False)

    if canonical(full_a) == canonical(full_b) or canonical(value["generations"]) != canonical([full_a, full_b, full_a]):
        raise ValueError("separate-process generation oracle mismatch")
    for full in (full_a, full_b):
        if not isinstance(full, dict) or not full.get("ast") or full.get("diagnostics") != []:
            raise ValueError("missing AST or nonempty/missing diagnostics")
        scope = "exact-three-module-local-bit-reference-fixture-only" if reference else "exact-three-module-constant-rhs-fixture-only"
        if full.get("scope") != scope:
            raise ValueError("unknown fixture scope")
    if reference:
        validate_reference_usage(full_a, b=False)
        validate_reference_usage(full_b, b=True)
        validate_reference_ledgers(value["reference_event_ledgers"], full_ledgers)
    assertions = value["assertions"]
    expected_assertions = REFERENCE_ASSERTIONS if reference else ASSERTIONS
    if not isinstance(assertions, dict) or set(assertions) != expected_assertions or any(v is not True for v in assertions.values()):
        raise ValueError("native assertions missing/failed")
    counters = value["counters"]
    if not isinstance(counters, dict) or set(counters) != COUNTERS:
        raise ValueError("native counters missing")
    for work in counters.values():
        if not isinstance(work, dict) or set(work) != WORK or any(type(v) is not int or v < 0 for v in work.values()):
            raise ValueError("native work counters invalid")
    control = counters["control_before"]
    if control != counters["control_after"] or any(control[key] <= 0 for key in WORK):
        raise ValueError("control semantic work missing or repeated")
    for key in (("expressions", "conversions", "lookups") if reference else ("expressions", "conversions")):
        if not 0 < counters["changed_before"][key] < counters["changed_after_b"][key] < counters["changed_after_a"][key]:
            raise ValueError("changed-leaf rebinding work not demonstrated")




def validate_reference_usage(snapshot, *, b: bool) -> None:
    """Check actual common stock/fork exports, not self-reported assertion booleans."""
    instances = snapshot.get("ast", {}).get("concrete_instances")
    if not isinstance(instances, list):
        raise ValueError("concrete instance reference evidence missing")
    paths = [item.get("path") for item in instances if isinstance(item, dict)]
    if len(paths) != len(instances) or len(set(paths)) != len(paths):
        raise ValueError("invalid or duplicate concrete instance")
    if set(paths) != {"top", "top.u0", "top.u2", "top.u1"}:
        raise ValueError("exact fixture instance inventory mismatch")
    by_path = {item["path"]: item for item in instances}
    for path in ("top.u0", "top.u2"):
        instance = by_path.get(path)
        if instance is None:
            raise ValueError("changed instance missing")
        if instance.get("usage_semantics") != "per-instance-AST-occurrences; native-flags-shared-declaration-aggregate":
            raise ValueError("reference usage aggregation semantics missing")
        expected = {
            f"{path}.a": (1 if b else 3, 0, True, False),
            f"{path}.b": (2 if b else 0, 0, b, False),
            f"{path}.y": (0, 1, False, True),
            f"{path}.k": (0, 1, False, True),
        }
        records = instance.get("reference_usage")
        if not isinstance(records, list) or len(records) != len(expected):
            raise ValueError("per-symbol usage missing")
        seen = set()
        for record in records:
            if not isinstance(record, dict) or set(record) != {"target", "read_occurrences", "write_occurrences", "native_read", "native_write"}:
                raise ValueError("invalid reference usage fields")
            target = record["target"]
            if not isinstance(target, str) or target not in expected or target in seen:
                raise ValueError("unknown/duplicate reference target")
            seen.add(target)
            if any(type(record[key]) is not int for key in ("read_occurrences", "write_occurrences")):
                raise ValueError("invalid occurrence count")
            if any(type(record[key]) is not bool for key in ("native_read", "native_write")):
                raise ValueError("invalid native usage flag")
            actual = tuple(record[key] for key in ("read_occurrences", "write_occurrences", "native_read", "native_write"))
            if actual != expected[target]:
                raise ValueError("reference occurrence/flag mismatch")
        targets = instance.get("resolved_targets")
        if not isinstance(targets, list) or any(not isinstance(target, str) for target in targets):
            raise ValueError("qualified targets missing")
        expected_targets = Counter({target: values[0] + values[1] for target, values in expected.items()
                                    if values[0] + values[1]})
        if Counter(targets) != expected_targets:
            raise ValueError("qualified target occurrence mismatch")



def validate_reference_ledgers(generations, full_ledgers) -> None:
    if not isinstance(full_ledgers, list) or len(full_ledgers) != 2:
        raise ValueError("separate-process native event oracle missing")
    expected = [full_ledgers[0], full_ledgers[1], full_ledgers[0]]
    if json.dumps(generations, sort_keys=True) != json.dumps(expected, sort_keys=True):
        raise ValueError("separate-process native event ledger mismatch")
    maps = []
    for records in full_ledgers:
        if not isinstance(records, list) or not records:
            raise ValueError("native event ledger empty")
        counts = {}
        for record in records:
            if not isinstance(record, dict) or set(record) != {"target", "read_events", "write_events"}:
                raise ValueError("native event ledger fields invalid")
            target = record["target"]
            if not isinstance(target, str) or not target or target in counts:
                raise ValueError("native event ledger target invalid/duplicated")
            if any(type(record[key]) is not int or record[key] < 0 for key in ("read_events", "write_events")):
                raise ValueError("native event count invalid")
            counts[target] = (record["read_events"], record["write_events"])
        maps.append(counts)
    a, b = maps
    if a.keys() != b.keys():
        raise ValueError("native event target set changed")
    changed = {f"{path}.{symbol}" for path in ("top.u0", "top.u2") for symbol in ("a", "b")}
    if not changed <= a.keys():
        raise ValueError("changed native event targets missing")
    for target in a:
        if a[target][1] != b[target][1] or (target not in changed and a[target][0] != b[target][0]):
            raise ValueError("unaffected native reference events changed")
    for path in ("top.u0", "top.u2"):
        ar, br = f"{path}.a", f"{path}.b"
        if not 0 < b[ar][0] < a[ar][0] or a[br][0] != 0 or b[br][0] <= 0:
            raise ValueError("native owner withdrawal/shared contribution preservation missing")
        if a[ar][0] - b[ar][0] != b[br][0]:
            raise ValueError("withdrawn and rebound native multiplicities differ")


def isolation_guard(source: Path, canary: Path) -> str:
    # Direct multiline code: nested exec/string escaping would defer syntax bugs until CI.
    return f"""import os
import pathlib
import tempfile
p = pathlib.Path({str(source)!r})
assert p.is_file(), "fixture is not readable"
assert not pathlib.Path({str(canary)!r}).exists(), "host canary visible"
assert set(os.environ) <= {{'PATH', 'LANG', 'PYTHONDONTWRITEBYTECODE', 'LC_CTYPE'}}, "unexpected environment"
assert len(pathlib.Path('/proc/net/route').read_text().splitlines()) == 1, "network route visible"
with tempfile.NamedTemporaryFile() as temporary:
    temporary.write(b'private tmp')
    temporary.flush()
try:
    p.open('ab').close()
except OSError:
    pass
else:
    raise AssertionError('source writable')
print('ISOLATION_PASS')
"""


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--sources", type=Path, required=True)
    parser.add_argument("--state", type=Path, required=True)
    args = parser.parse_args()
    sources, state = args.sources.resolve(), args.state.resolve()
    state.mkdir(parents=True, exist_ok=False)
    evidence = state / "evidence"
    evidence.mkdir()
    report = {"status": "NOT_RUN", "scope": "experimental native expression invalidation only",
              "production_pin_changed": False, "general_g0": "NOT_ESTABLISHED",
              "github_sha": os.environ.get("GITHUB_SHA"),
              "lock_sha256": digest(REPO / "toolchain.lock.json"),
              "workflow_sha256": digest(REPO / ".github/workflows/ci.yml"),
              "platform": platform.platform(), "steps": [], "build_flags": BUILD_FLAGS,
              "patch_sha256": None, "oracle_sha256": None, "sources": None}

    def run_step(name, spec, argv, *, timeout=180, rss_limit=3 * 1024**3):
        progress(name, "started", timeout_seconds=timeout, rss_limit_bytes=rss_limit)
        try:
            result = execute(spec, argv, timeout=timeout, rss_limit=rss_limit, stage=name)
        except Exception as exc:
            progress(name, "blocked", error_type=type(exc).__name__)
            raise
        progress(name, "finished", returncode=result["returncode"], failures=result["failures"],
                 elapsed_seconds=result["wall_seconds"],
                 peak_sampled_rss_bytes=result["peak_sampled_process_tree_rss_bytes"])
        (evidence / f"{name}.json").write_text(json.dumps(result, indent=2) + "\n")
        report["steps"].append({"name": name, "returncode": result["returncode"],
                                "failures": result["failures"]})
        if result["returncode"] or result["failures"]:
            raise RuntimeError(f"{name} failed; inspect {name}.json")
        return result["stdout"]

    try:
        report["patch_sha256"] = digest(HERE / "core.patch")
        report["oracle_sha256"] = digest(HERE / "LeafUpdate.cpp")
        report["sources"] = strict_json((sources / "sources.json").read_text())
        if report["sources"] != strict_json((HERE / "sources.json").read_text()):
            raise ValueError("installed source manifest differs from reviewed manifest")
        # Exercise actual isolation, not merely argv construction. No native fallback.
        canary = state / "host-only-canary"
        canary.write_text("synthetic-isolation-canary")
        guard = isolation_guard(HERE / "LeafUpdate.cpp", canary)
        guard_spec = probe_spec(sources=(HERE / "LeafUpdate.cpp",))
        run_step("isolation-check", guard_spec, ["/usr/bin/python3", "-c", guard])
        # Even toolchain identity commands are sandboxed.
        tool_spec = probe_spec()
        for tool in ("g++", "cmake", "ninja"):
            run_step(f"version-{tool}", tool_spec, [f"/usr/bin/{tool}", "--version"])
        patched = state / "patched"
        shutil.copytree(sources / "slang", patched)
        patch_spec = probe_spec(tools=(HERE / "core.patch",), writable=(patched,), cwd=patched)
        run_step("apply-patch", patch_spec,
                 ["/usr/bin/patch", "--batch", "--fuzz=0", "-p1", "-i", str(HERE / "core.patch")])
        binaries = {}
        for variant, tree in (("stock", sources / "slang"), ("patched", patched)):
            build = state / f"build-{variant}"
            build.mkdir()
            spec = probe_spec(sources=(tree, HERE, sources / "fmt", sources / "boost_regex",
                                       sources / "tomlplusplus"), writable=(build,), cwd=build)
            argv = ["/usr/bin/cmake", "-S", str(HERE), "-B", str(build), *BUILD_FLAGS,
                    f"-DSLANG_SOURCE={tree}", f"-DPCU_STOCK={'ON' if variant == 'stock' else 'OFF'}"]
            for dep in ("fmt", "boost_regex", "tomlplusplus"):
                argv.append(f"-DFETCHCONTENT_SOURCE_DIR_{dep.upper()}={sources / dep}")
            run_step(f"configure-{variant}", spec, argv)
            run_step(f"build-{variant}", spec,
                     ["/usr/bin/cmake", "--build", str(build), "--target", "pcu_leaf_oracle", "-j", "2"],
                     timeout=3600, rss_limit=10 * 1024**3)
            binaries[variant] = build / "pcu_leaf_oracle"
        report["binary_sha256"] = {key: digest(path) for key, path in binaries.items()}
        for reference in (False, True):
            prefix = "reference-" if reference else ""
            outputs = {}
            for variant, binary in binaries.items():
                spec = probe_spec(tools=(binary,), sources=(HERE / "LeafUpdate.cpp",))
                for version in ("a", "b"):
                    outputs[variant, version] = run_step(f"{prefix}full-{variant}-{version}", spec,
                                                        [str(binary), f"--{prefix}full", version])
            for version in ("a", "b"):
                if not outputs["stock", version].strip() or outputs["stock", version] != outputs["patched", version]:
                    raise RuntimeError(f"{prefix}stock/fork full oracle mismatch: {version}")
            binary = binaries["patched"]
            spec = probe_spec(tools=(binary,), sources=(HERE / "LeafUpdate.cpp",))
            result = run_step(f"{prefix}incremental", spec, [str(binary), f"--{prefix}incremental"])
            incremental = strict_json(result)
            full_ledgers = None
            if reference:
                full_ledgers = [strict_json(run_step(f"reference-ledger-full-{version}", spec,
                                [str(binary), "--reference-ledger-full", version])) for version in ("a", "b")]
            validate_incremental(incremental, strict_json(outputs["patched", "a"]),
                                 strict_json(outputs["patched", "b"]), reference=reference,
                                 full_ledgers=full_ledgers)
            report[f"{prefix}incremental"] = incremental
            report[f"{prefix}stock_vs_patched_full"] = "PASS"
        report["status"] = "REFERENCE_MICRO_PROTOTYPE_PASS"
    except Exception as exc:
        report["status"] = "FAIL"
        report["error"] = f"{type(exc).__name__}: {exc}"
    finally:
        (evidence / "report.json").write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps(report, indent=2))
    return 0 if report["status"] == "REFERENCE_MICRO_PROTOTYPE_PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
