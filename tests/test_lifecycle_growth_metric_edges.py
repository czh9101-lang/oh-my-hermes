"""Metric boundary limits, independent safety and composite decision eligibility."""
from copy import deepcopy
import unittest

from _lifecycle_configuration import at, operation, sequence
from _lifecycle_metrics import complete, composite_request, configured, member_set, request, result, rows


class MetricCoverageEdgesTests(unittest.TestCase):
    def test_T6_holds_when_primary_composite_member_contradicts_parent(self):
        # Given: improved configured primary, but one observed constituent unchanged.
        payload = configured()
        source = request(payload)
        parent = payload["experiment"]["primary_metric_ref"]
        member = member_set(parent, ["member_a", "member_b"])
        from omh.workflows.lifecycle_growth_configuration_values import artifact_digest
        payload["metric_plan_binding"] = {"schema_version": "lifecycle_growth_metric_plan_binding/v1",
            "experiment_digest": artifact_digest(payload["experiment"]),
            "configuration_digest": at(payload, "configuration_binding.seal.observation")["configuration_digest"],
            "composite_memberships": [member]}
        source.update(metric_plan_binding=payload["metric_plan_binding"], composites=[{"member_set": member,
            "results": [result("member_a", "primary", "improved"), result("member_b", "primary", "unchanged")]}])
        payload["metric_coverage"] = operation("metrics", source)
        # When: interpreting all resolved members.
        outcome = operation("evaluate", payload)
        # Then: the parent cannot hide a member that does not support promotion.
        self.assertEqual(outcome["disposition"], "review")
        self.assertIn("metric_decision_ineligible", sequence(outcome["evidence_reason_codes"]))

    def test_T4_ignores_harm_when_coverage_is_bound_to_another_readout(self):
        # Given: safe current readout with a stale harmful metric report.
        payload = complete()
        at(payload, "metric_coverage.results.1")["state"] = "failed"
        payload["metric_coverage"]["readout_digest"] = "sha256:" + "f" * 64
        # When: evaluating against actual current bytes.
        outcome = operation("evaluate", payload)
        # Then: unrelated harm cannot manufacture rollback.
        self.assertEqual(outcome["disposition"], "insufficient_data")
        self.assertIn("metric_configuration_mismatch", sequence(outcome["evidence_reason_codes"]))

    def test_T4_preserves_independent_aggregate_rollback_when_metrics_missing(self):
        # Given: validated aggregate rollback and no metric-level companion.
        payload = configured()
        payload["readout"].update(guardrail_state="failed", disposition="rollback")
        # When: evaluating partial evidence.
        outcome = operation("evaluate", payload)
        # Then: legacy incompleteness is not a veto on observed safety.
        self.assertEqual(outcome["disposition"], "rollback")
        self.assertIn("metric_coverage_legacy", sequence(outcome["evidence_reason_codes"]))

    def test_T1_rejects_when_top_level_shape_or_bounds_are_invalid(self):
        for change in ("missing", "extra", "digest", "result_bound", "ref_bound", "boolean", "null"):
            with self.subTest(change=change):
                # Given: malformed closed coverage, not a valid missing expected result.
                payload = complete()
                coverage = payload["metric_coverage"]
                if change == "missing": coverage.pop("readout_digest")
                if change == "extra": coverage["raw_query"] = "PRIVATE_SENTINEL"
                if change == "digest": coverage["configuration_digest"] = "sha256:bad"
                if change == "result_bound": coverage["results"] = rows(coverage["results"]) * 10
                if change == "ref_bound": at(coverage, "results.0")["evidence_refs"] = ["receipt"] * 9
                if change == "boolean": at(coverage, "results.0")["state"] = True
                if change == "null": coverage["results"] = None
                # When/Then: malformed input is rejected at the real operation seam.
                with self.assertRaises(ValueError): operation("evaluate", payload)

    def test_T7_rejects_when_error_state_and_category_contradict(self):
        for state, category, refs in (("errored", None, []), ("missing", "timeout", []),
                                      ("missing", None, ["receipt"]), ("passed", "timeout", ["receipt"]),
                                      ("errored", "PRIVATE_SENTINEL", [])):
            with self.subTest(state=state, category=category):
                # Given: inconsistent observed/error metadata.
                payload = complete()
                at(payload, "metric_coverage.results.1").update(state=state, error_category=category, evidence_refs=refs)
                # When/Then: safe parsing rejects it instead of retaining provider text.
                with self.assertRaises(ValueError) as caught: operation("evaluate", payload)
                self.assertNotIn("PRIVATE_SENTINEL", str(caught.exception))

    def test_T6_holds_when_reviewed_member_binding_is_removed_or_rebound(self):
        for change in ("removed", "plan", "configuration"):
            with self.subTest(change=change):
                # Given: a coverage artifact bound to a separate reviewed member set.
                payload, source = composite_request()
                payload["metric_coverage"] = operation("metrics", source)
                if change == "removed": payload.pop("metric_plan_binding")
                if change == "plan": payload["metric_plan_binding"]["experiment_digest"] = "sha256:" + "a" * 64
                if change == "configuration": payload["metric_plan_binding"]["configuration_digest"] = "sha256:" + "a" * 64
                # When: reconciling the current reviewed binding.
                outcome = operation("evaluate", payload)
                # Then: self-selected observed membership cannot ship.
                self.assertEqual(outcome["disposition"], "insufficient_data")
                self.assertIn("metric_composite_members_mismatch", sequence(outcome["evidence_reason_codes"]))

    def test_T6_rejects_when_composite_member_set_is_not_canonical(self):
        for change in ("duplicates", "unsorted", "digest", "timestamp", "raw"):
            with self.subTest(change=change):
                # Given: malformed resolved member identity.
                payload, source = composite_request()
                payload["metric_coverage"] = operation("metrics", source)
                members = at(payload, "metric_coverage.composites.0.member_set")
                if change == "duplicates": members["member_refs"] = sequence(members["member_refs"]) * 2
                if change == "unsorted": sequence(members["member_refs"]).reverse()
                if change == "digest": members["member_set_digest"] = "sha256:" + "a" * 64
                if change == "timestamp": members["observed_at"] = "2026-02-30T09:00:00Z"
                if change == "raw": members["raw_members"] = "PRIVATE_SENTINEL"
                # When/Then: invalid member identities are rejected before any outcome.
                with self.assertRaises(ValueError): operation("evaluate", payload)

    def test_T6_rolls_back_when_member_harm_coexists_with_missing_parent_metric(self):
        # Given: observed guardrail constituent failure and missing primary.
        payload, source = composite_request()
        coverage = operation("metrics", source)
        at(coverage, "composites.0.results.0")["state"] = "failed"
        rows(coverage["results"]).pop(0)
        payload["metric_coverage"] = coverage
        # When: the full evaluator interprets the bound composite.
        outcome = operation("evaluate", payload)
        # Then: incomplete coverage cannot veto independently observed harm.
        self.assertEqual(outcome["disposition"], "rollback")
        self.assertIn("metric_result_missing", sequence(outcome["evidence_reason_codes"]))

    def test_T1_rejects_when_unexpected_member_claims_harm(self):
        # Given: an unrelated constituent masquerading as an expected guardrail.
        payload, source = composite_request()
        coverage = operation("metrics", source)
        rows(at(coverage, "composites.0")["results"]).append(result("unexpected_member", "guardrail", "failed"))
        payload["metric_coverage"] = coverage
        # When/Then: closed exact-set reconciliation rejects it, not rollback.
        with self.assertRaises(ValueError): operation("evaluate", payload)

    def test_T4_rolls_back_when_configuration_is_absent_but_metric_harm_is_bound(self):
        # Given: current-plan/current-readout harm independent of missing launch integrity.
        payload = complete()
        at(payload, "metric_coverage.results.1")["state"] = "failed"
        rows(payload["metric_coverage"]["results"]).pop(0)
        payload.pop("configuration_binding")
        # When: evaluating an otherwise independently bound harmful observation.
        outcome = operation("evaluate", payload)
        # Then: missing identity must not veto safety or claim metric completeness.
        self.assertEqual(outcome["disposition"], "rollback")
        self.assertFalse(outcome["metric_completeness"])
        self.assertIn("configuration_identity_missing", sequence(outcome["evidence_reason_codes"]))

    def test_T1_rejects_when_an_unconfigured_composite_claims_results(self):
        # Given: a valid member-set shape for an unconfigured parent.
        payload, source = composite_request()
        coverage = operation("metrics", source)
        at(coverage, "composites.0")["member_set"] = member_set("unconfigured_parent", ["metric_member_a", "metric_member_b"])
        payload["metric_coverage"] = coverage
        # When/Then: unexpected configured outcomes are invalid, not merely incomplete.
        with self.assertRaises(ValueError): operation("evaluate", payload)

    def test_T2_preserves_inputs_when_evaluating_coverage(self):
        # Given: caller-owned immutable-in-practice synthetic artifacts.
        payload = complete()
        before = deepcopy(payload)
        # When: evaluating local coverage.
        operation("evaluate", payload)
        # Then: no caller mutation or hidden seal rewrite.
        self.assertEqual(payload, before)
