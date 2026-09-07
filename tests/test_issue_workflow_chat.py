from __future__ import annotations

import unittest

from _local_package import load_local_package

load_local_package()

from omh.plugin_bundle.omh.awareness import awareness_primer_payload, workflow_context_card_for_workflow
from omh.quality.chat_card_coverage import CHAT_CARD_COVERAGE_CASES, build_chat_card_coverage_demo
from omh.routing.action_copy import NEXT_ACTION_LABELS
from omh.wrapper.contract import VISIBLE_ACTIONS, _ACK_PRIMARY_ACTIONS_BY_NEXT_ACTION, build_chat_interaction_payload


WORKFLOW_CHAT_CONTRACTS = {
    "decision-prototype": ("intent_to_plan", "decision-prototype: test one uncertain cache choice before planning", "decision_prototype", "decision_prototype_prepared", "prepare_decision_prototype"),
    "lifecycle-growth": ("research_and_ops", "lifecycle-growth: prepare an onboarding journey and holdout experiment", "lifecycle_growth", "lifecycle_growth_prepared", "prepare_lifecycle_growth"),
    "product-discovery-validation": ("intent_to_plan", "product-discovery-validation: test whether this customer problem deserves a PRD", "product_discovery_validation", "product_discovery_validation_prepared", "prepare_product_discovery_validation"),
    "sales-pipeline-review": ("research_and_ops", "sales-pipeline-review: review this pipeline export for stale deals and forecast calibration", "sales_pipeline_review", "sales_pipeline_review_prepared", "prepare_sales_pipeline_review"),
}


class IssueWorkflowChatTests(unittest.TestCase):
    def test_workflows_are_registered_in_awareness_lanes_and_context_cards(self) -> None:
        lanes = {lane["id"]: set(lane["skills"]) for lane in awareness_primer_payload()["lanes"]}
        for workflow, (lane, _, _, _, _) in WORKFLOW_CHAT_CONTRACTS.items():
            with self.subTest(workflow=workflow):
                self.assertIn(workflow, lanes[lane])
                self.assertEqual(workflow_context_card_for_workflow(workflow)["id"], lane)

    def test_actions_are_visible_acknowledged_and_labeled(self) -> None:
        for workflow, (_, _, _, _, action) in WORKFLOW_CHAT_CONTRACTS.items():
            with self.subTest(workflow=workflow):
                self.assertIn(action, VISIBLE_ACTIONS)
                self.assertEqual(_ACK_PRIMARY_ACTIONS_BY_NEXT_ACTION[action][0], action)
                self.assertTrue(NEXT_ACTION_LABELS[action])

    def test_actual_wrapper_renderers_return_dedicated_evidence_bounded_cards(self) -> None:
        for workflow, (_, message, kind, phase, action) in WORKFLOW_CHAT_CONTRACTS.items():
            with self.subTest(workflow=workflow):
                payload = build_chat_interaction_payload(message, source="discord")
                response = payload["chat_response"]
                self.assertEqual(payload["route"]["selected_skill"], workflow)
                self.assertEqual(response["kind"], kind)
                self.assertEqual(payload["next_action"], action)
                self.assertEqual(response["state"]["phase"], phase)
                self.assertEqual(response["actions"][0]["id"], action)
                self.assertEqual(response["actions"][0]["style"], "primary")
                self.assertTrue(response["claim_boundary"])
                self.assertTrue(response["state"]["evidence_not_observed"])

    def test_coverage_corpus_exercises_each_dedicated_card(self) -> None:
        cases = {case.expected_skill: case for case in CHAT_CARD_COVERAGE_CASES}
        rows = {row["expected"]["workflow"]: row for row in build_chat_card_coverage_demo(source="discord")["cases"]}
        for workflow, (_, _, _, _, action) in WORKFLOW_CHAT_CONTRACTS.items():
            with self.subTest(workflow=workflow):
                self.assertEqual(cases[workflow].expected_next_action, action)
                self.assertTrue(rows[workflow]["dedicated_card"])
                self.assertTrue(rows[workflow]["passed"])
