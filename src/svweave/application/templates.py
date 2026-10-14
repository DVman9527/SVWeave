"""Files written by `svweave init`. Pure data; schema-valid by construction (tested)."""

from __future__ import annotations

import json
import re
from pathlib import Path

MANIFEST_TEMPLATE = """\
# SVWeave project configuration (data only: nothing here is ever executed).
# Unknown fields are errors. See docs/manifest.md.
schema_version: "1.0"
project:
  id: "{project_id}"
  root: "."
  default_profile: "default"

source_roots:
  - id: "project"
    path: "."
    include: ["**"]
    exclude: ["**/out/**", "**/waves/**", ".svweave/**", ".git/**"]
    writable: false

profiles:
  default:
    cwd: "."
    language_mode: "systemverilog"
    source_inputs:
      - kind: "filelist"
        path: "filelists/rtl.f"          # create this literal filelist
        dialect: "svweave-filelist-v1"
        path_mode: "cwd"
    compilation_units:
      mode: "single"
    top_modules: []
    defines: []
    include_dirs: []
    top_parameters: []
    library_dirs: []
    library_files: []
    library_extensions: [".v", ".sv"]
    environment_allowlist: []
    generated_inputs: []
    diagnostic_policy: "no_errors"

analysis:
  missing_input_policy: "block"
  unsupported_option_policy: "block"
  stub_policy: "explicit_only"
  max_build_seconds: 120
  max_worker_memory_mib: 8192

editing:
  enabled: false
  allowed_paths: []
  max_files_per_patch: 5
  max_changed_lines: 200
  require_human_approval: true
  validate_profiles: []
"""


def policy_dict(root: Path) -> dict[str, object]:
    return {
        "schema_version": "1.0",
        "allowed_roots": [str(root.resolve())],  # absolute: policy roots are never relative
        "writable_paths": [],
        "approval_required": True,
        "network_enabled": False,
        "editing_enabled": False,
    }


def manifest_text(dirname: str) -> str:
    pid = re.sub(r"[^A-Za-z0-9_.-]", "-", dirname).strip("-.") or "svweave-project"
    return MANIFEST_TEMPLATE.format(project_id=pid)


def policy_text(root: Path) -> str:
    return json.dumps(policy_dict(root), indent=2) + "\n"
