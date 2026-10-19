"""HDL worker process: ``python -m svweave.adapters.slang.worker``.

Protocol: one JSON WorkerRequest on stdin, one JSON result document on stdout, logs on stderr
only. The process runs a single build and exits; a native crash therefore kills only this
process (plan 6/7, API-03).
"""

from __future__ import annotations

import json
import os
import resource
import shlex
import signal
import subprocess
import sys
import tempfile
import time
from typing import Any

from svweave.adapters.slang.protocol import (
    DTO_SCHEMA,
    EXTRACTOR_VERSION,
    RUSAGE_MARKER,
    WorkerRequest,
)


def _inject_fault(kind: str) -> None:
    """Test-only fault injection. Only reachable through WorkerRequest.test_fault, which the
    runner sets exclusively from an explicit keyword argument (never from configuration)."""
    if kind == "abort":
        os.abort()
    elif kind == "segv":
        os.kill(os.getpid(), signal.SIGSEGV)
    elif kind == "exit3":
        os._exit(3)
    elif kind == "hang":
        child = subprocess.Popen(["sleep", "3600"])  # a descendant that must be reaped with us
        print(f"FAULT_CHILD_PID={child.pid}", file=sys.stderr, flush=True)
        time.sleep(3600)
    elif kind == "grandchild":
        # a descendant that outlives a NORMAL worker exit; the runner must still reap the group
        child = subprocess.Popen(["sleep", "3600"])
        print(f"FAULT_CHILD_PID={child.pid}", file=sys.stderr, flush=True)
    elif kind == "oom":
        chunks = []
        while True:
            chunks.append(bytearray(256 * 1024 * 1024))


def build_and_extract(req: WorkerRequest) -> dict[str, Any]:
    import pyslang
    from pyslang import driver as drv

    from svweave.adapters.slang.extract import Extractor

    os.chdir(req.snapshot_root)
    # slang's driver writes option/file errors straight to fd 2: capture them.
    saved = os.dup(2)
    tmp = tempfile.TemporaryFile()  # noqa: SIM115 - closed in the finally block below
    os.dup2(tmp.fileno(), 2)
    try:
        d = drv.Driver()
        d.addStandardArgs()
        line = " ".join(shlex.quote(a) for a in ["slang", *req.args])
        ok_cmd = bool(d.parseCommandLine(line, drv.CommandLineOptions()))
        ok_opt = bool(d.processOptions()) if ok_cmd else False
        ok_parse = bool(d.parseAllSources()) if ok_cmd else False
        sys.stderr.flush()
        tmp.seek(0)
        messages = [m for m in tmp.read().decode("utf-8", "replace").splitlines() if m.strip()]
    finally:
        os.dup2(saved, 2)
        os.close(saved)
        tmp.close()
    status = {
        "parseCommandLine": ok_cmd,
        "processOptions": ok_opt,
        "parseAllSources": ok_parse,
        "pyslang_version": getattr(pyslang, "__version__", ""),
    }
    if not ok_cmd:
        return {
            "schema_version": DTO_SCHEMA,
            "extractor_version": EXTRACTOR_VERSION,
            "pyslang_version": getattr(pyslang, "__version__", ""),
            "status": {**status, "compiled": False},
            "driver_messages": messages,
            "diagnostics": [],
            "dependencies": {},
            "declarations": [],
            "references": [],
            "instances": [],
            "port_bindings": [],
            "assertions": [],
            "specializations": [],
            "inactive": [],
            "gaps": [],
            "unsupported_nodes": [],
            "stats": {},
        }
    comp = d.createCompilation()
    ex = Extractor(d, comp, req, messages)
    result = ex.run()
    result["status"] = {**status, "compiled": True}
    return result


def _rusage_line() -> str:
    me = resource.getrusage(resource.RUSAGE_SELF)
    ch = resource.getrusage(resource.RUSAGE_CHILDREN)
    doc = {
        "maxrss_kib": max(me.ru_maxrss, ch.ru_maxrss),  # KiB on Linux
        "user_s": round(me.ru_utime + ch.ru_utime, 3),
        "sys_s": round(me.ru_stime + ch.ru_stime, 3),
    }
    return RUSAGE_MARKER + json.dumps(doc, separators=(",", ":"))


def main() -> int:
    raw = sys.stdin.read()
    req = WorkerRequest.model_validate_json(raw)
    if req.test_fault:
        _inject_fault(req.test_fault)
    t0 = time.monotonic()
    result = build_and_extract(req)
    result["stats"]["worker_wall_s"] = round(time.monotonic() - t0, 3)
    sys.stdout.write(json.dumps(result, ensure_ascii=False, separators=(",", ":")))
    sys.stdout.flush()
    print(f"worker done: {result['stats']}", file=sys.stderr)
    print(_rusage_line(), file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
