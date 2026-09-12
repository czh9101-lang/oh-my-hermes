"""Immutable launch identity through the existing public workflow entry seam."""
from __future__ import annotations

from copy import deepcopy
import unittest

from _local_package import load_local_package

load_local_package()
from omh.workflows.lifecycle_growth_contracts import validate_lifecycle_growth_artifact
from test_lifecycle_growth_exposure import exposure_inputs
from _lifecycle_configuration import at, bind, configuration_input, operation, sequence


class LifecycleGrowthConfigurationTests(unittest.TestCase):
    def observed(self):
        try:
            return bind(exposure_inputs())
        except ValueError as exc:
            self.fail(f"configuration public operation must accept semantic input: {exc}")

    def test_C3_ships_when_all_five_actual_artifacts_match(self):
        # Given: one sealed observation and five actual artifact bindings.
        from _lifecycle_metrics import cover
        payload = cover(self.observed())
        # When: the wrapped readout takes the same gate as evaluation.
        result = operation("readout", payload)
        # Then: healthy observed inputs can ship, with explicit integrity.
        self.assertEqual((result["disposition"], result["configuration_integrity"]), ("ship", True))

    def test_C1_rejects_when_identity_fields_are_missing_extra_or_contradictory(self):
        # Given: observed identity with valid digest/time/refs.
        identity = at(self.observed(), "configuration_binding.identity")
        self.assertEqual(validate_lifecycle_growth_artifact(identity), [])
        variants = [{key: value for key, value in identity.items() if key != "observed_at"}]
        variants += [{**identity, **change} for change in (
            {"raw_audience": "PRIVATE_SENTINEL"}, {"configuration_digest": "sha256:bad"},
            {"observed_at": "2026-09-01"}, {"observed_at": None}, {"evidence_refs": []},
            {"identity_state": "legacy"}, {"identity_state": "unknown"}, {"revision_ref": []})]
        for value in variants:
            with self.subTest(value=value):
                # When: parsing the closed identity.
                errors = validate_lifecycle_growth_artifact(value)
                # Then: bad metadata never passes.
                self.assertTrue(errors)

    def test_C4_reports_drift_when_rule_or_share_changes_after_seal(self):
        for field, value in (("rule_ref", "rule_changed"), ("rollout_share", 60)):
            with self.subTest(field=field):
                # Given: a sealed launch with later assignment mutation.
                payload = self.observed()
                at(payload, "audience_review.rules.0")[field] = value
                # When: current review differs from launch digest.
                result = operation("evaluate", payload)
                # Then: promotion requires review, not shipping.
                self.assertEqual(result["disposition"], "review")
                self.assertIn("configuration_drift", sequence(result["evidence_reason_codes"]))

    def test_C5_reports_mismatch_when_analysis_binding_names_another_identity(self):
        # Given: analysis alone names another configuration.
        payload = self.observed()
        at(payload, "configuration_binding.bindings.analysis_status")["configuration_digest"] = "sha256:" + "f" * 64
        # When: reconciling all five artifacts.
        result = operation("evaluate", payload)
        # Then: the aggregate cannot conceal analysis identity mismatch.
        self.assertIn("configuration_binding_mismatch", sequence(result["evidence_reason_codes"]))
        self.assertNotEqual(result["disposition"], "ship")

    def test_C6_matches_digest_when_decimal_forms_are_equivalent(self):
        # Given: same configuration with int and float shares.
        payload = self.observed()
        request = configuration_input(payload, observed=False)
        at(request, "artifacts.audience_review.rules.0")["rollout_share"] = 50.0
        # When: canonicalized through the public builder operation.
        result = operation("configuration", request)
        # Then: normalization does not invent drift.
        self.assertEqual(at(result, "identity")["configuration_digest"], at(payload, "configuration_binding.identity")["configuration_digest"])

    def test_C2_changes_digest_when_any_assignment_field_changes(self):
        # Given: all assignment-bearing plan and audience fields.
        payload = self.observed()
        changes = [("experiment", key, value) for key, value in (
            ("treatment_ref", "other_treatment"), ("control_ref", "other_control"),
            ("assignment_unit", "device"), ("exposure_unit", "device"),
            ("assignment_event_ref", "other_assignment"), ("actual_exposure_event_ref", "other_exposure"),
            ("holdout_rationale_ref", "other_rationale"))]
        changes += [("audience_review", "holdout_exclusion_share", 15)]
        for slot, key, value in changes:
            with self.subTest(slot=slot, key=key):
                request = configuration_input(payload, observed=False)
                request["artifacts"][slot][key] = value
                # When: assignment configuration changes.
                result = operation("configuration", request)
                # Then: canonical identity differs.
                self.assertNotEqual(at(result, "identity")["configuration_digest"], at(payload, "configuration_binding.identity")["configuration_digest"])

    def test_C8_holds_when_legacy_has_no_configuration_identity(self):
        # Given: the previously shippable, healthy exposure fixture.
        payload = exposure_inputs()
        # When: evaluated through the existing operation seam.
        result = operation("evaluate", payload)
        # Then: identity absence is explicit and cannot ship.
        self.assertEqual(result["disposition"], "insufficient_data")
        self.assertIn("configuration_identity_missing", sequence(result["evidence_reason_codes"]))

    def test_C4_holds_when_configuration_companion_is_malformed(self):
        # Given: an unrecognized raw configuration companion, previously ignored.
        payload = exposure_inputs()
        payload["configuration_binding"] = {"raw_audience": "PRIVATE_SENTINEL"}
        # When/Then: malformed boundary data must not be silently ignored.
        with self.assertRaises(ValueError):
            operation("evaluate", payload)

    def test_C9_rejects_when_evaluation_operation_has_extra_keys(self):
        # Given: a misspelled binding slot must never bypass configuration gates.
        payload = exposure_inputs()
        payload["configuration_bindng"] = {}
        # When/Then: closed operation input rejects it.
        with self.assertRaises(ValueError):
            operation("readout", payload)

    def test_C7_preserves_rollback_when_identity_is_missing(self):
        # Given: independently valid guardrail failure.
        payload = exposure_inputs()
        payload["readout"].update(guardrail_state="failed", disposition="rollback")
        # When: configuration is absent.
        result = operation("evaluate", payload)
        # Then: safety wins without claiming integrity.
        self.assertEqual(result["disposition"], "rollback")
        self.assertIn("configuration_identity_missing", sequence(result["evidence_reason_codes"]))

    def test_C8_keeps_readable_artifacts_when_identity_is_missing(self):
        # Given: a historic v1 artifact.
        payload = deepcopy(exposure_inputs()["readout"])
        # When: validated without migration.
        errors = validate_lifecycle_growth_artifact(payload)
        # Then: compatibility does not require rewriting stored bytes.
        self.assertEqual(errors, [])


if __name__ == "__main__":
    unittest.main()
