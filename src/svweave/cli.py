"""`svweave` command line. Thin: parses argv, calls application use cases, prints envelopes.

stdout carries exactly one JSON envelope per invocation; exit codes follow plan 15.4.
"""

from __future__ import annotations

import functools
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

import click

from svweave.application import services
from svweave.application.doctor import run_doctor
from svweave.application.init import init_workspace
from svweave.application.options import WorkspaceOptions
from svweave.application.runner import execute, exit_code
from svweave.application.session import Session
from svweave.application.use_cases import (
    cache_gc,
    check_snapshot,
    create_snapshot,
    resolve_manifest,
)
from svweave.config.schemas import check_schemas, emit_schemas
from svweave.domain.cursor import MAX_RESPONSE_BYTES
from svweave.domain.envelope import AnalysisCoverage, Envelope, Outcome
from svweave.domain.errors import EXIT_BUILD, EXIT_CONFIG, EXIT_OK, EXIT_STALE, SvwError
from svweave.domain.errors import ErrorCode as EC
from svweave.domain.states import Completeness, Freshness
from svweave.gateway.schemas import check_mcp_schemas, emit_mcp_schemas


class _State:
    def __init__(self, opts: WorkspaceOptions, request_id: str | None, compact: bool) -> None:
        self.opts = opts
        self.request_id = request_id
        self.compact = compact
        #: (allow_stale, allow_partial) while a query command runs, else None
        self.query_policy: tuple[bool, bool] | None = None

    def session(self, *, use_graph: bool = False) -> Session:
        return Session(self.opts, use_graph=use_graph)

    def emit(self, env: Envelope, code: int | None = None) -> None:
        from svweave.gateway.tools import redact_envelope
        from svweave.security.redaction import Redactor

        env = redact_envelope(env, Redactor([self.opts.root]))
        text = env.to_json(indent=None if self.compact else 2)
        if len(text.encode("utf-8")) > MAX_RESPONSE_BYTES:
            # the 128 KiB cap applies to the bytes actually printed: pretty indentation can push a
            # page over it, so fall back to the compact form the services sized the page for (M4)
            text = env.to_json(indent=None)
        click.echo(text)
        if code is None and self.query_policy is not None:
            code = query_exit_code(env, *self.query_policy)
        sys.exit(exit_code(env) if code is None else code)

    def run(self, fn: Callable[[str], Envelope], *, profile: str | None = None) -> Envelope:
        return execute(
            fn,
            request_id=self.request_id,
            profile=profile,
            workspace_id=self.opts.workspace_id(),
        )


pass_state = click.make_pass_decorator(_State)


def query_exit_code(env: Envelope, allow_stale: bool, allow_partial: bool) -> int | None:
    """Plan 15.4 for queries: a STALE/HISTORICAL build exits 5 and a PARTIAL (or BLOCKED) one
    exits 3 unless the user explicitly allows it; the structured result carries the state either
    way. Errors keep their own mapping (None)."""
    if env.error is not None:
        return None
    if env.freshness in (Freshness.STALE, Freshness.HISTORICAL) and not allow_stale:
        return EXIT_STALE
    cov = env.analysis_coverage
    if cov is not None and cov.completeness == Completeness.BLOCKED:
        return EXIT_BUILD
    if cov is not None and cov.completeness == Completeness.PARTIAL and not allow_partial:
        return EXIT_BUILD
    return None


def _diff_flags(f: Callable[..., Any]) -> Callable[..., Any]:
    """Like :func:`_query_flags` for ``diff-impact``: a diff is read against a workspace that has
    changed since the build, so STALE is the expected state and never changes the exit code (the
    envelope still reports ``freshness``). ``--allow-partial`` keeps its meaning."""

    @click.option("--allow-partial", is_flag=True, help="Exit 0 for a PARTIAL build (default 3).")
    @functools.wraps(f)
    def wrapper(st: _State, *a: Any, allow_partial: bool, **kw: Any) -> Any:
        st.query_policy = (True, allow_partial)
        return f(st, *a, **kw)

    return wrapper


def _query_flags(f: Callable[..., Any]) -> Callable[..., Any]:
    """Adds --allow-stale/--allow-partial to a query command (apply BELOW @pass_state)."""

    @click.option("--allow-partial", is_flag=True, help="Exit 0 for a PARTIAL build (default 3).")
    @click.option(
        "--allow-stale", is_flag=True, help="Exit 0 for a STALE/HISTORICAL build (default 5)."
    )
    @functools.wraps(f)
    def wrapper(st: _State, *a: Any, allow_stale: bool, allow_partial: bool, **kw: Any) -> Any:
        st.query_policy = (allow_stale, allow_partial)
        return f(st, *a, **kw)

    return wrapper


@click.group(context_settings={"help_option_names": ["-h", "--help"]})
@click.option(
    "--root",
    type=click.Path(file_okay=False, path_type=Path),
    default=Path("."),
    show_default=True,
    help="Project directory containing svweave.yaml.",
)
@click.option("--manifest", "manifest_path", type=click.Path(dir_okay=False, path_type=Path))
@click.option(
    "--policy",
    "policy_path",
    type=click.Path(dir_okay=False, path_type=Path),
    help="Trusted policy.json (default: $XDG_CONFIG_HOME/svweave/workspaces/<key>/policy.json, "
    "outside the repository).",
)
@click.option("--local", "local_path", type=click.Path(dir_okay=False, path_type=Path))
@click.option("--request-id", default=None)
@click.option("--compact", is_flag=True, help="Single-line JSON output.")
@click.pass_context
def cli(
    ctx: click.Context,
    root: Path,
    manifest_path: Path | None,
    policy_path: Path | None,
    local_path: Path | None,
    request_id: str | None,
    compact: bool,
) -> None:
    """SVWeave: build-aware SystemVerilog analysis gateway (M1: config, manifest, snapshot)."""
    root = root.resolve()
    opts = WorkspaceOptions(
        root=root,
        manifest_path=manifest_path.resolve() if manifest_path else None,
        policy_path=policy_path.resolve() if policy_path else None,
        local_path=local_path.resolve() if local_path else None,
    )
    ctx.obj = _State(opts, request_id, compact)


@cli.command("init")
@pass_state
def init_cmd(st: _State) -> None:
    """Create svweave.yaml template, .svweave/ and policy.json (never overwrites)."""
    st.emit(st.run(lambda rid: init_workspace(st.opts.root, rid, st.opts.policy_path)))


@cli.command("doctor")
@click.option(
    "--probe",
    is_flag=True,
    help="Run real fixture probes of every backend (slow, starts sandboxed processes).",
)
@pass_state
def doctor_cmd(st: _State, probe: bool) -> None:
    """Config, policy, backend and client-boundary checks (backends NOT probed unless --probe)."""
    st.emit(st.run(lambda rid: run_doctor(st.opts, rid, probe=probe)))


@cli.group("manifest")
def manifest_grp() -> None:
    """Manifest operations."""


@manifest_grp.command("resolve")
@click.option("--profile", default=None, help="Profile name (default: project.default_profile).")
@pass_state
def manifest_resolve(st: _State, profile: str | None) -> None:
    """Resolve a profile into a normalized manifest + ledger."""
    st.emit(st.run(lambda rid: resolve_manifest(st.opts, profile, rid), profile=profile))


@cli.group("snapshot")
def snapshot_grp() -> None:
    """Snapshot operations."""


@snapshot_grp.command("create")
@click.option("--profile", default=None)
@pass_state
def snapshot_create(st: _State, profile: str | None) -> None:
    """Capture an immutable snapshot for a profile."""
    st.emit(st.run(lambda rid: create_snapshot(st.opts, profile, rid), profile=profile))


@snapshot_grp.command("check")
@click.option("--snapshot", "snapshot_id", required=True, help="64-hex snapshot id.")
@pass_state
def snapshot_check(st: _State, snapshot_id: str) -> None:
    """Compare a snapshot with the current workspace (exit 5 when STALE)."""
    env = st.run(lambda rid: check_snapshot(st.opts, snapshot_id, rid))
    stale = env.error is None and env.freshness.value == "STALE"
    st.emit(env, EXIT_STALE if stale else None)


@cli.group("cache")
def cache_grp() -> None:
    """Cache operations."""


@cache_grp.command("gc")
@click.option(
    "--dry-run/--no-dry-run",
    default=True,
    show_default=True,
    help="Only list what would be deleted (default).",
)
@pass_state
def cache_gc_cmd(st: _State, dry_run: bool) -> None:
    """Garbage-collect snapshots (keeps last 3 + referenced ones)."""
    st.emit(st.run(lambda rid: cache_gc(st.opts, dry_run, rid)))


@cli.group("schema")
def schema_grp() -> None:
    """JSON Schema emission."""


@schema_grp.command("emit")
@click.option(
    "--out",
    "out_dir",
    type=click.Path(file_okay=False, path_type=Path),
    default=Path("schemas"),
    show_default=True,
)
@click.option("--check", is_flag=True, help="Do not write; exit 2 if files are out of sync.")
@pass_state
def schema_emit(st: _State, out_dir: Path, check: bool) -> None:
    """Write (or verify) schemas/*.schema.json generated from the models."""
    out = out_dir if out_dir.is_absolute() else st.opts.root / out_dir

    def run(rid: str) -> Envelope:
        if check:
            bad = check_schemas(out) + check_mcp_schemas(out / "mcp")
            return Envelope(
                request_id=rid,
                outcome=Outcome.OK if not bad else Outcome.ERROR,
                analysis_coverage=AnalysisCoverage(completeness=Completeness.COMPLETE),
                data={"out_of_sync": bad},
            )
        return Envelope(
            request_id=rid,
            outcome=Outcome.OK,
            analysis_coverage=AnalysisCoverage(completeness=Completeness.COMPLETE),
            data={"written": [str(p) for p in emit_schemas(out) + emit_mcp_schemas(out / "mcp")]},
        )

    env = st.run(run)
    st.emit(env, EXIT_CONFIG if env.data.get("out_of_sync") else EXIT_OK)


@cli.command("build")
@click.option("--profile", default=None, help="Profile name (default: project.default_profile).")
@click.option("--no-reuse", is_flag=True, help="Rebuild even if a COMPLETE build exists.")
@pass_state
def build_cmd(st: _State, profile: str | None, no_reuse: bool) -> None:
    """Resolve, snapshot and analyse a profile with slang (exit 3: errors/PARTIAL/BLOCKED)."""
    from svweave.application.build import run_build
    from svweave.application.evidence import record_static_for

    env = st.run(lambda rid: run_build(st.opts, profile, rid, reuse=not no_reuse), profile=profile)
    record_static_for(st.opts, env.build_id)
    st.emit(env, EXIT_BUILD if env.outcome in (Outcome.PARTIAL, Outcome.BLOCKED) else None)


@cli.group("graph")
def graph_grp() -> None:
    """Static graph export of a build (self-contained HTML, nothing is served)."""


@graph_grp.command("export")
@click.option("--build", "build_id", required=True, help="64-hex build id.")
@click.option(
    "--name", default=None, help="File name stem under .svweave/exports/ (default graph-<build>)."
)
@click.option(
    "--max-instances", type=int, default=5000, show_default=True, help="Draw at most N instances, shallowest first."
)
@click.option(
    "--max-fanout",
    type=int,
    default=16,
    show_default=True,
    help="Signals used by more instances are listed as hubs, not drawn.",
)
@pass_state
@_query_flags
def graph_export_cmd(
    st: _State, build_id: str, name: str | None, max_instances: int, max_fanout: int
) -> None:
    """Write .svweave/exports/<name>.html: instance hierarchy plus port-association connections."""
    sess = st.session()
    st.emit(
        st.run(
            lambda rid: services.graph_export(
                sess,
                rid,
                build_id=build_id,
                name=name,
                max_instances=max_instances,
                max_fanout=max_fanout,
            )
        )
    )


@cli.group("query")
def query_grp() -> None:
    """Semantic queries bound to an immutable build id."""


def _paging(f: Callable[..., Any]) -> Callable[..., Any]:
    f = click.option("--cursor", default=None, help="Opaque cursor from a previous page.")(f)
    return click.option(
        "--limit", type=int, default=None, help="Items per page (default 100, max 500)."
    )(f)


_build_opt = click.option("--build", "build_id", required=True, help="64-hex build id.")


@query_grp.command("search")
@click.option("--snapshot", "snapshot_id", required=True, help="64-hex snapshot id.")
@click.option("--query", required=True, help="Literal text (graph candidates or text fallback).")
@click.option("--scope", default=None, help="Relative path glob inside the snapshot.")
@click.option(
    "--graph/--no-graph",
    default=True,
    show_default=True,
    help="Try the graph backend (starts a sandboxed process); --no-graph = text fallback only.",
)
@_paging
@pass_state
def query_search(
    st: _State,
    snapshot_id: str,
    query: str,
    scope: str | None,
    graph: bool,
    limit: int | None,
    cursor: str | None,
) -> None:
    """Source/graph candidates in a snapshot (never build-specific facts)."""
    sess = st.session(use_graph=graph)
    try:
        st.emit(
            st.run(
                lambda rid: services.search(
                    sess,
                    rid,
                    snapshot_id=snapshot_id,
                    query=query,
                    scope=scope,
                    limit=limit,
                    cursor=cursor,
                )
            )
        )
    finally:
        sess.close()


@query_grp.command("hierarchy")
@_build_opt
@click.option("--top", default=None, help="Top instance or definition name.")
@click.option("--instance", default=None, help="Instance id or path.")
@click.option("--depth", type=int, default=None, help="Levels below the root (0 = root only).")
@_paging
@pass_state
@_query_flags
def query_hierarchy(
    st: _State,
    build_id: str,
    top: str | None,
    instance: str | None,
    depth: int | None,
    limit: int | None,
    cursor: str | None,
) -> None:
    """Instance tree with parameters, generate context and blackbox/stub flags."""
    sess = st.session()
    st.emit(
        st.run(
            lambda rid: services.hierarchy(
                sess,
                rid,
                build_id=build_id,
                instance=instance,
                top=top,
                depth=depth,
                limit=limit,
                cursor=cursor,
            )
        )
    )


@query_grp.command("symbol")
@_build_opt
@click.option("--symbol", "symbol_id", default=None, help="symbol_id")
@click.option("--file", "file", default=None, help="Logical file path (with --byte).")
@click.option("--byte", "byte", type=int, default=None, help="Zero-based byte offset.")
@click.option("--name", default=None, help="Qualified or bare name (several matches -> ambiguous).")
@_paging
@pass_state
@_query_flags
def query_symbol(
    st: _State,
    build_id: str,
    symbol_id: str | None,
    file: str | None,
    byte: int | None,
    name: str | None,
    limit: int | None,
    cursor: str | None,
) -> None:
    """Declaration lookup; several candidates give AMBIGUOUS_SYMBOL (exit 3)."""
    sess = st.session()
    st.emit(
        st.run(
            lambda rid: services.symbol(
                sess,
                rid,
                build_id=build_id,
                symbol_id=symbol_id,
                file=file,
                byte=byte,
                name=name,
                limit=limit,
                cursor=cursor,
            )
        )
    )


@query_grp.command("references")
@_build_opt
@click.option("--symbol", "symbol_id", required=True, help="symbol_id")
@click.option("--scope", default=None, help="Logical path prefix to search.")
@_paging
@pass_state
@_query_flags
def query_references(
    st: _State,
    build_id: str,
    symbol_id: str,
    scope: str | None,
    limit: int | None,
    cursor: str | None,
) -> None:
    """Resolved and unresolved occurrences, with the searched scope."""
    sess = st.session()
    st.emit(
        st.run(
            lambda rid: services.references(
                sess,
                rid,
                build_id=build_id,
                symbol_id=symbol_id,
                scope=scope,
                limit=limit,
                cursor=cursor,
            )
        )
    )


@query_grp.command("impact")
@_build_opt
@click.option("--symbol", "symbol_id", default=None, help="symbol_id")
@click.option("--name", default=None, help="Declaration name or qualified name.")
@click.option("--file", "file", default=None, help="Logical file path (with --byte).")
@click.option("--byte", type=int, default=None, help="Zero-based byte offset (with --file).")
@click.option("--instance", default=None, help="Instance id or path.")
@click.option("--depth", type=int, default=3, show_default=True, help="Dependent/dependency levels.")
@click.option("--limit", type=int, default=None, help="Items per list (default 50, max 500).")
@pass_state
@_query_flags
def query_impact(
    st: _State,
    build_id: str,
    symbol_id: str | None,
    name: str | None,
    file: str | None,
    byte: int | None,
    instance: str | None,
    depth: int,
    limit: int | None,
) -> None:
    """What a change touches: instances, dependents, port connections and references."""
    sess = st.session()
    st.emit(
        st.run(
            lambda rid: services.impact(
                sess,
                rid,
                build_id=build_id,
                symbol_id=symbol_id,
                file=file,
                byte=byte,
                name=name,
                instance=instance,
                depth=depth,
                limit=limit,
            )
        )
    )


@query_grp.command("diff-impact")
@_build_opt
@click.option("--base", default="HEAD", show_default=True, help="Git revision the build was made from.")
@click.option("--head", default=None, help="Git revision of the change (default: the working tree).")
@click.option("--repo", default=None, help="Git repository directory (default: the project root).")
@click.option("--depth", type=int, default=3, show_default=True, help="Dependent/dependency levels.")
@click.option("--limit", type=int, default=None, help="Items per list (default 50, max 500).")
@pass_state
@_diff_flags
def query_diff_impact(
    st: _State,
    build_id: str,
    base: str,
    head: str | None,
    repo: str | None,
    depth: int,
    limit: int | None,
) -> None:
    """What a git diff touches in a build: declarations, modules, instances, dependents."""
    sess = st.session()
    st.emit(
        st.run(
            lambda rid: services.diff_impact(
                sess,
                rid,
                build_id=build_id,
                base=base,
                head=head,
                repo=repo,
                depth=depth,
                limit=limit,
            )
        )
    )


@query_grp.command("rename-preview")
@_build_opt
@click.option("--symbol", "symbol_id", default=None, help="symbol_id")
@click.option("--name", default=None, help="Declaration name or qualified name.")
@click.option("--file", "file", default=None, help="Logical file path (with --byte).")
@click.option("--byte", type=int, default=None, help="Zero-based byte offset (with --file).")
@click.option("--to", "new_name", required=True, help="The new identifier.")
@click.option("--limit", type=int, default=None, help="Items per list (default 50, max 500).")
@click.option(
    "--write-spec",
    default=None,
    help="File name stem: write the edit request to .svweave/exports/<stem>.json when ready.",
)
@pass_state
@_query_flags
def query_rename_preview(
    st: _State,
    build_id: str,
    symbol_id: str | None,
    name: str | None,
    file: str | None,
    byte: int | None,
    new_name: str,
    limit: int | None,
    write_spec: str | None,
) -> None:
    """Every edit a rename would make (verified against the snapshot), diff, blockers and an edit request. Read-only."""
    sess = st.session()
    st.emit(
        st.run(
            lambda rid: services.rename_preview(
                sess,
                rid,
                build_id=build_id,
                new_name=new_name,
                symbol_id=symbol_id,
                file=file,
                byte=byte,
                name=name,
                limit=limit,
                write_spec=write_spec,
            )
        )
    )


@query_grp.command("connections")
@_build_opt
@click.option("--instance", required=True, help="Instance id or path.")
@click.option("--port", default=None, help="Restrict to one formal port.")
@_paging
@pass_state
@_query_flags
def query_connections(
    st: _State,
    build_id: str,
    instance: str,
    port: str | None,
    limit: int | None,
    cursor: str | None,
) -> None:
    """Direct formal/actual port associations of one instance."""
    sess = st.session()
    st.emit(
        st.run(
            lambda rid: services.connections(
                sess,
                rid,
                build_id=build_id,
                instance=instance,
                port=port,
                limit=limit,
                cursor=cursor,
            )
        )
    )


@query_grp.command("assertions")
@_build_opt
@click.option("--scope", default=None, help="Instance id or path (filters elaborated entries).")
@click.option(
    "--kind",
    "entry_type",
    type=click.Choice(["source", "bind", "elaborated"]),
    default=None,
    help="Inventory part.",
)
@_paging
@pass_state
@_query_flags
def query_assertions(
    st: _State,
    build_id: str,
    scope: str | None,
    entry_type: str | None,
    limit: int | None,
    cursor: str | None,
) -> None:
    """SVA inventory (execution status is always NOT_RUN)."""
    sess = st.session()
    st.emit(
        st.run(
            lambda rid: services.assertions(
                sess,
                rid,
                build_id=build_id,
                scope=scope,
                entry_type=entry_type,
                limit=limit,
                cursor=cursor,
            )
        )
    )


@query_grp.command("diagnostics")
@_build_opt
@_paging
@pass_state
@_query_flags
def query_diagnostics(st: _State, build_id: str, limit: int | None, cursor: str | None) -> None:
    """Compiler diagnostics of a build."""
    sess = st.session()
    st.emit(
        st.run(
            lambda rid: services.diagnostics(
                sess, rid, build_id=build_id, limit=limit, cursor=cursor
            )
        )
    )


@query_grp.command("evidence")
@click.option("--build", "build_id", default=None, help="64-hex build id.")
@click.option("--evidence", "evidence_id", default=None, help="64-hex evidence id.")
@click.option(
    "--type",
    "evidence_type",
    type=click.Choice(["static", "eda_compile", "simulation", "formal", "coverage"]),
    default=None,
)
@_paging
@pass_state
@_query_flags
def query_evidence(
    st: _State,
    build_id: str | None,
    evidence_id: str | None,
    evidence_type: str | None,
    limit: int | None,
    cursor: str | None,
) -> None:
    """Evidence of a build (static record + imported external records) or one record."""
    sess = st.session()
    st.emit(
        st.run(
            lambda rid: services.evidence(
                sess,
                rid,
                build_id=build_id,
                evidence_id=evidence_id,
                evidence_type=evidence_type,
                limit=limit,
                cursor=cursor,
            )
        )
    )


@cli.command("serve")
@click.option("--transport", type=click.Choice(["stdio"]), default="stdio", show_default=True)
@click.option(
    "--workspace",
    type=click.Path(file_okay=False, path_type=Path),
    default=None,
    help="Workspace directory containing svweave.yaml (default: --root).",
)
@click.option("--graph/--no-graph", default=True, show_default=True)
@pass_state
def serve_cmd(st: _State, transport: str, workspace: Path | None, graph: bool) -> None:
    """Run the MCP server on stdio (stdout = protocol only; logs -> stderr)."""
    from dataclasses import replace

    from svweave.server import serve_stdio

    opts = st.opts if workspace is None else replace(st.opts, root=workspace.resolve())
    try:
        serve_stdio(opts, use_graph=graph)
    except SvwError as e:
        click.echo(f"svweave serve: {e.code.value}: {e.message}", err=True)
        sys.exit(e.exit_code)


@cli.group("evidence")
def evidence_grp() -> None:
    """External evidence store."""


@evidence_grp.command("import")
@click.option(
    "--manifest",
    "evidence_manifest",
    required=True,
    type=click.Path(dir_okay=False, path_type=Path),
    help="Evidence manifest JSON (schemas/evidence.schema.json); one record or a list.",
)
@pass_state
def evidence_import(st: _State, evidence_manifest: Path) -> None:
    """Import external evidence (schema, artifact hashes, path policy, build linkage checked)."""
    from svweave.application.evidence import import_manifest

    st.emit(st.run(lambda rid: import_manifest(st.opts, evidence_manifest, rid)))


@cli.command("report")
@click.option("--build", "build_id", required=True, help="64-hex build id.")
@click.option("--format", "fmt", type=click.Choice(["json"]), default="json", show_default=True)
@pass_state
def report_cmd(st: _State, build_id: str, fmt: str) -> None:
    """Status report (plan 28.2 template) filled from real data; unevaluated gates say so."""
    from svweave.application.report import build_report

    sess = st.session()
    st.emit(st.run(lambda rid: build_report(sess, rid, build_id)))


@cli.group("debug")
def debug_grp() -> None:
    """Debugging aids."""


@debug_grp.command("bundle")
@click.option("--build", "build_id", required=True, help="64-hex build id.")
@click.option("--out", "out_dir", type=click.Path(file_okay=False, path_type=Path), default=None)
@pass_state
def debug_bundle_cmd(st: _State, build_id: str, out_dir: Path | None) -> None:
    """Reproduction bundle: hashes, normalized manifest, diagnostics summary, redacted paths."""
    from svweave.application.debug_bundle import make_bundle

    st.emit(st.run(lambda rid: make_bundle(st.opts, build_id, rid, out_dir)))


@cli.group("edit")
def edit_grp() -> None:
    """Controlled edit loop (disabled unless the trusted policy enables editing)."""


def _txn_opt(f: Callable[..., Any]) -> Callable[..., Any]:
    return click.option("--transaction", "transaction_id", required=True, help="Transaction id.")(f)


@edit_grp.command("prepare")
@click.option(
    "--spec",
    "spec",
    required=True,
    type=click.Path(dir_okay=False, path_type=Path),
    help="JSON request (schemas/edit.schema.json): base build/snapshot, edits, key.",
)
@pass_state
def edit_prepare(st: _State, spec: Path) -> None:
    """Create a candidate copy and apply the edits there (the workspace is not touched)."""
    import json

    from svweave.application import edit as edit_mod

    def run(rid: str) -> Envelope:
        try:
            raw = json.loads(spec.read_text(encoding="utf-8"))
        except (OSError, ValueError) as e:
            raise SvwError(EC.INVALID_CONFIG, f"cannot read the spec: {type(e).__name__}") from e
        sess = st.session()
        try:
            return edit_mod.prepare(st.opts, rid, raw, registry=sess.registry)
        finally:
            sess.close()

    st.emit(st.run(run))


@edit_grp.command("validate")
@_txn_opt
@pass_state
def edit_validate(st: _State, transaction_id: str) -> None:
    """Static slang validation of baseline and candidate; diff and patch hash for review."""
    from svweave.application import edit as edit_mod

    st.emit(st.run(lambda rid: edit_mod.validate(st.opts, rid, transaction_id=transaction_id)))


@edit_grp.command("approve")
@_txn_opt
@click.option("--patch-hash", required=True, help="64-hex patch hash shown by `edit validate`.")
@click.option("--ttl-minutes", type=click.IntRange(1, 24 * 60), default=30, show_default=True)
@click.option("--confirm-token", envvar="SVWEAVE_TEST_CONFIRM_TOKEN", hidden=True, default=None)
@click.pass_context
def edit_approve(
    ctx: click.Context,
    transaction_id: str,
    patch_hash: str,
    ttl_minutes: int,
    confirm_token: str | None,
) -> None:
    """HUMAN approval (trusted terminal only): shows the diff, you type 12 hash characters.

    A token from SVWEAVE_TEST_CONFIRM_TOKEN is accepted for test harnesses only (NOT qualified);
    it is rejected when given on the command line."""
    from svweave.application import edit as edit_mod
    from svweave.editing.confirm import choose_confirmer

    st: _State = ctx.obj
    src = ctx.get_parameter_source("confirm_token")
    if confirm_token is not None and src is not click.core.ParameterSource.ENVIRONMENT:
        raise click.UsageError("--confirm-token is only honoured from the environment variable")
    st.emit(
        st.run(
            lambda rid: edit_mod.approve(
                st.opts,
                rid,
                transaction_id=transaction_id,
                patch_hash=patch_hash,
                confirm=choose_confirmer(),
                ttl_s=ttl_minutes * 60,
            )
        )
    )


@edit_grp.command("commit")
@_txn_opt
@click.option("--patch-hash", required=True)
@click.option("--request-key", default=None, help="Idempotency key (default: derived).")
@pass_state
def edit_commit(st: _State, transaction_id: str, patch_hash: str, request_key: str | None) -> None:
    """Apply an approved transaction (compare-and-set, journal, post-apply verification)."""
    from svweave.application import edit as edit_mod

    key = request_key or f"commit-{transaction_id}-{patch_hash[:12]}"
    st.emit(
        st.run(
            lambda rid: edit_mod.commit(
                st.opts,
                rid,
                transaction_id=transaction_id,
                expected_patch_hash=patch_hash,
                request_key=key,
            )
        )
    )


@edit_grp.command("abort")
@_txn_opt
@pass_state
def edit_abort(st: _State, transaction_id: str) -> None:
    """Remove the candidate; keeps the audit record; never reverts applied changes."""
    from svweave.application import edit as edit_mod

    st.emit(st.run(lambda rid: edit_mod.abort(st.opts, rid, transaction_id=transaction_id)))


@edit_grp.command("recover")
@_txn_opt
@click.option(
    "--roll-forward", "mode", flag_value="roll_forward", help="Apply the remaining files."
)
@click.option("--rollback", "mode", flag_value="rollback", help="Restore originals (CAS on after).")
@click.option(
    "--accept-current",
    "mode",
    flag_value="accept_current",
    help="Keep every file as it is on disk and clear the block.",
)
@pass_state
def edit_recover(st: _State, transaction_id: str, mode: str | None) -> None:
    """Resolve an interrupted commit from its journal, file by file; never overwrites others' bytes."""
    from svweave.application import edit as edit_mod

    st.emit(
        st.run(lambda rid: edit_mod.recover(st.opts, rid, transaction_id=transaction_id, mode=mode))
    )


@edit_grp.command("show")
@_txn_opt
@pass_state
def edit_show(st: _State, transaction_id: str) -> None:
    """State and audit trail of a transaction."""
    from svweave.application import edit as edit_mod

    st.emit(st.run(lambda rid: edit_mod.show(st.opts, rid, transaction_id=transaction_id)))


def main() -> None:
    cli(prog_name="svweave")


if __name__ == "__main__":
    main()
