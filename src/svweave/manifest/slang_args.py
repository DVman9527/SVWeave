"""slang/pyslang driver argument ARRAY generated from a normalized manifest.

ADAPTER INPUT: an argv list (never a shell string) passed to the driver without shell
interpretation. Flag spellings were verified against the pinned slang 12.0.0 CLI (`--help`) and
the pyslang Driver in M2: `--single-unit`, `-I`, `-D`, `--top`, `-G`, `-v`, `-y`, `--libext`
(alias of `-Y`) are all accepted as written here; per-group compilation units use `-C` and are
built by `svweave.adapters.slang.args.build_invocation`, not by this function.
"""

from __future__ import annotations

from pathlib import Path

from svweave.config.context import snapshot_rel
from svweave.domain.errors import ErrorCode, SvwError
from svweave.manifest.models import NormalizedManifest


def language_args(nm: NormalizedManifest) -> list[str]:
    """Allowlisted language knobs only (``timescale``, ``std``, ``compat``); never a free-form
    pass-through."""
    args: list[str] = []
    if "std" in nm.language_options:
        args += ["--std", nm.language_options["std"]]
    if "timescale" in nm.language_options:
        args += ["--timescale", nm.language_options["timescale"]]
    if "compat" in nm.language_options:
        args += ["--compat", nm.language_options["compat"]]
    return args


def top_param_args(nm: NormalizedManifest) -> list[str]:
    """``-G <top>.<name>=<value>`` per top parameter. The scoped form is the only one used: slang
    12 applies it to that top alone (an unknown parameter is a slang error, not a silent drop)."""
    args: list[str] = []
    for tp in nm.top_parameters:
        args += ["-G", f"{tp.top}.{tp.name}={tp.value}"]
    return args


def to_slang_args(nm: NormalizedManifest, files_root: Path) -> list[str]:
    """Return argv (without the executable name).

    `files_root` is the directory logical paths are mounted under (normally
    `<snapshot>/files`; external roots live under `_ext/<id>/`).

    - single unit: `--single-unit`
    - per_file: slang's default (each file its own compilation unit), no flag
    - explicit_groups: not representable as one argv; raises CAPABILITY_MISMATCH until the M2
      adapter defines per-group `-C` file lists
    - `-G TOP.NAME=VALUE`: top-scoped parameter override (slang 12 hierarchical form, verified:
      it overrides only that top; a plain `-G NAME=VALUE` would hit every top declaring NAME)
    - defines are passed as two array items (`-D`, `NAME=VALUE`) with the value token-exact
      (quotes preserved as characters of the macro text)
    """
    if nm.cu_mode == "explicit_groups":
        raise SvwError(
            ErrorCode.CAPABILITY_MISMATCH,
            "explicit_groups cannot be expressed as a single slang argv yet (needs M2 adapter)",
            {"cu_mode": nm.cu_mode},
        )

    def p(logical: str) -> str:
        return str(files_root / snapshot_rel(logical))

    unit = nm.units[0]  # single/per_file: includes, defines, library are identical in all units
    args: list[str] = []
    args += language_args(nm)
    if nm.cu_mode == "single":
        args.append("--single-unit")
    for d in unit.include_dirs:
        args += ["-I", p(d.path)]
    for df in unit.defines:
        args += ["-D", df.name if df.value is None else f"{df.name}={df.value}"]
    for t in nm.top_modules:
        args += ["--top", t]
    args += top_param_args(nm)
    for lf in unit.library_files:
        args += ["-v", p(lf.path)]
    for ld in unit.library_dirs:
        args += ["-y", p(ld.path)]
    for ext in unit.library_extensions:
        args += ["--libext", ext]
    for u in nm.units:
        for s in u.sources:
            args.append(p(s.path))
        if nm.cu_mode == "single":
            break
    return args
