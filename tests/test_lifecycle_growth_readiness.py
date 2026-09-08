from __future__ import annotations

from copy import deepcopy
import unittest

from _local_package import load_local_package

load_local_package()
from omh.workflows.lifecycle_growth_contracts import (
    build_growth_experiment_plan,
    build_growth_measurement_readout,
    evaluate_lifecycle_growth,
    readout_lifecycle_growth,
    validate_lifecycle_growth_artifact,
)


LIFECYCLE_ID = "lifecycle_activation_q3"


class LifecycleGrowthReadinessTests(unittest.TestCase):
    def _experiment(self) -> dict[str, object]:
        return build_growth_experiment_plan(
            lifecycle_growth_id=LIFECYCLE_ID,
            treatment_ref="treatment_guided_setup",
            control_ref="control_existing_setup",
            assignment_unit="account",
            exposure_unit="account",
            sticky_assignment_policy="sticky",
            assignment_event_ref="event_assignment_v1",
            actual_exposure_event_ref="event_displayed_v1",
            primary_metric_ref="metric_activation_rate",
            guardrail_metric_refs=("metric_opt_out_rate",),
            holdout_state="preserved",
            holdout_rationale_ref="rationale_holdout",
            minimum_runtime_days=14,
            data_health_state="healthy",
            rollback_conditions=("rollback_guardrail",),
            pause_condition_refs=("pause_sample_ratio",),
            approval_state="approved",
        )

    def _readout(self, **overrides: object) -> dict[str, object]:
        values: dict[str, object] = {
            "lifecycle_growth_id": LIFECYCLE_ID,
            "eligible_count": 10,
            "attempted_count": 8,
            "delivered_count": 7,
            "displayed_count": 6,
            "acted_count": 2,
            "outcome_count": 1,
            "runtime_days_observed": 14,
            "denominator_state": "known",
            "data_freshness_state": "fresh",
            "instrumentation_state": "healthy",
            "sample_ratio_state": "match",
            "cross_exposure_state": "absent",
            "overlap_state": "absent",
            "primary_metric_state": "improved",
            "guardrail_state": "passed",
            "causal_claim_status": "established",
            "rollback_state": "not_triggered",
            "provider_evidence_refs": ("evidence_provider_delivery",),
            "actual_exposure_evidence_refs": ("evidence_provider_display",),
            "data_evidence_refs": ("evidence_metric",),
            "runtime_evidence_refs": ("evidence_runtime",),
            "causal_evidence_refs": ("evidence_holdout",),
            "step_outcomes": (
                {"step_ref": "step_welcome_in_app", "outcome": "matched", "reason_code": "reason_condition_true"},
                {"step_ref": "step_reminder_email", "outcome": "skipped", "reason_code": "reason_condition_false"},
            ),
            "step_trace_state": "recorded",
        }
        values.update(overrides)
        return build_growth_measurement_readout(**values)

    def test_given_all_zero_funnel_with_positive_caller_states_when_read_out_then_insufficient_data(self) -> None:
        readout = self._readout(
            eligible_count=0,
            attempted_count=0,
            delivered_count=0,
            displayed_count=0,
            acted_count=0,
            outcome_count=0,
            runtime_days_observed=0,
            provider_evidence_refs=(),
            actual_exposure_evidence_refs=(),
            runtime_evidence_refs=(),
        )

        result = readout_lifecycle_growth(readout)

        self.assertEqual(result["disposition"], "insufficient_data")
        self.assertEqual(result["interpretation_state"], "HOLD")

    def test_given_less_than_minimum_observed_runtime_when_evaluated_then_no_ship(self) -> None:
        result = evaluate_lifecycle_growth(self._experiment(), self._readout(runtime_days_observed=13))

        self.assertEqual(result["disposition"], "insufficient_data")
        self.assertIn("minimum runtime has not elapsed", result["artifact_errors"])

    def test_given_stale_or_broken_observed_run_when_read_out_then_interpretation_holds(self) -> None:
        for name, kwargs, expected in (
            ("stale", {"data_freshness_state": "stale"}, "insufficient_data"),
            ("ratio", {"sample_ratio_state": "mismatch"}, "review"),
            ("cross", {"cross_exposure_state": "detected"}, "review"),
            ("overlap", {"overlap_state": "detected"}, "review"),
            ("instrumentation", {"instrumentation_state": "broken"}, "review"),
            ("rollback", {"rollback_state": "triggered"}, "rollback"),
        ):
            with self.subTest(name=name):
                result = readout_lifecycle_growth(self._readout(**kwargs))
                self.assertEqual(result["disposition"], expected)
                self.assertEqual(result["interpretation_state"], "HOLD")

    def test_given_displayed_exposure_when_evaluated_then_delivery_and_exposure_remain_separate(self) -> None:
        result = evaluate_lifecycle_growth(self._experiment(), self._readout(delivered_count=7, displayed_count=6))

        self.assertEqual(result["delivery_count"], 7)
        self.assertEqual(result["actual_exposure_count"], 6)

    def test_given_mutated_count_or_missing_evidence_when_validated_then_errors_are_reported(self) -> None:
        invalid_count = deepcopy(self._readout())
        invalid_count["displayed_count"] = "invalid"
        missing_exposure_evidence = deepcopy(self._readout())
        missing_exposure_evidence["actual_exposure_evidence_refs"] = []

        self.assertTrue(any("displayed_count" in error for error in validate_lifecycle_growth_artifact(invalid_count)))
        self.assertTrue(any("actual_exposure_evidence_refs" in error for error in validate_lifecycle_growth_artifact(missing_exposure_evidence)))


if __name__ == "__main__":
    unittest.main()
