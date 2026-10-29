"""Narrow path relocation for the incremental fixture's empty include-directory warning.

The two independent oracle workspaces have different host paths (plan 9.4). Do not normalize
arbitrary diagnostic text: keep the warning, its snapshot/logical path, and all other fields.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Literal

from svweave.snapshot.models import SnapshotRecord

_EMPTY_INCLUDE = "include/preferred"
_EMPTY_LOCATIONS = {"macro_notes": [], "option": None, "primary": None, "ranges": []}


def relocate_empty_include_diagnostic(
    diagnostic: dict[str, Any], files_root: Path, record: SnapshotRecord,
    *, shape: Literal["row", "query"],
) -> dict[str, Any]:
    """Relocate ONLY the exact, location-free warning proven by this fixture's inventory.

    Unrecognized messages/metadata are returned unchanged, so the exact oracle comparison
    still rejects them. This never changes product output, drops a diagnostic or rewrites IDs.
    """
    result = dict(diagnostic)
    directory = files_root / _EMPTY_INCLUDE
    expected = f"warning: include directory '{directory}': No such file or directory [-Wcommand-line]"
    if (files_root.name != "files" or files_root.parent.name != record.snapshot_id
            or diagnostic.get("message") != expected
            or diagnostic.get("code") != "DriverMessage"
            or diagnostic.get("severity") != "warning"
            or diagnostic.get("is_error") not in (False, 0)):
        return result
    # A truly empty inventoried include search space, not a missing source or an arbitrary
    # warning path. Both engines currently materialize files, not empty lookup directories.
    if (directory.exists() or directory.is_symlink()
            or not any(lk.kind == "include" and lk.logical_dir == _EMPTY_INCLUDE and lk.entries == []
                       for lk in record.lookup)
            or any(f.logical_path == _EMPTY_INCLUDE or f.logical_path.startswith(_EMPTY_INCLUDE + "/")
                   for f in record.files)):
        return result
    if shape == "row":
        if (type(diagnostic.get("is_error")) is not int or diagnostic["is_error"] != 0
                or any(name not in diagnostic or diagnostic[name] is not None
                       for name in ("file", "span_start", "span_end", "suppression"))):
            return result
        try:
            locations = json.loads(diagnostic["locations"])
        except (KeyError, TypeError, ValueError):
            return result
        if locations != _EMPTY_LOCATIONS:
            return result
    else:
        if (diagnostic.get("is_error") is not False
                or any(name not in diagnostic or diagnostic[name] != value for name, value in (
                    ("location", None), ("ranges", []), ("macro_notes", []), ("option", None),
                ))):
            return result
    result["message"] = (
        f"warning: include directory 'snapshot:{record.snapshot_id}/files/{_EMPTY_INCLUDE}': "
        "No such file or directory [-Wcommand-line]"
    )
    return result
