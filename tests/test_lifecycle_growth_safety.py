from __future__ import annotations

from copy import deepcopy
import unittest

from _local_package import load_local_package

load_local_package()
from omh.workflows.lifecycle_growth_contracts import (
    build_step_outcome,
    build_throttle_grouping,
    prepare_lifecycle_growth,
    readout_lifecycle_growth,
    route_lifecycle_workflow_mutation,
    validate_lifecycle_growth_artifact,
)
from test_lifecycle_growth_contracts import LifecycleGrowthContractTests


def _grouping(**overrides: str) -> dict[str, object]:
    values = {
        "key_kind": "dynamic_expression",
        "configured_key_ref": "throttle_key_tenant_expression",
        "scope": "tenant",
        "resolved_value_state": "present",
        "resolved_value_ref": "tenant_acme",
        "window_reset_consequence": "none",
    }
    values.update(overrides)
    return build_throttle_grouping(**values)


def _launch_artifacts() -> dict[str, dict[str, object]]:
    return LifecycleGrowthContractTests()._launch_artifacts()


def _readout(**overrides: object) -> dict[str, object]:
    return LifecycleGrowthContractTests()._readout(**overrides)


class LifecycleThrottleGroupingTests(unittest.TestCase):
    def test_given_two_resolved_tenant_values_when_grouped_then_identities_differ(self) -> None:
        acme = _grouping(resolved_value_ref="tenant_acme")
        globex = _grouping(resolved_value_ref="tenant_globex")

        self.assertNotEqual(acme["group_identity"], globex["group_identity"])
        self.assertEqual((acme["fallback_state"], globex["fallback_state"]), ("none", "none"))

    def test_given_empty_dynamic_value_when_grouped_then_default_window_not_a_payload_path(self) -> None:
        empty = _grouping(resolved_value_state="empty", resolved_value_ref="")
        path_shaped = _grouping(resolved_value_ref="payload.tenant_id")
        static = _grouping(key_kind="static_path", configured_key_ref="payload.tenant_id", scope="recipient", resolved_value_ref="tenant_acme")

        self.assertEqual(empty["fallback_state"], "default_window")
        self.assertEqual(empty["group_identity"], "default_window:throttle_key_tenant_expression")
        self.assertNotEqual(path_shaped["group_identity"], static["group_identity"])
        self.assertTrue(str(path_shaped["group_identity"]).startswith("dynamic_expression:"))

    def test_given_static_payload_path_when_grouped_then_legacy_identity_is_stable(self) -> None:
        defined = _grouping(key_kind="static_path", configured_key_ref="payload.tenant_id", scope="recipient", resolved_value_ref="tenant_acme")
        again = _grouping(key_kind="static_path", configured_key_ref="payload.tenant_id", scope="recipient", resolved_value_ref="tenant_acme")
        missing = _grouping(key_kind="static_path", configured_key_ref="payload.tenant_id", scope="recipient", resolved_value_state="missing", resolved_value_ref="")

        self.assertEqual(defined["group_identity"], "static_path:payload.tenant_id=tenant_acme")
        self.assertEqual(defined, again)
        self.assertEqual((missing["group_identity"], missing["fallback_state"]), ("ungrouped", "ungrouped"))

    def test_given_kind_specific_fallbacks_when_mixed_then_builder_refuses(self) -> None:
        with self.assertRaises(ValueError):
            _grouping(key_kind="static_path", resolved_value_state="empty", resolved_value_ref="")
        with self.assertRaises(ValueError):
            _grouping(resolved_value_state="missing", resolved_value_ref="")
        with self.assertRaises(ValueError):
            _grouping(resolved_value_state="empty", resolved_value_ref="tenant_acme")

    def test_given_forged_group_identity_when_safety_validated_then_error_not_exception(self) -> None:
        safety = deepcopy(_launch_artifacts()["safety"])
        safety["throttle_grouping"]["group_identity"] = "dynamic_expression:throttle_key_tenant_expression=tenant_globex"

        errors = validate_lifecycle_growth_artifact(safety)

        self.assertIn("throttle_grouping.group_identity must match derived identity", errors)


class LifecycleStepOutcomeTests(unittest.TestCase):
    def test_given_matched_and_skipped_steps_when_recorded_then_reason_and_status_are_distinct(self) -> None:
        matched, skipped = _readout()["step_outcomes"]

        self.assertEqual((matched["outcome"], matched["status"]), ("matched", "step_proceeded"))
        self.assertEqual((skipped["outcome"], skipped["status"]), ("skipped", "step_skipped"))
        self.assertNotEqual(matched["reason_code"], skipped["reason_code"])
        self.assertEqual({matched["evaluated_values_state"], skipped["evaluated_values_state"]}, {"redacted"})

    def test_given_secret_or_whole_context_reason_when_recorded_then_value_is_masked(self) -> None:
        secret = build_step_outcome(step_ref="step_one", outcome="skipped", reason_code="api_key=sk-live-1234567890")
        context = build_step_outcome(step_ref="step_one", outcome="skipped", reason_code='{"subscriber": {"email": "a@b.c"}, "tenant": "acme"}')

        for record in (secret, context):
            self.assertNotIn("sk-live", record["reason_code"])
            self.assertNotIn("subscriber", record["reason_code"])
            self.assertTrue(str(record["reason_code"]).startswith("ref-"))
            self.assertEqual(set(record), {"step_ref", "outcome", "status", "reason_code", "evaluated_values_state"})

    def test_given_oversized_or_malformed_trace_when_built_or_validated_then_bounded(self) -> None:
        too_many = [{"step_ref": f"step_{index}", "outcome": "matched", "reason_code": "reason_condition_true"} for index in range(9)]
        with self.assertRaises(ValueError):
            _readout(step_outcomes=too_many)
        with self.assertRaises(ValueError):
            _readout(step_outcomes=[{"step_ref": "step_one", "outcome": "matched", "reason_code": "r", "evaluated_value": "42"}])

        forged = deepcopy(_readout())
        forged["step_outcomes"][1]["status"] = "step_proceeded"
        self.assertIn("step_outcomes[1].status must match outcome", validate_lifecycle_growth_artifact(forged))

    def test_given_trace_write_failure_when_read_out_then_delivery_and_disposition_are_unchanged(self) -> None:
        recorded = readout_lifecycle_growth(_readout(step_trace_state="recorded"))
        failed = readout_lifecycle_growth(_readout(step_trace_state="write_failed", step_outcomes=()))

        self.assertEqual(failed["disposition"], recorded["disposition"])
        self.assertEqual(failed["delivery_count"], recorded["delivery_count"])
        self.assertEqual(failed["artifact_errors"], [])

    def test_given_recorded_trace_without_provider_delivery_when_read_out_then_trace_is_not_delivery_evidence(self) -> None:
        result = readout_lifecycle_growth(_readout(delivered_count=0, displayed_count=0, acted_count=0, outcome_count=0, provider_evidence_refs=(), actual_exposure_evidence_refs=()))

        self.assertEqual(result["delivery_count"], 0)
        self.assertEqual(result["disposition"], "insufficient_data")


class LifecycleWorkflowMutationTests(unittest.TestCase):
    def _safety(self, **overrides: str) -> dict[str, object]:
        safety = _launch_artifacts()["safety"]
        safety.update(overrides)
        return safety

    def test_given_production_content_when_routed_then_view_only_and_edit_goes_to_draft(self) -> None:
        route = route_lifecycle_workflow_mutation(self._safety(workflow_content_state="production_read_only"))

        self.assertEqual(route["verdict"], "HOLD")
        self.assertEqual(route["content_view_state"], "view_only")
        self.assertEqual(route["mutation_target"], "development_draft")
        self.assertIn("production workflow content is view-only; route the edit to a development draft", route["reason_codes"])

    def test_given_draft_without_promotion_decision_when_routed_then_hold_until_explicit_promotion(self) -> None:
        pending = route_lifecycle_workflow_mutation(self._safety(promotion_decision_state="pending"))
        approved = route_lifecycle_workflow_mutation(self._safety(promotion_decision_state="approved"))

        self.assertEqual(pending["verdict"], "HOLD")
        self.assertIn("promotion decision is not approved", pending["reason_codes"])
        self.assertEqual(approved["verdict"], "READY")
        self.assertEqual(approved["promotion_result_state"], "not_observed")

    def test_given_production_or_unpromoted_content_when_prepared_then_launch_holds(self) -> None:
        for name, overrides in (
            ("production", {"workflow_content_state": "production_read_only"}),
            ("pending", {"promotion_decision_state": "pending"}),
        ):
            with self.subTest(name=name):
                artifacts = _launch_artifacts()
                artifacts["safety"].update(overrides)
                self.assertEqual(prepare_lifecycle_growth(artifacts)["verdict"], "HOLD")


if __name__ == "__main__":
    unittest.main()
