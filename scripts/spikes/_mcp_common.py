"""Shared helpers for the M0 MCP spikes (real backends, official `mcp` Python SDK client).

Not a product adapter: this only records what the upstream servers actually return.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parents[2]
TOOLCHAIN = REPO / ".toolchain"
STATE = TOOLCHAIN / "state"


def sha256_bytes(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


def sha256_file(p: Path) -> str:
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def canonical_json(obj: Any) -> bytes:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def tree_manifest(root: Path) -> dict[str, str]:
    """relative path -> sha256 for every regular file under root (symlinks recorded, not followed)."""
    out: dict[str, str] = {}
    for p in sorted(root.rglob("*")):
        rel = p.relative_to(root).as_posix()
        if p.is_symlink():
            out[rel] = "symlink:" + os.readlink(p)
        elif p.is_file():
            out[rel] = sha256_file(p)
    return out


def make_copy(src_dirs: list[Path], dest_root: Path) -> dict[str, str]:
    """Fresh copy of fixture dirs into dest_root (deleted first). Returns manifest."""
    if dest_root.exists():
        shutil.rmtree(dest_root)
    dest_root.mkdir(parents=True)
    for d in src_dirs:
        shutil.copytree(d, dest_root / d.name, symlinks=True)
    return tree_manifest(dest_root)


def dump_model(m: Any) -> Any:
    """Pydantic model -> JSON-able dict using wire aliases (camelCase)."""
    if hasattr(m, "model_dump"):
        return m.model_dump(mode="json", by_alias=True, exclude_none=True)
    return m


def tools_digest(tools: list[dict]) -> dict:
    """Per-tool and whole-list SHA-256 over canonical JSON of name+inputSchema(+outputSchema)."""
    per = {}
    for t in tools:
        core = {"name": t.get("name"), "inputSchema": t.get("inputSchema"),
                "outputSchema": t.get("outputSchema")}
        per[t["name"]] = sha256_bytes(canonical_json(core))
    whole = sha256_bytes(canonical_json(sorted(tools, key=lambda t: t["name"])))
    return {"tools_list_sha256": whole, "per_tool_schema_sha256": per}


def result_text(res: Any) -> str:
    """Concatenate text content blocks of a CallToolResult."""
    parts = []
    for c in getattr(res, "content", []) or []:
        t = getattr(c, "text", None)
        if t is not None:
            parts.append(t)
    return "\n".join(parts)


def parse_json_text(s: str) -> Any:
    try:
        return json.loads(s)
    except Exception:
        return None


@asynccontextmanager
async def mcp_session(command: str, args: list[str], env: dict[str, str], cwd: str | None,
                      errlog_path: Path, read_timeout: float = 120.0):
    """Spawn an MCP stdio server and yield (session, init_result_dict, timings)."""
    import mcp_types as types
    from mcp.client.session import ClientSession
    from mcp.client.stdio import StdioServerParameters, stdio_client

    errlog_path.parent.mkdir(parents=True, exist_ok=True)
    errlog = open(errlog_path, "w")
    params = StdioServerParameters(command=command, args=args, env=env, cwd=cwd)
    t0 = time.monotonic()
    try:
        async with stdio_client(params, errlog=errlog) as (r, w):
            async with ClientSession(r, w, read_timeout_seconds=read_timeout,
                                     client_info=types.Implementation(name="svweave-m0-spike",
                                                                      version="0.0.0")) as s:
                init = await s.initialize()
                timings = {"initialize_s": round(time.monotonic() - t0, 3)}
                yield s, dump_model(init), timings
    finally:
        errlog.close()


async def list_all_tools(session) -> list[dict]:
    import mcp_types as types
    tools: list[dict] = []
    cursor = None
    while True:
        res = await session.list_tools(params=types.PaginatedRequestParams(cursor=cursor) if cursor else None)
        tools.extend(dump_model(t) for t in res.tools)
        cursor = res.next_cursor
        if not cursor:
            break
    return tools


async def call(session, name: str, arguments: dict, timeout: float = 120.0) -> dict:
    t0 = time.monotonic()
    try:
        res = await session.call_tool(name, arguments, read_timeout_seconds=timeout)
        txt = result_text(res)
        return {"tool": name, "arguments": arguments, "ok": not bool(getattr(res, "is_error", False)),
                "is_error": bool(getattr(res, "is_error", False)), "elapsed_s": round(time.monotonic() - t0, 3),
                "text": txt, "json": parse_json_text(txt),
                "structured": getattr(res, "structured_content", None)}
    except Exception as e:  # record raw failure; never hide it
        return {"tool": name, "arguments": arguments, "ok": False, "exception": f"{type(e).__name__}: {e}",
                "elapsed_s": round(time.monotonic() - t0, 3)}


def write_json(path: Path, obj: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj, indent=2, default=str, ensure_ascii=False) + "\n")


def cbm_env(state: Path | None = None) -> dict[str, str]:
    s = state or (STATE / "cbm")
    return {
        "PATH": "/usr/bin:/bin",
        "HOME": str(s / "home"),
        "XDG_CONFIG_HOME": str(s / "home/.config"),
        "XDG_CACHE_HOME": str(s / "cache"),
        "CBM_CACHE_DIR": str(s / "cache"),
        "CBM_RUNTIME_DIR": str(s / "runtime"),
        "LANG": "C.UTF-8",
    }


def serena_env(state: Path | None = None) -> dict[str, str]:
    s = state or (STATE / "serena")
    # pinned Verible FIRST on PATH so SolidLSP never downloads (systemverilog_server.py checks PATH first)
    return {
        "PATH": f"{TOOLCHAIN / 'verible/bin'}:{TOOLCHAIN / 'serena-venv/bin'}:/usr/bin:/bin",
        "HOME": str(s / "home"),
        "SERENA_HOME": str(s / "home/.serena"),
        "XDG_CONFIG_HOME": str(s / "home/.config"),
        "XDG_CACHE_HOME": str(s / "cache"),
        "XDG_DATA_HOME": str(s / "data"),
        "LANG": "C.UTF-8",
        "PYTHONNOUSERSITE": "1",
        "UV_NO_SYNC": "1",
    }


def proc_tree_hwm(root_pid: int | None = None) -> list[dict]:
    """Peak RSS (VmHWM, KiB) of every live descendant process of root_pid (default: this process).

    Call while the backend is still running (inside the session) to attribute memory per process.
    """
    root_pid = root_pid or os.getpid()
    children: dict[int, list[int]] = {}
    info: dict[int, dict] = {}
    for d in os.listdir("/proc"):
        if not d.isdigit():
            continue
        try:
            st = open(f"/proc/{d}/stat").read()
            ppid = int(st.rsplit(")", 1)[1].split()[1])
            status = open(f"/proc/{d}/status").read()
            hwm = next((int(l.split()[1]) for l in status.splitlines() if l.startswith("VmHWM:")), None)
            cmd = open(f"/proc/{d}/cmdline", "rb").read().replace(b"\0", b" ").decode(errors="replace")[:300]
        except Exception:
            continue
        children.setdefault(ppid, []).append(int(d))
        info[int(d)] = {"pid": int(d), "ppid": ppid, "vm_hwm_kib": hwm, "cmd": cmd}
    out, stack = [], list(children.get(root_pid, []))
    while stack:
        p = stack.pop()
        if p in info:
            out.append(info[p])
        stack.extend(children.get(p, []))
    return out


def write_verible_filelist(project: Path) -> list[str]:
    """Write <project>/verible.filelist listing every .sv/.svh/.v/.vh (project-relative, sorted).

    QUIRK (M0): without this file verible-verilog-ls only resolves cross-file references into files
    it has already opened in the session, so Serena's find_referencing_symbols silently returns {}
    for callers in unopened files (and Serena's symbol cache means later sessions don't reopen them).
    """
    files = sorted(p.relative_to(project).as_posix() for p in project.rglob("*")
                   if p.is_file() and p.suffix in (".sv", ".svh", ".v", ".vh"))
    (project / "verible.filelist").write_text("\n".join(files) + "\n")
    return files
