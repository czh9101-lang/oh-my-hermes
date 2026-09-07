"""Executable contract tests for bounded decision prototypes."""

from __future__ import annotations

import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from _local_package import load_local_package

load_local_package()
from omh.runtime.decision_prototypes import persist_decision_prototype, read_decision_prototype  # noqa: E402
from omh.system.paths import OmhPaths  # noqa: E402
from omh.workflows.decision_prototypes import (  # noqa: E402
    DecisionPrototypeError,
    compact_decision_prototype_receipt,
    observe_decision_prototype,
    prepare_decision_prototype,
    validate_decision_prototype,
)


def _proposal(**overrides: object) -> dict[str, object]:
    return {
        "decision_id": "cache-backend",
        "context_decision_ref": "frontier-cache-choice",
        "question": "Does sqlite meet the declared local lookup latency target?",
        "alternatives": ["sqlite", "redis"],
        "hypothesis": "sqlite completes the synthetic lookup within the declared limit",
        "target": {"kind": "task", "ref": "synthetic-lookup"},
        "experiment_kind": "timing_probe",
        "budget": {"time_seconds": 300, "tool_count": 1, "file_count": 2, "command_count": 1},
        "executor": {"profile": "generic", "available": True, "capability_limits": ["no_network"]},
        "workspace": {
            "kind": "scratch_directory",
            "identity": "scratch-cache-probe",
            "write_boundary": "declared_workspace_only",
        },
        "measurement_method": "Measure synthetic lookup latency against the declared threshold.",
        "stop_conditions": ["hypothesis_supported", "timeout"],
        "commands": [{"command": "uv run python probe.py", "expected_observation": "latency-ms"}],
        **overrides,
    }


def _observation(**overrides: object) -> dict[str, object]:
    return {
        "adapter_contract": "decision_prototype_observation/v1",
        "adapter_id": "local-timing-adapter",
        "state": "observed",
        "measurements": [{"metric": "latency-ms", "value": "12", "evidence_ref": "measurement-1"}],
        "interpretation": "The measured synthetic lookup is within the declared limit.",
        "confidence": "medium",
        "unresolved_questions": ["Production load remains unmeasured."],
        "supported_option": "sqlite",
        "rejected_options": ["redis"],
        "decision": "discard",
        "cleanup_status": "observed",
        "prototype_code_ref": "scratch-cache-probe",
        "actual_workspace": {
            "identity": "scratch-cache-probe",
            "write_boundary": "declared_workspace_only",
        },
        "consumed_budget": {"time_seconds": 12, "tool_count": 1, "file_count": 1, "command_count": 1},
        **overrides,
    }


class DecisionPrototypeContractTests(unittest.TestCase):
    def test_prepare_returns_a_bounded_executor_neutral_handoff_when_executor_is_unavailable(self) -> None:
        # Given: one empirical context decision and an unavailable executor.
        proposal = _proposal(executor={"profile": "generic", "available": False, "capability_limits": ["no_browser"]})

        # When: Hermes prepares the prototype.
        prototype = prepare_decision_prototype(proposal)

        # Then: only a prepared handoff exists; no result is invented.
        self.assertEqual(prototype["schema_version"], "decision_prototype/v1")
        self.assertEqual(prototype["execution"]["status"], "prepared_not_observed")
        self.assertEqual(prototype["execution"]["evidence_class"], "prepared_not_observed")
        self.assertEqual(prototype["execution"]["handoff"]["commands"], proposal["commands"])
        self.assertEqual(prototype["observations"], [])

    def test_prepare_refuses_an_unbounded_or_multi_question_experiment(self) -> None:
        # Given: an experiment that asks two questions and exceeds a command budget.
        proposal = _proposal(question="Does sqlite meet latency? Does redis meet latency?", budget={"time_seconds": 300, "tool_count": 1, "file_count": 2, "command_count": 6})

        # When: Hermes validates the declaration.
        errors = validate_decision_prototype(proposal)

        # Then: it cannot become a prototype artifact.
        self.assertTrue(any("exactly one question" in error for error in errors), errors)
        self.assertTrue(any("command_count" in error for error in errors), errors)

    def test_prepare_refuses_writes_outside_the_declared_workspace(self) -> None:
        # Given: a scratch identity but an unrestricted write boundary.
        proposal = _proposal(workspace={"kind": "scratch_directory", "identity": "scratch-cache-probe", "write_boundary": "repository"})

        # When: Hermes validates the declaration.
        errors = validate_decision_prototype(proposal)

        # Then: production writes cannot be authorized by a prototype.
        self.assertTrue(any("write_boundary" in error for error in errors), errors)

    def test_observe_records_only_adapter_reported_timeout_and_inconclusive_states(self) -> None:
        # Given: one runnable bounded prototype.
        prepared = prepare_decision_prototype(_proposal())

        # When: its adapter observes a timeout and then an inconclusive run.
        timed_out = observe_decision_prototype(prepared, _observation(state="timeout", measurements=[], supported_option="", rejected_options=[], decision="keep", cleanup_status="not_observed"))
        inconclusive = observe_decision_prototype(prepared, _observation(state="inconclusive", measurements=[], supported_option="", rejected_options=[], decision="keep", cleanup_status="not_observed"))

        # Then: both states remain observed limits rather than a product verdict.
        self.assertEqual(timed_out["execution"]["status"], "timeout")
        self.assertEqual(inconclusive["execution"]["status"], "inconclusive")
        self.assertEqual(timed_out["observations"][0]["measurements"], [])

    def test_validation_returns_errors_for_malformed_nested_values_without_crashing(self) -> None:
        # Given: malformed proposal and artifact containers from an untrusted persistence boundary.
        prepared = prepare_decision_prototype(_proposal())
        malformed = (
            None,
            [],
            _proposal(alternatives=["sqlite", None, "redis"]),
            _proposal(budget=[]),
            _proposal(budget={"time_seconds": 300, "tool_count": 1, "file_count": 2, "command_count": "bad"}),
            _proposal(commands=[None]),
            {**prepared, "execution": []},
            {**prepared, "observations": [None]},
            {**prepared, "promotion": []},
            {**prepared, "observations": [_observation(rejected_options=[{}])]},
        )

        # When: each is passed to the public validator.
        errors = [validate_decision_prototype(value) for value in malformed]

        # Then: every malformed value is refused as data, never raised as an AttributeError.
        self.assertTrue(all(result for result in errors), errors)

    def test_observe_requires_measurement_evidence_and_matches_workspace_and_budget(self) -> None:
        # Given: one prepared timing probe and adapter reports that do not meet its contract.
        prepared = prepare_decision_prototype(_proposal())

        # When: an option has no measurement, a workspace differs, or consumption exceeds the declaration.
        reports = (
            _observation(measurements=[]),
            _observation(actual_workspace={"identity": "other-scratch", "write_boundary": "declared_workspace_only"}),
            _observation(consumed_budget={"time_seconds": 301, "tool_count": 1, "file_count": 1, "command_count": 1}),
        )

        # Then: none can become an accepted observation.
        for report in reports:
            with self.subTest(report=report):
                with self.assertRaises(DecisionPrototypeError):
                    observe_decision_prototype(prepared, report)

    def test_timeout_preserves_partial_measurements_without_selecting_an_option(self) -> None:
        # Given: a timing probe that times out after one measured sample.
        prepared = prepare_decision_prototype(_proposal())
        partial = _observation(
            state="timeout",
            supported_option="",
            rejected_options=[],
            decision="keep",
            cleanup_status="failed",
        )

        # When: its adapter reports the bounded timeout.
        observed = observe_decision_prototype(prepared, partial)

        # Then: the partial measurement remains evidence while no option conclusion is made.
        self.assertEqual(observed["observations"][0]["measurements"], partial["measurements"])
        self.assertEqual(observed["observations"][0]["supported_option"], "")
        self.assertEqual(observed["observations"][0]["cleanup_status"], "failed")

    def test_validate_rejects_handoff_parity_and_declared_command_budget_mismatches(self) -> None:
        # Given: a prepared handoff whose copied commands or declared budget was edited.
        prepared = prepare_decision_prototype(_proposal())
        bad_handoff = {**prepared, "execution": {**prepared["execution"], "handoff": {**prepared["execution"]["handoff"], "expected_observations": []}}}
        overdeclared = _proposal(budget={"time_seconds": 300, "tool_count": 1, "file_count": 2, "command_count": 1}, commands=[
            {"command": "uv run python one.py", "expected_observation": "one"},
            {"command": "uv run python two.py", "expected_observation": "two"},
        ])

        # When: the public validator reads both values.
        handoff_errors = validate_decision_prototype(bad_handoff)
        budget_errors = validate_decision_prototype(overdeclared)

        # Then: copied handoff and proposal budget contracts remain closed and equal.
        self.assertTrue(any("handoff" in error for error in handoff_errors), handoff_errors)
        self.assertTrue(any("command_count" in error for error in budget_errors), budget_errors)

    def test_validate_refuses_an_execution_state_without_an_adapter_observation(self) -> None:
        # Given: a prepared prototype with no adapter observation.
        prepared = prepare_decision_prototype(_proposal())
        forged = {**prepared, "execution": {**prepared["execution"], "status": "observed", "adapter_id": "local-timing-adapter"}}

        # When: its persisted contract is validated.
        errors = validate_decision_prototype(forged)

        # Then: execution cannot be inferred from a status field.
        self.assertTrue(any("execution must match" in error for error in errors), errors)

    def test_observe_requires_the_explicit_adapter_contract_and_observed_cleanup_before_discard(self) -> None:
        # Given: one runnable bounded prototype.
        prepared = prepare_decision_prototype(_proposal())

        # When: an uncontracted result or unobserved cleanup tries to discard it.
        with self.assertRaises(DecisionPrototypeError):
            observe_decision_prototype(prepared, _observation(adapter_contract="", cleanup_status="observed"))
        with self.assertRaises(DecisionPrototypeError):
            observe_decision_prototype(prepared, _observation(cleanup_status="failed"))

        # Then: neither result can claim a discarded prototype.
        self.assertEqual(prepared["observations"], [])

    def test_receipt_carries_a_context_decision_without_transcript_replay_and_blocks_promotion_without_plan(self) -> None:
        # Given: a measured prototype descended from one context decision.
        observed = observe_decision_prototype(prepare_decision_prototype(_proposal()), _observation())

        # When: planning consumes its compact receipt.
        receipt = compact_decision_prototype_receipt(observed)

        # Then: it sees the bounded decision only, and prototype code is not promotable without an accepted plan.
        self.assertEqual(receipt["context_decision_ref"], "frontier-cache-choice")
        self.assertEqual(receipt["supported_option"], "sqlite")
        self.assertNotIn("transcript", receipt)
        invalid = {
            **observed,
            "promotion": {
                "production_code_permitted": True,
                "accepted_plan_ref": "",
                "accepted_plan_status": "",
                "implementation_handoff_ref": "",
            },
        }
        self.assertTrue(any("accepted_plan_ref" in error for error in validate_decision_prototype(invalid)))

    def test_persist_round_trips_the_validated_public_artifact(self) -> None:
        # Given: a compact observed prototype.
        observed = observe_decision_prototype(prepare_decision_prototype(_proposal()), _observation())
        with TemporaryDirectory() as tmp:
            paths = OmhPaths(Path(tmp) / ".omh", Path(tmp) / ".hermes")

            # When: the runtime persists it through the public store adapter.
            persisted = persist_decision_prototype(paths, observed)
            restored = read_decision_prototype(paths, "cache-backend")

            # Then: the durable artifact remains identical and validated.
            self.assertEqual(persisted, observed)
            self.assertEqual(restored, observed)


if __name__ == "__main__":
    unittest.main()
