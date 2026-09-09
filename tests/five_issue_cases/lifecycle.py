"""Tracked lifecycle boundary; integrated CLI scenarios belong to phase C.

No provider/native surface runs here, and no foundation helper pass is promoted
to a criterion PASS. This producer acquires no processes, files, or profiles.
"""
from __future__ import annotations

from . import CaseResult, unavailable_case


def run_case(case_id: str) -> CaseResult:
    """Explicitly report the outstanding public-wiring/source-review dependency."""
    reason = "lifecycle_public_wiring_pending" if case_id != "L7" else "lifecycle_registry_and_source_review_pending"
    return unavailable_case(case_id, reason)
