"""Pure oracle tests: relocating one verified host path must not hide diagnostic changes."""

from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any

import pytest

from svweave.snapshot.models import LookupInventory, SnapFile, SnapshotRecord, SnapshotSpec
from tests.integration.incremental_compare import relocate_empty_include_diagnostic

SID = "a" * 64


def _record() -> SnapshotRecord:
    return SnapshotRecord(
        snapshot_id=SID, workspace_id="t", profile="p", config_id="b" * 64,
        created_at="2026-10-29T04:23:21Z", attempts=1,
        spec=SnapshotSpec(explicit=[], lookups=[], root_ids=["project"]), files=[],
        lookup=[LookupInventory(kind="include", logical_dir="include/preferred", entries=[],
                                original_dir="/original/include/preferred")],
    )


def _diagnostic(root: Path, shape: str) -> dict[str, Any]:
    common = {
        "code": "DriverMessage", "severity": "warning",
        "message": f"warning: include directory '{root}/include/preferred': No such file or directory [-Wcommand-line]",
    }
    if shape == "row":
        return {**common, "build_id": "c" * 64, "job_id": "job-original", "diagnostic_id": 7,
                "is_error": 0, "file": None, "span_start": None, "span_end": None,
                "suppression": None,
                "locations": json.dumps({"macro_notes": [], "option": None, "primary": None, "ranges": []})}
    return {**common, "is_error": False, "location": None, "ranges": [], "macro_notes": [], "option": None}


@pytest.mark.parametrize("shape", ["row", "query"])
def test_verified_empty_lookup_warning_relocates_only_host_prefix(tmp_path: Path, shape: str) -> None:
    roots = [tmp_path / name / ".svweave" / "snapshots" / SID / "files" for name in ("incremental", "full")]
    messages = [_diagnostic(root, shape) for root in roots]
    untouched = copy.deepcopy(messages)
    normalized = [relocate_empty_include_diagnostic(d, root, _record(), shape=shape)
                  for d, root in zip(messages, roots, strict=True)]
    assert normalized[0] == normalized[1]
    assert messages == untouched
    for before, after in zip(messages, normalized, strict=True):
        assert {key for key in before if before[key] != after[key]} == {"message"}
        assert f"snapshot:{SID}/files/include/preferred" in after["message"]
    # Keeping a diagnostic is essential: count differences must still fail the list comparison.
    assert normalized[:1] != normalized


@pytest.mark.parametrize("shape", ["row", "query"])
@pytest.mark.parametrize("field,value", [
    ("code", "OtherDiagnostic"), ("severity", "error"), ("is_error", True),
    ("message", "warning: an unrelated command-line warning"),
    ("message", "error: include directory is unavailable"),
])
def test_other_type_or_message_cannot_be_normalized_away(
    tmp_path: Path, shape: str, field: str, value: Any,
) -> None:
    root = tmp_path / SID / "files"
    original = _diagnostic(root, shape)
    changed = {**original, field: value}
    expected = relocate_empty_include_diagnostic(original, root, _record(), shape=shape)
    actual = relocate_empty_include_diagnostic(changed, root, _record(), shape=shape)
    assert actual == changed and actual != expected


@pytest.mark.parametrize("shape", ["row", "query"])
@pytest.mark.parametrize("replacement", ["include/other", "include/preferred/../other", "include/preferred-extra"])
def test_different_logical_path_is_not_host_relocation(tmp_path: Path, shape: str, replacement: str) -> None:
    root = tmp_path / SID / "files"
    original = _diagnostic(root, shape)
    changed = {**original, "message": original["message"].replace("include/preferred", replacement)}
    assert relocate_empty_include_diagnostic(changed, root, _record(), shape=shape) == changed
    assert changed != relocate_empty_include_diagnostic(original, root, _record(), shape=shape)


@pytest.mark.parametrize("shape", ["row", "query"])
@pytest.mark.parametrize("before,after", [
    (SID, "e" * 64), ("No such file or directory", "Permission denied"),
    ("[-Wcommand-line]", "[-Wother-warning]"),
])
def test_snapshot_or_warning_details_are_never_rewritten(
    tmp_path: Path, shape: str, before: str, after: str,
) -> None:
    root = tmp_path / SID / "files"
    original = _diagnostic(root, shape)
    changed = {**original, "message": original["message"].replace(before, after)}
    assert relocate_empty_include_diagnostic(changed, root, _record(), shape=shape) == changed
    assert changed != relocate_empty_include_diagnostic(original, root, _record(), shape=shape)


def test_different_valid_snapshot_id_remains_visible(tmp_path: Path) -> None:
    first = _record()
    second = first.model_copy(update={"snapshot_id": "e" * 64})
    results = []
    for record in (first, second):
        root = tmp_path / record.snapshot_id / "files"
        result = relocate_empty_include_diagnostic(_diagnostic(root, "row"), root, record, shape="row")
        assert record.snapshot_id in result["message"]
        results.append(result)
    assert results[0] != results[1]


@pytest.mark.parametrize("shape", ["row", "query"])
def test_location_and_identity_differences_are_retained(tmp_path: Path, shape: str) -> None:
    root = tmp_path / SID / "files"
    original = _diagnostic(root, shape)
    changed = dict(original)
    if shape == "row":
        changed.update(file="rtl/top.sv", locations=json.dumps({"primary": {"file": "rtl/top.sv"}}))
    else:
        changed["location"] = {"file": "rtl/top.sv", "start": 1, "end": 2}
    assert relocate_empty_include_diagnostic(changed, root, _record(), shape=shape) == changed
    expected = relocate_empty_include_diagnostic(original, root, _record(), shape=shape)
    assert changed != expected
    if shape == "row":
        for field in ("build_id", "job_id", "diagnostic_id"):
            changed = {**original, field: "different"}
            actual = relocate_empty_include_diagnostic(changed, root, _record(), shape=shape)
            assert actual[field] == "different" and actual != expected


@pytest.mark.parametrize("reason", ["nonempty-inventory", "captured-file", "directory-exists", "wrong-snapshot"])
def test_path_relocation_requires_exact_snapshot_and_empty_lookup_proof(tmp_path: Path, reason: str) -> None:
    root = tmp_path / SID / "files"
    record = _record()
    if reason == "nonempty-inventory":
        record.lookup[0].entries.append("other.svh")
    elif reason == "captured-file":
        record.files.append(SnapFile(logical_path="include/preferred/other.svh", original_path="/original/other.svh",
                                    snapshot_path="files/include/preferred/other.svh", sha256="d" * 64, size=1, role="include_lookup"))
    elif reason == "directory-exists":
        (root / "include/preferred").mkdir(parents=True)
    else:
        record.snapshot_id = "e" * 64
    diagnostic = _diagnostic(root, "row")
    assert relocate_empty_include_diagnostic(diagnostic, root, record, shape="row") == diagnostic
