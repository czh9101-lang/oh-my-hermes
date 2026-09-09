from __future__ import annotations

import unittest

from _local_package import load_local_package
from test_product_discovery_validation import DISCOVERY_ID, _ledger, _prepared_artifacts

load_local_package()
from omh.workflows.decision_receipt_handoffs import build_decision_receipt_handoff  # noqa: E402
from omh.workflows.product_discovery_validation import (  # noqa: E402
    build_discovery_decision_frame,
    build_discovery_evidence_ledger,
    discovery_audience_gate,
    evaluate_product_discovery,
    prepare_product_discovery,
    product_brief_consumption,
    validate_product_discovery_artifact,
)


NOW = "2030-01-01T02:00:00+00:00"
UNDEFINED_STATES = ("unknown", "synthetic_only", "non_recruitable")


def _other_segment_ledger(segment_ref: str) -> dict[str, object]:
    entry = dict(_ledger()["entries"][0])
    entry["segment_ref"] = segment_ref
    return build_discovery_evidence_ledger(discovery_id=DISCOVERY_ID, entries=[entry])


class DiscoveryAudienceGateTests(unittest.TestCase):
    def test_undefined_audience_keeps_evidence_work_and_blocks_every_build_output(self) -> None:
        # Given: a discovery whose audience is only unknown, synthetic, or unreachable.
        for state in UNDEFINED_STATES:
            with self.subTest(segment_definition_state=state):
                artifacts = _prepared_artifacts(segment_definition_state=state)

                # When: the frame and the customer discovery plan are prepared and gated.
                gate = discovery_audience_gate(artifacts["frame"])

                # Then: evidence work continues while every build output stays blocked.
                self.assertEqual(artifacts["plan"]["status"], "prepared_not_observed")
                self.assertEqual(gate["audience_gate"], "audience_undefined")
                self.assertTrue(gate["evidence_work_permitted"])
                self.assertFalse(gate["solution_work_permitted"])
                self.assertEqual(
                    gate["blocked_outputs"], ["product-brief", "decision-prototype", "coding-handoff"]
                )
                self.assertTrue(gate["missing_audience_evidence_refs"])
                self.assertEqual(gate["next_route"], "product-discovery-validation")

    def test_defined_audience_names_no_missing_audience_evidence(self) -> None:
        # Given: a recruitable segment and a behaviorally observed segment.
        for state in ("recruitable", "behaviorally_observed"):
            with self.subTest(segment_definition_state=state):
                frame = _prepared_artifacts(segment_definition_state=state)["frame"]

                # When: the same audience gate reads the frame.
                gate = discovery_audience_gate(frame)

                # Then: nothing is blocked and no audience evidence is outstanding.
                self.assertEqual(gate["audience_gate"], "audience_defined")
                self.assertEqual(gate["blocked_outputs"], [])
                self.assertEqual(gate["missing_audience_evidence_refs"], [])

    def test_undefined_audience_cannot_persevere_or_reach_product_brief(self) -> None:
        # Given: customer evidence that would otherwise validate the precommitted test.
        for state in UNDEFINED_STATES:
            with self.subTest(segment_definition_state=state):
                artifacts = _prepared_artifacts(segment_definition_state=state)

                # When: the package is evaluated with that supporting external-human evidence.
                receipt = evaluate_product_discovery(
                    prepare_product_discovery(**artifacts, ledger=_ledger()), now=NOW
                )
                handoff = build_decision_receipt_handoff(receipt, target_workflow="product-brief")

                # Then: the receipt stays inconclusive and names the missing audience evidence.
                self.assertEqual((receipt["decision"], receipt["problem_gate"]), ("inconclusive", "inconclusive"))
                self.assertFalse(receipt["solution_work_permitted"])
                self.assertTrue(receipt["missing_audience_evidence_refs"])
                self.assertIn("risk-audience-undefined", receipt["residual_risk_refs"])
                self.assertEqual(receipt["next_route"], "product-discovery-validation")
                self.assertEqual(product_brief_consumption(receipt), {})
                self.assertEqual(handoff["decision_state"], "blocked")
                self.assertEqual(handoff["blocked_reason"], "discovery_audience_undefined")

    def test_recruitable_audience_with_segment_matched_evidence_passes_the_problem_gate(self) -> None:
        # Given: a recruitable segment and re-entered external-human evidence from it.
        artifacts = _prepared_artifacts()

        # When: every other precommitted criterion is met at evaluation time.
        receipt = evaluate_product_discovery(prepare_product_discovery(**artifacts, ledger=_ledger()), now=NOW)

        # Then: solution work is permitted and the product-brief route opens.
        self.assertEqual((receipt["decision"], receipt["problem_gate"]), ("persevere", "validated"))
        self.assertTrue(receipt["solution_work_permitted"])
        self.assertEqual(receipt["missing_audience_evidence_refs"], [])
        self.assertEqual(receipt["next_route"], "product-brief")
        self.assertEqual(product_brief_consumption(receipt)["target_segment_ref"], "segment-new-teams")

    def test_evidence_from_another_segment_needs_an_explicit_pivot_frame(self) -> None:
        # Given: representative external-human evidence observed in a different segment.
        artifacts = _prepared_artifacts()
        foreign = _other_segment_ledger("segment-enterprise-ops")

        # When: it is offered against the framed segment, then against a pivoted frame.
        unmatched = evaluate_product_discovery(
            prepare_product_discovery(**artifacts, ledger=foreign), now=NOW
        )
        pivoted = _prepared_artifacts()
        pivoted["frame"] = build_discovery_decision_frame(
            discovery_id=DISCOVERY_ID,
            problem_ref="problem-onboarding-dropoff",
            segment_ref="segment-enterprise-ops",
            segment_definition_state="recruitable",
            alternative_refs=["alternative-spreadsheet"],
            decision_owner_ref="owner-product",
            learning_budget_ref="budget-discovery-1",
            deadline_at="2030-01-02T00:00:00+00:00",
            kill_criteria_refs=["criterion-no-repeated-problem"],
        )

        # Then: the framed segment stays unvalidated and a frame-only relabel is refused.
        self.assertEqual(unmatched["decision"], "inconclusive")
        self.assertEqual(unmatched["segment_ref"], "segment-new-teams")
        with self.assertRaisesRegex(ValueError, "must match the framed target segment"):
            prepare_product_discovery(**pivoted, ledger=foreign)

    def test_a_receipt_cannot_declare_permitted_solution_work_for_an_undefined_audience(self) -> None:
        # Given: a validated receipt from a recruitable segment.
        artifacts = _prepared_artifacts()
        receipt = evaluate_product_discovery(prepare_product_discovery(**artifacts, ledger=_ledger()), now=NOW)

        # When: an untrusted record relabels the audience or forges the derived permission.
        relabelled = {**receipt, "segment_definition_state": "unknown"}
        forged = {**receipt, "solution_work_permitted": False}

        # Then: both are rejected and neither can feed the product brief.
        self.assertTrue(validate_product_discovery_artifact(relabelled))
        self.assertTrue(validate_product_discovery_artifact(forged))
        self.assertEqual(product_brief_consumption(relabelled), {})
        self.assertEqual(product_brief_consumption(forged), {})

    def test_the_audience_gate_refuses_a_record_that_is_not_a_decision_frame(self) -> None:
        # Given: a valid artifact from the same family that is not the decision frame.
        plan = _prepared_artifacts()["plan"]

        # When: it is submitted to the audience gate.
        # Then: the gate refuses it instead of inferring an audience.
        with self.assertRaisesRegex(ValueError, "discovery decision frame"):
            discovery_audience_gate(plan)


if __name__ == "__main__":
    unittest.main()
