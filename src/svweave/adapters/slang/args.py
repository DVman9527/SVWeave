"""Build the complete slang argument array (and per-group command files) for the worker.

Flag spellings verified against the pinned slang 12.0.0 ``--help`` and the pyslang Driver
(ADR-003): ``--single-unit``, ``-I``, ``-D NAME[=VALUE]`` (value token-exact), ``--top``,
``-G TOP.NAME=VALUE`` (top-scoped), ``-v``, ``-y``, ``--libext`` (alias of ``-Y``), ``-C <file>`` (one
independent compilation unit per file; relative paths inside a ``-C`` file resolve against that
file, so absolute paths are always written).

``single`` and ``per_file`` delegate to ``manifest.slang_args.to_slang_args``. ``explicit_groups``
(BLD-10) writes one command file per group into ``group_dir`` (the run directory, OUTSIDE the
snapshot; the worker request lists them so the dependency check can tell them apart from leaks).
"""

from __future__ import annotations

import shlex
from dataclasses import dataclass, field
from pathlib import Path

from svweave.adapters.slang.protocol import GroupFile
from svweave.config.context import snapshot_rel
from svweave.domain.errors import ErrorCode, SvwError
from svweave.domain.ids import sha256_hex
from svweave.manifest.models import NormalizedManifest
from svweave.manifest.slang_args import language_args, to_slang_args, top_param_args


@dataclass
class Invocation:
    args: list[str]
    group_files: list[GroupFile] = field(default_factory=list)
    ledger: list[dict[str, str]] = field(default_factory=list)  # file name -> text (no secrets)


def _tops_and_params(nm: NormalizedManifest) -> list[str]:
    args: list[str] = language_args(nm)
    for t in nm.top_modules:
        args += ["--top", t]
    args += top_param_args(nm)
    return args


def build_invocation(nm: NormalizedManifest, files_root: Path, group_dir: Path) -> Invocation:
    if nm.cu_mode != "explicit_groups":
        return Invocation(args=to_slang_args(nm, files_root))

    def p(logical: str) -> str:
        return str(files_root / snapshot_rel(logical))

    args = _tops_and_params(nm)
    inv = Invocation(args=args)
    group_dir.mkdir(parents=True, exist_ok=True)
    for u in nm.units:
        if u.library_files or u.library_dirs:
            raise SvwError(
                ErrorCode.CAPABILITY_MISMATCH,
                f"compilation group {u.group!r} declares library inputs; per-group library "
                "search is not supported by slang -C files",
                {"group": u.group},
            )
        lines: list[str] = []
        for d in u.include_dirs:
            lines += ["-I", p(d.path)]
        for df in u.defines:
            lines += ["-D", df.name if df.value is None else f"{df.name}={df.value}"]
        for s in u.sources:
            lines.append(p(s.path))
        text = " ".join(shlex.quote(x) for x in lines) + "\n"
        path = group_dir / f"{u.id}.f"
        path.write_text(text, encoding="utf-8")
        inv.group_files.append(GroupFile(path=str(path), sha256=sha256_hex(text)))
        inv.args += ["-C", str(path)]
        inv.ledger.append({"file": path.name, "text": text})
    return inv
