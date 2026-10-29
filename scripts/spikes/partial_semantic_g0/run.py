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


def execute(spec, argv: list[str], *, timeout: int, rss_limit: int,
            output_limit: int = 8 * 1024 * 1024) -> dict:
    command = wrap(spec, argv)  # No subprocess exists until mandatory isolation succeeds.
    start = time.monotonic()
    proc = subprocess.Popen(command.argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, env=LAUNCH_ENV, start_new_session=True)
    buffers = [bytearray(), bytearray()]
    failures = []
    peak_rss = 0

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


def validate_incremental(value, full_a, full_b) -> None:
    if not isinstance(value, dict) or set(value) != {"status", "general_g0", "generations", "assertions", "counters"}:
        raise ValueError("unknown or missing native oracle fields")
    if value["status"] != "MICRO_PROTOTYPE_PASS" or value["general_g0"] != "NOT_ESTABLISHED":
        raise ValueError("native oracle status/scope invalid")
    if full_a == full_b or value["generations"] != [full_a, full_b, full_a]:
        raise ValueError("separate-process generation oracle mismatch")
    for full in (full_a, full_b):
        if not isinstance(full, dict) or not full.get("ast") or full.get("diagnostics") != []:
            raise ValueError("missing AST or nonempty/missing diagnostics")
        if full.get("scope") != "exact-three-module-constant-rhs-fixture-only":
            raise ValueError("unknown fixture scope")
    assertions = value["assertions"]
    if not isinstance(assertions, dict) or set(assertions) != ASSERTIONS or any(v is not True for v in assertions.values()):
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
    for key in ("expressions", "conversions"):
        if not 0 < counters["changed_before"][key] < counters["changed_after_b"][key] < counters["changed_after_a"][key]:
            raise ValueError("changed-leaf rebinding work not demonstrated")



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
        result = execute(spec, argv, timeout=timeout, rss_limit=rss_limit)
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
        outputs = {}
        for variant, binary in binaries.items():
            spec = probe_spec(tools=(binary,), sources=(HERE / "LeafUpdate.cpp",))
            for version in ("a", "b"):
                outputs[variant, version] = run_step(f"full-{variant}-{version}", spec,
                                                    [str(binary), "--full", version])
        for version in ("a", "b"):
            if not outputs["stock", version].strip() or outputs["stock", version] != outputs["patched", version]:
                raise RuntimeError(f"stock/fork full oracle mismatch: {version}")
        binary = binaries["patched"]
        spec = probe_spec(tools=(binary,), sources=(HERE / "LeafUpdate.cpp",))
        result = run_step("incremental", spec, [str(binary), "--incremental"])
        incremental = strict_json(result)
        validate_incremental(incremental, strict_json(outputs["patched", "a"]),
                             strict_json(outputs["patched", "b"]))
        report["incremental"] = incremental
        report["stock_vs_patched_full"] = "PASS"
        report["status"] = "MICRO_PROTOTYPE_PASS"
    except Exception as exc:
        report["status"] = "FAIL"
        report["error"] = f"{type(exc).__name__}: {exc}"
    finally:
        (evidence / "report.json").write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps(report, indent=2))
    return 0 if report["status"] == "MICRO_PROTOTYPE_PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
