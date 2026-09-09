"""Tracked capacity boundary; parent integration owns real C1-C7 scenarios."""
from __future__ import annotations

from . import CaseResult, unavailable_case


def run_case(case_id: str) -> CaseResult:
    """No public dispatch/native admission was exercised by this foundation."""
    result = unavailable_case(case_id, 'capacity_integration_pending')
    result['observations'] = {'integrated_dispatch_executed': False,
                              'native_positive_admission_executed': False}
    return result
