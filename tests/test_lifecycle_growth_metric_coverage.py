"""Given approved configuration, observed metric coverage governs promotion."""
from copy import deepcopy
import json
import unittest

from _lifecycle_configuration import at, operation, sequence
from _lifecycle_metrics import BASE, complete, composite_request, configured, result, rows


class LifecycleGrowthMetricCoverageTests(unittest.TestCase):
    def observed(self):
        try:
            return complete()
        except ValueError as exc:
            self.fail(f"metrics operation must accept observed fixture: {exc}")

    def test_T8_holds_when_legacy_aggregates_are_complete(self):
        # Given: the actual #1503 positive fixture without per-metric observations.
        payload = configured()
        # When: evaluating a completed aggregate analysis.
        outcome = operation("evaluate", payload)
        # Then: legacy evidence cannot prove metric completeness.
        self.assertEqual(outcome["disposition"], "insufficient_data")
        self.assertIn("metric_coverage_legacy", sequence(outcome["evidence_reason_codes"]))
        self.assertFalse(outcome["metric_completeness"])

    def test_T8_reads_when_historic_readout_has_no_plan(self):
        # Given: the unchanged closed v1 readout.
        readout = configured()["readout"]
        # When: reading through the public operation.
        outcome = operation("readout", readout)
        # Then: its legacy status is explicit.
        self.assertIn("metric_coverage_legacy", sequence(outcome["evidence_reason_codes"]))

    def test_T2_ships_when_every_exact_metric_is_observed(self):
        # Given: exact metric refs bound to real plan, run, readout and launch seal.
        payload = self.observed()
        # When: evaluating the observations.
        outcome = operation("evaluate", payload)
        # Then: both independent gates pass.
        self.assertEqual((outcome["disposition"], outcome["metric_completeness"], outcome["configuration_integrity"]), ("ship", True, True))

    def test_T3_holds_when_guardrail_is_absent_missing_or_errored(self):
        for state in ("absent", "missing", "errored", "indeterminate"):
            with self.subTest(state=state):
                # Given: completed analysis with one configured guardrail unavailable.
                payload = self.observed()
                entries = rows(payload["metric_coverage"]["results"])
                guard = entries.pop()
                if state != "absent":
                    entries.append(result(guard["metric_ref"], "guardrail", state))
                # When: interpreting the partial result set.
                outcome = operation("evaluate", payload)
                # Then: aggregate passing cannot bypass the exact missing condition.
                code = "metric_result_missing" if state == "absent" else "metric_result_" + state
                self.assertEqual((outcome["disposition"], outcome["interpretation_state"]), ("insufficient_data", "HOLD"))
                self.assertIn(code, sequence(outcome["evidence_reason_codes"]))

    def test_T4_rolls_back_when_guardrail_fails_with_other_metric_unknown(self):
        for state in ("missing", "errored"):
            with self.subTest(state=state):
                # Given: independently observed harm, unknown primary, configuration drift.
                payload = self.observed()
                entries = rows(payload["metric_coverage"]["results"])
                entries[0] = result(entries[0]["metric_ref"], "primary", state)
                entries[1] = result(entries[1]["metric_ref"], "guardrail", "failed")
                at(payload, "audience_review.rules.0")["rollout_share"] = 60
                # When: deriving the local disposition.
                outcome = operation("readout", payload)
                # Then: harm wins without erasing missing evidence or drift.
                self.assertEqual(outcome["disposition"], "rollback")
                self.assertIn("configuration_drift", sequence(outcome["evidence_reason_codes"]))
                self.assertIn("metric_result_" + state, sequence(outcome["evidence_reason_codes"]))

    def test_T5_holds_when_primary_is_indeterminate(self):
        # Given: improved aggregate but primary result cannot be computed.
        payload = self.observed()
        entries = rows(payload["metric_coverage"]["results"])
        entries[0] = result(entries[0]["metric_ref"], "primary", "indeterminate")
        # When: evaluating through wrapped readout.
        outcome = operation("readout", payload)
        # Then: completed analysis is distinct from successful metrics.
        self.assertEqual(outcome["disposition"], "insufficient_data")
        self.assertIn("metric_result_indeterminate", sequence(outcome["evidence_reason_codes"]))

    def test_T5_reviews_when_primary_contradicts_aggregate(self):
        # Given: aggregate improved with an observed unchanged primary.
        payload = self.observed()
        at(payload, "metric_coverage.results.0")["state"] = "unchanged"
        # When: evaluating the contradiction.
        outcome = operation("evaluate", payload)
        # Then: complete does not imply eligible to ship.
        self.assertEqual(outcome["disposition"], "review")
        self.assertIn("metric_aggregate_mismatch", sequence(outcome["evidence_reason_codes"]))

    def test_T1_rejects_when_records_are_structurally_invalid(self):
        for change in ("duplicate", "unexpected", "role", "state", "field", "extra", "evidence", "error"):
            with self.subTest(change=change):
                # Given: an otherwise valid set with one malformed record.
                payload = self.observed()
                entries = rows(payload["metric_coverage"]["results"])
                if change == "duplicate": entries.append(deepcopy(entries[0]))
                if change == "unexpected": entries[0]["metric_ref"] = "metric_unexpected"
                if change == "role": entries[0]["role"] = "guardrail"
                if change == "state": entries[0]["state"] = "passed"
                if change == "field": entries[0].pop("error_category")
                if change == "extra": entries[0]["raw_error"] = "PRIVATE_SENTINEL"
                if change == "evidence": entries[0]["evidence_refs"] = []
                if change == "error": entries[0]["error_category"] = "PRIVATE_SENTINEL"
                # When/Then: the actual evaluator boundary rejects it, never fabricates coverage.
                with self.assertRaises(ValueError):
                    operation("evaluate", payload)

    def test_T2_rejects_when_plan_metric_refs_collide(self):
        for collision in ("primary", "guardrail"):
            with self.subTest(collision=collision):
                # Given: readable historical plan with an ambiguous expected metric set.
                payload = configured()
                refs = sequence(payload["experiment"]["guardrail_metric_refs"])
                refs.append(payload["experiment"]["primary_metric_ref"] if collision == "primary" else refs[0])
                # When/Then: new decisions reject the ambiguous plan before interpreting results.
                with self.assertRaises(ValueError):
                    operation("evaluate", payload)

    def test_T2_holds_when_coverage_binding_is_stale(self):
        for field in ("configuration_digest", "experiment_digest", "readout_digest", "analysis_run_ref", "lifecycle_growth_id"):
            with self.subTest(field=field):
                # Given: a result set copied from another artifact or run.
                payload = self.observed()
                payload["metric_coverage"][field] = "sha256:" + "f" * 64 if "digest" in field else "other_ref"
                # When: reconciling actual artifacts.
                outcome = operation("evaluate", payload)
                # Then: matching result strings alone cannot ship.
                self.assertNotEqual(outcome["disposition"], "ship")
                self.assertIn("metric_configuration_mismatch", sequence(outcome["evidence_reason_codes"]))

    def test_T6_reconciles_when_composite_members_are_complete_or_dropped(self):
        for change in ("complete", "dropped", "drift", "unresolved"):
            with self.subTest(change=change):
                # Given: a frozen reviewed member set separate from observed results.
                payload, source = composite_request()
                try:
                    coverage = operation("metrics", source)
                except ValueError as exc:
                    self.fail(f"metrics composite operation must be supported: {exc}")
                payload["metric_coverage"] = coverage
                if change == "dropped": rows(at(coverage, "composites.0")["results"]).pop()
                if change == "drift": at(coverage, "composites.0.member_set")["observed_at"] = "2026-09-02T09:00:00Z"
                if change == "unresolved": coverage["composites"] = []
                # When: evaluating exact reviewed membership.
                outcome = operation("evaluate", payload)
                # Then: only the frozen complete set can ship.
                self.assertEqual(outcome["disposition"], "ship" if change == "complete" else "insufficient_data")
                if change != "complete": self.assertFalse(outcome["metric_completeness"])

    def test_T10_holds_when_prepare_with_readout_lacks_metrics(self):
        # Given: all launch artifacts with valid observed configuration only.
        payload = json.loads(BASE.read_text(encoding="utf-8"))
        payload.update(configured())
        # When: preparing expansion through the real preparation seam.
        outcome = operation("prepare", payload)
        # Then: prepare cannot bypass the stronger gate.
        self.assertEqual(outcome["verdict"], "HOLD")
        self.assertIn("metric_coverage_legacy", sequence(outcome["hold_reasons"]))

    def test_T10_accepts_when_prepare_forwards_complete_metric_companion(self):
        # Given: full launch fixture plus observed metrics.
        payload = json.loads(BASE.read_text(encoding="utf-8"))
        payload.update(self.observed())
        # When: preparing expansion.
        outcome = operation("prepare", payload)
        # Then: valid metrics reach the same evaluator without a bypass.
        self.assertEqual(outcome["verdict"], "READY")

    def test_T7_validates_when_provider_failure_uses_safe_category(self):
        # Given: provider supplied only a category, never raw provider text.
        payload = self.observed()
        entries = rows(payload["metric_coverage"]["results"])
        entries[0] = result(entries[0]["metric_ref"], "primary", "errored")
        # When: validating the durable companion.
        outcome = operation("validate", payload["metric_coverage"])
        # Then: safe errors are valid records, not successful computation.
        self.assertTrue(outcome["valid"])


if __name__ == "__main__":
    unittest.main()
