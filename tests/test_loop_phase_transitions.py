from __future__ import annotations

import copy
import unittest

from _local_package import load_local_package

load_local_package()
from omh.workflows.loop_phase_transitions import (
    LOOP_GOAL_DRIVER_OBSERVATION_SCHEMA,
    LOOP_PHASES,
    LOOP_PHASE_TRANSITION_SCHEMA,
    native_goal_status,
    phase_target,
    transition_loop_phase,
    validate_loop_goal_driver_observation,
    validate_loop_phase_transition,
)


OBSERVED_AT = "2026-08-24T12:00:00Z"
DIGEST = "a" * 64


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


def _observation() -> dict[str, object]:
    session_ref = "hermes-session-release"
    return {
        "schema_version": LOOP_GOAL_DRIVER_OBSERVATION_SCHEMA,
        "observation_id": "native-goal-release-turns-1-2",
        "loop_id": "loop-release",
        "session_ref": session_ref,
        "goal_command_sha256": DIGEST,
        "observation_source": "hermes_host",
        "observed_at": OBSERVED_AT,
        "activation": {
            "status": "observed",
            "evidence_refs": ["hermes:session-release:goal-accepted"],
        },
        "turns": [
            {
                "turn_index": 1,
                "session_ref": session_ref,
                "from_phase": "interview",
                "to_phase": "plan",
                "phase_gate": "goal_contract_observed",
                "turn_ended_evidence_refs": ["hermes:session-release:turn-1-ended"],
                "phase_gate_evidence_refs": ["wrapper:goal-contract:observed"],
            },
            {
                "turn_index": 2,
                "session_ref": session_ref,
                "from_phase": "plan",
                "to_phase": "research",
                "phase_gate": "plan_observed",
                "turn_ended_evidence_refs": ["hermes:session-release:turn-2-ended"],
                "phase_gate_evidence_refs": ["artifact:bounded-plan:sha256:abc123"],
            },
        ],
        "summary": "Hermes accepted the goal and continued two turns.",
        "privacy": "metadata_only",
    }


class PhaseGraphTests(unittest.TestCase):
    def test_phase_target_returns_action_target_and_gate_when_phase_is_native(self) -> None:
        result = phase_target("research")

        self.assertEqual(result, ("executor_handoff", "handoff", "research_evidence_observed"))
        self.assertTrue({"waiting", "blocked", "complete"}.issubset(LOOP_PHASES))

    def test_transition_is_appended_when_edge_and_gate_are_legal(self) -> None:
        cycle: dict[str, object] = {"loop_id": "loop-release", "phase": "interview"}

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
        cycle: dict[str, object] = {"loop_id": "loop-release", "phase": "interview"}

        with self.assertRaisesRegex(ValueError, "illegal loop phase transition"):
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

        self.assertEqual(cycle, {"loop_id": "loop-release", "phase": "interview"})

    def test_transition_is_rejected_when_gate_is_wrong(self) -> None:
        errors = validate_loop_phase_transition(_transition(phase_gate="plan_observed"))

        self.assertIn("phase_gate must be goal_contract_observed for interview -> plan", errors)

    def test_transition_is_rejected_when_observed_progress_has_no_refs(self) -> None:
        errors = validate_loop_phase_transition(_transition(evidence_refs=[]))

        self.assertIn("evidence_refs must contain observed-progress evidence", errors)


class GoalDriverObservationTests(unittest.TestCase):
    def test_observation_is_valid_when_activation_and_two_turns_are_observed(self) -> None:
        errors = validate_loop_goal_driver_observation(
            _observation(),
            expected_loop_id="loop-release",
            expected_goal_command_sha256=DIGEST,
        )

        self.assertEqual(errors, [])

    def test_observation_rejects_digest_session_turn_and_minimum_failures(self) -> None:
        cases: tuple[tuple[str, object, str], ...] = (
            ("goal_command_sha256", "A" * 64, "lowercase 64-hex"),
            ("session_ref", "../session", "session_ref must be a storage-safe non-empty id"),
            ("turns", [_observation()["turns"][0]], "at least two observed turns"),
        )
        for field, invalid, expected in cases:
            with self.subTest(field=field):
                value = _observation()
                value[field] = invalid

                self.assertTrue(any(expected in error for error in validate_loop_goal_driver_observation(value)))

    def test_observation_rejects_cross_session_and_noncontiguous_turns(self) -> None:
        cases = (("session_ref", "other-session"), ("turn_index", 3))
        for field, invalid in cases:
            with self.subTest(field=field):
                value = _observation()
                turns = value["turns"]
                assert isinstance(turns, list)
                turns[1][field] = invalid

                errors = validate_loop_goal_driver_observation(value)

                self.assertTrue(any(field in error for error in errors))

    def test_observation_rejects_non_metadata_fields(self) -> None:
        value = _observation()
        value["artifact_quota"] = 4
        turns = value["turns"]
        assert isinstance(turns, list)
        turns[0]["prompt"] = "raw content"

        errors = validate_loop_goal_driver_observation(value)

        self.assertTrue(any("forbidden fields" in error for error in errors))

    def test_observation_rejects_unbounded_or_structured_metadata(self) -> None:
        cases: tuple[tuple[str, object, str], ...] = (
            ("structured summary", {"transcript": "raw private content"}, "summary must be"),
            ("oversized summary", "x" * 321, "summary must be"),
            (
                "oversized evidence ref",
                ["artifact:" + ("x" * 321)],
                "activation.evidence_refs",
            ),
            (
                "duplicate evidence refs",
                ["artifact:one", "artifact:one"],
                "activation.evidence_refs",
            ),
        )
        for label, invalid, expected in cases:
            with self.subTest(label=label):
                value = _observation()
                if label.endswith("summary"):
                    value["summary"] = invalid
                else:
                    activation = value["activation"]
                    assert isinstance(activation, dict)
                    activation["evidence_refs"] = invalid

                errors = validate_loop_goal_driver_observation(value)

                self.assertTrue(any(expected in error for error in errors), errors)

    def test_transition_rejects_unclosed_native_goal_projection(self) -> None:
        valid_native_goal = {
            "observation_id": "native-goal-release-turns-1-2",
            "session_ref": "hermes-session-release",
            "goal_command_sha256": DIGEST,
            "turn_index": 1,
        }
        cases = (
            {**valid_native_goal, "prompt": "raw private prompt"},
            {key: value for key, value in valid_native_goal.items() if key != "turn_index"},
            {**valid_native_goal, "turn_index": True},
        )
        for native_goal in cases:
            with self.subTest(native_goal=native_goal):
                errors = validate_loop_phase_transition(
                    _transition(native_goal=native_goal)
                )

                self.assertTrue(
                    any("native_goal" in error for error in errors),
                    errors,
                )

    def test_native_goal_status_derives_latest_accepted_observation(self) -> None:
        observation = _observation()
        cycle: dict[str, object] = {"goal_driver_observations": [copy.deepcopy(observation)]}

        result = native_goal_status(cycle)

        self.assertEqual(
            result,
            {
                "activation_status": "observed",
                "continuation_status": "observed",
                "session_ref": "hermes-session-release",
                "last_turn_index": 2,
            },
        )

    def test_native_goal_status_ignores_malformed_stored_observations(self) -> None:
        invalid = _observation()
        invalid["privacy"] = "raw"

        result = native_goal_status({"goal_driver_observations": [invalid]})

        self.assertEqual(
            result,
            {
                "activation_status": "not_observed",
                "continuation_status": "not_observed",
                "session_ref": "",
                "last_turn_index": 0,
            },
        )


if __name__ == "__main__":
    unittest.main()
