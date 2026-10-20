"""Tool registry and dispatch (plan 14.2, 14.3).

``Gateway.call`` is the ONLY path from a request to a use case: it validates the arguments against
the tool's input schema first (API-01: an invalid request never reaches a backend, the database
or the job manager), runs the matching function of ``application.services`` and returns the common
envelope. ``svw_edit`` is added by M4 through :func:`register_tool`; nothing is stubbed here.
"""

from __future__ import annotations

import contextlib
import json
import re
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from jsonschema import Draft202012Validator, ValidationError, validators

from svweave.application import services
from svweave.application.events import EventLog
from svweave.application.runner import execute
from svweave.application.session import Session
from svweave.domain.envelope import Envelope, Outcome, error_envelope, new_request_id
from svweave.domain.errors import ErrorCode, SvwError
from svweave.gateway import schemas
from svweave.security.redaction import Redactor

MAX_REQUEST_BYTES = 64 * 1024


def _pattern(validator: Any, patrn: str, instance: Any, schema: Any) -> Any:
    """`pattern` as a FULL match: Python's `$` would accept a trailing newline."""
    if validator.is_type(instance, "string") and re.fullmatch(patrn, instance) is None:
        yield ValidationError(f"does not match {patrn!r}")


_Validator = validators.extend(Draft202012Validator, {"pattern": _pattern})  # type: ignore[no-untyped-call]
Handler = Callable[[Session, str, dict[str, Any]], Envelope]

_RO = {
    "readOnlyHint": True,
    "destructiveHint": False,
    "idempotentHint": True,
    "openWorldHint": False,
}
#: Not read-only: may start backend processes / write derived caches (status probe, graph index).
_DERIVED = {
    "readOnlyHint": False,
    "destructiveHint": False,
    "idempotentHint": True,
    "openWorldHint": False,
}


@dataclass(frozen=True)
class ToolSpec:
    name: str
    title: str
    description: str
    annotations: dict[str, bool]
    handler: Handler
    input_schema: dict[str, Any]
    output_schema: dict[str, Any]
    _validator: Draft202012Validator = field(init=False, repr=False, compare=False)

    def __post_init__(self) -> None:
        Draft202012Validator.check_schema(self.input_schema)
        object.__setattr__(self, "_validator", _Validator(self.input_schema))

    def validate(self, args: dict[str, Any]) -> None:
        errs = sorted(self._validator.iter_errors(args), key=lambda e: list(map(str, e.path)))
        if not errs:
            return
        shown = []
        for e in errs[:10]:
            shown.append(
                {
                    "path": "/" + "/".join(str(p) for p in e.absolute_path),
                    "validator": e.validator,
                    # never echo the offending value of pattern/length violations
                    "message": (
                        f"violates {e.validator}"
                        if e.validator in ("pattern", "maxLength", "minLength", "enum", "const")
                        else e.message[:160]
                    ),
                }
            )
        raise SvwError(
            ErrorCode.INVALID_CONFIG,
            f"invalid arguments for {self.name}",
            {"errors": shown, "tool": self.name},
        )


def _drop_none(d: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in d.items() if v is not None}


def _status(s: Session, rid: str, a: dict[str, Any]) -> Envelope:
    return services.status(s, rid, profile=a.get("profile"), probe=bool(a.get("probe", False)))


def _build(s: Session, rid: str, a: dict[str, Any]) -> Envelope:
    return services.build(
        s,
        rid,
        profile=a["profile"],
        reuse=bool(a.get("reuse", True)),
        expected_snapshot_id=a.get("expected_snapshot_id"),
    )


def _job(s: Session, rid: str, a: dict[str, Any]) -> Envelope:
    return services.job(s, rid, job_id=a["job_id"], action=a.get("action", "status"))


def _search(s: Session, rid: str, a: dict[str, Any]) -> Envelope:
    return services.search(
        s,
        rid,
        snapshot_id=a["snapshot_id"],
        query=a["query"],
        scope=a.get("scope"),
        limit=a.get("limit"),
        cursor=a.get("cursor"),
    )


def _symbol(s: Session, rid: str, a: dict[str, Any]) -> Envelope:
    loc = a.get("location") or {}
    return services.symbol(
        s,
        rid,
        build_id=a["build_id"],
        symbol_id=a.get("symbol_id"),
        file=loc.get("file"),
        byte=loc.get("byte"),
        name=a.get("name"),
        limit=a.get("limit"),
        cursor=a.get("cursor"),
    )


def _references(s: Session, rid: str, a: dict[str, Any]) -> Envelope:
    return services.references(
        s,
        rid,
        build_id=a["build_id"],
        symbol_id=a["symbol_id"],
        scope=a.get("scope"),
        limit=a.get("limit"),
        cursor=a.get("cursor"),
    )


def _hierarchy(s: Session, rid: str, a: dict[str, Any]) -> Envelope:
    return services.hierarchy(
        s,
        rid,
        build_id=a["build_id"],
        instance=a.get("instance_id") or a.get("instance_path"),
        top=a.get("top"),
        depth=a.get("depth"),
        limit=a.get("limit"),
        cursor=a.get("cursor"),
    )


def _connections(s: Session, rid: str, a: dict[str, Any]) -> Envelope:
    return services.connections(
        s,
        rid,
        build_id=a["build_id"],
        instance=a.get("instance_id") or a["instance_path"],
        port=a.get("port"),
        limit=a.get("limit"),
        cursor=a.get("cursor"),
    )


def _impact(s: Session, rid: str, a: dict[str, Any]) -> Envelope:
    loc = a.get("location") or {}
    return services.impact(
        s,
        rid,
        build_id=a["build_id"],
        symbol_id=a.get("symbol_id"),
        file=loc.get("file"),
        byte=loc.get("byte"),
        name=a.get("name"),
        instance=a.get("instance_id") or a.get("instance_path"),
        depth=a.get("depth", 3),
        limit=a.get("limit"),
    )


def _diff_impact(s: Session, rid: str, a: dict[str, Any]) -> Envelope:
    return services.diff_impact(
        s,
        rid,
        build_id=a["build_id"],
        base=a.get("base", "HEAD"),
        head=a.get("head"),
        depth=a.get("depth", 3),
        limit=a.get("limit"),
    )


def _assertions(s: Session, rid: str, a: dict[str, Any]) -> Envelope:
    return services.assertions(
        s,
        rid,
        build_id=a["build_id"],
        scope=a.get("scope"),
        entry_type=a.get("kind"),
        limit=a.get("limit"),
        cursor=a.get("cursor"),
    )


def _evidence(s: Session, rid: str, a: dict[str, Any]) -> Envelope:
    return services.evidence(
        s,
        rid,
        build_id=a.get("build_id"),
        evidence_id=a.get("evidence_id"),
        evidence_type=a.get("type"),
        limit=a.get("limit"),
        cursor=a.get("cursor"),
    )


def _edit(s: Session, rid: str, a: dict[str, Any]) -> Envelope:
    args = {k: v for k, v in a.items() if k != "action"}
    return services.edit(s, rid, action=a["action"], **args)


def _spec(name: str, title: str, desc: str, ann: dict[str, bool], h: Handler) -> ToolSpec:
    return ToolSpec(
        name, title, desc, dict(ann), h, schemas.input_schema(name), schemas.output_schema(name)
    )


TOOLS: dict[str, ToolSpec] = {}


def register_tool(spec: ToolSpec) -> None:
    """Registration point for tools added by later milestones (``svw_edit`` in M4)."""
    if spec.name in TOOLS:
        raise ValueError(f"tool {spec.name} already registered")
    TOOLS[spec.name] = spec


_COMMON = (
    " Returns the SVWeave envelope: check outcome, freshness and analysis_coverage; PARTIAL, "
    "STALE or truncated results never prove absence."
)

for _t in (
    _spec(
        "svw_status",
        "Workspace status",
        "Backend health (NOT_PROBED unless probe=true), capability summary, current build per "
        "profile with freshness, pending edits, isolation mode and client-boundary state. "
        "probe=true starts backend processes and is slow." + _COMMON,
        _DERIVED,
        _status,
    ),
    _spec(
        "svw_build",
        "Start build job",
        "Start a background build job for a profile and return its job_id immediately. Writes "
        "SVWeave metadata/cache only, never source. One build runs at a time; use svw_job to "
        "poll or cancel." + _COMMON,
        {
            "readOnlyHint": False,
            "destructiveHint": False,
            "idempotentHint": False,
            "openWorldHint": False,
        },
        _build,
    ),
    _spec(
        "svw_job",
        "Job status or cancel",
        "Status of a build job, or cancel it (kills the worker process tree)." + _COMMON,
        {
            "readOnlyHint": False,
            "destructiveHint": False,
            "idempotentHint": True,
            "openWorldHint": False,
        },
        _job,
    ),
    _spec(
        "svw_search",
        "Search snapshot (candidates)",
        "Graph or literal-text candidates in an immutable snapshot (snapshot_id required). "
        "Candidates are SOURCE_CANDIDATE evidence only, not build-specific facts." + _COMMON,
        _DERIVED,
        _search,
    ),
    _spec(
        "svw_symbol",
        "Symbol declaration",
        "Declaration lookup in a build by symbol_id, source location (file + zero-based byte) or "
        "name; several matches return AMBIGUOUS_SYMBOL with candidates." + _COMMON,
        _RO,
        _symbol,
    ),
    _spec(
        "svw_references",
        "Symbol references",
        "Resolved and unresolved occurrences of a symbol in a build (paged)." + _COMMON,
        _RO,
        _references,
    ),
    _spec(
        "svw_hierarchy",
        "Instance hierarchy",
        "Instance tree of a build from a top, an instance_id or an instance_path (paged)."
        + _COMMON,
        _RO,
        _hierarchy,
    ),
    _spec(
        "svw_connections",
        "Port connections",
        "Direct formal/actual port associations of one instance (paged)." + _COMMON,
        _RO,
        _connections,
    ),
    _spec(
        "svw_impact",
        "Change impact",
        "What changing one declaration (module, port, signal, ...) or instance touches, from one "
        "build: instances, dependent/dependency definitions up to depth levels, port connection "
        "sites and references. A port and its implicit net are one target; other ambiguity "
        "returns AMBIGUOUS_SYMBOL. Lists are capped (limit) with complete totals; counts are not "
        "a risk score, absence is not proof of absence on PARTIAL/STALE results." + _COMMON,
        _RO,
        _impact,
    ),
    _spec(
        "svw_diff_impact",
        "Git diff impact",
        "What a git diff touches in a build: changed declarations, their owning modules, the "
        "instances and dependent definitions, changed ports with their connection sites. base "
        "(default HEAD) is the revision the build was made from, head defaults to the working "
        "tree; the repository is the project root. Runs a read-only `git diff`. Old-side lines are "
        "mapped onto the build's snapshot: exact only for files marked aligned. Header/macro "
        "changes are not re-expanded. Counts, not a risk score." + _COMMON,
        _RO,
        _diff_impact,
    ),
    _spec(
        "svw_assertions",
        "SVA inventory",
        "Static SVA inventory of a build (paged). Execution/proof status is NOT represented."
        + _COMMON,
        _RO,
        _assertions,
    ),
    _spec(
        "svw_evidence",
        "Evidence records",
        "Evidence of a build (static analysis record, imported external records, per-kind "
        "NOT_RUN states) or one record by evidence_id. External records are declared, never "
        "verified by SVWeave." + _COMMON,
        _RO,
        _evidence,
    ),
):
    register_tool(_t)


register_tool(
    _spec(
        "svw_edit",
        "Controlled edit",
        "Edit loop on a CANDIDATE copy: prepare (symbol_replacement by declaration_id, "
        "slang_symbol by qualified name / symbol_id for a task, function or constructor, or "
        "exact_range with expected_sha256) -> validate (static slang check of baseline and "
        "candidate, diff, patch hash) -> commit (only after a human approved the patch hash with "
        "`svweave edit approve` in a trusted terminal; this tool cannot approve and has no "
        "approval field) -> abort. Writes the original workspace only in commit. Editing is "
        "disabled by default. EDA / simulation / formal are NOT_RUN." + _COMMON,
        {
            "readOnlyHint": False,
            "destructiveHint": True,
            "idempotentHint": True,
            "openWorldHint": False,
        },
        _edit,
    )
)


@dataclass
class ToolOutcome:
    envelope: Envelope
    is_error: bool


class UnknownToolError(Exception):
    """A protocol-level problem: the tool does not exist."""


def is_tool_error(env: Envelope) -> bool:
    """Execution error vs legitimate domain result.

    ERROR is a failed execution; BLOCKED without data is a request that could not be served.
    PARTIAL (and BLOCKED builds that carry data) are valid domain results, not errors.
    """
    return env.outcome is Outcome.ERROR or (env.outcome is Outcome.BLOCKED and not env.data)


#: keys whose string value is verbatim source/diff text: a path-like substring there is code, not a
#: host path, so it is returned as is (the snapshot bytes are the data the user asked for)
_VERBATIM_KEYS = frozenset({"text", "snippet", "diff"})


def _scrub(obj: Any, redactor: Redactor, key: str | None = None) -> Any:
    """Recursive text-only redaction of structured output (host paths and secrets in strings).
    Unlike ``Redactor.value`` it never blanks a value because of its KEY name."""
    if isinstance(obj, str):
        return obj if key in _VERBATIM_KEYS else redactor.text(obj)
    if isinstance(obj, dict):
        return {k: _scrub(v, redactor, k if isinstance(k, str) else None) for k, v in obj.items()}
    if isinstance(obj, list | tuple):
        return [_scrub(v, redactor, key) for v in obj]
    return obj


_PATH_FIELDS = ("violations", "reasons", "limitations", "stderr_tail", "error", "run_dir")


def redact_envelope(env: Envelope, redactor: Redactor, *, deep: bool = False) -> Envelope:
    """Defense in depth (SEC-04): scrub host paths/secrets from the envelope's free text
    (warnings, error, analysis_coverage.limitations) and from the known path-bearing ``data``
    fields (blocked-build ``violations``, reasons, stderr tails...). ``deep`` (the MCP gateway)
    scrubs EVERY string of ``data`` except verbatim source/diff text; the local CLI keeps the
    paths it prints on purpose (policy locations, run directories) unless they sit in a known
    path-bearing field."""
    d = env.model_copy(deep=True)
    d.warnings = [redactor.text(w) for w in d.warnings]
    if d.error is not None:
        d.error.message = redactor.text(d.error.message)
        d.error.details = redactor.value(d.error.details) if d.error.details else {}
    if d.analysis_coverage is not None:
        d.analysis_coverage.limitations = [redactor.text(x) for x in d.analysis_coverage.limitations]
        if d.analysis_coverage.scope:
            d.analysis_coverage.scope = redactor.text(d.analysis_coverage.scope)
    if deep:
        d.data = _scrub(d.data, redactor)
    else:
        for k in _PATH_FIELDS:
            if k in d.data:
                d.data[k] = _scrub(d.data[k], redactor, k)
    return d


class Gateway:
    def __init__(self, session: Session, events: EventLog | None = None) -> None:
        self.session = session
        self.events = events or session.events

    def tools(self) -> list[ToolSpec]:
        return list(TOOLS.values())

    def call(self, name: str, arguments: dict[str, Any] | None) -> ToolOutcome:
        spec = TOOLS.get(name)
        if spec is None:
            raise UnknownToolError(name)
        rid = new_request_id()
        args = arguments or {}
        t0 = time.monotonic()
        validated: dict[str, Any] = {}  # filled ONLY after the schema accepted the request

        def run(r: str) -> Envelope:
            if not isinstance(args, dict):
                raise SvwError(ErrorCode.INVALID_CONFIG, "arguments must be an object")
            if len(json.dumps(args, default=str)) > MAX_REQUEST_BYTES:
                raise SvwError(
                    ErrorCode.INVALID_CONFIG, "request too large", {"max_bytes": MAX_REQUEST_BYTES}
                )
            spec.validate(args)  # nothing below this line runs for an invalid request
            validated.update(args)
            return spec.handler(self.session, r, args)

        # no request value reaches the error envelope or the log before validation
        env = execute(run, request_id=rid, workspace_id=self.session.workspace_id())
        if env.profile is None and isinstance(validated.get("profile"), str):
            env.profile = validated["profile"]
        with contextlib.suppress(SvwError):
            env = redact_envelope(env, self.session.redactor(), deep=True)
        if self.events is not None:
            bid = validated.get("build_id")
            self.events.emit(
                "gateway",
                f"tool:{name}",
                request_id=rid,
                build_id=bid if isinstance(bid, str) else None,
                job_id=env.data.get("job_id") if isinstance(env.data.get("job_id"), str) else None,
                duration_s=time.monotonic() - t0,
                outcome=env.outcome.value,
                error_code=env.error.code if env.error else None,
            )
        return ToolOutcome(env, is_tool_error(env))


__all__ = [
    "TOOLS",
    "Gateway",
    "ToolOutcome",
    "ToolSpec",
    "UnknownToolError",
    "register_tool",
    "redact_envelope",
    "error_envelope",
]
