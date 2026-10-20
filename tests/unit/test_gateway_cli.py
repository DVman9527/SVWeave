"""CLI exit codes per command (plan 15.4), `serve` startup errors, and DOC-01."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

import click
from click.testing import CliRunner

from svweave.cli import cli
from tests.conftest import Ws
from tests.unit._gateway_fx import record, stub_build, write_manifest

REPO = Path(__file__).resolve().parents[2]
H = "0" * 64


def run(root: Path, *args: str) -> tuple[int, dict[str, Any]]:
    r = CliRunner().invoke(cli, ["--root", str(root), "--compact", *args])
    assert r.stdout.strip(), (args, r.output)
    return r.exit_code, json.loads(r.stdout)


def test_exit_codes_per_command(ws: Ws, tmp_path: Path) -> None:
    opts, bid, sid = stub_build(ws)
    root = ws.root
    good = write_manifest(tmp_path, record(build_id=bid, snapshot_id=sid), "good.json")
    bad_schema = write_manifest(tmp_path, record(result="MAYBE"), "bad.json")
    escape = write_manifest(
        tmp_path, record(artifacts=[{"path": "/etc/hostname", "sha256": H}]), "esc.json"
    )
    table: list[tuple[list[str], int, str | None]] = [
        (["doctor"], 0, None),
        (["cache", "gc"], 0, None),
        (["evidence", "import", "--manifest", str(good)], 0, None),
        (["evidence", "import", "--manifest", str(bad_schema)], 2, "INVALID_CONFIG"),
        (["evidence", "import", "--manifest", str(tmp_path / "nope.json")], 3, "MISSING_INPUT"),
        (["evidence", "import", "--manifest", str(escape)], 6, "PATH_NOT_ALLOWED"),
        (["query", "evidence", "--build", bid], 0, None),
        (["query", "evidence"], 2, "INVALID_CONFIG"),
        (["query", "evidence", "--build", H], 3, "MISSING_INPUT"),
        (["query", "hierarchy", "--build", "xyz"], 2, "INVALID_CONFIG"),
        (["query", "hierarchy", "--build", H], 3, "MISSING_INPUT"),
        (["query", "search", "--snapshot", H, "--query", "x", "--no-graph"], 3, "MISSING_INPUT"),
        (
            ["query", "search", "--snapshot", "nothex", "--query", "x", "--no-graph"],
            2,
            "INVALID_CONFIG",
        ),
        (["query", "search", "--snapshot", sid, "--query", "module", "--no-graph"], 0, None),
        (
            ["query", "search", "--snapshot", sid, "--query", "x", "--no-graph", "--limit", "9999"],
            2,
            "INVALID_CONFIG",
        ),
        (
            [
                "query",
                "search",
                "--snapshot",
                sid,
                "--query",
                "x",
                "--no-graph",
                "--cursor",
                "garbage.token",
            ],
            2,
            "INVALID_CONFIG",
        ),
        (["report", "--build", H], 3, "MISSING_INPUT"),
        (["report", "--build", bid, "--format", "json"], 0, None),
        (["debug", "bundle", "--build", H], 3, "MISSING_INPUT"),
        (["debug", "bundle", "--build", bid], 0, None),
        (["snapshot", "check", "--snapshot", sid], 0, None),
    ]
    for args, want, code in table:
        got, env = run(root, *args)
        assert got == want, (args, got, env.get("error"))
        assert (env["error"]["code"] if env["error"] else None) == code, args
    # stale snapshot -> 5
    ws.write("rtl/a.sv", "module a; // changed\nendmodule\n")
    got, env = run(root, "snapshot", "check", "--snapshot", sid)
    assert got == 5 and env["freshness"] == "STALE"


def test_build_blocked_exit_3_and_evidence_not_recorded(neg: Ws) -> None:
    got, env = run(neg.root, "build", "--profile", "p")
    assert got == 3 and env["outcome"] in ("BLOCKED", "ERROR")


def test_text_search_via_cli_is_partial_degraded_and_exit_0(ws: Ws) -> None:
    opts, bid, sid = stub_build(ws)
    code, env = run(
        ws.root, "query", "search", "--snapshot", sid, "--query", "module", "--no-graph"
    )
    assert code == 0 and env["outcome"] == "PARTIAL"
    assert env["data"]["backend_used"] == "text_fallback" and env["data"]["degraded"] is True
    assert env["data"]["items"] and env["snapshot_id"] == sid and env["freshness"] == "CURRENT"
    assert env["provenance"]["evidence_kind"] == "SOURCE_CANDIDATE"


def test_serve_fails_fast_on_bad_config_with_clean_stdout(tmp_path: Path) -> None:
    r = CliRunner().invoke(cli, ["--root", str(tmp_path), "serve", "--no-graph"])
    assert r.exit_code != 0
    assert r.stdout == ""  # nothing but protocol may ever reach stdout
    assert "serve" in (r.stderr or r.output)


def test_serve_workspace_option_and_unsupported_transport(tmp_path: Path) -> None:
    r = CliRunner().invoke(cli, ["serve", "--transport", "http"])
    assert r.exit_code == 2
    r = CliRunner().invoke(cli, ["serve", "--workspace", str(tmp_path / "empty"), "--no-graph"])
    assert r.exit_code != 0 and r.stdout == ""


def test_schema_emit_check_covers_mcp_and_evidence_schemas(tmp_path: Path) -> None:
    code, env = run(REPO, "schema", "emit", "--check")
    assert code == 0 and env["data"]["out_of_sync"] == []
    out = tmp_path / "s"
    run(REPO, "schema", "emit", "--out", str(out))
    assert (out / "evidence.schema.json").is_file()
    assert len(list((out / "mcp").glob("*.json"))) == 26
    (out / "mcp" / "svw_status.input.json").write_text("{}")
    code, env = run(REPO, "schema", "emit", "--check", "--out", str(out))
    assert code == 2 and "mcp/svw_status.input.json" in env["data"]["out_of_sync"]


# ------------------------------------------------------------------------------ DOC-01
CMD = re.compile(r"(?<![\w/.-])svweave((?:\s+(?!-)[a-z][a-z0-9-]*)*)")


def doc_commands(text: str) -> list[tuple[int, list[str]]]:
    """`svweave ...` command paths in code spans/blocks, skipping blocks marked planned-m4."""
    out: list[tuple[int, list[str]]] = []
    in_fence, skip = False, False
    for n, line in enumerate(text.splitlines(), 1):
        if line.startswith("```"):
            if not in_fence:
                in_fence, skip = True, "planned" in line
            else:
                in_fence = skip = False
            continue
        if skip:
            continue
        segs = [line] if in_fence else re.findall(r"`([^`]+)`", line)
        for seg in segs:
            for m in CMD.finditer(seg):
                words = m.group(1).split()
                if words or seg.strip().startswith("svweave"):
                    out.append((n, words))
    return out


def resolves(words: list[str]) -> bool:
    cur: click.Command = cli
    for w in words:
        if not isinstance(cur, click.Group):
            return True  # remaining words are positional prose
        nxt = cur.commands.get(w)
        if nxt is None:
            return False
        cur = nxt
    return True


def test_DOC_01_documented_commands_exist() -> None:
    docs = sorted((REPO / "docs").glob("*.md"))
    docs = [d for d in docs if "adr" not in d.parts and d.name != "m0_spike_report.md"]
    for readme_name in ("README.md", "README.zh-TW.md", "README.zh-CN.md"):
        readme = REPO / readme_name
        if readme.exists():
            docs.append(readme)
    seen = 0
    missing = []
    for d in docs:
        for n, words in doc_commands(d.read_text()):
            seen += 1
            if words and not resolves(words):
                missing.append(f"{d.name}:{n}: svweave {' '.join(words)}")
    assert seen > 15
    assert not missing, missing


def test_DOC_01_edit_commands_are_documented_and_exist() -> None:
    txt = (REPO / "docs" / "operations.md").read_text()
    documented = {" ".join(w) for _, w in doc_commands(txt)}
    for c in ("edit prepare", "edit validate", "edit approve", "edit commit", "edit abort",
              "edit recover"):
        assert c in documented and resolves(c.split()), c
