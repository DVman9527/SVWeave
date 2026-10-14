"""Locate the pinned toolchain (``toolchain.lock.json``) and the frozen capability report."""

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from svweave.domain.errors import ErrorCode, SvwError

LOCK_NAME = "toolchain.lock.json"
REPORT_NAME = "capability_report.json"
ENV_ROOT = "SVWEAVE_TOOLCHAIN_ROOT"


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def installed_root() -> Path:
    """Root of the INSTALLED tool (where its ``toolchain.lock.json`` ships). Derived from this
    module's own location, never from the cwd or from a repository under analysis."""
    return Path(__file__).resolve().parents[3]


def locate_root(explicit: Path | None = None) -> Path | None:
    """THE single toolchain-root discovery (build identity, backend registry, doctor).

    Order: an explicitly passed root, the ``SVWEAVE_TOOLCHAIN_ROOT`` environment variable, then the
    installed tool's own root. There is deliberately NO upward search from the cwd and none from
    the analysed repository: a hostile repository must not be able to supply the lock (SEC-03).
    An explicit/env root that has no lock yields ``None`` (no silent fallback)."""
    if explicit is not None:
        cand = Path(explicit)
    elif os.environ.get(ENV_ROOT):
        cand = Path(os.environ[ENV_ROOT])
    else:
        cand = installed_root()
    return cand if (cand / LOCK_NAME).is_file() else None


@dataclass(frozen=True)
class Toolchain:
    """Paths and pins of the external backends. Immutable; read from the lock file."""

    root: Path
    lock: dict[str, Any]
    report: dict[str, Any]

    @classmethod
    def discover(cls, root: Path | None = None) -> Toolchain:
        found = locate_root(root)
        if found is not None:
            return cls.load(found)
        raise SvwError(
            ErrorCode.MISSING_INPUT,
            f"{LOCK_NAME} not found (set {ENV_ROOT} or pass the toolchain root)",
            {"searched": str(root or os.environ.get(ENV_ROOT) or installed_root())},
        )

    @classmethod
    def load(cls, root: Path) -> Toolchain:
        root = Path(root).resolve()
        try:
            lock = json.loads((root / LOCK_NAME).read_text())
            report = json.loads((root / REPORT_NAME).read_text())
        except (OSError, ValueError) as e:
            raise SvwError(
                ErrorCode.MISSING_INPUT,
                f"cannot read toolchain lock/capability report: {e}",
                {"root": str(root)},
            ) from e
        return cls(root=root, lock=lock, report=report)

    # ----------------------------------------------------------------- components
    def component(self, name: str) -> dict[str, Any]:
        comp = self.lock.get("components", {}).get(name)
        if not isinstance(comp, dict):
            raise SvwError(ErrorCode.MISSING_INPUT, f"lock has no component {name!r}", {"component": name})
        return comp

    @property
    def cbm_binary(self) -> Path:
        return self.root / str(self.component("codebase_memory_mcp")["binary"])

    @property
    def cbm_binary_sha256(self) -> str:
        return str(self.component("codebase_memory_mcp")["binary_sha256"])

    @property
    def serena_venv(self) -> Path:
        return self.root / str(self.component("serena")["venv_dir"])

    @property
    def serena_exe(self) -> Path:
        return self.serena_venv / "bin" / "serena"

    @property
    def serena_version(self) -> str:
        pkg = str(self.component("serena")["package"])
        return pkg.split("==", 1)[1]

    @property
    def verible_ls(self) -> Path:
        return self.root / str(self.component("verible")["binary"])

    @property
    def verible_bin_dir(self) -> Path:
        return self.verible_ls.parent

    @property
    def verible_ls_sha256(self) -> str:
        return str(self.component("verible")["binary_sha256"])

    @property
    def pyslang_version(self) -> str:
        return str(self.component("pyslang")["version"])

    @property
    def toolchain_dir(self) -> Path:
        return self.root / ".toolchain"

    @property
    def fixtures_dir(self) -> Path:
        return self.root / "fixtures"

    # ----------------------------------------------------------------- report
    def backend_report(self, key: str) -> dict[str, Any]:
        b = self.report.get("backends", {}).get(key)
        if not isinstance(b, dict):
            raise SvwError(
                ErrorCode.CAPABILITY_MISMATCH,
                f"capability report has no backend {key!r}",
                {"backend": key},
            )
        return b

    def pinned_protocol(self, key: str) -> str:
        v = self.backend_report(key).get("protocol_version")
        if not isinstance(v, str):
            raise SvwError(
                ErrorCode.CAPABILITY_MISMATCH,
                f"capability report has no pinned protocol_version for {key!r}",
                {"backend": key},
            )
        return v

    def pinned_tool_hashes(self, key: str) -> dict[str, str]:
        digest = self.backend_report(key).get("schema_digest", {})
        per = digest.get("per_tool_schema_sha256", {})
        return {str(k): str(v) for k, v in per.items()}
