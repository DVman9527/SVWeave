#!/usr/bin/env python3
"""Critical mutation suite (plan 20.5, release gate "Critical invariants").

Twenty-seven deliberate defects cover the ten classes of plan 20.5 (class 7 has two variants),
the M12–M22 extra write-safety rules, M23–M26 Slurm boundaries, and M27 shared-launcher workspace
binding. They are injected, one at a time, into a THROWAWAY COPY of the
repository. For each one the mapped test subset is run against the copy; a mutation is ``KILLED`` when the subset
fails (pytest exit 1 with at least one FAILED test) and ``SURVIVED`` when it stays green. Anything
else (patch anchor missing, syntax/collection error, timeout, red baseline) is ``ERROR`` and is
never counted as killed.

How the copy is really imported
-------------------------------
The slang worker runs ``python -I`` (isolated: ``PYTHONPATH`` is ignored), so an environment
variable would not make it import the copy. Instead a throwaway venv
(``python -m venv --without-pip``) is created inside the scratch directory: its site-packages is a
hard-linked/copied clone of the repo ``.venv`` one (without the editable ``svweave`` hooks) plus
ONE ``.pth`` file naming the copy's ``src`` (``.pth`` entries are honoured under ``-I``; the
sandboxed worker only sees ``sys.prefix``). The suite then asserts, with ``python -I``, that
``svweave.__file__`` and every patched module resolve inside the scratch copy and (after patching)
that the module file really contains the mutation marker.

Usage::

    .venv/bin/python scripts/mutation_suite.py [--only M01,M05] [--scratch DIR] [--keep]
        [--out evidence/mutation/<date>.json] [--timeout 1200]
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import re
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
MARK = "SVW_MUTATION"
IGNORE_DIRS = {".venv", ".toolchain", ".svweave", ".git", "__pycache__", ".pytest_cache",
               ".mypy_cache", ".ruff_cache", "evidence", "htmlcov", ".claude"}


@dataclass
class Patch:
    path: str  # repo-relative
    old: str
    new: str


@dataclass
class Mutation:
    mid: str
    title: str
    plan: str
    patches: list[Patch]
    tests: list[str]  # pytest args (paths / -k expressions)
    result: dict[str, object] = field(default_factory=dict)


def m(mid: str, title: str, plan: str, patches: list[Patch], tests: list[str]) -> Mutation:
    return Mutation(mid, title, plan, patches, tests)


def mark(mid: str, text: str) -> str:
    return f"{text}  # {MARK}:{mid}"


MUTATIONS: list[Mutation] = [
    m(
        "M01", "remove the header hash from the snapshot inventory (headers hash to a constant)",
        "20.5 #1; 9.1/9.3",
        [Patch("src/svweave/snapshot/capture.py",
               "            sha, size = _hash_file(e.real)\n",
               "            sha, size = _hash_file(e.real)\n"
               "            if e.logical.endswith((\".svh\", \".vh\")):\n"
               "                sha = \"0\" * 64  # " + MARK + ":M01\n")],
        ["tests/unit/test_snapshot.py", "tests/unit/test_review_fixes.py", "-k",
         "SNP or header"],
    ),
    m(
        "M02", "ignore include-directory order (slang -I dirs are sorted alphabetically)",
        "20.5 #2; BLD-07",
        [Patch("src/svweave/manifest/slang_args.py",
               "    for d in unit.include_dirs:\n        args += [\"-I\", p(d.path)]\n",
               "    for d in sorted(unit.include_dirs, key=lambda x: x.path):  # " + MARK + ":M02\n"
               "        args += [\"-I\", p(d.path)]\n")],
        ["tests/unit/test_slang_args.py", "tests/integration/test_build_identity.py", "-k",
         "BLD_07 or include or incdir or slang_args or args"],
    ),
    m(
        "M03", "merge methods by bare name (function/task qualified_name = bare name)",
        "20.5 #3; SEM same-name methods",
        [Patch("src/svweave/adapters/slang/extract.py",
               "        qn = sym.lexicalPath\n        cu = self._cu_of(sym)\n        mac = ns.get(\"macro\")\n",
               "        qn = sym.lexicalPath\n"
               "        if dkind in (\"function\", \"task\"):\n"
               "            qn = name  # " + MARK + ":M03\n"
               "        cu = self._cu_of(sym)\n        mac = ns.get(\"macro\")\n")],
        ["tests/integration/test_uvm.py", "tests/integration/test_sem.py",
         "tests/integration/test_edt_flow.py::test_bare_method_name_is_ambiguous_even_with_real_serena"],
    ),
    m(
        "M04", "PARTIAL becomes COMPLETE (ingest completeness verdict always COMPLETE)",
        "20.5 #4; SEM-07",
        [Patch("src/svweave/adapters/slang/ingest.py",
               "    completeness = Completeness.PARTIAL if reasons else Completeness.COMPLETE\n",
               "    completeness = Completeness.COMPLETE  # " + MARK + ":M04\n")],
        ["tests/integration/test_sem.py", "tests/integration/test_sva.py",
         "tests/integration/test_cli_semantic.py"],
    ),
    m(
        "M05", "cancel the expected-hash compare-and-set (exact range and commit precondition)",
        "20.5 #5; EDT-02",
        [Patch("src/svweave/editing/plan.py",
               "    if sha256_hex(data[e.start_byte : e.end_byte]) != e.expected_sha256:\n",
               "    if False:  # " + MARK + ":M05\n"),
         Patch("src/svweave/application/edit.py",
               "            if jr.current_sha(p) != jf.before_sha256:\n"
               "                raise SvwError(\n"
               "                    ErrorCode.EDIT_CONFLICT,\n"
               "                    f\"{jf.logical_path} changed since the base snapshot; refusing to overwrite\",\n"
               "                    {\"reason\": \"expected_hash_mismatch\", \"path\": jf.logical_path},\n"
               "                )\n",
               "            if False:  # " + MARK + ":M05\n"
               "                raise SvwError(ErrorCode.EDIT_CONFLICT, \"unreachable\")\n"),
         Patch("src/svweave/editing/journal.py",
               "        if cur != jf.before_sha256:\n            raise SvwError(\n                ErrorCode.EDIT_CONFLICT,\n"
               "                f\"{jf.logical_path} changed since the base snapshot; refusing to overwrite\",\n",
               "        if False:  # " + MARK + ":M05\n            raise SvwError(\n                ErrorCode.EDIT_CONFLICT,\n"
               "                f\"{jf.logical_path} changed since the base snapshot; refusing to overwrite\",\n")],
        ["tests/unit/test_edit_prepare.py", "tests/unit/test_edit_commit.py",
         "tests/fault_injection/test_edit_crash.py"],
    ),
    m(
        "M06", "accept a fake approval (HMAC signature check disabled)",
        "20.5 #6; EDT-04",
        [Patch("src/svweave/editing/approval.py",
               "    if not hmac.compare_digest(rec[\"hmac\"], _sig(key, body)):\n",
               "    if False:  # " + MARK + ":M06\n")],
        ["tests/unit/test_edit_commit.py", "tests/unit/test_edit_machine.py",
         "tests/unit/test_edit_validate.py"],
    ),
    m(
        "M07", "snapshot/candidate separation lost (candidate files are hardlinks of the snapshot, "
               "so a candidate write mutates the snapshot)",
        "20.5 #7 (backend must not write the original: snapshot side); ADR-007, SEC-01",
        [Patch("src/svweave/editing/candidate.py",
               "                with open(s, \"rb\") as fin, open(d, \"xb\") as fout:\n"
               "                    while chunk := fin.read(_CHUNK):\n"
               "                        fout.write(chunk)\n"
               "                os.chmod(d, 0o644)\n",
               "                os.link(s, d)  # " + MARK + ":M07\n")],
        ["tests/unit/test_edit_prepare.py", "tests/unit/test_edit_validate.py",
         "tests/unit/test_adapters_serena.py"],
    ),
    m(
        "M11", "sandbox write-surface check removed (a backend may get a writable bind of the "
               "protected original)",
        "20.5 #7 (backend must not write the original: sandbox side); SEC-05, ADR-011",
        [Patch("src/svweave/security/sandbox.py",
               "            if _overlaps(w, pr):\n",
               "            if False:  # " + MARK + ":M11\n")],
        ["tests/integration/test_int_sandbox.py::test_sec05_cannot_make_original_writable",
         "tests/unit/test_sandbox.py"],
    ),
    m(
        "M08", "blind rollback after a crash (conflicting files are classified as applied, "
               "recovery CAS removed)",
        "20.5 #8; EDT-07/08",
        [Patch("src/svweave/editing/journal.py",
               "        else:\n            v = \"conflict\"\n",
               "        else:\n            v = \"applied\"  # " + MARK + ":M08\n"),
         Patch("src/svweave/application/edit.py",
               "    if jr.current_sha(path) != expect_current:\n"
               "        raise SvwError(ErrorCode.EDIT_CONFLICT, \"file changed during recovery\",\n",
               "    if False:  # " + MARK + ":M08\n"
               "        raise SvwError(ErrorCode.EDIT_CONFLICT, \"file changed during recovery\",\n")],
        ["tests/fault_injection/test_edit_crash.py", "tests/unit/test_edit_commit.py"],
    ),
    m(
        "M09", "ignore unsupported options (unknown flag is logged and dropped, build not blocked)",
        "20.5 #9; MAN unsupported flags",
        [Patch("src/svweave/manifest/resolver.py",
               "                self._issue(\n"
               "                    ErrorCode.UNSUPPORTED_OPTION,\n"
               "                    f\"unsupported option {raw!r}; not dropped, build is BLOCKED\",\n"
               "                    o,\n"
               "                    chain,\n"
               "                    reason=\"unknown_option\",\n"
               "                    option=raw,\n"
               "                )\n",
               "                self._log(\"ignored_option\", raw, o, chain, scope.id,\n"
               "                          classification=\"output_only\")  # " + MARK + ":M09\n")],
        ["tests/unit/test_manifest.py", "tests/unit/test_slang_args.py", "tests/unit/test_config.py"],
    ),
    m(
        "M10", "treat a static build as simulation PASS (absent evidence reported as PASS)",
        "20.5 #10; REL-01, SVA-03",
        [Patch("src/svweave/application/evidence.py",
               "        return {\"state\": \"NOT_RUN\", \"records\": 0, \"by_result\": {}, \"runs\": []}\n",
               "        return {\"state\": \"PASS\", \"records\": 0, \"by_result\": {}, \"runs\": []}"
               "  # " + MARK + ":M10\n")],
        ["tests/unit/test_gateway_evidence.py", "tests/unit/test_gateway_status_report.py",
         "tests/contract"],
    ),
    m(
        "M12", "roll-forward skips recovery gates (policy / approval / candidate rechecks)",
        "A3 (beyond 20.5); EDT-04, EDT-07/08",
        [Patch("src/svweave/application/edit.py",
               "            _roll_forward_gates(ctx, txn, j)  # A3: same gates as a normal commit\n",
               "            pass  # " + MARK + ":M12\n")],
        ["tests/fault_injection/test_recover_gates.py"],
    ),
    m(
        "M13", "commit and roll-forward skip the current edit-policy recheck (A4)",
        "A4 (beyond 20.5); EDT-04",
        # Removing a recheck is caught by the policy-denied diagnosis (POLICY_DENIED /
        # path_not_allowed, validate_profiles_changed); the HMAC approval binding independently
        # still blocks the write, so this does not claim the recheck is the only safeguard.
        [Patch("src/svweave/application/edit.py",
               "        recheck_edit_policy(ctx, list(txn.record.get(\"files\", [])),\n"
               "                            list(txn.record.get(\"profiles\", [])))\n",
               "        pass  # " + MARK + ":M13\n"),
         Patch("src/svweave/application/edit.py",
               "    recheck_edit_policy(ctx, [f.logical_path for f in j.files],\n"
               "                        list(txn.record.get(\"profiles\", [])))\n",
               "    pass  # " + MARK + ":M13\n")],
        # node ids, not "-k": a -k expression would filter every listed file. "m12" in the two
        # test names below is an older review finding (F10/M12), not mutation M12.
        ["tests/unit/test_edit_final_a.py",
         "tests/fault_injection/test_recover_gates.py",
         "tests/unit/test_edit_review_fixes.py::test_f10_m12_commit_time_policy_narrowing_is_policy_denied",
         "tests/unit/test_edit_review_fixes.py::test_m12_precommit_path_check_is_independent_of_the_freeze_check"],
    ),
    m(
        "M14", "slang_symbol resolution ignores the build/snapshot identity and COMPLETE check",
        "ADR-013 (beyond 20.5); AGENTS rule 5, EDT-03",
        # The name -> declaration lookup is only valid in the COMPLETE build of the base
        # snapshot. prepare() checks the same facts first; the resolver repeats them so it never
        # trusts its caller, and these two tests call the resolver directly.
        [Patch("src/svweave/editing/plan.py",
               "    if b[\"snapshot_id\"] != rec.snapshot_id:\n",
               "    if False:  # " + MARK + ":M14\n"),
         Patch("src/svweave/editing/plan.py",
               "    if b[\"completeness\"] != \"COMPLETE\":\n",
               "    if False:  # " + MARK + ":M14\n")],
        ["tests/unit/test_edit_slang_symbol.py::test_resolver_refuses_a_build_that_belongs_to_another_snapshot",
         "tests/unit/test_edit_slang_symbol.py::test_partial_build_refuses_name_resolution_at_prepare_and_in_the_resolver"],
    ),
    m(
        "M15", "slang_symbol picks the first of several matching names (ambiguity refusal removed)",
        "ADR-013 (beyond 20.5); EDT-03",
        [Patch("src/svweave/editing/plan.py",
               "        if len(rows) > 1:\n",
               "        if False:  # " + MARK + ":M15\n")],
        ["tests/unit/test_edit_slang_symbol.py::test_b1_bare_name_shared_by_module_interface_and_class_is_ambiguous",
         "tests/unit/test_edit_slang_symbol.py::test_bare_name_matching_several_is_ambiguous_and_lists_scoped_candidates"],
    ),
    m(
        "M16", "slang_symbol replacement shape check removed (class / comment / qualifier escapes)",
        "ADR-013 (beyond 20.5); EDT-06",
        [Patch("src/svweave/editing/plan.py",
               "    if rshape is None or rshape[0] != shape[0]:\n",
               "    if False:  # " + MARK + ":M16\n")],
        ["tests/unit/test_edit_slang_symbol.py::test_b3_replacement_must_be_exactly_one_declaration",
         "tests/unit/test_edit_slang_symbol.py::test_replacement_must_be_a_whole_declaration_of_the_same_kind"],
    ),
    m(
        "M17", "slang_symbol scope-escape check after the candidate build removed",
        "ADR-013 (beyond 20.5); EDT-06",
        # the lexical shape check is a first line; this is the decisive slang-based one
        [Patch("src/svweave/application/edit.py",
               "        problems = slang_scope_escape(db, base_env.build_id, cand_env.build_id, targets)\n",
               "        problems = []  # " + MARK + ":M17\n")],
        ["tests/unit/test_edit_slang_symbol.py::test_b3_renamed_constructor_is_a_scope_escape_and_cannot_be_approved",
         "tests/unit/test_edit_slang_symbol.py::test_b3_renamed_task_is_a_scope_escape"],
    ),
    m(
        "M18", "slang_symbol base-span shape check removed (extern / prototype / odd span)",
        "ADR-013 (beyond 20.5); EDT-06",
        # the mutant treats ANY span as well-formed (never None) so the kill is the missing
        # refusal ("DID NOT RAISE"), not a crash further down
        [Patch("src/svweave/editing/plan.py",
               "    shape = _subroutine_shape(cur, strict=False)\n",
               "    shape = _subroutine_shape(cur, strict=False) or (\n"
               "        b\"task\" if row[\"kind\"] == \"task\" else b\"function\", b\"\")"
               "  # " + MARK + ":M18\n")],
        ["tests/unit/test_edit_slang_symbol.py::test_m18_non_declaration_span_is_refused_with_the_specific_reason",
         "tests/unit/test_edit_slang_symbol.py::test_extern_prototype_span_is_refused"],
    ),
    m(
        "M19", "slang_symbol accepts a symbol_id that is not a task or function",
        "ADR-013 (beyond 20.5); EDT-03",
        [Patch("src/svweave/editing/plan.py",
               "        if row[\"kind\"] not in subr:\n",
               "        if False:  # " + MARK + ":M19\n")],
        ["tests/unit/test_edit_slang_symbol.py::test_symbol_id_of_a_non_subroutine_is_refused"],
    ),
    m(
        "M20", "slang_symbol replacement may contain preprocessor directives / pasting / any macro",
        "ADR-013 (beyond 20.5); EDT-06",
        [Patch("src/svweave/editing/plan.py",
               "    if why is not None:\n",
               "    if False:  # " + MARK + ":M20\n")],
        ["tests/unit/test_edit_slang_symbol.py::test_r1_directives_pasting_and_stringification_are_refused",
         "tests/unit/test_edit_slang_symbol.py::test_r1_macro_end_task_escape_on_a_module_task_is_refused",
         "tests/unit/test_edit_slang_symbol.py::test_r2_macro_redefinition_cannot_change_later_code",
         "tests/unit/test_edit_slang_symbol.py::test_r1_macro_invocation_rules"],
    ),
    m(
        "M21", "slang_symbol candidate target span is not required to equal the replacement",
        "ADR-013 (beyond 20.5); EDT-06",
        [Patch("src/svweave/editing/plan.py",
               "                if side == \"candidate\" and t.get(\"new_start\") is not None and (\n",
               "                if False and (  # " + MARK + ":M21\n")],
        ["tests/unit/test_edit_slang_symbol.py::test_r1_span_equality_backstop_rejects_a_candidate_whose_target_span_differs"],
    ),
    m(
        "M22", "slang_symbol macro-body walk ignores state-changing directives (include / undef / define)",
        "ADR-013 (beyond 20.5); EDT-06",
        [Patch("src/svweave/editing/plan.py",
               "                if inner in _STATE_DIRECTIVES:\n",
               "                if False:  # " + MARK + ":M22\n")],
        ["tests/unit/test_edit_slang_symbol.py::test_n2_state_changing_directive_inside_a_macro_body_is_refused"],
    ),
    m(
        "M23", "remove the P0 remote editing provider guard",
        "support_srun SRUN-23/P0 editing",
        [Patch("src/svweave/editing/plan.py",
               '    if ctx.execution.provider != "local":\n',
               '    if False:  # ' + MARK + ':M23\n')],
        ["tests/unit/test_srun_integration_guards.py::test_SRUN_P0_edit_guard_precedes_baseline_and_candidate"],
    ),

    m(
        "M24", "ignore exact scheduler ownership on cancellation",
        "support_srun SRUN-09/11/23",
        [Patch("src/svweave/execution/ownership.py",
               " or expected != observed.ownership):\n",
               "):  # " + MARK + ":M24\n")],
        ["tests/unit/test_execution_provider.py::test_SRUN_09_exact_borrowed_step_only"],
    ),
    m(
        "M25", "drop unknown remote cleanup snapshot pins",
        "support_srun SRUN-17/23",
        [Patch("src/svweave/storage/db.py",
               "    remote_pins = pinned_snapshots(snapshots_dir.parent, c)\n",
               "    remote_pins: set[str] | None = set()  # " + MARK + ":M25\n")],
        ["tests/unit/test_srun_integration_guards.py::test_SRUN_17_unknown_cleanup_pins_snapshots_and_orphans"],
    ),

    m(
        "M26", "ignore unresolved remote workspace submission barrier",
        "support_srun SRUN-10/17/23",
        [Patch("src/svweave/execution/ledger.py",
               "    if workspace_requires_reconciliation(state_dir, conn):\n",
               "    if False:  # " + MARK + ":M26\n")],
        ["tests/unit/test_srun_audit_regressions.py::test_SRUN_10_failed_remote_attempt_blocks_direct_and_async_new_work"],
    ),

    m(
        "M27", "allow a bound shared-launcher workspace to rebind through a later symlink",
        "plan 14.2 workspace binding; GLB-04",
        [Patch("src/svweave/application/options.py",
               "        if self.bound_manifest_dir is None:\n",
               "        if True:  # " + MARK + ":M27\n")],
        ["tests/unit/test_global_launcher.py::test_GLB_04_bound_canonical_directory_cannot_be_retargeted",
         "tests/unit/test_global_launcher.py::test_GLB_04_bound_manifest_cannot_become_a_symlink",
         "tests/unit/test_global_launcher.py::test_GLB_04_rejected_binding_does_not_write_retargeted_event_log"],
    ),
]


def run(cmd: list[str], cwd: Path, env: dict[str, str], timeout: float) -> tuple[int, str, float]:
    t0 = time.monotonic()
    try:
        p = subprocess.run(cmd, cwd=cwd, env=env, capture_output=True, text=True, timeout=timeout)
        return p.returncode, p.stdout + p.stderr, time.monotonic() - t0
    except subprocess.TimeoutExpired as e:
        out = (e.stdout or b"")
        txt = out.decode("utf-8", "replace") if isinstance(out, bytes) else str(out)
        return 124, txt + "\n[TIMEOUT]", time.monotonic() - t0


def make_copy(scratch: Path) -> Path:
    dst = scratch / "repo"
    if scratch.exists():
        shutil.rmtree(scratch)
    scratch.mkdir(parents=True)

    def ignore(d: str, names: list[str]) -> set[str]:
        return {n for n in names if n in IGNORE_DIRS or n.endswith(".pyc")}

    shutil.copytree(REPO, dst, ignore=ignore, symlinks=True)
    # backends / UVM library are read-only inputs shared with the real repo
    if (REPO / ".toolchain").exists():
        (dst / ".toolchain").symlink_to(REPO / ".toolchain")
    return dst


def _link_or_copy(src: str, dst: str) -> None:
    try:
        os.link(src, dst)
    except OSError:
        shutil.copy2(src, dst)


def make_venv(scratch: Path, copy_root: Path) -> Path:
    """Throwaway venv whose site-packages is a (hard-linked or copied) clone of the repo venv's,
    minus every editable ``svweave`` hook, plus ONE .pth pointing at the copy's ``src``.

    It must be a real clone and not a .pth to the repo's site-packages: the slang worker runs in a
    bwrap sandbox that only mounts ``sys.prefix`` (this venv) and the svweave source tree, so a
    foreign site-packages path would be invisible to it (pydantic/pyslang import errors)."""
    venv = scratch / "venv"
    subprocess.run([sys.executable, "-m", "venv", "--without-pip", str(venv)], check=True)
    sp = next((venv / "lib").glob("python3*/site-packages"))
    repo_sp = REPO / ".venv" / "lib" / sp.parent.name / "site-packages"
    if not repo_sp.is_dir():
        raise SystemExit(f"{repo_sp} missing: create the repo venv first")

    def skip(_d: str, names: list[str]) -> set[str]:
        return {n for n in names if n.endswith((".pth", ".egg-link")) or n.startswith(
            ("svweave", "__editable__", "__pycache__")) or n == "pip" or n.startswith("pip-")}

    shutil.copytree(repo_sp, sp, ignore=skip, copy_function=_link_or_copy, dirs_exist_ok=True,
                    symlinks=True)
    (sp / "svweave_mutation_copy.pth").write_text(f"{copy_root / 'src'}\n")
    py = venv / "bin" / "python"
    return py


def module_of(rel: str) -> str:
    return rel.removeprefix("src/").removesuffix(".py").replace("/", ".")


def resolve_origin(py: Path, mod: str, copy_root: Path) -> str:
    code = f"import importlib.util as u; s=u.find_spec({mod!r}); print(s.origin)"
    p = subprocess.run([str(py), "-I", "-c", code], capture_output=True, text=True, cwd="/")
    return p.stdout.strip()


def apply_patches(mut: Mutation, copy_root: Path) -> None:
    for pt in mut.patches:
        f = copy_root / pt.path
        text = f.read_text(encoding="utf-8")
        n = text.count(pt.old)
        if n != 1:
            raise RuntimeError(f"{mut.mid}: anchor found {n} times (need exactly 1) in {pt.path}:\n"
                               f"{pt.old}")
        f.write_text(text.replace(pt.old, pt.new), encoding="utf-8")


def restore(mut: Mutation, copy_root: Path) -> None:
    for pt in mut.patches:
        shutil.copyfile(REPO / pt.path, copy_root / pt.path)


FAILED_RE = re.compile(r"^FAILED (\S+)", re.M)


def pytest_cmd(py: Path, args: list[str]) -> list[str]:
    return [str(py), "-m", "pytest", "--no-cov", "-q", "-p", "no:cacheprovider", "-rf",
            "--maxfail=5", "--tb=line", *args]


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    ap.add_argument("--only", default="", help="comma list of mutation ids, e.g. M01,M05")
    ap.add_argument("--scratch", type=Path, default=REPO / ".svweave" / "mutation")
    ap.add_argument("--keep", action="store_true", help="keep the scratch copy")
    ap.add_argument("--out", type=Path,
                    default=REPO / "evidence" / "mutation" / f"{dt.date.today().isoformat()}.json")
    ap.add_argument("--timeout", type=float, default=1200.0, help="seconds per pytest run")
    ap.add_argument("--no-baseline", action="store_true")
    a = ap.parse_args(argv)

    want = {x for x in a.only.split(",") if x}
    muts = [x for x in MUTATIONS if not want or x.mid in want]
    copy_root = make_copy(a.scratch.resolve())
    py = make_venv(a.scratch.resolve(), copy_root)
    env = {k: v for k, v in os.environ.items() if k in ("PATH", "HOME", "LANG", "LC_ALL", "TMPDIR")}
    env["SVWEAVE_REQUIRE_BACKENDS"] = os.environ.get("SVWEAVE_REQUIRE_BACKENDS", "0")
    env["PYTHONDONTWRITEBYTECODE"] = "1"

    origin = resolve_origin(py, "svweave", copy_root)
    import_proof = {"svweave_origin_under_python_I": origin,
                    "inside_scratch_copy": origin.startswith(str(copy_root / "src"))}
    print(f"[mut] svweave imported (python -I) from: {origin}")
    if not import_proof["inside_scratch_copy"]:
        print("[mut] FATAL: the copy is not what gets imported; results would be meaningless")
        return 2

    baseline: dict[str, dict[str, object]] = {}
    results = []
    for mut in muts:
        key = " ".join(mut.tests)
        rec: dict[str, object] = {"id": mut.mid, "title": mut.title, "plan": mut.plan,
                                  "files": [p.path for p in mut.patches], "tests": mut.tests}
        try:
            if key not in baseline and not a.no_baseline:
                rc, out, dur = run(pytest_cmd(py, mut.tests), copy_root, env, a.timeout)
                baseline[key] = {"rc": rc, "seconds": round(dur, 1),
                                 "tail": out.strip().splitlines()[-3:]}
                print(f"[mut] baseline for {mut.mid}: rc={rc} {baseline[key]['tail'][-1:]}")
            b = baseline.get(key)
            rec["baseline_rc"] = None if b is None else b["rc"]
            if b is not None and b["rc"] != 0:
                rec.update(status="ERROR", reason="baseline (unmutated copy) is not green; a "
                           "mutation cannot be judged", baseline_tail=b["tail"])
                results.append(rec)
                continue
            apply_patches(mut, copy_root)
            proofs = {}
            for pt in mut.patches:
                mod = module_of(pt.path)
                org = resolve_origin(py, mod, copy_root)
                content = Path(org).read_text() if org and Path(org).is_file() else ""
                ok = org.startswith(str(copy_root / "src")) and f"{MARK}:{mut.mid}" in content
                proofs[mod] = {"origin_in_copy": org.startswith(str(copy_root / "src")),
                               "marker_present": f"{MARK}:{mut.mid}" in content}
                if not ok:
                    raise RuntimeError(f"{mut.mid}: patched module {mod} not what python -I imports "
                                       f"({org})")
            rec["import_proof"] = proofs
            rc, out, dur = run(pytest_cmd(py, mut.tests), copy_root, env, a.timeout)
            failed = FAILED_RE.findall(out)
            tail = out.strip().splitlines()[-4:]
            if rc == 1 and failed:
                status = "KILLED"
            elif rc == 0:
                status = "SURVIVED"
            else:
                status = "ERROR"
            rec.update(status=status, pytest_rc=rc, seconds=round(dur, 1),
                       failing_tests=failed[:10], pytest_tail=tail)
            if status != "KILLED":
                rec["pytest_output_tail"] = out.strip().splitlines()[-40:]
        except Exception as e:  # noqa: BLE001 - recorded loudly, never counted as killed
            rec.update(status="ERROR", reason=f"{type(e).__name__}: {e}")
        finally:
            restore(mut, copy_root)
        print(f"[mut] {mut.mid} {rec['status']}: {mut.title}")
        results.append(rec)

    killed = sum(1 for r in results if r["status"] == "KILLED")
    summary = {
        "suite": "critical-mutations (plan 20.5)",
        "date": dt.datetime.now(dt.UTC).isoformat(timespec="seconds"),
        "import_proof": import_proof,
        "total": len(results), "killed": killed,
        "survived": [r["id"] for r in results if r["status"] == "SURVIVED"],
        "errors": [r["id"] for r in results if r["status"] == "ERROR"],
        "all_killed": killed == len(MUTATIONS) and len(results) == len(MUTATIONS),
        "baselines": baseline,
        "mutations": results,
    }
    a.out.parent.mkdir(parents=True, exist_ok=True)
    # no private absolute paths in committed evidence (plan 26.3)
    text = json.dumps(summary, indent=2).replace(str(REPO), "${REPO}")
    a.out.write_text(text + "\n")
    print(f"[mut] killed {killed}/{len(results)}; survived={summary['survived']} "
          f"errors={summary['errors']}; wrote {a.out}")
    if not a.keep:
        shutil.rmtree(a.scratch, ignore_errors=True)
    return 0 if summary["all_killed"] else 1


if __name__ == "__main__":
    sys.exit(main())
