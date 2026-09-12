"""Canonical field coverage, seal ordering, and artifact-binding adversarial cases."""
from copy import deepcopy
import unittest

from _local_package import load_local_package
load_local_package()
from _lifecycle_configuration import at, bind, configuration_input, operation, sequence, ConfigurationRequest
from test_lifecycle_growth_exposure import exposure_inputs
from omh.workflows.lifecycle_growth_configuration_values import configuration_digest
from omh.workflows.lifecycle_growth_launch import build_launch_audience_review


def rebuild(request: ConfigurationRequest) -> ConfigurationRequest:
    review = request["artifacts"]["audience_review"]
    lifecycle_id, semantics, share = review["lifecycle_growth_id"], review["evaluation_semantics"], review["holdout_exclusion_share"]
    assert isinstance(lifecycle_id, str) and isinstance(semantics, str) and isinstance(share, (int, float))
    request["artifacts"]["audience_review"] = build_launch_audience_review(
        lifecycle_growth_id=lifecycle_id, evaluation_semantics=semantics,
        rules=[{key: value for key, value in at(rule).items() if key != "reachable"} for rule in sequence(review["rules"])],
        holdout_exclusion_share=share,
    )
    return request


class ConfigurationCanonicalTests(unittest.TestCase):
    def test_C2_changes_digest_when_each_rule_semantic_changes(self):
        baseline = bind(exposure_inputs())
        for field, value in (("rule_ref", "rule_new"), ("evaluation_domain_ref", "domain_v2"),
                ("bucketing_domain_ref", "bucket_v2"), ("bucketing_subject", "device"),
                ("condition_refs", ["condition_v2"]), ("rollout_share", 51),
                ("variant_ref", "variant_v2"), ("result_kind", "split")):
            with self.subTest(field=field):
                # Given: one changed assignment-bearing rule field.
                request = configuration_input(baseline, observed=False)
                at(request, "artifacts.audience_review.rules.0")[field] = value
                if field == "result_kind":
                    at(request, "artifacts.audience_review.rules.0")["variant_ref"] = None
                # When: deriving the canonical digest.
                result = operation("configuration", rebuild(request))
                # Then: no assignment change is omitted.
                self.assertNotEqual(at(result, "identity")["configuration_digest"], at(baseline, "configuration_binding.identity")["configuration_digest"])

    def test_C2_distinguishes_order_null_unknown_and_holdout(self):
        baseline = bind(exposure_inputs())
        for change in ("rule_order", "condition_order", "null_domain", "unknown_semantics", "unknown_holdout"):
            with self.subTest(change=change):
                # Given: ordered versus unresolved configuration classes.
                request = configuration_input(baseline, observed=False)
                review = at(request, "artifacts.audience_review")
                if change == "rule_order":
                    sequence(review["rules"]).reverse()
                elif change == "condition_order":
                    sequence(at(review, "rules.0")["condition_refs"]).reverse()
                elif change == "null_domain":
                    at(review, "rules.0")["bucketing_domain_ref"] = None
                elif change == "unknown_semantics":
                    review["evaluation_semantics"] = "unknown"
                else:
                    request["artifacts"]["experiment"].update(holdout_state="unknown", holdout_rationale_ref="")
                # When: projecting canonically.
                result = operation("configuration", rebuild(request))
                # Then: unknown/null are not equal to known assignment semantics.
                self.assertNotEqual(at(result, "identity")["configuration_digest"], at(baseline, "configuration_binding.identity")["configuration_digest"])

    def test_C6_normalizes_zero_and_object_order_but_excludes_nonassignment_fields(self):
        # Given: equivalent zero forms, different key order and review metadata.
        source = configuration_input(bind(exposure_inputs()), observed=False)["artifacts"]
        at(source, "audience_review.rules.0")["rollout_share"] = 0
        equivalent = deepcopy(source)
        at(equivalent, "audience_review.rules.0")["rollout_share"] = -0.0
        equivalent["experiment"] = dict(reversed(list(equivalent["experiment"].items())))
        equivalent["experiment"].update(approval_state="pending", minimum_runtime_days=99)
        # When: digesting only assignment configuration.
        result = configuration_digest(equivalent)
        # Then: serialization trivia and decision metadata do not create drift.
        self.assertEqual(result, configuration_digest(source))

    def test_C1_rejects_boolean_nonfinite_raw_and_invalid_sticky_policy(self):
        baseline = bind(exposure_inputs())
        for field, value in (("rollout_share", True), ("rollout_share", float("nan")),
                             ("rollout_share", float("inf")), ("raw_condition", "PRIVATE_SENTINEL")):
            with self.subTest(field=field, value=value):
                # Given: untrusted configuration data.
                source = deepcopy(baseline)
                at(source, "audience_review.rules.0")[field] = value
                # When/Then: invalid classes cannot acquire a digest.
                with self.assertRaises(ValueError):
                    configuration_digest(source)
        source = deepcopy(baseline)
        source["experiment"]["sticky_assignment_policy"] = "nonsticky"
        with self.assertRaises(ValueError):
            configuration_digest(source)

    def test_C1_normalizes_safe_reference_whitespace(self):
        # Given: metadata_ref permits surrounding whitespace.
        source = configuration_input(bind(exposure_inputs()), observed=False)["artifacts"]
        equivalent = deepcopy(source)
        sequence(at(equivalent, "audience_review.rules.0")["condition_refs"])[0] = " condition_eligible_v1 "
        # When: normalizing through the existing safe-reference contract.
        result = configuration_digest(equivalent)
        # Then: normalized references are equivalent.
        self.assertEqual(result, configuration_digest(source))


class ConfigurationSealTests(unittest.TestCase):
    def test_C3_hashes_each_actual_artifact_including_nested_analysis(self):
        for slot in ("experiment", "audience_review", "exposure_evidence", "analysis_status", "readout"):
            with self.subTest(slot=slot):
                # Given: stale actual-artifact hash, though every configuration string matches.
                payload = bind(exposure_inputs())
                at(payload, "configuration_binding.bindings." + slot)["artifact_digest"] = "sha256:" + "a" * 64
                # When: evaluating the actual objects.
                result = operation("evaluate", payload)
                # Then: all five hashes are enforced.
                self.assertIn("configuration_binding_mismatch", sequence(result["evidence_reason_codes"]))

    def test_C3_seals_first_observation_when_input_order_is_reversed(self):
        # Given: launch/exposure arrive together in reverse temporal order.
        payload = bind(exposure_inputs())
        request = configuration_input(payload)
        first = at(payload, "configuration_binding.seal.observation")
        request["observations"] = [{**first, "kind": "exposure", "observed_at": "2026-09-02T09:00:00Z", "evidence_ref": "receipt_later"}, first]
        # When: sealing the first batch.
        result = operation("configuration", request)
        # Then: the first receipt wins, not array order.
        self.assertEqual(at(result, "seal")["observation"], first)

    def test_C4_conflicts_without_replacing_predecessor_when_earlier_contradiction_arrives(self):
        # Given: a new earlier conflicting receipt plus the caller-carried predecessor.
        payload = bind(exposure_inputs())
        request = configuration_input(payload)
        request["predecessor_seal"] = payload["configuration_binding"]["seal"]
        first = at(request, "predecessor_seal.observation")
        request["observations"] = [{**first, "observed_at": "2026-08-31T09:00:00Z", "evidence_ref": "earlier_conflict", "configuration_digest": "sha256:" + "b" * 64}]
        payload["configuration_binding"] = operation("configuration", request)
        # When: evaluating the conflicting append.
        result = operation("evaluate", payload)
        # Then: no silent rebase makes the new readout shippable.
        self.assertEqual(payload["configuration_binding"]["seal"], request["predecessor_seal"])
        self.assertIn("configuration_seal_conflict", sequence(result["evidence_reason_codes"]))
        self.assertEqual(result["disposition"], "review")

    def test_C7_rolls_back_when_drifted_but_not_when_readout_is_malformed(self):
        # Given: genuine harm and configuration drift.
        payload = bind(exposure_inputs())
        at(payload, "audience_review.rules.0")["rollout_share"] = 60
        payload["readout"].update(guardrail_state="failed", disposition="rollback")
        # When: interpreting harm alongside drift.
        result = operation("evaluate", payload)
        # Then: safety wins.
        self.assertEqual(result["disposition"], "rollback")
        self.assertIn("configuration_drift", sequence(result["evidence_reason_codes"]))

    def test_C7_refuses_rollback_when_readout_is_malformed(self):
        # Given: malformed readout claims a failed guardrail.
        payload = bind(exposure_inputs())
        payload["readout"].update(guardrail_state="failed", disposition="rollback", displayed_count=True)
        # When: independently validating the harm artifact.
        result = operation("evaluate", payload)
        # Then: malformed data cannot manufacture a safety signal.
        self.assertNotEqual(result["disposition"], "rollback")

    def test_C8_holds_when_mutable_conditions_have_no_observed_revision(self):
        # Given: a mutable condition handle without a pinned definition.
        payload = bind(exposure_inputs())
        request = configuration_input(payload)
        request["metadata"]["revision_ref"] = None
        at(request, "artifacts.audience_review.rules.0")["condition_refs"] = ["mutable_condition"]
        # When: constructing an asserted observed identity.
        result = operation("configuration", rebuild(request))
        # Then: it is downgraded to unknown, not falsely observed.
        self.assertEqual(at(result, "identity")["identity_state"], "unknown")
