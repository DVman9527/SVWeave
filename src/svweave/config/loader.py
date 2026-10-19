"""Loaders: YAML/JSON -> strict models. Unknown fields are INVALID_CONFIG with a location.

Only `yaml.safe_load`-equivalent construction is used (no Python object tags), and duplicate
mapping keys are rejected instead of silently overridden.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, ValidationError

from svweave.config.models import LocalConfig, Manifest, Policy
from svweave.domain.errors import ErrorCode, SvwError

MAX_CONFIG_BYTES = 1024 * 1024


class _UniqueKeyLoader(yaml.SafeLoader):
    pass


def _construct_mapping(
    loader: _UniqueKeyLoader, node: yaml.MappingNode, deep: bool = False
) -> dict[Any, Any]:
    seen: set[Any] = set()
    for key_node, _ in node.value:
        key = loader.construct_object(key_node, deep=True)
        if key in seen:
            raise yaml.constructor.ConstructorError(
                None,
                None,
                f"duplicate key {key!r}",
                key_node.start_mark,
            )
        seen.add(key)
    return yaml.SafeLoader.construct_mapping(loader, node, deep)


_UniqueKeyLoader.add_constructor(
    yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG,
    _construct_mapping,
)


def _read_text(path: Path) -> str:
    try:
        if path.stat().st_size > MAX_CONFIG_BYTES:
            raise SvwError(
                ErrorCode.INVALID_CONFIG,
                f"{path.name} exceeds {MAX_CONFIG_BYTES} bytes",
                {"file": str(path)},
            )
        return path.read_text(encoding="utf-8")
    except FileNotFoundError as e:
        raise SvwError(
            ErrorCode.INVALID_CONFIG, f"config file not found: {path}", {"file": str(path)}
        ) from e
    except (OSError, UnicodeDecodeError) as e:
        raise SvwError(
            ErrorCode.INVALID_CONFIG, f"cannot read {path}: {e}", {"file": str(path)}
        ) from e


def _line_index(text: str) -> yaml.Node | None:
    try:
        node: yaml.Node | None = yaml.compose(text, Loader=_UniqueKeyLoader)
        return node
    except yaml.YAMLError:
        return None


def _locate(root: yaml.Node | None, loc: tuple[Any, ...]) -> int | None:
    """1-based line of the deepest node reachable along `loc`."""
    node = root
    line = node.start_mark.line + 1 if node is not None else None
    for part in loc:
        if node is None:
            break
        nxt = None
        if isinstance(node, yaml.MappingNode):
            for k, v in node.value:
                if isinstance(k, yaml.ScalarNode) and k.value == str(part):
                    # unknown keys: point at the key itself
                    nxt = v
                    line = k.start_mark.line + 1
                    break
        elif (
            isinstance(node, yaml.SequenceNode) and isinstance(part, int) and part < len(node.value)
        ):
            nxt = node.value[part]
            line = nxt.start_mark.line + 1
        node = nxt
    return line


def _fmt_loc(loc: tuple[Any, ...]) -> str:
    out = ""
    for p in loc:
        out += f"[{p}]" if isinstance(p, int) else (f".{p}" if out else str(p))
    return out or "<root>"


def validation_issues(err: ValidationError, text: str | None, file: str) -> list[dict[str, Any]]:
    root = _line_index(text) if text is not None else None
    issues: list[dict[str, Any]] = []
    for e in err.errors(include_url=False):
        loc = tuple(e["loc"])
        issue: dict[str, Any] = {
            "file": file,
            "location": _fmt_loc(loc),
            "type": e["type"],
            "message": e["msg"],
        }
        line = _locate(root, loc) if root is not None else None
        if line is not None:
            issue["line"] = line
        issues.append(issue)
    return issues


def _invalid(file: str, issues: list[dict[str, Any]]) -> SvwError:
    first = issues[0]
    where = first["location"] + (f" (line {first['line']})" if "line" in first else "")
    return SvwError(
        ErrorCode.INVALID_CONFIG,
        f"{file}: {where}: {first['message']}",
        {"issues": issues},
    )


def _validate[M: BaseModel](model: type[M], data: Any, text: str | None, file: str) -> M:
    try:
        return model.model_validate(data)
    except ValidationError as e:
        raise _invalid(file, validation_issues(e, text, file)) from e


def load_yaml_model[M: BaseModel](model: type[M], path: Path) -> M:
    text = _read_text(path)
    try:
        data = yaml.load(text, Loader=_UniqueKeyLoader)  # noqa: S506 - SafeLoader subclass
    except yaml.YAMLError as e:
        mark = getattr(e, "problem_mark", None)
        details: dict[str, Any] = {"file": str(path)}
        if mark is not None:
            details["line"] = mark.line + 1
            details["column"] = mark.column + 1
        raise SvwError(
            ErrorCode.INVALID_CONFIG, f"{path.name}: YAML error: {e}".replace("\n", " "), details
        ) from e
    if not isinstance(data, dict):
        raise SvwError(
            ErrorCode.INVALID_CONFIG,
            f"{path.name}: top level must be a mapping",
            {"file": str(path)},
        )
    return _validate(model, data, text, path.name)


def load_manifest(path: Path) -> Manifest:
    return load_yaml_model(Manifest, path)


def load_local_config(path: Path) -> LocalConfig:
    return load_yaml_model(LocalConfig, path)


def load_policy(path: Path) -> Policy:
    text = _read_text(path)
    if path.stat().st_uid != os.geteuid():
        raise SvwError(
            ErrorCode.POLICY_DENIED,
            f"{path.name} is not owned by the current user",
            {"file": str(path)},
        )
    try:
        data = json.loads(text)
    except json.JSONDecodeError as e:
        raise SvwError(
            ErrorCode.INVALID_CONFIG,
            f"{path.name}: JSON error: {e.msg}",
            {"file": str(path), "line": e.lineno, "column": e.colno},
        ) from e
    if not isinstance(data, dict):
        raise SvwError(
            ErrorCode.INVALID_CONFIG,
            f"{path.name}: top level must be an object",
            {"file": str(path)},
        )
    return _validate(Policy, data, None, path.name)


def policy_hash(policy: Policy) -> str:
    from svweave.domain.ids import canonical_hash

    return canonical_hash(policy.model_dump(mode="json"))
