"""Semantic extraction on pyslang 12.0.0 (plan 10.3, ADR-003). Runs INSIDE the worker process.

Everything here uses the API verified in ADR-003. Nothing is guessed with regular expressions:
anything the binding cannot give is recorded in ``gaps`` / ``unsupported_nodes`` and surfaced as
a limitation. Output is a plain JSON-serializable dict (see protocol.ExtractResult).

Conventions:
* A *declaration* is a source occurrence (name span + kind + qualified scope + CU + macro
  context). Elaborated copies (several instances of one module, several class specializations)
  share one declaration; instances and specializations are separate records.
* A *reference* is a source occurrence of a name that slang resolved (or failed to resolve).
  Occurrences are deduplicated across instances/specializations by (span, kind, target).
* All spans are original-file byte spans; see locator.py.
"""

from __future__ import annotations

import contextlib
from collections import Counter
from pathlib import Path
from typing import Any

import pyslang
from pyslang import ast, parsing
from pyslang import syntax as syn

from svweave.adapters.slang.locator import Loc, Locator
from svweave.adapters.slang.protocol import (
    DTO_SCHEMA,
    EXTRACTOR_VERSION,
    WorkerRequest,
)

SK = ast.SymbolKind
EK = ast.ExpressionKind

#: Symbol kinds that become declarations.
_DECL_KINDS: dict[Any, str] = {
    SK.Package: "package",
    SK.ClassType: "class",
    SK.GenericClassDef: "class",
    SK.ClassProperty: "field",
    SK.Variable: "variable",
    SK.Net: "net",
    SK.Parameter: "parameter",
    SK.TypeParameter: "type_parameter",
    SK.Port: "port",
    SK.InterfacePort: "interface_port",
    SK.FormalArgument: "argument",
    SK.Genvar: "genvar",
    SK.GenerateBlock: "generate_block",
    SK.GenerateBlockArray: "generate_block",
    SK.Modport: "modport",
    SK.Property: "property",
    SK.Sequence: "sequence",
    SK.TypeAlias: "typedef",
    SK.ConstraintBlock: "constraint",
    SK.CovergroupType: "covergroup",
    SK.Coverpoint: "coverpoint",
    SK.CoverCross: "cover_cross",
    SK.ClockingBlock: "clocking",
    SK.EnumValue: "enum_value",
    SK.Field: "struct_field",
    SK.LetDecl: "let",
    SK.Instance: "instance",
    SK.Checker: "checker",
    SK.ModportPort: "modport_port",
}

#: Symbol kinds the walker passes over without being a gap (structure, built-in types, plumbing).
_IGNORED_KINDS: frozenset[Any] = frozenset(
    {
        SK.Root,
        SK.CompilationUnit,
        SK.InstanceBody,
        SK.DeferredMember,
        SK.TransparentMember,
        SK.EmptyMember,
        SK.PredefinedIntegerType,
        SK.ScalarType,
        SK.FloatingType,
        SK.EnumType,
        SK.PackedArrayType,
        SK.FixedSizeUnpackedArrayType,
        SK.DynamicArrayType,
        SK.DPIOpenArrayType,
        SK.AssociativeArrayType,
        SK.QueueType,
        SK.PackedStructType,
        SK.UnpackedStructType,
        SK.PackedUnionType,
        SK.UnpackedUnionType,
        SK.VoidType,
        SK.NullType,
        SK.CHandleType,
        SK.StringType,
        SK.EventType,
        SK.UnboundedType,
        SK.TypeRefType,
        SK.UntypedType,
        SK.SequenceType,
        SK.PropertyType,
        SK.VirtualInterfaceType,
        SK.ErrorType,
        SK.ForwardingTypedef,
        SK.NetType,
        SK.ProceduralBlock,
        SK.StatementBlock,
        SK.ContinuousAssign,
        SK.ElabSystemTask,
        SK.MethodPrototype,
        SK.InstanceArray,
        SK.Attribute,
        SK.AssertionPort,
        SK.CovergroupBody,
        SK.CoverCrossBody,
        SK.CoverageBin,
        SK.LocalAssertionVar,
        SK.Iterator,
        SK.PatternVar,
        SK.MultiPort,
        SK.ClockVar,
        SK.CheckerInstanceBody,
        SK.Definition,
        SK.Subroutine,
        SK.ExplicitImport,
        SK.WildcardImport,
    }
)

_SEV = {
    "DiagnosticSeverity.Ignored": "ignored",
    "DiagnosticSeverity.Note": "note",
    "DiagnosticSeverity.Warning": "warning",
    "DiagnosticSeverity.Error": "error",
    "DiagnosticSeverity.Fatal": "fatal",
}

#: Diagnostic codes that mean "a name could not be resolved" -> unresolved reference rows.
_UNRESOLVED_CODES = frozenset(
    {
        "UndeclaredIdentifier",
        "UnknownModule",
        "UnknownClassOrPackage",
        "UnknownPackage",
        "UnknownMember",
        "UnknownInterface",
        "UnknownPackageMember",
        "UnknownType",
        "UnknownClass",
        "UnknownPrimitive",
    }
)

_ASSERTION_KINDS = {
    "Assert": "assert",
    "Assume": "assume",
    "CoverProperty": "cover_property",
    "CoverSequence": "cover_sequence",
    "Restrict": "restrict",
    "Expect": "expect",
}

_IDENT_OK = frozenset("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_$")


def _short(enum_val: Any) -> str:
    return str(enum_val).rsplit(".", 1)[-1]


def format_path(components: list[dict[str, Any]]) -> str:
    """Display path; special names are escaped SV-style. Identity never uses this string."""
    out: list[str] = []
    for c in components:
        name = str(c["name"])
        if not name or any(ch not in _IDENT_OK for ch in name) or name[0].isdigit():
            name = "\\" + name + " "
        idx = c.get("index")
        if idx:
            name += "".join(f"[{i}]" for i in idx)
        out.append(name)
    return ".".join(out)


def const_text(cv: Any) -> str:
    """Readable constant: decimal for plain integers, slang's own formatting otherwise."""
    try:
        v = cv.value
        if isinstance(v, pyslang.SVInt) and not v.hasUnknown:
            return str(v.toString(pyslang.LiteralBase.Decimal, False))
    except Exception:  # noqa: BLE001
        pass
    return str(cv)


def flag_bits(obj: Any) -> int:
    """``.flags`` of symbols is a pybind IntEnum that raises for combined bit values; recover
    the integer from the error text."""
    try:
        return int(obj.flags.value)
    except ValueError as e:
        try:
            return int(str(e).split()[0])
        except (ValueError, IndexError):
            return 0
    except Exception:  # noqa: BLE001
        return 0


method_flags = flag_bits


class Extractor:
    def __init__(self, drv: Any, comp: Any, req: WorkerRequest, driver_messages: list[str]) -> None:
        self.drv = drv
        self.comp = comp
        self.sm = comp.sourceManager or drv.sourceManager
        self.root = Path(req.snapshot_root)
        self.req = req
        self.driver_messages = driver_messages
        self.loc = Locator(self.sm, self.root, req.options.max_text_bytes)
        self.cu_index: dict[Any, int] = {}
        for i, cu in enumerate(comp.getCompilationUnits()):
            self.cu_index[cu] = i
        self.decls: list[dict[str, Any]] = []
        self._decl_by_key: dict[tuple[Any, ...], int] = {}
        self._sym_decl: dict[Any, int | None] = {}
        self.refs: list[dict[str, Any]] = []
        self._ref_keys: set[tuple[Any, ...]] = set()
        self.specs: dict[str, dict[str, Any]] = {}
        self._spec_queue: list[Any] = []
        self.insts: list[dict[str, Any]] = []
        self._inst_by_hier: dict[str, int] = {}
        self.bindings: list[dict[str, Any]] = []
        self.assertions: list[dict[str, Any]] = []
        self._assert_keys: set[tuple[Any, ...]] = set()
        self.inactive: list[dict[str, Any]] = []
        self.gaps: list[dict[str, Any]] = []
        self.errors: Counter[str] = Counter()
        # unsupported symbol kinds, counted PER OCCURRENCE and per scope (project vs external
        # root), so completeness never depends on instantiation order (B1)
        self.kind_counts: Counter[tuple[str, str]] = Counter()
        self.kind_sample: dict[tuple[str, str], Loc | None] = {}
        # reference occurrences that could not be recorded: (reason, scope) -> count (M1)
        self.drops: Counter[tuple[str, str]] = Counter()
        self._retvar: dict[Any, Any] = {}
        self.diagnostics: list[dict[str, Any]] = []
        self.stats: Counter[str] = Counter()
        self._handlers: dict[Any, Any] = {
            SK.Package: self._h_decl,
            SK.ClassType: self._h_class,
            SK.GenericClassDef: self._h_decl,
            SK.ClassProperty: self._h_valuedecl,
            SK.Variable: self._h_valuedecl,
            SK.Net: self._h_valuedecl,
            SK.Parameter: self._h_valuedecl,
            SK.TypeParameter: self._h_decl,
            SK.Port: self._h_decl,
            SK.InterfacePort: self._h_decl,
            SK.FormalArgument: self._h_valuedecl,
            SK.Genvar: self._h_decl,
            SK.GenerateBlock: self._h_decl,
            SK.GenerateBlockArray: self._h_decl,
            SK.Modport: self._h_decl,
            SK.ModportPort: self._h_decl,
            SK.Property: self._h_decl,
            SK.Sequence: self._h_decl,
            SK.TypeAlias: self._h_typealias,
            SK.ConstraintBlock: self._h_decl,
            SK.CovergroupType: self._h_decl,
            SK.Coverpoint: self._h_decl,
            SK.CoverCross: self._h_decl,
            SK.ClockingBlock: self._h_decl,
            SK.EnumValue: self._h_decl,
            SK.Field: self._h_valuedecl,
            SK.LetDecl: self._h_decl,
            SK.Instance: self._h_instance_node,
            SK.DefParam: self._h_defparam,
            SK.Subroutine: self._h_subroutine,
            SK.ExplicitImport: self._h_import,
            SK.WildcardImport: self._h_import,
            EK.NamedValue: self._h_value,
            EK.HierarchicalValue: self._h_value,
            EK.Call: self._h_call,
            EK.NewClass: self._h_new,
        }

    # ================================================================== infrastructure
    def _guard(self, what: str, fn: Any, *a: Any) -> Any:
        try:
            return fn(*a)
        except Exception as e:  # noqa: BLE001 - recorded, never silent
            self.errors[f"{what}:{type(e).__name__}"] += 1
            return None

    def _cu_of(self, sym: Any) -> int | None:
        try:
            cu = sym.parentScope.compilationUnit
            return self.cu_index.get(cu)
        except Exception:  # noqa: BLE001
            return None

    def _type_text(self, t: Any) -> str | None:
        try:
            s = str(t)
        except Exception:  # noqa: BLE001
            return None
        return s if len(s) <= 512 else s[:512] + "..."

    # ================================================================== declarations
    def _dkind(self, sym: Any) -> str | None:
        k = sym.kind
        if k == SK.Definition:
            return _short(sym.definitionKind).lower()
        if k == SK.Subroutine:
            return "task" if _short(sym.subroutineKind) == "Task" else "function"
        return _DECL_KINDS.get(k)

    def decl_for(self, sym: Any) -> int | None:
        """Declaration index of a symbol (registered on demand); None if it has no source."""
        if sym is None:
            return None
        if sym in self._sym_decl:
            return self._sym_decl[sym]
        orig = sym
        if sym.kind == SK.ClassType:
            g = sym.genericClass
            if g is not None:
                sym = g
                if sym in self._sym_decl:
                    self._sym_decl[orig] = self._sym_decl[sym]
                    return self._sym_decl[sym]
        res = self._guard("decl_for", self._make_decl, sym)
        self._sym_decl[orig] = res
        self._sym_decl[sym] = res
        return res  # type: ignore[no-any-return]

    def _make_decl(self, sym: Any) -> int | None:
        dkind = self._dkind(sym)
        if dkind is None:
            self.stats[f"decl_none:no_kind:{_short(sym.kind)}"] += 1
            return None
        if not self.loc.valid(sym.location):
            self.stats["decl_none:invalid_location"] += 1
            return None
        if sym.kind == SK.Variable and flag_bits(sym) & int(ast.VariableFlags.CompilerGenerated):
            return None  # `this`, function return-value variable, ...
        if sym.kind == SK.TypeAlias and (
            sym.syntax is None or sym.syntax.kind != syn.SyntaxKind.TypedefDeclaration
        ):
            dkind = "type_parameter"  # alias created for a type parameter
        name = sym.name
        if sym.kind == SK.CovergroupType and not name and sym.syntax is not None:
            return self._make_covergroup_decl(sym)
        ns = self.loc.name_span(sym)
        if ns is None:
            self.stats[f"decl_none:no_span:{_short(sym.kind)}"] += 1
            return None
        qn = sym.lexicalPath
        cu = self._cu_of(sym)
        mac = ns.get("macro")
        mkey = (mac["name"], mac.get("expansion", {}).get("start")) if mac else None
        key = (ns["file"], ns["start"], ns["end"], dkind, qn, cu, mkey)
        idx = self._decl_by_key.get(key)
        if idx is not None:
            return idx
        idx = len(self.decls)
        self._decl_by_key[key] = idx
        full = None
        if sym.syntax is not None:
            full = self._guard("fullspan", self.loc.range, sym.syntax.sourceRange)
        d: dict[str, Any] = {
            "idx": idx,
            "kind": dkind,
            "name": name,
            "qualified_name": qn,
            "loc": ns,
            "full": {"start": full["start"], "end": full["end"]}
            if full and full["file"] == ns["file"]
            else None,
            "cu": cu,
            "library": None,
            "type": None,
            "attrs": {},
        }
        self.decls.append(d)
        try:
            self._fill_attrs(sym, d)
        except Exception as e:  # noqa: BLE001
            self.errors[f"attrs:{type(e).__name__}"] += 1
        return idx

    def _make_covergroup_decl(self, sym: Any) -> int | None:
        """Covergroup types are anonymous in slang's symbol table; the name token is in syntax."""
        tok = sym.syntax.name
        ns = self.loc.span(tok.range.start, tok.range.end)
        if ns is None:
            return None
        name = tok.valueText
        qn = f"{sym.lexicalPath}::{name}"
        cu = self._cu_of(sym)
        key = (ns["file"], ns["start"], ns["end"], "covergroup", qn, cu, None)
        idx = self._decl_by_key.get(key)
        if idx is not None:
            return idx
        idx = len(self.decls)
        self._decl_by_key[key] = idx
        full = self._guard("fullspan", self.loc.range, sym.syntax.sourceRange)
        self.decls.append(
            {
                "idx": idx,
                "kind": "covergroup",
                "name": name,
                "qualified_name": qn,
                "loc": ns,
                "full": {"start": full["start"], "end": full["end"]}
                if full and full["file"] == ns["file"]
                else None,
                "cu": cu,
                "library": None,
                "type": None,
                "attrs": {},
            }
        )
        return idx

    def _fill_attrs(self, sym: Any, d: dict[str, Any]) -> None:
        k = sym.kind
        a = d["attrs"]
        if k in (
            SK.Variable,
            SK.ClassProperty,
            SK.Net,
            SK.FormalArgument,
            SK.Field,
            SK.Parameter,
            SK.EnumValue,
        ):
            d["type"] = self._type_text(sym.type)
            if sym.type.isVirtualInterface:
                a["virtual_interface"] = True  # typed candidate only; never bound statically
            if k == SK.ClassProperty:
                a["visibility"] = _short(sym.visibility).lower()
                a["rand_mode"] = _short(sym.randMode).lower().rstrip("_")
            if k == SK.Parameter:
                a["value"] = const_text(sym.value)
                a["local"] = bool(sym.isLocalParam)
                a["port_param"] = bool(sym.isPortParam)
            if k == SK.FormalArgument:
                a["direction"] = _short(sym.direction).lower()
        elif k == SK.Port:
            a["direction"] = _short(sym.direction).lower()
            d["type"] = self._type_text(sym.type)
        elif k == SK.InterfacePort:
            a["interface"] = sym.interfaceDef.name if sym.interfaceDef is not None else None
            a["modport"] = sym.modport or None
        elif k == SK.TypeAlias:
            d["type"] = self._type_text(sym.targetType.type)
        elif k == SK.Subroutine:
            fl = method_flags(sym)
            a["virtual"] = bool(fl & 1) or bool(sym.isVirtual)
            a["pure"] = bool(fl & 2)
            a["static"] = bool(fl & 4)
            a["constructor"] = bool(fl & 8)
            a["visibility"] = _short(sym.visibility).lower()
            a["is_method"] = sym.thisVar is not None or bool(fl & 4)
            d["type"] = self._type_text(sym.returnType)
            a["arguments"] = [
                {
                    "name": x.name,
                    "type": self._type_text(x.type),
                    "direction": _short(x.direction).lower(),
                }
                for x in sym.arguments
            ]
            ov = sym.override
            a["override_decl_idx"] = self.decl_for(ov) if ov is not None else None
            if sym.syntax is not None and sym.syntax.kind == syn.SyntaxKind.DPIImport:
                a["dpi_import"] = True
                a["dpi_spec"] = sym.syntax.specString.valueText
        elif k == SK.ClassType:
            a["generic"] = False
            self._class_attrs(sym, a)
        elif k == SK.GenericClassDef:
            a["generic"] = True
            if sym.syntax is not None and getattr(sym.syntax, "parameters", None) is not None:
                a["parameter_port_text"] = self.loc.text(
                    self.loc.range(sym.syntax.parameters.sourceRange)
                )
            a["abstract"] = False
            ext = getattr(sym.syntax, "extendsClause", None) if sym.syntax is not None else None
            if ext is not None:
                a["base_class_text"] = self.loc.text(self.loc.range(ext.baseName.sourceRange))
        elif k == SK.Definition:
            a["definition_kind"] = _short(sym.definitionKind).lower()
            a["instance_count"] = int(sym.instanceCount)
            try:
                d["library"] = sym.sourceLibrary.name
            except Exception:  # noqa: BLE001
                d["library"] = None
            for at in self.comp.getAttributes(sym):
                if at.name == "svw_stub":
                    a["stub"] = self._attr_text(sym, "svw_stub")
        elif k in (SK.GenerateBlock, SK.GenerateBlockArray):
            a["uninstantiated"] = bool(sym.isUninstantiated)
        elif k == SK.Modport:
            a["ports"] = [
                {"name": m.name, "direction": _short(m.direction).lower()}
                for m in sym
                if m.kind == SK.ModportPort
            ]
        elif k == SK.ModportPort:
            a["direction"] = _short(sym.direction).lower()
            d["type"] = self._type_text(sym.type)
            isym = sym.internalSymbol
            a["internal_decl_idx"] = self.decl_for(isym) if isym is not None else None
        elif k == SK.Instance:
            a["definition"] = sym.definition.name
        elif k == SK.Property or k == SK.Sequence:
            a["ports"] = [p.name for p in sym.ports]

    def _attr_text(self, sym: Any, name: str) -> str | None:
        node = sym.syntax
        attrs = getattr(node, "attributes", None)
        if attrs is None:
            return None
        for inst in attrs:
            for spec in inst.specs:
                if hasattr(spec, "name") and spec.name.valueText == name:
                    v = getattr(spec, "value", None)
                    if v is None:
                        return ""
                    txt = self.loc.text(self.loc.range(v.expr.sourceRange)) or ""
                    return txt.strip('"')
        return None

    def _class_attrs(self, cls: Any, a: dict[str, Any]) -> None:
        a["abstract"] = bool(cls.isAbstract)
        a["interface_class"] = bool(cls.isInterface)
        bc = cls.baseClass
        if bc is not None:
            a["base_class"] = self._type_text(bc)
            a["base_decl_idx"] = self.decl_for(bc)
        else:
            a["base_class"] = None
            a["base_decl_idx"] = None

    # ================================================================== references
    @staticmethod
    def scope_of(loc: Loc | None) -> str:
        """'external' for files of an external source root (``_ext/<id>/``) or outside the
        snapshot, else 'project'. Unknown locations count as project (conservative)."""
        if loc is None:
            return "project"
        f = str(loc.get("file", ""))
        return "external" if f.startswith("_ext/") or loc.get("outside_root") else "project"

    def drop(self, reason: str, loc: Loc | None) -> None:
        self.drops[(reason, self.scope_of(loc))] += 1

    def add_ref(
        self,
        kind: str,
        loc: Loc | None,
        target_idx: int | None,
        status: str = "resolved_static",
        reason: str | None = None,
        target_name: str | None = None,
        context: dict[str, Any] | None = None,
    ) -> None:
        if loc is None:
            self.drop("no_location", None)
            return
        mac = loc.get("macro")
        key = (
            loc["file"],
            loc["start"],
            loc["end"],
            kind,
            target_idx,
            status,
            target_name,
            mac["expansion"].get("start") if mac and "expansion" in mac else None,
        )
        if key in self._ref_keys:
            return
        self._ref_keys.add(key)
        self.refs.append(
            {
                "kind": kind,
                "loc": loc,
                "target_idx": target_idx,
                "target_name": target_name,
                "status": status,
                "reason": reason,
                "context": context or {},
            }
        )

    def note_type(self, t: Any) -> None:
        """Register class specializations reachable through a type (worklist-walked later)."""
        if t is None:
            return
        try:
            ct = t.canonicalType
            if ct.kind != SK.ClassType or ct.genericClass is None:
                return
            key = ct.hierarchicalPath
        except Exception:  # noqa: BLE001
            return
        if key in self.specs:
            return
        self.specs[key] = {"text": key}
        self._spec_queue.append(ct)

    def _type_ref(self, sym: Any) -> None:
        ds = sym.declaredType if sym.kind != SK.TypeAlias else sym.targetType
        ts = ds.typeSyntax
        t = ds.type
        self.note_type(t)
        if ts is None or t is None:
            return
        tk = t.kind
        if tk == SK.TypeAlias or tk == SK.ClassType or tk == SK.CovergroupType:
            target = t
        else:
            return
        idx = self.decl_for(target)
        if idx is None:
            return
        loc = self.loc.range(ts.sourceRange)
        spec = None
        if tk == SK.ClassType and target.genericClass is not None:
            spec = target.hierarchicalPath
        self.add_ref("type", loc, idx, context={"specialization": spec} if spec else None)

    def _is_command_line_override(self, n: Any) -> bool:
        """slang models a top-scoped ``-G top.NAME=VALUE`` as a DefParam symbol living in the
        synthetic ``<command-line>`` buffer; a user ``defparam`` in a file stays unsupported."""
        try:
            return str(self.sm.getFileName(n.location)) == "<command-line>"
        except Exception:  # noqa: BLE001
            return False

    # ------------------------------------------------------------------ symbol handlers
    def on_node(self, n: Any) -> Any:
        h = self._handlers.get(n.kind)
        if h is not None:
            try:
                h(n)
            except Exception as e:  # noqa: BLE001
                self.errors[f"handler:{_short(n.kind)}:{type(e).__name__}"] += 1
        elif isinstance(n, ast.Symbol) and n.kind not in _IGNORED_KINDS:
            self._note_unsupported(n)
        return ast.VisitAction.Advance

    def _note_unsupported(self, n: Any) -> None:
        sample = self._guard("sample", self.loc.name_span, n)
        key = (_short(n.kind), self.scope_of(sample))
        self.kind_counts[key] += 1
        if key not in self.kind_sample:
            self.kind_sample[key] = sample

    def _h_decl(self, n: Any) -> None:
        if n.kind in (SK.GenerateBlock, SK.GenerateBlockArray) and not n.name:
            return
        self.decl_for(n)

    def _h_valuedecl(self, n: Any) -> None:
        if n.name == "this" or (n.kind == SK.Variable and n.name == ""):
            return
        if self.decl_for(n) is None and n.kind == SK.Variable:
            return
        self._type_ref(n)

    def _h_typealias(self, n: Any) -> None:
        self.decl_for(n)
        self._type_ref(n)

    def _h_subroutine(self, n: Any) -> None:
        if not self.loc.valid(n.location):
            return
        self.decl_for(n)
        rv = n.returnValVar
        if rv is not None:
            self._retvar[rv] = n
        self.note_type(n.returnType)
        for a in n.arguments:
            self.note_type(a.type)

    def _h_class(self, n: Any) -> None:
        idx = self.decl_for(n)
        self.note_type(n)
        bc = n.baseClass
        if bc is not None:
            self.note_type(bc)
            bidx = self.decl_for(bc)
            syn_node = n.syntax
            ext = getattr(syn_node, "extendsClause", None) if syn_node is not None else None
            if bidx is not None and ext is not None:
                self.add_ref(
                    "extends",
                    self.loc.range(ext.baseName.sourceRange),
                    bidx,
                    context={"derived_decl_idx": idx},
                )

    def _h_import(self, n: Any) -> None:
        pkg = n.package
        if pkg is None:
            return
        pidx = self.decl_for(pkg)
        if pidx is None or n.syntax is None:
            return
        loc = self.loc.range(n.syntax.sourceRange)
        self.add_ref("import", loc, pidx, context={"package": n.packageName})

    def _h_defparam(self, n: Any) -> None:
        """A source ``defparam`` is a reference from the assignment's name to the overridden
        parameter. slang applies the override during elaboration, so the hierarchy's parameter
        values already reflect it; only the reference was missing. The manifest's own
        ``-G top.NAME=VALUE`` (a DefParam in the synthetic ``<command-line>`` buffer, recorded in
        config_id) is not source and is skipped."""
        if self._is_command_line_override(n):
            return
        syntax = n.syntax
        name = getattr(syntax, "name", None)
        loc = self.loc.range(name.sourceRange) if name is not None else None
        target = n.target
        idx = self.decl_for(target) if target is not None else None
        if idx is None or loc is None:
            self._note_unsupported(n)  # target not resolvable to a declaration: stay honest
            return
        self.add_ref("defparam", loc, idx)

    def _h_instance_node(self, n: Any) -> None:
        self.decl_for(n)
        s = n.syntax
        parent = s.parent if s is not None else None
        if parent is not None and parent.kind == syn.SyntaxKind.HierarchyInstantiation:
            didx = self.decl_for(n.definition)
            tok = parent.type
            loc = self.loc.span(tok.range.start, tok.range.end)
            self.add_ref("instantiation", loc, didx, context={"instance": n.name})

    # ------------------------------------------------------------------ expression handlers
    def _h_value(self, n: Any) -> None:
        sym = n.symbol
        self.note_type(n.type)
        loc = self.loc.range(n.sourceRange)
        if loc is None:
            self.drop("no_location", None)
            return
        txt = self.loc.text(loc) or ""
        kind = "value" if n.kind == EK.NamedValue else "hier_value"
        if txt.startswith(".*"):
            kind = "wildcard_connection"
        idx = self.decl_for(sym)
        ctx: dict[str, Any] | None = None
        if (
            idx is None
            and sym.kind == SK.Variable
            and flag_bits(sym) & int(ast.VariableFlags.CompilerGenerated)
        ):
            # implicit variables: the function return value (named like the function) and `this`
            sub = self._retvar.get(sym)
            if sub is not None:
                idx = self.decl_for(sub)
                kind, ctx = "return_value", {"function": sub.name}
            elif sym.name == "this":
                ct = sym.type.canonicalType if sym.type is not None else None
                if ct is not None and ct.kind == SK.ClassType:
                    idx = self.decl_for(ct)
                    kind, ctx = "this", {"class": ct.name}
        if idx is None:
            reason = (
                "iterator_variable"
                if sym.kind == SK.Iterator
                else "compiler_generated_variable"
                if sym.kind == SK.Variable
                and flag_bits(sym) & int(ast.VariableFlags.CompilerGenerated)
                else "no_source_declaration"
            )
            self.drop(reason, loc)
            return
        if kind == "value":
            self.loc.verify(loc, sym.name)
        self.add_ref(kind, loc, idx, context=ctx)

    @staticmethod
    def _call_name_token(n: Any) -> Any:
        s = n.syntax
        left = getattr(s, "left", None) if s is not None else None
        if left is None:
            return None
        return left.getLastToken()

    def _h_call(self, n: Any) -> None:
        self.note_type(n.type)
        s = n.syntax
        left = getattr(s, "left", None) if s is not None else None
        if left is not None:
            tok = left.getLastToken()
            loc = self.loc.span(tok.range.start, tok.range.end)
        else:
            loc = self.loc.range(n.sourceRange)
        if n.isSystemCall:
            self.add_ref(
                "system_call", loc, None, target_name=n.subroutineName, context={"builtin": True}
            )
            return
        sub = n.subroutine
        idx = self.decl_for(sub)
        args: list[str | None] = []
        arglist = getattr(n.syntax, "arguments", None) if n.syntax is not None else None
        if arglist is not None:
            # arguments as WRITTEN (n.arguments also contains slang-inserted default values)
            for a in list(arglist.parameters):
                if isinstance(a, parsing.Token):
                    continue  # list separator
                if len(args) < 16:
                    args.append(self.loc.text(self.loc.range(a.sourceRange)))
        elif n.syntax is None:
            for a in list(n.arguments)[:16]:
                args.append(self.loc.text(self.loc.range(a.sourceRange)))
        ctx: dict[str, Any] = {"args": args, "callee": sub.hierarchicalPath}
        status = "resolved_static"
        reason = None
        if sub.isVirtual and not self._static_dispatch(left):
            status = "dynamic_unresolved"
            reason = "virtual_method_runtime_dispatch"
            ctx["virtual"] = True
        if sub.hierarchicalPath != sub.lexicalPath:
            ctx["specialization_path"] = sub.hierarchicalPath
        if idx is None:
            # Built-in methods (queue.size(), randomize(), ...) have no source declaration.
            ctx["builtin"] = True
            self.add_ref(
                "call",
                loc,
                None,
                reason="builtin_method_no_source",
                target_name=n.subroutineName,
                context=ctx,
            )
            return
        if loc is not None:
            self.loc.verify(loc, sub.name)
        self.add_ref("call", loc, idx, status, reason, context=ctx)

    @staticmethod
    def _static_dispatch(left: Any) -> bool:
        """`super.f()` and `Class::f()` call a specific method: no runtime dispatch."""
        if left is None:
            return False
        try:
            first = left.getFirstToken()
            if first.kind == parsing.TokenKind.SuperKeyword:
                return True
            if left.kind == syn.SyntaxKind.ScopedName:
                return str(left.separator.kind).endswith("DoubleColon")
        except Exception:  # noqa: BLE001
            return False
        return False

    def _h_new(self, n: Any) -> None:
        t = n.type
        self.note_type(t)
        if t is None or t.canonicalType.kind != SK.ClassType:
            return
        idx = self.decl_for(t.canonicalType)
        if idx is None or n.syntax is None:
            return
        loc = self.loc.range(n.syntax.sourceRange)
        self.add_ref("new", loc, idx)

    # ================================================================== whole-design passes
    def run(self) -> dict[str, Any]:
        comp = self.comp
        self._diagnostics()
        root = comp.getRoot()
        self._definitions()
        root.visit(self.on_node)
        self._drain_specs()
        for top in root.topInstances:
            self._guard("instances", self._walk_instance, top, [], None, [])
        self._drain_specs()
        self._binds()
        self._inactive_regions()
        self._gaps()
        deps = self._dependencies()
        self._unresolved_from_diags()
        self._postprocess()
        return self._result(deps)

    # ------------------------------------------------------------------ diagnostics
    def _diagnostics(self) -> None:
        eng = self.drv.diagEngine
        for dg in self.comp.getAllDiagnostics():
            sev = eng.getSeverity(dg.code, dg.location)
            # ADR-003 quirk: getAllDiagnostics() also returns default-off diagnostics.
            if sev == pyslang.DiagnosticSeverity.Ignored:
                continue
            code = str(dg.code)
            if code.startswith("DiagCode(") and code.endswith(")"):
                code = code[9:-1]
            loc = self._guard("diag_loc", self.loc.span, dg.location)
            ranges = [
                r for r in (self._guard("diag_rng", self.loc.range, r) for r in dg.ranges) if r
            ]
            self.diagnostics.append(
                {
                    "severity": _SEV.get(str(sev), str(sev)),
                    "code": code,
                    # isError() only says the code is an error by nature; a warning-option diagnostic
                    # (e.g. format-too-many-args) can still be *effectively* an error. slang's own
                    # error count follows the effective severity, so do the same.
                    "is_error": bool(dg.isError())
                    or sev in (pyslang.DiagnosticSeverity.Error, pyslang.DiagnosticSeverity.Fatal),
                    "message": eng.formatMessage(dg),
                    "option": eng.getOptionName(dg.code) or None,
                    "loc": loc,
                    "ranges": ranges,
                    "macro_notes": ([loc["macro"]] if loc and "macro" in loc else []),
                }
            )

    def _unresolved_from_diags(self) -> None:
        for dg in self.diagnostics:
            if dg["code"] not in _UNRESOLVED_CODES or not dg["is_error"]:
                continue
            rng = dg["ranges"][0] if dg["ranges"] else dg["loc"]
            if rng is None:
                continue
            name = self.loc.text(rng)
            self.add_ref(
                "unresolved",
                rng,
                None,
                status="candidate_source",
                reason=dg["code"],
                target_name=name,
                context={"message": dg["message"]},
            )

    # ------------------------------------------------------------------ definitions
    def _definitions(self) -> None:
        tops = {t.name for t in self.comp.getRoot().topInstances}
        for df in self.comp.getDefinitions():
            idx = self.decl_for(df)
            if idx is None:
                continue
            a = self.decls[idx]["attrs"]
            active = df.instanceCount > 0 or df.name in tops
            a["active"] = bool(active)
            if not active:
                self.gaps.append(
                    {
                        "kind": "definition_not_elaborated",
                        "name": df.name,
                        "decl_idx": idx,
                        "detail": "defined in source but not elaborated under the selected tops; "
                        "its body references are not extracted",
                    }
                )
                self.inactive.append(
                    {
                        "kind": "definition_not_elaborated",
                        "name": df.name,
                        "decl_idx": idx,
                        "loc": self.decls[idx]["loc"],
                        "status": "inactive_for_profile",
                    }
                )

    # ------------------------------------------------------------------ specializations
    def _drain_specs(self) -> None:
        while self._spec_queue:
            ct = self._spec_queue.pop()
            self._guard("spec", self._do_spec, ct)

    def _do_spec(self, ct: Any) -> None:
        key = ct.hierarchicalPath
        params: list[dict[str, Any]] = []
        for m in ct:
            if m.kind == SK.Parameter:
                params.append({"name": m.name, "kind": "value", "value": const_text(m.value)})
            elif m.kind == SK.TypeParameter:
                params.append({"name": m.name, "kind": "type", "value": str(m.targetType.type)})
        gidx = self.decl_for(ct.genericClass)
        bc = ct.baseClass
        self.specs[key] = {
            "text": key,
            "class_decl_idx": gidx,
            "parameters": params,
            "base_class": self._type_text(bc) if bc is not None else None,
            "base_decl_idx": self.decl_for(bc) if bc is not None else None,
            "methods": [],
        }
        ct.visit(self.on_node)
        for m in ct:
            if m.kind == SK.Subroutine and self.loc.valid(m.location):
                di = self.decl_for(m)
                if di is not None:
                    self.specs[key]["methods"].append(di)

    # ------------------------------------------------------------------ instances
    def _walk_instance(
        self, inst: Any, prefix: list[dict[str, Any]], parent_idx: int | None, gen_ctx: list[str]
    ) -> None:
        comps = [
            *prefix,
            {"kind": "instance", "name": inst.name, "index": list(inst.arrayPath) or None},
        ]
        idx = len(self.insts)
        hier = inst.hierarchicalPath
        d = inst.definition
        params: list[dict[str, Any]] = []
        for p in inst.body.parameters:
            if p.kind == SK.Parameter:
                params.append(
                    {
                        "name": p.name,
                        "kind": "value",
                        "value": const_text(p.value),
                        "type": self._type_text(p.type),
                        "overridden": bool(p.isOverridden),
                        "local": bool(p.isLocalParam),
                    }
                )
            else:
                params.append(
                    {
                        "name": p.name,
                        "kind": "type",
                        "value": str(p.targetType.type),
                        "overridden": bool(p.isOverridden),
                        "local": bool(p.isLocalParam),
                    }
                )
        didx = self.decl_for(d)
        stub = self.decls[didx]["attrs"].get("stub") if didx is not None else None
        port_names = {p.name for p in inst.body.portList}
        others = [
            m
            for m in inst.body
            if m.kind
            not in (
                SK.Port,
                SK.InterfacePort,
                SK.MultiPort,
                SK.Parameter,
                SK.TypeParameter,
                SK.Genvar,
                SK.TransparentMember,
                SK.EmptyMember,
                SK.DeferredMember,
            )
            and not (m.name in port_names and m.kind in (SK.Net, SK.Variable))
        ]
        rec: dict[str, Any] = {
            "idx": idx,
            "path": format_path(comps),
            "hier_path": hier,
            "components": comps,
            "parent_idx": parent_idx,
            "definition": d.name,
            "definition_decl_idx": didx,
            "parameters": params,
            "specialization": ",".join(
                f"{p['name']}={p['value']}" for p in params if not p["local"]
            ),
            "generate_context": list(gen_ctx),
            "is_top": parent_idx is None,
            "is_interface": bool(inst.isInterface),
            "stub": stub is not None,
            "stub_name": stub,
            "blackbox_candidate": not others and len(port_names) > 0,
            "loc": self.loc.name_span(inst) if inst.syntax is not None else None,
            "bind": self._bind_info(inst),
        }
        self.insts.append(rec)
        self._inst_by_hier[hier] = idx
        if inst.syntax is not None:
            self._guard("bindings", self._port_bindings, inst, idx)
        self._walk_scope(inst.body, comps, idx, [])

    def _bind_info(self, inst: Any) -> dict[str, Any] | None:
        s = inst.syntax
        node = s.parent if s is not None else None
        while node is not None:
            if node.kind == syn.SyntaxKind.BindDirective:
                return {
                    "directive_loc": self.loc.range(node.sourceRange),
                    "target_text": str(node.target).strip(),
                }
            node = node.parent
        return None

    def _walk_scope(
        self, scope: Any, comps: list[dict[str, Any]], inst_idx: int, gen_ctx: list[str]
    ) -> None:
        for m in scope:
            k = m.kind
            if k == SK.Instance:
                self._guard("instance", self._walk_instance, m, comps, inst_idx, gen_ctx)
            elif k == SK.InstanceArray:
                self._walk_instance_array(m, comps, inst_idx, gen_ctx)
            elif k == SK.GenerateBlockArray:
                for e in m.entries:
                    if e.isUninstantiated:
                        self._inactive_generate(e, comps)
                        continue
                    try:
                        gi = int(str(e.arrayIndex))
                    except (TypeError, ValueError):
                        gi = None
                    c = {
                        "kind": "generate",
                        "name": m.name or e.name,
                        "index": [gi] if gi is not None else None,
                    }
                    self._walk_scope(e, [*comps, c], inst_idx, [*gen_ctx, format_path([c])])
            elif k == SK.GenerateBlock:
                if m.isUninstantiated:
                    self._inactive_generate(m, comps)
                    continue
                c = {"kind": "generate", "name": m.name or m.externalName, "index": None}
                self._walk_scope(m, [*comps, c], inst_idx, [*gen_ctx, format_path([c])])
            elif k in (SK.ProceduralBlock, SK.StatementBlock, SK.Subroutine):
                if k == SK.Subroutine and not self.loc.valid(m.location):
                    continue
                self._guard("assert_scan", self._scan_assertions, m, inst_idx)

    def _walk_instance_array(
        self, arr: Any, comps: list[dict[str, Any]], inst_idx: int, gen_ctx: list[str]
    ) -> None:
        for e in arr.elements:
            if e.kind == SK.Instance:
                self._guard("instance", self._walk_instance, e, comps, inst_idx, gen_ctx)
            elif e.kind == SK.InstanceArray:
                self._walk_instance_array(e, comps, inst_idx, gen_ctx)

    def _inactive_generate(self, blk: Any, comps: list[dict[str, Any]]) -> None:
        idx = self.decl_for(blk)
        self.inactive.append(
            {
                "kind": "generate_branch_not_selected",
                "name": blk.name or blk.externalName,
                "decl_idx": idx,
                "loc": self.loc.name_span(blk) if blk.name else None,
                "scope": format_path(comps),
                "status": "inactive_for_profile",
            }
        )

    # ------------------------------------------------------------------ port bindings
    def _conn_styles(self, inst: Any) -> tuple[dict[str, str], bool]:
        styles: dict[str, str] = {}
        wildcard = False
        s = inst.syntax
        conns = getattr(s, "connections", None)
        if conns is None:
            return styles, wildcard
        names = [p.name for p in inst.body.portList]
        pos = 0
        for c in conns:
            kind = c.kind
            if kind == syn.SyntaxKind.OrderedPortConnection:
                if pos < len(names):
                    styles[names[pos]] = "positional"
                pos += 1
            elif kind == syn.SyntaxKind.NamedPortConnection:
                nm = c.name.valueText
                if c.openParen is None or not c.openParen:
                    styles[nm] = "named_implicit"
                elif c.expr is None:
                    styles[nm] = "open"
                else:
                    styles[nm] = "named"
            elif kind == syn.SyntaxKind.WildcardPortConnection:
                wildcard = True
        return styles, wildcard

    def _port_bindings(self, inst: Any, inst_idx: int) -> None:
        styles, wildcard = self._conn_styles(inst)
        for pc in inst.portConnections:
            port = pc.port
            pname = port.name
            style = styles.get(pname)
            if style is None:
                style = "wildcard" if wildcard and pc.expression is not None else "omitted"
            rec: dict[str, Any] = {
                "instance_idx": inst_idx,
                "formal": pname,
                "port_kind": _short(port.kind),
                "direction": None,
                "port_type": None,
                "port_decl_idx": self.decl_for(port),
                "style": style,
                "actual_text": None,
                "actual_loc": None,
                "actual_kind": None,
                "actual_type": None,
                "bit_width": None,
                "is_constant": False,
                "implicit_actual": style in ("wildcard", "named_implicit"),
                "referenced": [],
                "parts": [],
                "interface_instance": None,
                "modport": None,
                "supported": True,
                "note": None,
            }
            if port.kind == SK.InterfacePort:
                rec["port_type"] = port.interfaceDef.name if port.interfaceDef is not None else None
                ifs, mp = pc.ifaceConn
                if ifs is not None:
                    rec["interface_instance"] = ifs.hierarchicalPath
                if mp is not None:
                    rec["modport"] = mp.name
                elif port.modport:
                    rec["modport"] = port.modport
            else:
                with contextlib.suppress(Exception):
                    rec["direction"] = _short(port.direction).lower()
                rec["port_type"] = self._type_text(port.type)
            e = pc.expression
            if e is not None:
                self._fill_actual(rec, e, style)
            elif style in ("positional", "named") and port.kind != SK.InterfacePort:
                rec["supported"] = False
                rec["note"] = "connection present but slang exposes no expression"
            self.bindings.append(rec)

    def _fill_actual(self, rec: dict[str, Any], e: Any, style: str) -> None:
        inner = e
        # ADR-003 quirk: output actuals are Assignment(left=actual, right=EmptyArgument).
        if e.kind == EK.Assignment and e.syntax is None:
            inner = e.left
        core = inner
        while core.kind == EK.Conversion and core.isImplicit:
            core = core.operand
        loc = self.loc.use_site_range(e.sourceRange)
        if loc is None:
            rec["supported"] = False
            rec["note"] = (
                "actual expression starts/ends inside a macro expansion and no consistent "
                "use-site range exists; span withheld"
            )
        rec["actual_loc"] = loc
        rec["actual_kind"] = _short(core.kind)
        rec["actual_type"] = self._type_text(core.type)
        try:
            rec["bit_width"] = int(core.type.bitWidth) if core.type.isIntegral else None
        except Exception:  # noqa: BLE001
            rec["bit_width"] = None
        rec["is_constant"] = (
            core.kind not in (EK.NamedValue, EK.HierarchicalValue)
            and (core.constant is not None)
            and not self._has_value_refs(core)
        )
        text = self.loc.text(loc)
        refs: list[dict[str, Any]] = []

        def collect(x: Any) -> Any:
            if x.kind in (EK.NamedValue, EK.HierarchicalValue):
                refs.append(
                    {
                        "name": x.symbol.name,
                        "path": x.symbol.hierarchicalPath,
                        "decl_idx": self.decl_for(x.symbol),
                        "loc": self.loc.range(x.sourceRange),
                    }
                )
            return ast.VisitAction.Advance

        core.visit(collect)
        rec["referenced"] = refs
        if text is not None and text.startswith(".*"):
            # `.*`: the actual is implicit, the signal of the same name in scope.
            text = refs[0]["name"] if refs else None
            rec["implicit_actual"] = True
        rec["actual_text"] = text
        if core.kind == EK.Concatenation:
            for op in core.operands:
                ploc = self.loc.use_site_range(op.sourceRange)
                rec["parts"].append(
                    {"text": self.loc.text(ploc), "loc": ploc, "kind": _short(op.kind)}
                )

    @staticmethod
    def _has_value_refs(core: Any) -> bool:
        found = []

        def f(x: Any) -> Any:
            if x.kind in (EK.NamedValue, EK.HierarchicalValue):
                found.append(1)
            return ast.VisitAction.Advance

        core.visit(f)
        return bool(found)

    # ------------------------------------------------------------------ SVA
    def _scan_assertions(self, sym: Any, inst_idx: int) -> None:
        def cb(n: Any) -> Any:
            if isinstance(n, (ast.ConcurrentAssertionStatement, ast.ImmediateAssertionStatement)):
                self._guard("assertion", self._record_assertion, n, inst_idx)
            return ast.VisitAction.Advance

        sym.visit(cb)

    def _spec_text(self, node: Any) -> str | None:
        if node is None:
            return None
        return self.loc.text(self.loc.range(node.sourceRange))

    def _clock_disable(self, spec_syntax: Any) -> tuple[str | None, str | None, Any]:
        """(clock text, disable-iff text, property symbol used) from a PropertySpec syntax."""
        if spec_syntax is None:
            return None, None, None
        clock = self._spec_text(spec_syntax.clocking) if spec_syntax.clocking is not None else None
        dis = None
        if spec_syntax.disable is not None:
            dis = self._spec_text(spec_syntax.disable.expr)
        return clock, dis, None

    def _record_assertion(self, n: Any, inst_idx: int) -> None:
        s = n.syntax
        if s is None:
            return
        loc = self.loc.range(s.sourceRange)
        if loc is None:
            return
        key = (inst_idx, loc["file"], loc["start"], loc["end"])
        if key in self._assert_keys:
            return
        self._assert_keys.add(key)
        label = None
        if getattr(s, "label", None) is not None:
            label = s.label.name.valueText
        concurrent = isinstance(n, ast.ConcurrentAssertionStatement)
        akind = _ASSERTION_KINDS.get(_short(n.assertionKind), _short(n.assertionKind).lower())
        clock = dis = None
        prop_name = None
        prop_decl = None
        spec_syntax = getattr(s, "propertySpec", None)
        if concurrent and spec_syntax is not None:
            clock, dis, _ = self._clock_disable(spec_syntax)
            ps = n.propertySpec
            expr = getattr(ps, "expr", None)
            sym = getattr(expr, "symbol", None) if expr is not None else None
            if sym is not None and sym.kind in (SK.Property, SK.Sequence):
                prop_name = sym.name
                prop_decl = self.decl_for(sym)
                psyn = sym.syntax
                pspec = getattr(psyn, "propertySpec", None) or getattr(psyn, "seqExpr", None)
                if (
                    pspec is not None
                    and getattr(pspec, "kind", None) == syn.SyntaxKind.PropertySpec
                ):
                    c2, d2, _ = self._clock_disable(pspec)
                    clock = clock or c2
                    dis = dis or d2
        self.assertions.append(
            {
                "entry_type": "elaborated",
                "kind": akind,
                "style": "concurrent" if concurrent else "immediate",
                "label": label,
                "name": label or prop_name,
                "instance_idx": inst_idx,
                "source": loc,
                "property": prop_name,
                "property_decl_idx": prop_decl,
                "clock": clock,
                "disable_iff": dis,
                "execution_status": "NOT_RUN",
            }
        )

    def _binds(self) -> None:
        """Bind directives from syntax trees (source side), matched to elaborated instances."""
        by_dir: dict[tuple[str, int], list[int]] = {}
        for i in self.insts:
            b = i["bind"]
            if b and b["directive_loc"]:
                dl = b["directive_loc"]
                by_dir.setdefault((dl["file"], dl["start"]), []).append(i["idx"])
        seen: set[tuple[str, int]] = set()
        for tree in self.drv.syntaxTrees:

            def v(node: Any) -> None:
                if node.kind != syn.SyntaxKind.BindDirective:
                    return
                loc = self.loc.range(node.sourceRange)
                if loc is None:
                    return
                k = (loc["file"], loc["start"])
                if k in seen:
                    return
                seen.add(k)
                elab = []
                for ci in by_dir.get(k, []):
                    parent = self.insts[ci]["parent_idx"]
                    elab.append({"checker_instance_idx": ci, "target_instance_idx": parent})
                self.assertions.append(
                    {
                        "entry_type": "bind",
                        "kind": "bind",
                        "source": loc,
                        "target_text": str(node.target).strip(),
                        "instantiation_text": self._spec_text(node.instantiation),
                        "elaborated": elab,
                        "execution_status": "NOT_RUN",
                    }
                )

            try:
                tree.root.visit(v)
            except Exception as e:  # noqa: BLE001
                self.errors[f"binds:{type(e).__name__}"] += 1

    def _sva_sources(self) -> None:
        """Source-level SVA inventory: property/sequence declarations + de-duplicated statements."""
        for d in self.decls:
            if d["kind"] in ("property", "sequence"):
                self.assertions.append(
                    {
                        "entry_type": "source",
                        "kind": f"{d['kind']}_decl",
                        "name": d["name"],
                        "decl_idx": d["idx"],
                        "source": {**d["loc"], **(d["full"] or {})},
                        "execution_status": "NOT_RUN",
                    }
                )
        seen: set[tuple[Any, ...]] = set()
        for a in list(self.assertions):
            if a["entry_type"] != "elaborated":
                continue
            k = (a["source"]["file"], a["source"]["start"], a["source"]["end"])
            if k in seen:
                continue
            seen.add(k)
            self.assertions.append({**a, "entry_type": "source", "instance_idx": None})

    # ------------------------------------------------------------------ inactive `ifdef regions
    def _tokens(self, node: Any) -> Any:
        for i in range(len(node)):
            c = node[i]
            if c is None:
                continue
            if isinstance(c, parsing.Token):
                yield c
            else:
                yield from self._tokens(c)

    def _inactive_regions(self) -> None:
        if not self.req.options.include_inactive_regions:
            return
        seen: set[tuple[str, int]] = set()
        for tree in self.drv.syntaxTrees:
            try:
                for tok in self._tokens(tree.root):
                    for tr in tok.trivia:
                        if tr.kind != parsing.TriviaKind.Directive:
                            continue
                        node = tr.syntax()
                        dis = getattr(node, "disabledTokens", None)
                        if dis is None or len(dis) == 0:
                            continue
                        self._one_region(node, dis, seen)
            except Exception as e:  # noqa: BLE001
                self.errors[f"inactive:{type(e).__name__}"] += 1

    def _one_region(self, node: Any, dis: Any, seen: set[tuple[str, int]]) -> None:
        first, last = dis[0], dis[len(dis) - 1]
        loc = self.loc.span(first.range.start, last.range.end)
        if loc is None:
            return
        k = (loc["file"], loc["start"])
        if k in seen:
            return
        seen.add(k)
        cond = None
        if getattr(node, "expr", None) is not None:
            cond = self._spec_text(node.expr)
        decls: list[dict[str, Any]] = []
        text = self.loc.text(loc) or ""
        try:
            tree = syn.SyntaxTree.fromText(text)
            root = tree.root
            nodes = (
                [root]
                if self._frag_decl(root)[0] is not None
                else (
                    list(getattr(root, "members", []))
                    if root.kind == syn.SyntaxKind.CompilationUnit
                    else []
                )
            )
            for m in nodes:
                nm, kind = self._frag_decl(m)
                if nm is not None:
                    decls.append(
                        {
                            "kind": kind,
                            "name": nm,
                            "start": loc["start"]
                            + len(text[: m.sourceRange.start.offset].encode()),
                            "status": "inactive_for_profile",
                        }
                    )
        except Exception as e:  # noqa: BLE001
            self.errors[f"inactive_parse:{type(e).__name__}"] += 1
        self.inactive.append(
            {
                "kind": "disabled_conditional_region",
                "directive": _short(node.kind),
                "condition": cond,
                "loc": loc,
                "declarations": decls,
                "status": "inactive_for_profile",
            }
        )

    @staticmethod
    def _frag_decl(m: Any) -> tuple[str | None, str]:
        k = m.kind
        names = {
            syn.SyntaxKind.ModuleDeclaration: "module",
            syn.SyntaxKind.InterfaceDeclaration: "interface",
            syn.SyntaxKind.ProgramDeclaration: "program",
            syn.SyntaxKind.PackageDeclaration: "package",
        }
        if k in names:
            return m.header.name.valueText, names[k]
        if k == syn.SyntaxKind.ClassDeclaration:
            return m.name.valueText, "class"
        return None, ""

    # ------------------------------------------------------------------ gaps & deps
    def _gaps(self) -> None:
        for key, spec in self.specs.items():
            if "class_decl_idx" not in spec:
                self.gaps.append(
                    {
                        "kind": "specialization_body_not_walked",
                        "name": key,
                        "detail": "specialization reached but never processed",
                    }
                )
        specialized = {s.get("class_decl_idx") for s in self.specs.values()}
        for d in self.decls:
            if d["kind"] == "class" and d["attrs"].get("generic") and d["idx"] not in specialized:
                self.gaps.append(
                    {
                        "kind": "generic_class_not_specialized",
                        "name": d["qualified_name"],
                        "decl_idx": d["idx"],
                        "detail": "generic class body is not elaborated; references inside it are "
                        "not extracted",
                    }
                )
        for d in self.decls:
            if d["kind"] == "instance":
                continue
        for i in self.insts:
            if i["stub"]:
                self.gaps.append(
                    {
                        "kind": "stub_boundary",
                        "name": i["definition"],
                        "instance": i["path"],
                        "detail": f"explicit stub {i['stub_name']!r}: internals unknown",
                    }
                )
            elif i["blackbox_candidate"]:
                self.gaps.append(
                    {
                        "kind": "empty_module_body",
                        "name": i["definition"],
                        "instance": i["path"],
                        "detail": "module has ports but no body (possible blackbox, not declared)",
                    }
                )

    def _dependencies(self) -> dict[str, Any]:
        sm = self.sm
        files: list[dict[str, Any]] = []
        seen: set[str] = set()
        unnamed = 0
        skip = {pyslang.BufferKind.Macro, pyslang.BufferKind.MacroArg}
        bufs = list(sm.getAllBuffers())
        for b in bufs:
            kind = sm.getBufferKind(b)
            name = sm.getRawFileName(b)
            if kind in skip:
                continue
            if not name or name.startswith("<"):
                unnamed += 1
                continue
            fp = str(sm.getFullPath(b))
            if fp in seen:
                continue
            seen.add(fp)
            rel, outside = self.loc.rel(fp)
            files.append({"path": rel, "outside_root": outside, "kind": _short(kind)})
        incs = []
        for t in self.drv.syntaxTrees:
            for inc in t.getIncludeDirectives():
                incs.append({"path": inc.path, "system": bool(inc.isSystem)})
        return {
            "files": files,
            "include_directives": incs,
            "total_buffers": len(bufs),
            "unnamed_buffers": unnamed,
        }

    # ------------------------------------------------------------------ post-processing
    def _postprocess(self) -> None:
        self._sva_sources()
        for b in self.bindings:
            ii = b.get("interface_instance")
            b["interface_instance_idx"] = self._inst_by_hier.get(ii) if ii else None
        self._override_candidates()
        self._containers()
        self._assertion_errors()
        for (ks, sc), n in sorted(self.kind_counts.items()):
            self.stats[f"unsupported_kind:{ks}:{sc}"] = n

    def _override_candidates(self) -> None:
        """Static set of overriding methods for virtual calls (runtime target stays unresolved)."""
        children: dict[int, list[int]] = {}
        for d in self.decls:
            ov = d["attrs"].get("override_decl_idx")
            if d["kind"] in ("function", "task") and ov is not None:
                children.setdefault(ov, []).append(d["idx"])
        for r in self.refs:
            if r["status"] != "dynamic_unresolved" or r["target_idx"] is None:
                continue
            out: list[int] = []
            todo = [r["target_idx"]]
            while todo:
                for c in children.get(todo.pop(), []):
                    if c not in out:
                        out.append(c)
                        todo.append(c)
            r["context"]["override_candidates"] = sorted(out)

    def _containers(self) -> None:
        """Enclosing declaration of each reference, by innermost containing full span."""
        by_file: dict[str, list[tuple[int, int, int]]] = {}
        for d in self.decls:
            f = d["full"]
            if f and d["kind"] in (
                "function",
                "task",
                "class",
                "module",
                "interface",
                "program",
                "package",
                "constraint",
                "covergroup",
                "property",
                "sequence",
                "checker",
            ):
                by_file.setdefault(d["loc"]["file"], []).append((f["start"], f["end"], d["idx"]))
        for lst in by_file.values():
            lst.sort(key=lambda t: (t[0], -t[1]))
        refs_by_file: dict[str, list[dict[str, Any]]] = {}
        for r in self.refs:
            refs_by_file.setdefault(r["loc"]["file"], []).append(r)
        for fname, rs in refs_by_file.items():
            decls = by_file.get(fname, [])
            rs.sort(key=lambda r: r["loc"]["start"])
            stack: list[tuple[int, int, int]] = []
            di = 0
            for r in rs:
                pos = r["loc"]["start"]
                while di < len(decls) and decls[di][0] <= pos:
                    stack.append(decls[di])
                    di += 1
                while stack and stack[-1][1] <= pos:
                    stack.pop()
                r["container_idx"] = stack[-1][2] if stack else None
        for r in self.refs:
            r.setdefault("container_idx", None)

    def _assertion_errors(self) -> None:
        errs = [d for d in self.diagnostics if d["is_error"] and d["loc"]]
        for a in self.assertions:
            src = a.get("source")
            if not src:
                continue
            spans = [src]
            pd = a.get("property_decl_idx")
            if pd is not None and self.decls[pd]["full"]:
                f = self.decls[pd]["full"]
                spans.append(
                    {"file": self.decls[pd]["loc"]["file"], "start": f["start"], "end": f["end"]}
                )
            hit = [
                d["code"]
                for d in errs
                if any(
                    d["loc"]["file"] == s["file"] and s["start"] <= d["loc"]["start"] < s["end"]
                    for s in spans
                )
            ]
            a["resolved"] = not hit
            a["errors"] = sorted(set(hit))
        for a in self.assertions:
            if a["entry_type"] == "bind":
                hit = [
                    d["code"]
                    for d in errs
                    if a["source"]["file"] == d["loc"]["file"]
                    and a["source"]["start"] <= d["loc"]["start"] < a["source"]["end"]
                ]
                a["errors"] = sorted(set(hit))
                a["resolved"] = not hit and bool(a["elaborated"])

    def _result(self, deps: dict[str, Any]) -> dict[str, Any]:
        unsupported = [
            {
                "kind": k,
                "scope": scope,
                "count": n,
                "sample": self.kind_sample.get((k, scope)),
                "detail": "symbol kind not extracted by this extractor version",
            }
            for (k, scope), n in sorted(self.kind_counts.items())
        ]
        for k, n in sorted(self.errors.items()):
            unsupported.append(
                {
                    "kind": f"extractor_exception:{k}",
                    "scope": "project",
                    "count": n,
                    "sample": None,
                    "detail": "exception while extracting; affected nodes missing",
                }
            )
        stats = dict(self.stats)
        stats.update(
            declarations=len(self.decls),
            references=len(self.refs),
            instances=len(self.insts),
            port_bindings=len(self.bindings),
            assertions=len(self.assertions),
            diagnostics=len(self.diagnostics),
            specializations=len(self.specs),
            errors=sum(self.errors.values()),
        )
        return {
            "schema_version": DTO_SCHEMA,
            "extractor_version": EXTRACTOR_VERSION,
            "pyslang_version": getattr(pyslang, "__version__", ""),
            "status": {},
            "driver_messages": self.driver_messages,
            "diagnostics": self.diagnostics,
            "dependencies": deps,
            "declarations": self.decls,
            "references": self.refs,
            "instances": self.insts,
            "port_bindings": self.bindings,
            "assertions": self.assertions,
            "specializations": list(self.specs.values()),
            "inactive": self.inactive,
            "gaps": self.gaps,
            "unsupported_nodes": unsupported,
            "dropped_refs": [
                {"reason": r, "scope": sc, "count": c} for (r, sc), c in sorted(self.drops.items())
            ],
            "stats": stats,
        }
