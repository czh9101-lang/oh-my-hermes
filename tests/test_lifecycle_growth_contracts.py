from __future__ import annotations

from copy import deepcopy
import unittest

from _local_package import load_local_package

load_local_package()
from omh.workflows.lifecycle_growth_contracts import (
    build_audience_trigger_policy,
    build_growth_experiment_plan,
    build_growth_handoff_disposition,
    build_growth_measurement_readout,
    build_lifecycle_growth_brief,
    build_lifecycle_safety_policy,
    evaluate_lifecycle_growth_entry,
    prepare_lifecycle_growth,
    validate_lifecycle_growth_artifact,
)


LIFECYCLE_ID = "lifecycle_activation_q3"


class LifecycleGrowthContractTests(unittest.TestCase):
    def _launch_artifacts(self) -> dict[str, dict[str, object]]:
        return {
            "brief": build_lifecycle_growth_brief(
                lifecycle_growth_id=LIFECYCLE_ID,
                lifecycle_stage="activation",
                target_behavior_ref="behavior_activation_completed",
                target_segment_ref="segment_new_accounts",
                baseline_ref="baseline_activation_weekly",
                available_surface_refs=("surface_in_app",),
                experiment_budget_ref="budget_activation_q3",
                observed_evidence_refs=("evidence_activation_weekly",),
                hypotheses=("hypothesis_guided_setup",),
                non_goals=("non_goal_external_send",),
                decision_owner="owner_growth",
            ),
            "audience": build_audience_trigger_policy(
                lifecycle_growth_id=LIFECYCLE_ID,
                audience_identity_state="known",
                audience_identity_ref="identity_account_stable",
                canonical_event_state="known",
                canonical_event_schema_ref="event_activation_completed_v1",
                entry_condition_ref="entry_new_account",
                exit_condition_ref="exit_activation_completed",
                exclusion_refs=("exclude_opted_out",),
                idempotency_key_ref="idempotency_account_journey_v1",
                reentry_policy="after_exit_only",
                collision_policy="hold_on_overlap",
            ),
            "safety": build_lifecycle_safety_policy(
                lifecycle_growth_id=LIFECYCLE_ID,
                consent_state="eligible",
                suppression_state="eligible",
                frequency_state="eligible",
                suppression_precedence="suppression_overrides_all",
                preference_policy_ref="policy_preferences_v1",
                global_frequency_budget_ref="budget_global_weekly",
                campaign_frequency_budget_ref="budget_activation_weekly",
                channel_eligibility_state="eligible",
                quiet_hours_state="eligible",
                locale_state="eligible",
                legal_tenant_state="eligible",
                throttle_grouping={
                    "key_kind": "dynamic_expression",
                    "configured_key_ref": "throttle_key_tenant_expression",
                    "scope": "tenant",
                    "resolved_value_state": "present",
                    "resolved_value_ref": "tenant_acme",
                    "window_reset_consequence": "none",
                },
                workflow_content_state="development_draft",
                mutation_route="development_draft_then_promotion",
                promotion_decision_state="approved",
                promotion_result_state="not_observed",
            ),
            "experiment": build_growth_experiment_plan(
                lifecycle_growth_id=LIFECYCLE_ID,
                treatment_ref="treatment_guided_setup",
                control_ref="control_existing_setup",
                assignment_unit="account",
                exposure_unit="account",
                sticky_assignment_policy="sticky",
                assignment_event_ref="event_assignment_v1",
                actual_exposure_event_ref="event_treatment_displayed_v1",
                primary_metric_ref="metric_activation_rate",
                guardrail_metric_refs=("metric_opt_out_rate",),
                holdout_state="preserved",
                holdout_rationale_ref="rationale_holdout_measurement",
                minimum_runtime_days=14,
                data_health_state="healthy",
                rollback_conditions=("rollback_guardrail_breach",),
                pause_condition_refs=("pause_sample_ratio_mismatch",),
                approval_state="approved",
            ),
            "handoff": build_growth_handoff_disposition(
                lifecycle_growth_id=LIFECYCLE_ID,
                proposed_action_refs=("action_connector_prepare",),
                proposed_action_kinds=("connector",),
                action_owner="owner_lifecycle_ops",
                approver="owner_growth",
                connector_evidence_state="observed_available",
                connector_evidence_refs=("evidence_connector_observed",),
                timing_state="not_scheduled",
                stop_condition_refs=("stop_consent_changed",),
                analysis_cancellation={
                    "request_state": "not_requested",
                    "run_ref": "",
                    "cancel_scope": "",
                    "acknowledgement_state": "not_observed",
                    "result_state": "not_observed",
                    "evidence_refs": (),
                },
            ),
        }

    def _readout(self, **overrides: object) -> dict[str, object]:
        values: dict[str, object] = {
            "lifecycle_growth_id": LIFECYCLE_ID,
            "eligible_count": 120,
            "attempted_count": 100,
            "delivered_count": 95,
            "displayed_count": 80,
            "acted_count": 30,
            "outcome_count": 24,
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
            "data_evidence_refs": ("evidence_metric_readout",),
            "runtime_evidence_refs": ("evidence_runtime_days",),
            "causal_evidence_refs": ("evidence_holdout_analysis",),
            "step_outcomes": (
                {"step_ref": "step_welcome_in_app", "outcome": "matched", "reason_code": "reason_condition_true"},
                {"step_ref": "step_reminder_email", "outcome": "skipped", "reason_code": "reason_condition_false"},
            ),
            "step_trace_state": "recorded",
            "analysis_status": {
                "run_state": "completed",
                "run_ref": "analysis_run_activation_q3_01",
                "observed_at": "2026-09-08T09:00:00Z",
                "elapsed_minutes": 42,
                "service_expectation_minutes": 120,
                "evidence_refs": ("evidence_analysis_run_completed",),
            },
        }
        values.update(overrides)
        return build_growth_measurement_readout(**values)

    def test_given_approved_first_experiment_when_prepared_then_launch_is_ready_without_readout(self) -> None:
        artifacts = self._launch_artifacts()

        readiness = prepare_lifecycle_growth(artifacts)

        self.assertEqual(readiness["verdict"], "READY")
        self.assertTrue(readiness["launch_ready"])
        self.assertTrue(all(validate_lifecycle_growth_artifact(record) == [] for record in artifacts.values()))

    def test_given_unknown_eligibility_or_owner_when_prepared_then_hold(self) -> None:
        for name, mutate in (
            ("consent", lambda records: records["safety"].__setitem__("consent_state", "unknown")),
            ("suppression", lambda records: records["safety"].__setitem__("suppression_state", "unknown")),
            ("frequency", lambda records: records["safety"].__setitem__("frequency_state", "unknown")),
            ("identity", lambda records: records["audience"].__setitem__("audience_identity_state", "unknown")),
            ("event", lambda records: records["audience"].__setitem__("canonical_event_state", "unknown")),
            ("owner", lambda records: records["brief"].__setitem__("decision_owner", "")),
            ("connector", lambda records: records["handoff"].__setitem__("connector_evidence_state", "unavailable")),
            ("asserted_connector", lambda records: records["handoff"].__setitem__("connector_evidence_state", "caller_asserted_available")),
        ):
            with self.subTest(name=name):
                artifacts = self._launch_artifacts()
                mutate(artifacts)
                self.assertEqual(prepare_lifecycle_growth(artifacts)["verdict"], "HOLD")

    def test_given_unknown_denominator_or_unhealthy_observed_readout_when_prepared_then_existing_run_holds(self) -> None:
        for name, kwargs in (
            ("denominator", {"denominator_state": "unknown"}),
            ("stale", {"data_freshness_state": "stale"}),
        ):
            with self.subTest(name=name):
                artifacts = self._launch_artifacts()
                artifacts["readout"] = self._readout(**kwargs)
                self.assertEqual(prepare_lifecycle_growth(artifacts)["verdict"], "HOLD")

    def test_given_complete_artifacts_when_inspected_then_required_policy_contracts_are_present(self) -> None:
        artifacts = self._launch_artifacts()
        experiment = artifacts["experiment"]
        safety = artifacts["safety"]

        self.assertEqual(experiment["assignment_unit"], experiment["exposure_unit"])
        self.assertEqual(experiment["sticky_assignment_policy"], "sticky")
        self.assertIn("actual_exposure_event_ref", experiment)
        self.assertIn("treatment_ref", experiment)
        self.assertIn("control_ref", experiment)
        self.assertEqual(safety["suppression_precedence"], "suppression_overrides_all")
        self.assertIn("global_frequency_budget_ref", safety)
        self.assertIn("campaign_frequency_budget_ref", safety)

    def test_given_wrong_slots_or_mismatched_lifecycle_ids_when_prepared_then_hold(self) -> None:
        wrong_slot = self._launch_artifacts()
        wrong_slot["brief"] = wrong_slot["audience"]
        mismatch = self._launch_artifacts()
        mismatch["handoff"]["lifecycle_growth_id"] = "lifecycle_other"

        self.assertEqual(prepare_lifecycle_growth(wrong_slot)["verdict"], "HOLD")
        self.assertEqual(prepare_lifecycle_growth(mismatch)["verdict"], "HOLD")

    def test_given_duplicate_reentry_or_overlap_when_entry_is_decided_then_hold(self) -> None:
        audience = self._launch_artifacts()["audience"]
        duplicate = evaluate_lifecycle_growth_entry(audience, event_id_ref="event_42", prior_event_id_refs=("event_42",), prior_exit_observed=True, active_intervention_refs=())
        reentry = evaluate_lifecycle_growth_entry(audience, event_id_ref="event_43", prior_event_id_refs=("event_42",), prior_exit_observed=False, active_intervention_refs=())
        overlap = evaluate_lifecycle_growth_entry(audience, event_id_ref="event_44", prior_event_id_refs=(), prior_exit_observed=True, active_intervention_refs=("intervention_existing",))

        self.assertEqual(duplicate["verdict"], "HOLD")
        self.assertEqual(reentry["verdict"], "HOLD")
        self.assertEqual(overlap["verdict"], "HOLD")

    def test_given_malformed_artifact_when_validated_then_errors_not_exceptions_are_returned(self) -> None:
        malformed = deepcopy(self._readout())
        malformed["displayed_count"] = "invalid"

        errors = validate_lifecycle_growth_artifact(malformed)

        self.assertTrue(any("displayed_count" in error for error in errors))


if __name__ == "__main__":
    unittest.main()
