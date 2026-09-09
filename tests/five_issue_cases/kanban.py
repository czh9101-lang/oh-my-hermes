"""Tracked foundation-only Kanban setup; parent integration owns acceptance QA."""
from __future__ import annotations

from . import CaseResult, unavailable_case


def run_case(case_id: str) -> CaseResult:
    """Do not promote pure action/receipt tests into native/public-surface PASS."""
    if case_id not in {f"K{i}" for i in range(1, 9)}:
        return unavailable_case(case_id, "unsupported_case")
    result = unavailable_case(case_id, "kanban_public_native_integration_pending")
    result["inputs_metadata"] = {"criterion": case_id}
    result["observations"] = {"foundation_test_module": "test_agent_board_kanban",
                              "native_calls": 0, "integrated_acceptance": "pending"}
    result["provenance"]["native_required"] = case_id in {"K1", "K4", "K6", "K8"}
    return result
