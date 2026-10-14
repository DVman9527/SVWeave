"""Resolver for dialect `svweave-filelist-v1` (plan 8.3 / 8.4). See docs/manifest.md."""

from __future__ import annotations

import os
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from svweave.config.context import WorkspaceContext
from svweave.config.models import CompilationGroup, Define, Profile, SourceInput
from svweave.domain.errors import ErrorCode, SvwError
from svweave.domain.ids import sha256_hex
from svweave.manifest.lexer import (
    Token,
    env_names,
    expand_env,
    shell_syntax_reason,
    split_unquoted,
    tokenize,
    unquote,
)
from svweave.manifest.models import (
    DefineRef,
    DirRef,
    GeneratedRef,
    IgnoredOption,
    LedgerEntry,
    LibraryRef,
    NormalizedManifest,
    Origin,
    SourceRef,
    TopParam,
    Unit,
)

MAX_DEPTH = 32
MAX_FILELIST_BYTES = 4 * 1024 * 1024

#: Options accepted but ignored (output/log only); each lands in the ignored_options ledger.
#: value = number of following argument tokens consumed.
IGNORED_OPTIONS: dict[str, int] = {"-l": 1, "-logfile": 1, "-nologo": 0, "-quiet": 0, "-q": 0}

_DEFINE_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_$]*$")


@dataclass
class _Scope:
    """Accumulator for one compilation scope (global, or one explicit group)."""

    id: str
    sources: list[SourceRef] = field(default_factory=list)
    includes: list[DirRef] = field(default_factory=list)
    defines: list[DefineRef] = field(default_factory=list)
    lib_files: list[LibraryRef] = field(default_factory=list)
    lib_dirs: list[LibraryRef] = field(default_factory=list)
    lib_exts: list[str] = field(default_factory=list)

    def define_map(self) -> dict[str, DefineRef]:
        return {d.name: d for d in self.defines}


class _Resolver:
    def __init__(
        self, ctx: WorkspaceContext, name: str, profile: Profile, environ: Mapping[str, str]
    ) -> None:
        self.ctx = ctx
        self.name = name
        self.profile = profile
        self.environ = environ
        self.allow = set(profile.environment_allowlist)
        self.ledger: list[LedgerEntry] = []
        self.issues: list[dict[str, Any]] = []
        self.ignored: list[IgnoredOption] = []
        self.visited: list[str] = []
        self.hashes: dict[str, str] = {}
        # cwd is only a resolution base: trust-checked, but not subject to include/exclude globs
        self.cwd = ctx.resolve(profile.cwd, ctx.project_root, is_dir=True, rules=False)
        self.generated_logicals = self._generated_logicals()

    # -------------------------------------------------------------- helpers
    def _generated_logicals(self) -> set[str]:
        out: set[str] = set()
        for g in self.profile.generated_inputs:
            out.add(self.ctx.resolve(g.path, self.cwd.abs).logical)
        return out

    def _issue(
        self, code: ErrorCode, message: str, origin: Origin, chain: list[str], **details: Any
    ) -> None:
        self.issues.append(
            {
                "code": code.value,
                "message": message,
                "origin": origin.model_dump(exclude_none=True),
                "chain": list(chain),
                **details,
            }
        )

    def _log(
        self, kind: str, raw: str, origin: Origin, chain: list[str], scope: str | None, **kw: Any
    ) -> None:
        self.ledger.append(
            LedgerEntry(
                seq=len(self.ledger),
                kind=kind,
                raw=raw,
                origin=origin,
                chain=list(chain),
                unit=scope,
                **kw,
            )
        )

    def _exists(self, rp_real: Path, want_dir: bool) -> bool:
        return rp_real.is_dir() if want_dir else rp_real.is_file()

    def _expand(self, text: str, origin: Origin, chain: list[str]) -> str | None:
        """Env expansion for path-like tokens: allowlisted AND present AND non-empty, or block."""
        values: dict[str, str] = {}
        ok = True
        for var in env_names(text):
            if var not in self.allow:
                self._issue(
                    ErrorCode.MISSING_INPUT,
                    f"environment variable {var} is not in environment_allowlist",
                    origin,
                    chain,
                    reason="env_not_allowlisted",
                    variable=var,
                )
                ok = False
            elif not self.environ.get(var):
                self._issue(
                    ErrorCode.MISSING_INPUT,
                    f"allowlisted environment variable {var} is unset or empty",
                    origin,
                    chain,
                    reason="env_not_set",
                    variable=var,
                )
                ok = False
            else:
                values[var] = self.environ[var]
        return expand_env(text, values) if ok else None

    def _scan(self, tok: Token, origin: Origin, chain: list[str]) -> bool:
        reason = shell_syntax_reason(tok.raw)
        if reason is None:
            return True
        self._issue(
            ErrorCode.UNSUPPORTED_OPTION,
            f"{reason} is never evaluated; export a literal filelist: {tok.raw!r}",
            origin,
            chain,
            reason="shell_syntax",
            option=tok.raw,
        )
        return False

    def _path_text(self, raw: str, origin: Origin, chain: list[str]) -> str | None:
        return self._expand(unquote(raw), origin, chain)

    # -------------------------------------------------------------- scope entries
    def _add_source(
        self, scope: _Scope, text: str, raw: str, base: Path, origin: Origin, chain: list[str]
    ) -> None:
        rp = self.ctx.resolve(text, base)
        if not self._exists(rp.real, False):
            reason = "generated_input_missing" if rp.logical in self.generated_logicals else "file"
            self._issue(
                ErrorCode.MISSING_INPUT,
                f"source file not found: {rp.logical}",
                origin,
                chain,
                reason=reason if reason != "file" else "source_missing",
                path=rp.logical,
            )
            return
        base_logical = self.ctx.logical_of(base)
        scope.sources.append(
            SourceRef(path=rp.logical, raw=raw, origin=origin, base=base_logical, chain=list(chain))
        )
        self._log("source", raw, origin, chain, scope.id, resolved=rp.logical, base=base_logical)

    def _add_dir(
        self,
        scope: _Scope,
        kind: str,
        text: str,
        raw: str,
        base: Path,
        origin: Origin,
        chain: list[str],
    ) -> None:
        rp = self.ctx.resolve(text, base, is_dir=True)
        if not self._exists(rp.real, True):
            self._issue(
                ErrorCode.MISSING_INPUT,
                f"directory not found: {rp.logical}",
                origin,
                chain,
                reason="dir_missing",
                path=rp.logical,
            )
            return
        base_logical = self.ctx.logical_of(base)
        if kind == "include_dir":
            scope.includes.append(DirRef(path=rp.logical, raw=raw, origin=origin))
        else:
            scope.lib_dirs.append(LibraryRef(path=rp.logical, raw=raw, origin=origin))
        self._log(kind, raw, origin, chain, scope.id, resolved=rp.logical, base=base_logical)

    def _add_libfile(
        self, scope: _Scope, text: str, raw: str, base: Path, origin: Origin, chain: list[str]
    ) -> None:
        rp = self.ctx.resolve(text, base)
        if not self._exists(rp.real, False):
            self._issue(
                ErrorCode.MISSING_INPUT,
                f"library file not found: {rp.logical}",
                origin,
                chain,
                reason="library_missing",
                path=rp.logical,
            )
            return
        scope.lib_files.append(LibraryRef(path=rp.logical, raw=raw, origin=origin))
        self._log(
            "library_file",
            raw,
            origin,
            chain,
            scope.id,
            resolved=rp.logical,
            base=self.ctx.logical_of(base),
        )

    def _add_define(
        self, scope: _Scope, part: str, raw: str, origin: Origin, chain: list[str]
    ) -> None:
        name, eq, value = part.partition("=")
        if not _DEFINE_NAME.match(name):
            raise SvwError(
                ErrorCode.INVALID_CONFIG,
                f"{origin.file}:{origin.line}: invalid macro name {name!r} in {raw!r}",
                {"origin": origin.model_dump(exclude_none=True)},
            )
        self._put_define(scope, name, value if eq else None, part, origin, chain)

    def _put_define(
        self,
        scope: _Scope,
        name: str,
        value: str | None,
        raw: str,
        origin: Origin,
        chain: list[str],
    ) -> None:
        prev = scope.define_map().get(name)
        if prev is not None:
            if prev.value != value:
                raise SvwError(
                    ErrorCode.INVALID_CONFIG,
                    f"conflicting definitions of macro {name}: {prev.value!r} "
                    f"({prev.origin.file}:{prev.origin.line}) vs {value!r} "
                    f"({origin.file}:{origin.line})",
                    {
                        "reason": "conflicting_define",
                        "name": name,
                        "first": prev.origin.model_dump(exclude_none=True),
                        "second": origin.model_dump(exclude_none=True),
                    },
                )
            self._log(
                "define",
                raw,
                origin,
                chain,
                scope.id,
                name=name,
                value=value,
                classification="duplicate_identical",
            )
            return
        scope.defines.append(DefineRef(name=name, value=value, raw=raw, origin=origin))
        self._log("define", raw, origin, chain, scope.id, name=name, value=value)

    def _add_ext(self, scope: _Scope, ext: str, raw: str, origin: Origin, chain: list[str]) -> None:
        if not ext.startswith("."):
            raise SvwError(
                ErrorCode.INVALID_CONFIG,
                f"{origin.file}:{origin.line}: +libext+ entries must start with '.': {ext!r}",
                {"origin": origin.model_dump(exclude_none=True)},
            )
        if ext not in scope.lib_exts:
            scope.lib_exts.append(ext)
        self._log("library_ext", raw, origin, chain, scope.id, value=ext)

    # -------------------------------------------------------------- filelist
    def _filelist(
        self,
        scope: _Scope,
        raw_path: str,
        base: Path,
        content_mode: str,
        origin: Origin,
        chain: list[str],
        stack: list[Path],
    ) -> None:
        rp = self.ctx.resolve(raw_path, base)
        depth = len(chain) + 1
        if rp.real in stack:
            full = [*chain, rp.logical]
            raise SvwError(
                ErrorCode.MISSING_INPUT,
                "filelist include cycle: " + " -> ".join(full),
                {
                    "reason": "filelist_cycle",
                    "chain": full,
                    "origin": origin.model_dump(exclude_none=True),
                },
            )
        if depth > MAX_DEPTH:
            full = [*chain, rp.logical]
            raise SvwError(
                ErrorCode.MISSING_INPUT,
                f"filelist nesting exceeds max depth {MAX_DEPTH}",
                {"reason": "filelist_depth_exceeded", "max_depth": MAX_DEPTH, "chain": full},
            )
        if not rp.real.is_file():
            self._issue(
                ErrorCode.MISSING_INPUT,
                f"filelist not found: {rp.logical}",
                origin,
                chain,
                reason="filelist_missing",
                path=rp.logical,
            )
            return
        try:
            if rp.real.stat().st_size > MAX_FILELIST_BYTES:
                raise SvwError(
                    ErrorCode.RESOURCE_LIMIT,
                    f"filelist too large: {rp.logical}",
                    {"path": rp.logical},
                )
            raw_bytes = rp.real.read_bytes()
            text = raw_bytes.decode("utf-8")
        except UnicodeDecodeError as e:
            raise SvwError(
                ErrorCode.INVALID_CONFIG,
                f"filelist is not UTF-8: {rp.logical}",
                {"path": rp.logical},
            ) from e
        digest = sha256_hex(raw_bytes)
        if self.hashes.setdefault(rp.logical, digest) != digest:
            raise SvwError(
                ErrorCode.WORKSPACE_UNSTABLE,
                f"filelist changed while resolving: {rp.logical}",
                {"path": rp.logical},
            )
        self.visited.append(rp.logical)
        self._log(
            "filelist",
            raw_path,
            origin,
            chain,
            scope.id,
            resolved=rp.logical,
            base=self.ctx.logical_of(base),
            classification=content_mode,
        )
        content_base = self.cwd.abs if content_mode == "cwd" else rp.abs.parent
        new_chain = [*chain, rp.logical]
        tokens = tokenize(text, rp.logical)
        self._tokens(scope, tokens, rp.logical, content_base, new_chain, [*stack, rp.real])

    def _tokens(
        self,
        scope: _Scope,
        toks: list[Token],
        fname: str,
        base: Path,
        chain: list[str],
        stack: list[Path],
    ) -> None:
        i = 0

        def org(t: Token) -> Origin:
            return Origin(file=fname, line=t.line, col=t.col)

        def arg(opt: Token) -> Token:
            nonlocal i
            if i >= len(toks):
                raise SvwError(
                    ErrorCode.INVALID_CONFIG,
                    f"{fname}:{opt.line}: option {opt.raw} requires an argument",
                    {"origin": org(opt).model_dump()},
                )
            t = toks[i]
            i += 1
            return t

        while i < len(toks):
            t = toks[i]
            i += 1
            raw, o = t.raw, org(t)
            if not self._scan(t, o, chain):
                continue
            if raw in ("-f", "-F"):
                a = arg(t)
                if not self._scan(a, org(a), chain):
                    continue
                text = self._path_text(a.raw, org(a), chain)
                if text is None:
                    continue
                self._filelist(
                    scope, text, base, "cwd" if raw == "-f" else "filelist", org(a), chain, stack
                )
            elif raw in ("-v", "-y") or raw == "-I" or (raw.startswith("-I") and len(raw) > 2):
                if raw in ("-v", "-y", "-I"):
                    a = arg(t)
                    if not self._scan(a, org(a), chain):
                        continue
                    rawv, ao = a.raw, org(a)
                else:
                    rawv, ao = raw[2:], o
                text = self._path_text(rawv, ao, chain)
                if text is None:
                    continue
                if raw == "-v":
                    self._add_libfile(scope, text, rawv, base, ao, chain)
                elif raw == "-y":
                    self._add_dir(scope, "library_dir", text, rawv, base, ao, chain)
                else:
                    self._add_dir(scope, "include_dir", text, rawv, base, ao, chain)
            elif raw.startswith("+incdir+"):
                parts = [p for p in split_unquoted(raw[len("+incdir+") :], "+") if p]
                if not parts:
                    raise SvwError(
                        ErrorCode.INVALID_CONFIG,
                        f"{fname}:{t.line}: empty +incdir+",
                        {"origin": o.model_dump()},
                    )
                for p in parts:
                    text = self._path_text(p, o, chain)
                    if text is not None:
                        self._add_dir(scope, "include_dir", text, p, base, o, chain)
            elif raw.startswith("+define+"):
                parts = [p for p in split_unquoted(raw[len("+define+") :], "+") if p]
                if not parts:
                    raise SvwError(
                        ErrorCode.INVALID_CONFIG,
                        f"{fname}:{t.line}: empty +define+",
                        {"origin": o.model_dump()},
                    )
                for p in parts:
                    self._add_define(scope, p, raw, o, chain)
            elif raw.startswith("-D"):
                body = raw[2:]
                if not body:
                    self._issue(
                        ErrorCode.UNSUPPORTED_OPTION,
                        "'-D NAME=VALUE' (separate argument) is not supported; use -DNAME=VALUE",
                        o,
                        chain,
                        reason="unsupported_form",
                        option=raw,
                    )
                    continue
                self._add_define(scope, body, raw, o, chain)
            elif raw.startswith("+libext+"):
                exts = [p for p in split_unquoted(raw[len("+libext+") :], "+") if p]
                if not exts:
                    raise SvwError(
                        ErrorCode.INVALID_CONFIG,
                        f"{fname}:{t.line}: empty +libext+",
                        {"origin": o.model_dump()},
                    )
                for ext in exts:
                    self._add_ext(scope, unquote(ext), raw, o, chain)
            elif raw in IGNORED_OPTIONS:
                args: list[str] = []
                for _ in range(IGNORED_OPTIONS[raw]):
                    args.append(arg(t).raw)
                self.ignored.append(
                    IgnoredOption(
                        option=raw,
                        args=args,
                        classification="output_only",
                        origin=o,
                        chain=list(chain),
                    )
                )
                self._log(
                    "ignored_option",
                    " ".join([raw, *args]),
                    o,
                    chain,
                    scope.id,
                    classification="output_only",
                )
            elif raw.startswith(("-", "+")):
                self._issue(
                    ErrorCode.UNSUPPORTED_OPTION,
                    f"unsupported option {raw!r}; not dropped, build is BLOCKED",
                    o,
                    chain,
                    reason="unknown_option",
                    option=raw,
                )
            else:
                text = self._path_text(raw, o, chain)
                if text is not None:
                    self._add_source(scope, text, raw, base, o, chain)

    def _source_input(self, scope: _Scope, si: SourceInput, field_path: str) -> None:
        origin = Origin(file="svweave.yaml", field=field_path)
        if si.kind == "file":
            self._add_source(scope, si.path, si.path, self.cwd.abs, origin, [])
        else:
            self._filelist(scope, si.path, self.cwd.abs, si.path_mode, origin, [], [])

    # -------------------------------------------------------------- scopes
    def _profile_level(self, scope: _Scope, src: Profile | CompilationGroup, prefix: str) -> None:
        for i, d in enumerate(src.include_dirs):
            self._add_dir(
                scope,
                "include_dir",
                d,
                d,
                self.cwd.abs,
                Origin(file="svweave.yaml", field=f"{prefix}.include_dirs[{i}]"),
                [],
            )
        for i, df in enumerate(src.defines):
            self._declared_define(scope, df, f"{prefix}.defines[{i}]")
        for i, f in enumerate(src.library_files):
            self._add_libfile(
                scope,
                f,
                f,
                self.cwd.abs,
                Origin(file="svweave.yaml", field=f"{prefix}.library_files[{i}]"),
                [],
            )
        for i, d in enumerate(src.library_dirs):
            self._add_dir(
                scope,
                "library_dir",
                d,
                d,
                self.cwd.abs,
                Origin(file="svweave.yaml", field=f"{prefix}.library_dirs[{i}]"),
                [],
            )
        for ext in src.library_extensions:
            scope.lib_exts.append(ext)

    def _declared_define(self, scope: _Scope, df: Define, fld: str) -> None:
        raw = df.name if df.value is None else f"{df.name}={df.value}"
        self._put_define(scope, df.name, df.value, raw, Origin(file="svweave.yaml", field=fld), [])

    def _unit(self, uid: str, group: str | None, sources: list[SourceRef], s: _Scope) -> Unit:
        return Unit(
            id=uid,
            group=group,
            sources=sources,
            include_dirs=list(s.includes),
            defines=list(s.defines),
            library_files=list(s.lib_files),
            library_dirs=list(s.lib_dirs),
            library_extensions=list(s.lib_exts),
        )

    def _generated(self) -> list[GeneratedRef]:
        out: list[GeneratedRef] = []
        for i, g in enumerate(self.profile.generated_inputs):
            origin = Origin(file="svweave.yaml", field=f"generated_inputs[{i}]")
            rp = self.ctx.resolve(g.path, self.cwd.abs)
            if not rp.real.is_file():
                self._issue(
                    ErrorCode.MISSING_INPUT,
                    f"generated input declared but not produced: {rp.logical}",
                    origin,
                    [],
                    reason="generated_input_missing",
                    path=rp.logical,
                )
                continue
            digest = sha256_hex(rp.real.read_bytes())
            if g.output_hash is not None and g.output_hash != digest:
                self._issue(
                    ErrorCode.MISSING_INPUT,
                    f"generated input {rp.logical} does not match declared output_hash",
                    origin,
                    [],
                    reason="generated_input_hash_mismatch",
                    path=rp.logical,
                )
                continue
            out.append(
                GeneratedRef(
                    path=rp.logical,
                    generator_command_id=g.generator_command_id,
                    declared_output_hash=g.output_hash,
                    sha256=digest,
                    provenance_incomplete=g.generator_command_id is None or g.output_hash is None,
                )
            )
            self._log("generated_input", g.path, origin, [], None, resolved=rp.logical)
        return out

    def _top_params(self) -> list[TopParam]:
        seen: dict[tuple[str, str], str] = {}
        out: list[TopParam] = []
        for tp in self.profile.top_parameters:
            key = (tp.top, tp.name)
            if key in seen:
                if seen[key] != tp.value:
                    raise SvwError(
                        ErrorCode.INVALID_CONFIG,
                        f"conflicting top parameter {tp.top}.{tp.name}: {seen[key]!r} vs {tp.value!r}",
                        {"reason": "conflicting_parameter", "top": tp.top, "name": tp.name},
                    )
                continue
            seen[key] = tp.value
            out.append(TopParam(top=tp.top, name=tp.name, value=tp.value))
        return out

    def _deviations(
        self, units: list[Unit], lang_opts: dict[str, str], stubs: list[dict[str, str]]
    ) -> list[str]:
        """Where the analysis knowingly differs from the original tree or from a vendor flow
        (plan 9.4, 8.3, 16.5). Part of config_id; surfaced in build results and the report."""
        out = [
            "`__FILE__` and other path-observable strings show the immutable snapshot path, not "
            "the original path (plan 9.4); not bit-identical to the original tree"
        ]
        ext: set[str] = set()
        for u in units:
            paths = [
                *(x.path for x in u.sources),
                *(x.path for x in u.include_dirs),
                *(x.path for x in u.library_files),
                *(x.path for x in u.library_dirs),
            ]
            ext.update(p[1:].partition("/")[0] for p in paths if p.startswith("@"))
        if ext:
            out.append(
                "external-root exemptions in effect for root(s) "
                + ", ".join(sorted(ext))
                + ": unsupported nodes and unrecordable references inside them do not make the "
                "build PARTIAL (reported as limitations only)"
            )
        for k, v in sorted(lang_opts.items()):
            out.append(f"language option {k}={v} supplied by the manifest (not from the sources)")
        if lang_opts.get("compat") == "vcs":
            out.append(
                "slang --compat vcs relaxes IEEE checks (hierarchical const, use-before-declare, "
                "enum/string conversions, ...) to approximate VCS leniency: diagnostics slang would "
                "report under strict IEEE mode may be absent, so a clean result is weaker evidence "
                "of standard conformance; vendor extensions slang still rejects remain errors"
            )
        for st in sorted(stubs, key=lambda x: x["module"]):
            out.append(
                f"declared stub for module {st['module']}: replaces {st['replaced_scope']!r} "
                f"(original IP {st['original_ip']!r}, model sha256 {st['model_sha256']}); PARTIAL"
            )
        return out

    def run(self) -> NormalizedManifest:
        p = self.profile
        mode = p.compilation_units.mode
        units: list[Unit] = []
        pre = f"profiles.{self.name}"
        if mode == "explicit_groups":
            for gi, g in enumerate(p.compilation_units.groups):
                scope = _Scope(id=g.id)
                gp = f"{pre}.compilation_units.groups[{gi}]"
                self._profile_level(scope, g, gp)
                for si_i, si in enumerate(g.sources):
                    self._source_input(scope, si, f"{gp}.sources[{si_i}]")
                units.append(self._unit(f"cu_{g.id}", g.id, scope.sources, scope))
        else:
            scope = _Scope(id="global")
            self._profile_level(scope, p, pre)
            for si_i, si in enumerate(p.source_inputs):
                self._source_input(scope, si, f"{pre}.source_inputs[{si_i}]")
            if mode == "single":
                units.append(self._unit("cu0", None, scope.sources, scope))
            else:
                for n, s in enumerate(scope.sources):
                    units.append(self._unit(f"cu{n}", None, [s], scope))
        generated = self._generated()
        tops = self._top_params()
        if self.issues:
            first = self.issues[0]
            raise SvwError(
                ErrorCode(first["code"]),
                f"{len(self.issues)} blocking issue(s); first: {first['message']}",
                {"issues": self.issues},
            )
        lang_opts = {
            k: str(v) for k, v in p.language_options.model_dump(exclude_none=True).items()
        }
        stubs = [x.model_dump() for x in self.ctx.manifest.analysis.stubs]
        nm = NormalizedManifest(
            dialect=p.source_inputs[0].dialect if p.source_inputs else "svweave-filelist-v1",
            workspace_id=self.ctx.workspace_id,
            profile=self.name,
            language_mode=p.language_mode,
            cwd=self.cwd.logical,
            cu_mode=mode,
            units=units,
            top_modules=list(p.top_modules),
            top_parameters=tops,
            generated_inputs=generated,
            ignored_options=self.ignored,
            filelists=self.visited,
            filelist_hashes=self.hashes,
            diagnostic_policy=p.diagnostic_policy,
            language_options=lang_opts,
            stubs=stubs,
            semantic_deviations=self._deviations(units, lang_opts, stubs),
            ledger=self.ledger,
        )
        return nm.model_copy(update={"config_id": nm.compute_config_id()})


def resolve_profile(
    ctx: WorkspaceContext, profile_name: str, environ: Mapping[str, str] | None = None
) -> NormalizedManifest:
    """Resolve a profile into a normalized manifest. Never executes anything.

    `environ` defaults to os.environ but only allowlisted variables are ever read.
    """
    name, profile = ctx.profile(profile_name)
    if not isinstance(profile, Profile):
        raise SvwError(ErrorCode.INVALID_CONFIG, f"profile {name!r} is malformed")
    return _Resolver(ctx, name, profile, environ if environ is not None else os.environ).run()


def write_manifest(ctx: WorkspaceContext, nm: NormalizedManifest) -> Path:
    """Write `.svweave/manifests/<config_id>.json` atomically."""
    import json

    out_dir = ctx.state_dir / "manifests"
    out_dir.mkdir(parents=True, exist_ok=True)
    dest = out_dir / f"{nm.config_id}.json"
    tmp = out_dir / f".{nm.config_id}.{os.getpid()}.tmp"
    tmp.write_text(
        json.dumps(nm.model_dump(mode="json"), indent=2, sort_keys=True, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    os.replace(tmp, dest)
    return dest
