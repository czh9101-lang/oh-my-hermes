from __future__ import annotations

import unittest

from _local_package import load_local_package

load_local_package()
from omh.workflows.loop_phase_transitions import (
    LOOP_PHASES,
    LOOP_PHASE_TRANSITION_SCHEMA,
    phase_target,
    transition_loop_phase,
    validate_loop_phase_history,
    validate_loop_phase_transition,
)


OBSERVED_AT = "2026-08-24T12:00:00Z"


def _transition(**overrides: object) -> dict[str, object]:
    value: dict[str, object] = {
        "schema_version": LOOP_PHASE_TRANSITION_SCHEMA,
        "transition_id": "phase-transition-1",
        "sequence": 1,
        "loop_id": "loop-release",
        "from_phase": "interview",
        "to_phase": "plan",
        "from_phase_generation": 0,
        "to_phase_generation": 1,
        "phase_gate": "goal_contract_observed",
        "transition_kind": "queue_observation",
        "cause": "observed_progress",
        "source_ref": "wrapper:queue-1",
        "evidence_refs": ["wrapper:goal-contract:observed"],
        "observed_at": OBSERVED_AT,
    }
    value.update(overrides)
    return value


class PhaseGraphTests(unittest.TestCase):
    def test_phase_target_returns_action_target_and_gate_when_phase_is_native(
        self,
    ) -> None:
        result = phase_target("research")

        self.assertEqual(
            result,
            (
                "executor_handoff",
                "handoff",
                "research_evidence_observed",
            ),
        )
        self.assertTrue(
            {"waiting", "blocked", "complete"}.issubset(LOOP_PHASES)
        )

    def test_transition_is_appended_when_edge_and_gate_are_legal(self) -> None:
        cycle: dict[str, object] = {
            "loop_id": "loop-release",
            "phase": "interview",
        }

        result = transition_loop_phase(
            cycle,
            to_phase="plan",
            phase_gate="goal_contract_observed",
            transition_kind="queue_observation",
            cause="observed_progress",
            source_ref="wrapper:queue-1",
            evidence_refs=["wrapper:goal-contract:observed"],
            observed_at=OBSERVED_AT,
        )

        self.assertIs(result, cycle)
        self.assertEqual(result["phase"], "plan")
        self.assertEqual(result["phase_transitions"], [_transition()])

    def test_transition_is_rejected_when_edge_is_illegal(self) -> None:
        cycle: dict[str, object] = {
            "loop_id": "loop-release",
            "phase": "interview",
        }

        with self.assertRaisesRegex(
            ValueError, "illegal loop phase transition"
        ):
            transition_loop_phase(
                cycle,
                to_phase="research",
                phase_gate="goal_contract_observed",
                transition_kind="queue_observation",
                cause="observed_progress",
                source_ref="wrapper:queue-1",
                evidence_refs=["wrapper:evidence"],
                observed_at=OBSERVED_AT,
            )

        self.assertEqual(
            cycle, {"loop_id": "loop-release", "phase": "interview"}
        )

    def test_transition_is_rejected_when_gate_is_wrong(self) -> None:
        errors = validate_loop_phase_transition(
            _transition(phase_gate="plan_observed")
        )

        self.assertIn(
            "phase_gate must be goal_contract_observed for interview -> plan",
            errors,
        )

    def test_transition_is_rejected_when_observed_progress_has_no_refs(
        self,
    ) -> None:
        errors = validate_loop_phase_transition(
            _transition(evidence_refs=[])
        )

        self.assertIn(
            "evidence_refs must contain observed-progress evidence",
            errors,
        )

    def test_legacy_to_generation_aware_history_must_remain_connected(
        self,
    ) -> None:
        legacy = _transition()
        legacy.pop("from_phase_generation")
        legacy.pop("to_phase_generation")
        modern = _transition(
            transition_id="phase-transition-2",
            sequence=2,
            from_phase="research",
            to_phase="handoff",
            from_phase_generation=0,
            to_phase_generation=1,
            phase_gate="research_evidence_observed",
        )
        errors = validate_loop_phase_history(
            {
                "loop_id": "loop-release",
                "phase": "handoff",
                "phase_generation": 1,
                "phase_transitions": [legacy, modern],
                "goal_driver_observations": [],
            }
        )

        self.assertIn(
            "phase_transitions[1].phase transition chain is disconnected",
            errors,
        )

    def test_first_generation_aware_history_row_starts_at_zero(self) -> None:
        legacy = _transition()
        legacy.pop("from_phase_generation")
        legacy.pop("to_phase_generation")
        forged_generation = _transition(
            transition_id="phase-transition-2",
            sequence=2,
            from_phase="plan",
            to_phase="research",
            from_phase_generation=7,
            to_phase_generation=8,
            phase_gate="plan_observed",
        )
        generation_errors = validate_loop_phase_history(
            {
                "loop_id": "loop-release",
                "phase": "research",
                "phase_generation": 8,
                "phase_transitions": [legacy, forged_generation],
                "goal_driver_observations": [],
            }
        )

        self.assertIn(
            "phase_transitions[1].first generation-aware transition must start at generation 0",
            generation_errors,
        )


if __name__ == "__main__":
    unittest.main()
