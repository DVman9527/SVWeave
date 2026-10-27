"""Dedicated Serena state, config and context (M0 facts; plan 12.1, 7.3, ADR-007, ADR-012)."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import stat
from dataclasses import dataclass
from pathlib import Path

from svweave.domain.errors import ErrorCode, SvwError
from svweave.security import safeio
from svweave.security.python_runtime import (  # re-export the established adapter API
    python_binds as python_binds,
    python_links as python_links,
    python_prefix_to_bind as python_prefix_to_bind,
)

VERIBLE_SUFFIXES = (".sv", ".svh", ".v", ".vh")
FILELIST_NAME = "verible.filelist"
#: Files the adapter itself writes into the candidate; the gateway excludes them from edit diffs.
GENERATED_FILES: frozenset[str] = frozenset({FILELIST_NAME})

#: Exactly these tools are exposed by the SVWeave context: no shell, no file create, no project
#: switching, no memory tools, no regex replace.
CONTEXT_YML = """\
description: SVWeave context - symbol read + symbol body edit only; no shell, no file create, no project switching
prompt: null
excluded_tools: []
included_optional_tools: []
fixed_tools:
  - get_symbols_overview
  - find_symbol
  - find_referencing_symbols
  - find_declaration
  - replace_symbol_body
  - get_diagnostics_for_file
tool_description_overrides: {}
single_project: true
structured_tool_output: null
"""

PROJECT_YML = """\
project_name: "svw_candidate"
language_servers: ["systemverilog"]
encoding: "utf-8"
activation_command:
line_ending: lf
language_backend:
ignore_all_files_in_gitignore: false
ls_specific_settings: {}
ls_workspace_folders: ["."]
ls_additional_workspace_folders: []
ignored_paths: []
read_only: false
excluded_tools: []
included_optional_tools: []
fixed_tools: []
default_modes:
added_modes:
initial_prompt: ""
symbol_info_budget:
read_only_memory_patterns: []
ignored_memory_patterns: []
"""


def serena_config_yml(projects_dir: Path, trusted_path: Path) -> str:
    """Serena global config. ``trusted_project_path_patterns`` is EXACTLY the candidate path:
    omitting the key makes Serena trust ``**`` (every project)."""
    return f"""\
language_backend: LSP
line_ending: lf
gui_log_window: False
web_dashboard: False
web_dashboard_open_on_launch: False
web_dashboard_interface:
web_dashboard_listen_address: 127.0.0.1
web_dashboard_trusted_hosts:
  - 127.0.0.1
jetbrains_plugin_server_address: 127.0.0.1
jetbrains_launch_command:
log_level: 20
trace_lsp_communication: False
ls_specific_settings: {{}}
ls_priorities:
ignored_paths: []
read_only_memory_patterns: []
ignored_memory_patterns: []
tool_timeout: 120
excluded_tools: [execute_shell_command, create_text_file, activate_project, remove_project, open_dashboard]
included_optional_tools: []
fixed_tools: []
base_modes: []
default_modes:
default_max_tool_answer_chars: 150000
token_count_estimator: CHAR_COUNT
symbol_info_budget: 10
record_tool_usage: False
project_serena_folder_location: {json.dumps(str(projects_dir) + "/$projectFolderName/.serena")}
trusted_project_path_patterns: [{json.dumps(str(trusted_path))}]
projects: []
"""


def candidate_id(candidate: Path) -> str:
    return hashlib.sha256(os.path.realpath(candidate).encode()).hexdigest()[:16]


@dataclass(frozen=True)
class SerenaPaths:
    """``state_dir`` is HOST-ONLY (logs, the context file bind-mounted read-only). Only ``work``
    (HOME, SERENA_HOME, caches, project folder) is mounted read-write into the sandbox; the config
    and project.yml inside it are rewritten by the host before every launch, never read back."""

    state_dir: Path  # <state_root>/<candidate_id>

    @property
    def work(self) -> Path:
        return self.state_dir / "work"

    @property
    def home(self) -> Path:
        return self.work / "home"

    @property
    def serena_home(self) -> Path:
        return self.home / ".serena"

    @property
    def projects_dir(self) -> Path:
        return self.work / "projects"

    @property
    def ctx_dir(self) -> Path:
        return self.state_dir / "ctx"

    @property
    def context_file(self) -> Path:
        return self.ctx_dir / "svweave_context.yml"

    @property
    def logs(self) -> Path:
        return self.state_dir / "logs"

    @property
    def stderr_log(self) -> Path:
        return self.logs / "serena.stderr.log"

    def ensure(self) -> None:
        for d in (self.state_dir, self.logs, self.ctx_dir):
            d.mkdir(parents=True, exist_ok=True)
            d.chmod(0o700)
        for rel in ("work/home/.serena", "work/projects", "work/cache", "work/data"):
            safeio.ensure_private_dir(self.state_dir, rel)

    def env(self, verible_bin_dir: Path, serena_venv: Path) -> dict[str, str]:
        # Pinned Verible FIRST on PATH so SolidLSP never downloads one (M0 fact).
        return {
            "PATH": f"{verible_bin_dir}:{serena_venv / 'bin'}:/usr/bin:/bin",
            "HOME": str(self.home),
            "SERENA_HOME": str(self.serena_home),
            "XDG_CONFIG_HOME": str(self.home / ".config"),
            "XDG_CACHE_HOME": str(self.work / "cache"),
            "XDG_DATA_HOME": str(self.work / "data"),
            "LANG": "C.UTF-8",
            "PYTHONNOUSERSITE": "1",
            "UV_NO_SYNC": "1",
        }

    def prepare(self, candidate: Path) -> None:
        """Write config/context/project.yml into the DEDICATED state (never into the candidate)."""
        self.ensure()
        safeio.write_under(
            self.state_dir, "work/home/.serena/serena_config.yml",
            serena_config_yml(self.projects_dir, candidate).encode(),
        )
        safeio.write_atomic(self.context_file, CONTEXT_YML.encode())
        rel = f"work/projects/{candidate.name}/.serena"
        if safeio.safe_rel_path(rel) is None:
            raise safeio.unsafe(candidate.name, "candidate directory name is not a safe path component")
        safeio.ensure_private_dir(self.state_dir, rel)
        safeio.write_under(self.state_dir, rel + "/project.yml", PROJECT_YML.encode())


_GLOB_CHARS = frozenset("*?[")


def check_candidate(candidate: Path) -> Path:
    """Resolve the candidate and reject repository-controlled / unsafe state.

    * a ``.serena/`` directory would be used by Serena in preference to our dedicated project
      folder (M0 fact);
    * glob metacharacters in the path would widen ``trusted_project_path_patterns``;
    * a link or non-regular file at any adapter-generated path (``verible.filelist``) would let
      the host write through it.
    """
    cand = Path(candidate)
    if not cand.is_dir():
        raise SvwError(ErrorCode.MISSING_INPUT, "candidate directory does not exist",
                       {"reason": "candidate_missing"})
    real = Path(os.path.realpath(cand))
    if _GLOB_CHARS & set(str(real)):
        raise SvwError(
            ErrorCode.POLICY_DENIED,
            "candidate path contains glob metacharacters (*, ?, [) and cannot be trusted exactly",
            {"reason": "candidate_path_glob_chars"},
        )
    if (real / ".serena").exists() or (real / ".serena").is_symlink():
        raise SvwError(
            ErrorCode.POLICY_DENIED,
            "candidate contains a .serena/ directory (repository-controlled Serena configuration)",
            {"reason": "candidate_has_serena_dir"},
        )
    for name in sorted(GENERATED_FILES):
        try:
            st = os.lstat(real / name)
        except FileNotFoundError:
            continue
        if not stat.S_ISREG(st.st_mode):
            raise SvwError(
                ErrorCode.POLICY_DENIED,
                f"candidate path {name} is a link or not a regular file",
                {"reason": "candidate_generated_file_unsafe", "file": name},
            )
    return real


def write_verible_filelist(candidate: Path) -> list[str]:
    """``verible.filelist`` in the candidate root: without it cross-file references are lost (M0).

    The file is line based, so a source file name containing a newline / CR cannot be represented:
    the candidate is refused rather than silently altering the list. Written no-follow + atomic.
    """
    files: list[str] = []
    for dirpath, dirnames, filenames in os.walk(candidate, followlinks=False):
        dirnames.sort()
        for fn in sorted(filenames):
            p = Path(dirpath) / fn
            if p.suffix in VERIBLE_SUFFIXES and not p.is_symlink():
                rel = p.relative_to(candidate).as_posix()
                if "\n" in rel or "\r" in rel:
                    raise SvwError(
                        ErrorCode.POLICY_DENIED,
                        "candidate source file name contains a line break",
                        {"reason": "filename_has_newline"},
                    )
                files.append(rel)
    files.sort()
    safeio.write_under(candidate, FILELIST_NAME, ("\n".join(files) + "\n").encode(), 0o644)
    return files


def which_in(path_value: str, name: str) -> str | None:
    return shutil.which(name, path=path_value)
