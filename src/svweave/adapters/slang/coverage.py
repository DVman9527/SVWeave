"""Versioned, bounded reference extraction coverage (HDL-R01).

This is a capability boundary, not a scan for affected constructs. In particular, a zero
observation count cannot promote a feature to COMPLETE. Kept in the extractor package so
changes participate in its source identity and cannot reuse an old build cache.
"""

from __future__ import annotations

from svweave.domain.envelope import ReferenceCoverage, ReferenceObservations


def reference_coverage(observations: ReferenceObservations | None = None) -> ReferenceCoverage:
    blockers = ["known_reference_omissions", "unsearched_source_scopes"]
    if observations is None:
        blockers.append("coverage_metadata_unavailable")
    else:
        for field, code in (
            ("unsupported_nodes_project", "unsupported_project_nodes"),
            ("unsupported_nodes_external", "unsupported_external_nodes"),
            ("dropped_references_project", "dropped_project_references"),
            ("dropped_references_external", "dropped_external_references"),
            ("unresolved_references", "unresolved_references"),
            ("dynamic_references", "dynamic_targets_not_resolved"),
        ):
            if getattr(observations, field):
                blockers.append(code)
        if observations.scope_gaps:
            blockers.append("observed_scope_gaps")
        if observations.inactive_regions:
            blockers.append("observed_inactive_regions")
    return ReferenceCoverage(
        status="PARTIAL" if observations is not None else "UNKNOWN",
        features={
            "elaborated_source_occurrences": "PARTIAL",
            "packed_dimension_expressions": "UNSUPPORTED",
            "unpacked_dimension_expressions": "UNSUPPORTED",
            "subroutine_return_type_names": "UNSUPPORTED",
            "class_scope_prefix_names": "UNSUPPORTED",
            "aggregate_member_type_names": "PARTIAL",
        },
        scopes={
            "elaborated_bodies": "PARTIAL",
            "inactive_branches": "NOT_SEARCHED",
            "uninstantiated_generic_bodies": "NOT_SEARCHED",
            "unelaborated_definitions": "NOT_SEARCHED",
            "unloaded_library_members": "NOT_SEARCHED",
            "call_only_specialization_bodies": "NOT_SEARCHED",
        },
        observations=observations,
        blockers=blockers,
    )
