"""Worker DTO -> identified rows + completeness verdict (plan 9.1, 13.1, 9.5).

Pure Python (no pyslang). Identity rules (plan 13.1):

* ``source_id``      = H(snapshot_id, logical path, file bytes hash)
* ``declaration_id`` = H(source_id, name span, kind, qualified scope, CU, macro context)
* ``symbol_id``      = H(build_id, declaration_id[, specialization])
* ``instance_id``    = H(build_id, instance path COMPONENTS, specialization)  (never a split string)
* ``reference_id``   = H(build_id, source occurrence, kind, target / unresolved reason)
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from svweave.domain.errors import ErrorCode, SvwError
from svweave.domain.ids import canonical_hash, sha256_hex
from svweave.domain.states import Completeness, DiagnosticOutcome
from svweave.snapshot.models import SnapshotRecord
from svweave.storage.semantic import dumps

STATIC_LIMITATIONS = [
    "Static elaboration of the selected tops only; no runtime, simulation or formal result is "
    "represented.",
    "Assertion execution and proof status are NOT_RUN (slang only parses and elaborates them).",
    "References are source occurrences visited by slang in elaborated code; uninstantiated "
    "generic classes, inactive generate/`ifdef branches and library members never loaded are not "
    "covered, so absence of a reference is not proof of absence.",
    "Packed/unpacked dimension expressions, return-type names and class names used only as "
    "scope prefixes (Class::member) are not extracted as references.",
]

#: gap kinds that make a build PARTIAL (the others are reported as limitations only)
BLOCKING_GAP_KINDS = frozenset({"stub_boundary", "empty_module_body", "protected_envelope"})


def logical_of_snapshot_rel(rel: str) -> str:
    """Inverse of config.context.snapshot_rel: ``_ext/<id>/rest`` -> ``@<id>/rest``."""
    if rel.startswith("_ext/"):
        rid, _, rest = rel[5:].partition("/")
        return f"@{rid}/{rest}"
    return rel


def snapshot_rel_of_logical(logical: str) -> str:
    if logical.startswith("@"):
        rid, _, rest = logical[1:].partition("/")
        return f"_ext/{rid}/{rest}"
    return logical


@dataclass
class Ingested:
    rows: dict[str, list[dict[str, Any]]]
    completeness: Completeness
    diagnostic_outcome: DiagnosticOutcome
    reasons: list[str]
    unresolved_count: int
    dynamic_count: int
    limitations: list[str]
    scope: str
    summary: dict[str, Any] = field(default_factory=dict)


class _Ids:
    def __init__(self, snap: SnapshotRecord, build_id: str) -> None:
        self.build_id = build_id
        self.snapshot_id = snap.snapshot_id
        self.hash_by_rel = {
            snapshot_rel_of_logical(f.logical_path): (f.logical_path, f.sha256) for f in snap.files
        }

    def source(self, rel: str | None) -> tuple[str | None, str | None]:
        """(logical path, source_id) of a snapshot-relative file, or (rel, None) if unknown."""
        if rel is None:
            return None, None
        hit = self.hash_by_rel.get(rel)
        if hit is None:
            return logical_of_snapshot_rel(rel), None
        logical, sha = hit
        return logical, canonical_hash(["source", self.snapshot_id, logical, sha])


def _slim_loc(loc: dict[str, Any] | None, ids: _Ids) -> dict[str, Any] | None:
    """Location with the file mapped to its logical path (snapshot paths never leak out)."""
    if loc is None:
        return None
    out = dict(loc)
    logical, sid = ids.source(loc["file"])
    out["file"] = logical
    out["source_id"] = sid
    if "macro" in out and out["macro"].get("expansion"):
        exp = dict(out["macro"]["expansion"])
        exp["file"], exp["source_id"] = ids.source(exp["file"])
        out["macro"] = {**out["macro"], "expansion": exp}
    return out


MAX_LISTED_GAPS_PER_KIND = 10


def _classify_gaps(
    gaps: list[dict[str, Any]], diags: list[dict[str, Any]], stubs: list[dict[str, str]]
) -> list[dict[str, Any]]:
    """Blackbox classification (plan 16.5): every protected envelope and every implicit
    empty-bodied module is a blocking gap; a module declared in the manifest stub records becomes
    a recorded ``stub_boundary`` (original IP, model hash, replaced scope), still PARTIAL."""
    declared = {x["module"]: x for x in stubs}
    out: list[dict[str, Any]] = []
    for g in gaps:
        g = dict(g)
        rec = declared.get(str(g.get("name")))
        if rec is not None and g["kind"] in ("empty_module_body", "stub_boundary"):
            g["kind"] = "stub_boundary"
            g["detail"] = (
                f"declared stub for {rec['module']!r}: original IP {rec['original_ip']!r}, model "
                f"sha256 {rec['model_sha256']}, replaced scope {rec['replaced_scope']!r}; "
                "internals unknown"
            )
            g["declared_stub"] = dict(rec)
        out.append(g)
    for d in diags:
        if d.get("code") == "ProtectedEnvelope":
            loc = d.get("loc") or {}
            out.append(
                {
                    "kind": "protected_envelope",
                    "name": str(loc.get("file") or "<unknown>"),
                    "detail": "encrypted `pragma protect envelope skipped entirely by slang; the "
                    "protected source is not analysed",
                }
            )
    return out


_PARAM_MODULE = re.compile(r"parameter '[^']+' does not exist in '([^']+)'\s*$")


def _protected_modules(result: dict[str, Any]) -> set[str]:
    """Names of modules with a ``pragma protect`` envelope inside their own source extent.

    The extent is the declaration's ``full`` span from the extractor. A module is reported only if
    its name is declared exactly once in the whole result (a same-named module in another file would
    make the diagnostic's target ambiguous), and a declaration without a ``full`` span is never
    matched."""
    envelopes: dict[str, list[int]] = {}
    for d in result["diagnostics"]:
        loc = d.get("loc")
        if d.get("code") == "ProtectedEnvelope" and loc:
            envelopes.setdefault(loc["file"], []).append(loc["start"])
    if not envelopes:
        return set()
    modules = [d for d in result["declarations"] if d["kind"] == "module"]
    count: dict[str, int] = {}
    for d in modules:
        count[d["name"]] = count.get(d["name"], 0) + 1
    out: set[str] = set()
    for d in modules:
        full = d.get("full")
        offs = envelopes.get(d["loc"]["file"], [])
        if count[d["name"]] == 1 and full and any(full["start"] <= o < full["end"] for o in offs):
            out.add(d["name"])
    return out


def _protected_parameter_errors(result: dict[str, Any]) -> dict[str, int]:
    """``ParameterDoesNotExist`` errors (module name -> count) whose target module has a protected
    envelope in its text. Both facts come straight from the extractor document (the diagnostic's
    message, the declaration and envelope positions); nothing is guessed."""
    protected = _protected_modules(result)
    if not protected:
        return {}
    hits: dict[str, int] = {}
    for d in result["diagnostics"]:
        if d.get("code") != "ParameterDoesNotExist" or not d.get("is_error"):
            continue
        m = _PARAM_MODULE.search(d.get("message") or "")
        if m and m.group(1) in protected:
            hits[m.group(1)] = hits.get(m.group(1), 0) + 1
    return hits


def ingest(
    result: dict[str, Any],
    snap: SnapshotRecord,
    build_id: str,
    job_id: str,
    tops: list[str],
    stubs: list[dict[str, str]] | None = None,
) -> Ingested:
    """``stubs`` are the manifest's declared stub records (module, original_ip, model_sha256,
    replaced_scope; plan 16.5). A declared stub is still a PARTIAL boundary, but a recorded one."""
    ids = _Ids(snap, build_id)
    decls = result["declarations"]
    # ---- declarations
    decl_id: list[str] = []
    drows: list[dict[str, Any]] = []
    seen: set[str] = set()
    first_idx: dict[str, int] = {}
    for d in decls:
        loc = d["loc"]
        logical, sid = ids.source(loc["file"])
        mac = loc.get("macro")
        mkey = [mac["name"], mac.get("expansion", {}).get("start")] if mac else None
        did = canonical_hash(
            [
                "decl",
                sid or logical,
                loc["start"],
                loc["end"],
                d["kind"],
                d["qualified_name"],
                d["cu"],
                mkey,
            ]
        )
        if did in first_idx:
            raise SvwError(
                ErrorCode.INTERNAL,
                "declaration_id collision: two distinct extractor declarations share one identity "
                f"({d['kind']} {d['qualified_name']!r} at {loc['file']}:{loc['start']}); "
                "refusing to merge them silently",
                {"first_idx": first_idx[did], "second_idx": d["idx"], "declaration_id": did},
            )
        first_idx[did] = d["idx"]
        decl_id.append(did)
    for d, did in zip(decls, decl_id, strict=True):
        seen.add(did)
        loc = _slim_loc(d["loc"], ids)
        assert loc is not None
        attrs = dict(d["attrs"])
        for k in ("override_decl_idx", "base_decl_idx", "internal_decl_idx"):
            if k in attrs:
                v = attrs.pop(k)
                attrs[k.replace("_decl_idx", "_declaration_id")] = (
                    decl_id[v] if v is not None else None
                )
        qn = d["qualified_name"]
        name = d["name"]
        scope = qn[: -len(name)].rstrip(":.") if name and qn.endswith(name) and qn != name else ""
        full = d["full"]
        drows.append(
            {
                "declaration_id": did,
                "build_id": build_id,
                "symbol_id": canonical_hash(["sym", build_id, did]),
                "scope": scope,
                "kind": d["kind"],
                "source_id": loc["source_id"],
                "span_start": loc["start"],
                "span_end": loc["end"],
                "type": d["type"],
                "compilation_unit": None if d["cu"] is None else f"cu{d['cu']}",
                "name": name,
                "qualified_name": qn,
                "file": loc["file"],
                "line": loc["line"],
                "col": loc["col"],
                "full_start": full["start"] if full else None,
                "full_end": full["end"] if full else None,
                "library": d["library"],
                "macro": dumps(loc.get("macro")),
                "attrs": dumps(attrs),
                "loc": dumps(loc),
                "ord": d["idx"],
            }
        )
    # ---- instances
    inst_id: list[str] = []
    for i in result["instances"]:
        inst_id.append(canonical_hash(["inst", build_id, i["components"], i["specialization"]]))
    irows: list[dict[str, Any]] = []
    for i, iid in zip(result["instances"], inst_id, strict=True):
        bind = i["bind"]
        if bind and bind.get("directive_loc"):
            bind = {**bind, "directive_loc": _slim_loc(bind["directive_loc"], ids)}
        didx = i["definition_decl_idx"]
        irows.append(
            {
                "instance_id": iid,
                "build_id": build_id,
                "parent_id": inst_id[i["parent_idx"]] if i["parent_idx"] is not None else None,
                "path": i["path"],
                "definition": i["definition"],
                "specialization": i["specialization"],
                "hier_path": i["hier_path"],
                "components": dumps(i["components"]),
                "parameters": dumps(i["parameters"]),
                "generate_context": dumps(i["generate_context"]),
                "is_top": int(i["is_top"]),
                "is_interface": int(i["is_interface"]),
                "is_stub": int(i["stub"]),
                "stub_name": i["stub_name"],
                "blackbox_candidate": int(i["blackbox_candidate"]),
                "definition_declaration_id": decl_id[didx] if didx is not None else None,
                "loc": dumps(_slim_loc(i["loc"], ids)),
                "bind": dumps(bind),
                "ord": i["idx"],
            }
        )
    # ---- references
    rrows: dict[str, dict[str, Any]] = {}
    for n, r in enumerate(result["references"]):
        loc = _slim_loc(r["loc"], ids)
        assert loc is not None
        tid = decl_id[r["target_idx"]] if r["target_idx"] is not None else None
        ctx = dict(r["context"])
        if "override_candidates" in ctx:
            ctx["override_candidates"] = [decl_id[x] for x in ctx["override_candidates"]]
        if "derived_decl_idx" in ctx:
            v = ctx.pop("derived_decl_idx")
            ctx["derived_declaration_id"] = decl_id[v] if v is not None else None
        mac = loc.get("macro")
        rid = canonical_hash(
            [
                "ref",
                build_id,
                loc["source_id"] or loc["file"],
                loc["start"],
                loc["end"],
                r["kind"],
                tid or [r["reason"], r["target_name"]],
                mac.get("expansion", {}).get("start") if mac else None,
            ]
        )
        ci = r.get("container_idx")
        rrows.setdefault(
            rid,
            {
                "reference_id": rid,
                "build_id": build_id,
                "source_id": loc["source_id"],
                "span_start": loc["start"],
                "span_end": loc["end"],
                "target_id": tid,
                "kind": r["kind"],
                "resolution_status": r["status"],
                "unresolved_reason": r["reason"],
                "file": loc["file"],
                "line": loc["line"],
                "col": loc["col"],
                "loc": dumps(loc),
                "target_name": r["target_name"],
                "context": dumps(ctx),
                "container_id": decl_id[ci] if ci is not None else None,
                "ord": n,
            },
        )
    # ---- port bindings
    brows: list[dict[str, Any]] = []
    for n, b in enumerate(result["port_bindings"]):
        refs = [
            {
                **x,
                "decl_idx": None,
                "declaration_id": decl_id[x["decl_idx"]] if x["decl_idx"] is not None else None,
                "loc": _slim_loc(x["loc"], ids),
            }
            for x in b["referenced"]
        ]
        for x in refs:
            x.pop("decl_idx", None)
        pd = b["port_decl_idx"]
        ii = b.get("interface_instance_idx")
        brows.append(
            {
                "build_id": build_id,
                "instance_id": inst_id[b["instance_idx"]],
                "formal_port": b["formal"],
                "actual_expression": b["actual_text"],
                "referenced_symbols": dumps(refs),
                "types": dumps({"port": b["port_type"], "actual": b["actual_type"]}),
                "direction": b["direction"],
                "style": b["style"],
                "port_kind": b["port_kind"],
                "port_declaration_id": decl_id[pd] if pd is not None else None,
                "actual_loc": dumps(_slim_loc(b["actual_loc"], ids)),
                "actual_kind": b["actual_kind"],
                "bit_width": b["bit_width"],
                "is_constant": int(b["is_constant"]),
                "implicit_actual": int(b["implicit_actual"]),
                "parts": dumps([{**p, "loc": _slim_loc(p["loc"], ids)} for p in b["parts"]]),
                "interface_instance_id": inst_id[ii] if ii is not None else None,
                "modport": b["modport"],
                "supported": int(b["supported"]),
                "note": b["note"],
                "ord": n,
            }
        )
    # ---- assertions
    arows: list[dict[str, Any]] = []
    aseen: set[str] = set()
    for n, a in enumerate(result["assertions"]):
        src = _slim_loc(a.get("source"), ids)
        iidx = a.get("instance_idx")
        aiid: str | None = inst_id[iidx] if iidx is not None else None
        meta = {k: v for k, v in a.items() if k not in ("source",)}
        if "elaborated" in meta:
            meta["elaborated"] = [
                {
                    "checker_instance_id": inst_id[e["checker_instance_idx"]],
                    "target_instance_id": inst_id[e["target_instance_idx"]]
                    if e["target_instance_idx"] is not None
                    else None,
                }
                for e in meta["elaborated"]
            ]
        for k in ("property_decl_idx", "decl_idx"):
            if k in meta:
                v = meta.pop(k)
                meta[k.replace("_idx", "_id").replace("decl_id", "declaration_id")] = (
                    decl_id[v] if v is not None else None
                )
        meta.pop("instance_idx", None)
        meta["source"] = src
        aid = canonical_hash(
            [
                "sva",
                build_id,
                a["entry_type"],
                a["kind"],
                (src or {}).get("source_id") or (src or {}).get("file"),
                (src or {}).get("start"),
                (src or {}).get("end"),
                aiid,
            ]
        )
        if aid in aseen:
            continue
        aseen.add(aid)
        arows.append(
            {
                "assertion_id": aid,
                "build_id": build_id,
                "scope": result["instances"][iidx]["path"] if iidx is not None else None,
                "kind": a["kind"],
                "metadata": dumps(meta),
                "entry_type": a["entry_type"],
                "name": a.get("name"),
                "instance_id": aiid,
                "file": src["file"] if src else None,
                "span_start": src["start"] if src else None,
                "span_end": src["end"] if src else None,
                "execution_status": "NOT_RUN",
                "ord": n,
            }
        )
    # ---- diagnostics
    grows: list[dict[str, Any]] = []
    for g in result["diagnostics"]:
        loc = _slim_loc(g["loc"], ids)
        grows.append(
            {
                "build_id": build_id,
                "job_id": job_id,
                "severity": g["severity"],
                "code": g["code"],
                "locations": dumps(
                    {
                        "primary": loc,
                        "ranges": [_slim_loc(r, ids) for r in g["ranges"]],
                        "macro_notes": g["macro_notes"],
                        "option": g["option"],
                    }
                ),
                "message": g["message"],
                "suppression": None,
                "is_error": int(g["is_error"]),
                "file": loc["file"] if loc else None,
                "span_start": loc["start"] if loc else None,
                "span_end": loc["end"] if loc else None,
            }
        )
    for m in result["driver_messages"]:
        low = m.lower()
        sev = (
            "error"
            if low.startswith("error")
            else ("warning" if low.startswith("warning") else "note")
        )
        grows.append(
            {
                "build_id": build_id,
                "job_id": job_id,
                "severity": sev,
                "code": "DriverMessage",
                "locations": dumps(
                    {"primary": None, "ranges": [], "macro_notes": [], "option": None}
                ),
                "message": m,
                "suppression": None,
                "is_error": int(sev == "error"),
                "file": None,
                "span_start": None,
                "span_end": None,
            }
        )
    # ---- specializations / inactive / gaps / unsupported
    srows: list[dict[str, Any]] = []
    for s in result["specializations"]:
        if "class_decl_idx" not in s:
            continue
        cdi = s["class_decl_idx"]
        cid = decl_id[cdi] if cdi is not None else None
        bi = s["base_decl_idx"]
        srows.append(
            {
                "build_id": build_id,
                "symbol_id": canonical_hash(["spec", build_id, cid, s["text"]]),
                "class_declaration_id": cid,
                "text": s["text"],
                "parameters": dumps(s["parameters"]),
                "base_class": s["base_class"],
                "base_declaration_id": decl_id[bi] if bi is not None else None,
                "methods": dumps([decl_id[m] for m in s["methods"]]),
            }
        )
    xrows: list[dict[str, Any]] = []
    for n, x in enumerate(result["inactive"]):
        loc = _slim_loc(x.get("loc"), ids)
        data = {k: v for k, v in x.items() if k not in ("loc",)}
        if "decl_idx" in data:
            v = data.pop("decl_idx")
            data["declaration_id"] = decl_id[v] if v is not None else None
        xrows.append(
            {
                "build_id": build_id,
                "ord": n,
                "kind": x["kind"],
                "name": x.get("name"),
                "file": loc["file"] if loc else None,
                "span_start": loc["start"] if loc else None,
                "span_end": loc["end"] if loc else None,
                "status": x["status"],
                "data": dumps({**data, "loc": loc}),
            }
        )
    gaps = _classify_gaps(result["gaps"], result["diagnostics"], stubs or [])
    gap_rows: list[dict[str, Any]] = []
    for n, g in enumerate(gaps):
        data = {k: v for k, v in g.items() if k not in ("kind", "name", "detail")}
        if "decl_idx" in data:
            v = data.pop("decl_idx")
            data["declaration_id"] = decl_id[v] if v is not None else None
        gap_rows.append(
            {
                "build_id": build_id,
                "ord": n,
                "kind": g["kind"],
                "name": g.get("name"),
                "detail": g.get("detail"),
                "data": dumps(data),
            }
        )
    urows: list[dict[str, Any]] = []
    project_unsupported: dict[str, int] = {}
    external_unsupported: dict[str, int] = {}
    for n, u in enumerate(result["unsupported_nodes"]):
        sample = _slim_loc(u["sample"], ids)
        scope_u = u.get("scope")
        if scope_u is None:  # older DTO: derive from the sample
            scope_u = "external" if sample and str(sample["file"]).startswith("@") else "project"
        exc = u["kind"].startswith("extractor_exception")
        in_scope = int(exc or scope_u == "project")
        bucket = project_unsupported if in_scope else external_unsupported
        bucket[u["kind"]] = bucket.get(u["kind"], 0) + u["count"]
        urows.append(
            {
                "build_id": build_id,
                "ord": n,
                "kind": u["kind"],
                "count": u["count"],
                "sample": dumps(sample),
                "detail": u["detail"] + f" [{scope_u}]",
                "in_scope": in_scope,
            }
        )
    in_scope_unsupported = sum(project_unsupported.values())
    drops = result.get("dropped_refs", [])
    project_drops = sum(d["count"] for d in drops if d["scope"] == "project")
    external_drops = sum(d["count"] for d in drops if d["scope"] != "project")
    # ---- verdict
    reasons: list[str] = []
    has_errors = any(g["is_error"] for g in grows)
    st = result["status"]
    if not st.get("compiled", False):
        has_errors = True
        reasons.append("driver could not build the compilation (command line rejected)")
    failed_steps = [
        k for k in ("parseCommandLine", "processOptions", "parseAllSources") if st.get(k) is False
    ]
    if failed_steps:  # M5: the driver's own verdict, never inferred from stderr text
        has_errors = True
        reasons.append("slang driver step(s) failed: " + ", ".join(failed_steps))
    if has_errors:
        reasons.append("diagnostics contain errors (HAS_ERRORS)")
    unresolved = sum(
        1 for r in rrows.values() if r["resolution_status"] in ("candidate_source", "unsupported")
    )
    dynamic = sum(1 for r in rrows.values() if r["resolution_status"] == "dynamic_unresolved")
    if unresolved:
        reasons.append(f"{unresolved} unresolved/unsupported reference(s)")
    blocking_gaps = [g for g in gaps if g["kind"] in BLOCKING_GAP_KINDS]
    if blocking_gaps:
        by_kind = sorted({g["kind"] for g in blocking_gaps})
        reasons.append(
            f"{len(blocking_gaps)} stub/blackbox/protected-source boundary gap(s) "
            f"({', '.join(by_kind)}): internals unknown, absence of dependencies is not proven"
        )
    n_ts = sum(1 for d in result["diagnostics"] if d.get("code") == "MissingTimeScale" and d.get("is_error"))
    if n_ts:
        reasons.append(
            f"{n_ts} MissingTimeScale error(s): some sources have a `timescale and others do not; "
            "set the profile's language_options.timescale (the simulator's default timescale), "
            "for example \"1ns/1ps\""
        )
    protected_hits = _protected_parameter_errors(result)
    if protected_hits:
        n_err = sum(protected_hits.values())
        reasons.append(
            f"{n_err} ParameterDoesNotExist error(s) name module(s) whose body is a protected "
            f"(encrypted) envelope ({', '.join(sorted(protected_hits))}): the parameters live inside "
            "the envelope slang cannot read, so these are likely artifacts of the encryption, not "
            "design errors"
        )
    if in_scope_unsupported:
        reasons.append(
            f"{in_scope_unsupported} unsupported node occurrence(s) in project files "
            f"({', '.join(sorted(project_unsupported))})"
        )
    if project_drops:
        reasons.append(
            f"{project_drops} reference occurrence(s) in project files could not be recorded "
            "(negative reference conclusions are not supported)"
        )
    unsupported_ports = sum(1 for b in brows if not b["supported"])
    if unsupported_ports:
        reasons.append(f"{unsupported_ports} port association(s) slang did not expose")
    completeness = Completeness.PARTIAL if reasons else Completeness.COMPLETE
    limitations = list(STATIC_LIMITATIONS)
    if dynamic:
        limitations.append(
            f"{dynamic} virtual-method call(s) are dynamic_unresolved: the static target is the "
            "declared method, the runtime target is not determined."
        )
    if external_unsupported:
        limitations.append(
            f"{sum(external_unsupported.values())} unsupported node occurrence(s) in external "
            f"roots (not blocking): {', '.join(sorted(external_unsupported))}"
        )
    for d in drops:
        limitations.append(
            f"{d['count']} reference occurrence(s) dropped in {d['scope']} files: {d['reason']}"
        )
    kinds = {g["kind"] for g in gaps}
    if "generic_class_not_specialized" in kinds:
        limitations.append("Generic classes never specialized in this build are not analysed.")
    if "definition_not_elaborated" in kinds:
        limitations.append(
            "Modules defined but not elaborated under the tops are inactive_for_profile."
        )
    if any(x["kind"] == "disabled_conditional_region" for x in result["inactive"]):
        limitations.append(
            "`ifdef-disabled regions are inactive_for_profile (listed, not elaborated)."
        )
    # One line per boundary is unbounded (6637 on the SSE-315 SoC: ~700 KB in every envelope).
    # The first few are named; the count per kind says the rest, and coverage_gaps holds all rows.
    listed: dict[str, int] = {}
    omitted: dict[str, int] = {}
    for g in blocking_gaps:
        k = str(g["kind"])
        if listed.get(k, 0) < MAX_LISTED_GAPS_PER_KIND:
            listed[k] = listed.get(k, 0) + 1
            limitations.append(f"Blackbox boundary ({k}): {g['detail']}")
        else:
            omitted[k] = omitted.get(k, 0) + 1
    for k, n in sorted(omitted.items()):
        limitations.append(
            f"... and {n} more blackbox boundary gap(s) ({k}) not listed here; all "
            f"{n + listed[k]} are recorded in the build's coverage_gaps"
        )
    scope = (
        ",".join(tops)
        if tops
        else ",".join(i["definition"] for i in result["instances"] if i["is_top"])
    )
    unsup_summary: dict[str, int] = {}
    for u in result["unsupported_nodes"]:
        unsup_summary[u["kind"]] = unsup_summary.get(u["kind"], 0) + u["count"]
    summary = {
        "declarations": len(drows),
        "references": len(rrows),
        "instances": len(irows),
        "port_bindings": len(brows),
        "assertions": len(arows),
        "diagnostics": len(grows),
        "errors": sum(1 for g in grows if g["is_error"]),
        "warnings": sum(1 for g in grows if g["severity"] == "warning"),
        "specializations": len(srows),
        "unresolved": unresolved,
        "dynamic_unresolved": dynamic,
        "inactive_regions": len(xrows),
        "gaps": len(gap_rows),
        "unsupported_kinds": unsup_summary,
        "dropped_refs_project": project_drops,
        "dropped_refs_external": external_drops,
        "source_hash": sha256_hex(build_id),
    }
    return Ingested(
        rows={
            "declarations": drows,
            "references": list(rrows.values()),
            "instances": irows,
            "port_bindings": brows,
            "assertions": arows,
            "diagnostics": grows,
            "specializations": srows,
            "inactive_regions": xrows,
            "coverage_gaps": gap_rows,
            "unsupported_nodes": urows,
        },
        completeness=completeness,
        diagnostic_outcome=DiagnosticOutcome.HAS_ERRORS
        if has_errors
        else DiagnosticOutcome.NO_ERRORS,
        reasons=reasons,
        unresolved_count=unresolved,
        dynamic_count=dynamic,
        limitations=limitations,
        scope=scope,
        summary=summary,
    )
