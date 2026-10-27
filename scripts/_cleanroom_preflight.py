"""DEP-01 diagnostics for mandatory backend isolation inside the offline setup namespace.

This controller does not alter a policy, retry a denied native backend, or allow degraded mode.
The caller supplies the existing network-disabled transport; only the standard host-capability
probe runs in the child, before any source-analyzing backend is started.
"""
from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path
from subprocess import CompletedProcess
from typing import Any

PREFLIGHT_CODE = """
import json,os
from dataclasses import asdict
from pathlib import Path
from svweave.security.sandbox import probe_host

def read(path):
    try:
        return Path(path).read_text().strip()
    except OSError as error:
        return type(error).__name__ + ': ' + str(error)

def namespace(name):
    try:
        return os.readlink('/proc/self/ns/' + name)
    except OSError as error:
        return type(error).__name__ + ': ' + str(error)

mounts = []
for line in read('/proc/self/mountinfo').splitlines():
    fields = line.split()
    if len(fields) > 6 and fields[4] in ('/', '/proc', '/usr') and '-' in fields:
        separator = fields.index('-')
        mounts.append({'mountpoint': fields[4], 'options': fields[5],
                       'optional_fields': fields[6:separator],
                       'filesystem': fields[separator + 1], 'source': fields[separator + 2]})
capability = probe_host(refresh=True)
print(json.dumps({'capability': asdict(capability),
                  'namespaces': {name: namespace(name) for name in ('user', 'pid', 'net', 'mnt')},
                  'uid_map': read('/proc/self/uid_map'), 'gid_map': read('/proc/self/gid_map'),
                  'process_label': read('/proc/self/attr/current'), 'mounts': mounts}), flush=True)
raise SystemExit(0 if capability.available else 1)
"""


def collect_nested_preflight(
    python: Path,
    room: Path,
    run_netless: Callable[[list[str], Path, float], CompletedProcess[str]],
) -> dict[str, Any]:
    result = run_netless([str(python), "-I", "-c", PREFLIGHT_CODE], room, 60)
    report: dict[str, Any] = {"returncode": result.returncode, "stderr": result.stderr[-4000:]}
    try:
        payload = json.loads(result.stdout)
        if not isinstance(payload, dict):
            raise ValueError("preflight result is not an object")
        report["observation"] = payload
    except (ValueError, TypeError) as error:
        report["parse_error"] = f"{type(error).__name__}: {error}"
        report["stdout_tail"] = result.stdout[-4000:]
    capability = report.get("observation", {}).get("capability", {})
    report["ready"] = result.returncode == 0 and isinstance(capability, dict) and capability.get("available") is True
    return report
