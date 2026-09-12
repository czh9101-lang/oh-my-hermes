"""First-launch compatibility and the observation assertion boundary."""
from copy import deepcopy
import unittest

from _local_package import load_local_package
load_local_package()
from _lifecycle_configuration import at, bind, configuration_input, operation, sequence
from test_lifecycle_growth_exposure import exposure_inputs, launch_artifacts
from test_lifecycle_growth_configuration_edges import rebuild


class ConfigurationLaunchTests(unittest.TestCase):
    def test_C3_prepares_without_readout_when_only_expected_digest_is_known(self):
        # Given: reachability checks but no fictional analysis/launch observation.
        launch = launch_artifacts()
        source = exposure_inputs()
        launch["experiment"] = source["experiment"]
        launch["exposure_evidence"] = source["exposure_evidence"]
        request = configuration_input({"experiment": source["experiment"]}, observed=False)
        launch["audience_review"] = request["artifacts"]["audience_review"]
        launch["configuration_binding"] = operation("configuration", request)
        # When: preparing the first launch.
        result = operation("prepare", launch)
        # Then: readiness is allowed, but is not launch integrity.
        self.assertEqual((result["verdict"], result["configuration_integrity"]), ("READY", False))

    def test_C4_holds_first_launch_when_predecessor_conflicts(self):
        # Given: explicit prior seal contradicts the current reviewed assignment.
        launch = launch_artifacts()
        observed = bind(exposure_inputs())
        request = configuration_input({"experiment": observed["experiment"], "audience_review": observed["audience_review"]})
        request["predecessor_seal"] = deepcopy(observed["configuration_binding"]["seal"])
        at(request, "predecessor_seal.observation")["configuration_digest"] = "sha256:" + "b" * 64
        launch.update(experiment=observed["experiment"], exposure_evidence=observed["exposure_evidence"],
                      audience_review=observed["audience_review"], configuration_binding=operation("configuration", request))
        # When: omitting a readout cannot erase already supplied drift.
        result = operation("prepare", launch)
        # Then: the predecessor remains authoritative for promotion.
        self.assertEqual(result["verdict"], "HOLD")
        self.assertIn("configuration_drift", sequence(result["hold_reasons"]))

    def test_C1_holds_when_observed_identity_time_has_no_matching_receipt(self):
        # Given: a forged observed timestamp, not represented by seal or arrivals.
        payload = bind(exposure_inputs())
        at(payload, "configuration_binding.identity")["observed_at"] = "2026-09-03T09:00:00Z"
        # When: reconciling observation metadata.
        result = operation("evaluate", payload)
        # Then: copied hashes do not invent an observation.
        self.assertIn("configuration_identity_unknown", sequence(result["evidence_reason_codes"]))
        self.assertFalse(result["configuration_integrity"])

    def test_C8_reports_unknown_not_drift_when_only_a_mutable_handle_exists(self):
        # Given: a valid unknown identity with no digest, revision or observation.
        payload = bind(exposure_inputs())
        binding = payload["configuration_binding"]
        at(binding, "identity").update(identity_state="unknown", configuration_digest=None,
            revision_ref=None, observed_at=None, evidence_refs=[])
        binding.update(seal=None, observations=[])
        for entry in at(binding, "bindings").values():
            at(entry).update(configuration_digest=None, revision_ref=None, evidence_refs=[])
        # When: evaluating an unobserved handle-only identity.
        result = operation("evaluate", payload)
        # Then: absence is insufficient data, not invented observed drift.
        self.assertEqual(result["disposition"], "insufficient_data")
        self.assertEqual(result["evidence_reason_codes"], ["configuration_identity_unknown"])

    def test_C8_keeps_unknown_when_a_predecessor_exists_but_current_digest_is_absent(self):
        # Given: a known predecessor, but no current assignment observation.
        payload = bind(exposure_inputs())
        binding = payload["configuration_binding"]
        at(binding, "identity").update(identity_state="unknown", configuration_digest=None,
            revision_ref=None, observed_at=None, evidence_refs=[])
        for entry in at(binding, "bindings").values():
            at(entry).update(configuration_digest=None, revision_ref=None, evidence_refs=[])
        # When: current identity is unavailable, not observed different.
        result = operation("evaluate", payload)
        # Then: an old receipt cannot prove current drift or current integrity.
        self.assertEqual(result["evidence_reason_codes"], ["configuration_identity_unknown"])
        self.assertEqual(result["disposition"], "insufficient_data")

    def test_C8_reports_legacy_when_explicit_legacy_companion_is_read(self):
        # Given: a readable legacy companion without an observed identity claim.
        payload = bind(exposure_inputs())
        at(payload, "configuration_binding.identity").update(identity_state="legacy", observed_at=None, evidence_refs=[])
        # When: evaluating a migrated but unobserved artifact.
        result = operation("evaluate", payload)
        # Then: migration alone never grants integrity.
        self.assertEqual(result["disposition"], "insufficient_data")
        self.assertIn("configuration_identity_legacy", sequence(result["evidence_reason_codes"]))

    def test_C2_accepts_maximum_rule_count_when_derived_unknown_refs_exceed_eight(self):
        # Given: the existing audience contract permits 32 ordered rules.
        request = configuration_input(exposure_inputs(), observed=False)
        rule = at(request, "artifacts.audience_review.rules.0")
        request["artifacts"]["audience_review"]["rules"] = [{**rule, "rule_ref": f"rule_{index}", "bucketing_domain_ref": None} for index in range(32)]
        rebuild(request)
        # When: constructing the configuration companion for an unresolved audience.
        try:
            result = operation("configuration", request)
        except ValueError as exc:
            self.fail(f"valid bounded launch contract must remain readable: {exc}")
        # Then: unknown, not structurally rejected due to derived rule counts.
        self.assertEqual(at(result, "identity")["identity_state"], "unknown")
