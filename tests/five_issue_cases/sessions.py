"""Tracked session foundation is not dispatcher, CLI or native acceptance.

The parent replaces these pending scenarios during public-surface integration.
Foundation behavior is exercised by test_fanout_executor_sessions with direct
native-format events and explicitly fixture-attributed real child processes.
"""
from __future__ import annotations

from . import CaseResult, unavailable_case


def run_case(case_id: str) -> CaseResult:
    result = unavailable_case(case_id, 'sessions_integration_pending')
    result['observations'] = {'integrated_surface_exercised': False}
    return result
