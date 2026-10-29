"""Shared-install MCP launcher. Select one project before serving; never change cwd."""

from __future__ import annotations

import os
from pathlib import Path

import click

from svweave.application.options import WorkspaceOptions
from svweave.config.discovery import discover_workspace
from svweave.domain.errors import ErrorCode, SvwError
from svweave.orchestration.toolchain import ENV_ROOT, Toolchain


@click.command(context_settings={"help_option_names": ["-h", "--help"]})
@click.option("--repo", type=click.Path(file_okay=False, path_type=Path), default=None)
@click.option("--workspace", type=click.Path(file_okay=False, path_type=Path), default=None)
@click.option(
    "--toolchain-root", type=click.Path(file_okay=False, path_type=Path), default=None,
    help="Shared installed toolchain root, never discovered from the analysed project.",
)
@click.option("--execution-provider", type=click.Choice(["local", "srun"]), default=None)
@click.option("--execution-profile", default=None)
@click.option("--allocation-mode", type=click.Choice(["existing_allocation", "new_allocation"]))
@click.option("--allocation-id", default=None)
@click.option("--graph/--no-graph", default=True, show_default=True)
def launcher(
    repo: Path | None,
    workspace: Path | None,
    toolchain_root: Path | None,
    execution_provider: str | None,
    execution_profile: str | None,
    allocation_mode: str | None,
    allocation_id: str | None,
    graph: bool,
) -> None:
    """Start one workspace-bound MCP server from a shared installation.

    --repo/--workspace is optional: use SVWEAVE_PROJECT_ROOT, the Claude project hint or the
    nearest manifest from cwd. Selection never creates policy or grants trust. Explicit paths
    must contain svweave.yaml. stdout is reserved for MCP protocol, errors go to stderr.
    """
    from svweave.server import serve_stdio

    previous_toolchain = os.environ.get(ENV_ROOT)
    try:
        root = discover_workspace(repo=repo, workspace=workspace)
        if toolchain_root is None and previous_toolchain is not None and (
            not previous_toolchain or not Path(previous_toolchain).is_absolute()
        ):
            raise SvwError(ErrorCode.INVALID_CONFIG, f"{ENV_ROOT} must be absolute for a shared launcher")
        if toolchain_root is not None:
            # This option is host/operator configuration, not a tool request or repository value.
            try:
                selected_toolchain = toolchain_root.resolve(strict=True)
            except (OSError, RuntimeError, ValueError) as exc:
                raise SvwError(ErrorCode.INVALID_CONFIG, "invalid shared toolchain root") from exc
            toolchain = Toolchain.discover(selected_toolchain)
            os.environ[ENV_ROOT] = str(toolchain.root)
        overrides = {
            k: v for k, v in {
                "provider": execution_provider,
                "site_profile": execution_profile,
                "allocation_mode": allocation_mode,
                "allocation_id": allocation_id,
            }.items() if v is not None
        }
        opts = WorkspaceOptions(root=root, execution_overrides=overrides or None, bound_manifest_dir=root)
        opts.load()  # authorization before server/log/state initialization
        serve_stdio(opts, use_graph=graph)
    except SvwError as exc:
        click.echo(f"svweave-mcp: {exc.code.value}: {exc.message}", err=True)
        raise SystemExit(exc.exit_code) from exc
    finally:
        if toolchain_root is not None:
            if previous_toolchain is None:
                os.environ.pop(ENV_ROOT, None)
            else:
                os.environ[ENV_ROOT] = previous_toolchain


def main() -> None:
    launcher(prog_name="svweave-mcp")


if __name__ == "__main__":
    main()
