from __future__ import annotations

import unittest

from omh.plugin_bundle.omh.awareness import awareness_context_matches_message, awareness_route_hint
from omh.routing.chat import route_chat_message
from omh.routing.recommend import recommend_skills


class IssueWorkflowRoutingTests(unittest.TestCase):
    def test_natural_language_intents_reach_each_new_workflow(self) -> None:
        cases = (
            (
                "Before we plan the migration, run a small spike to test the risky cache assumption.",
                "decision-prototype",
                "prepare_decision_prototype",
            ),
            (
                "Design an onboarding journey experiment to improve activation while respecting consent and frequency limits.",
                "lifecycle-growth",
                "prepare_lifecycle_growth",
            ),
            (
                "Validate whether this early idea solves a real customer problem before we write a PRD.",
                "product-discovery-validation",
                "prepare_product_discovery_validation",
            ),
            (
                "Validate this idea with customer interviews before we write a PRD.",
                "product-discovery-validation",
                "prepare_product_discovery_validation",
            ),
            (
                "Review this week's pipeline export for stale deals, slipped close dates, and forecast calibration.",
                "sales-pipeline-review",
                "prepare_sales_pipeline_review",
            ),
            (
                "온보딩 여정의 활성화 실험을 설계하고 싶어요.",
                "lifecycle-growth",
                "prepare_lifecycle_growth",
            ),
            (
                "고객 발견 검증을 통해 이 아이디어를 계속할지 결정하고 싶어요.",
                "product-discovery-validation",
                "prepare_product_discovery_validation",
            ),
            (
                "이번 주 파이프라인 리뷰에서 정체된 딜과 영업 예측 보정을 확인해 주세요.",
                "sales-pipeline-review",
                "prepare_sales_pipeline_review",
            ),
        )

        for message, expected_skill, expected_action in cases:
            with self.subTest(message=message):
                recommendation = recommend_skills(message, limit=1)[0]
                route = route_chat_message(message, source="generic")

                self.assertEqual(recommendation["skill"], expected_skill)
                self.assertEqual(recommendation["next_action"], expected_action)
                self.assertEqual(route["selected_skill"], expected_skill)
                self.assertEqual(route["recommendations"][0]["next_action"], expected_action)

    def test_existing_sibling_intents_keep_their_owners(self) -> None:
        cases = (
            ("Research the market and competitors for this category.", "research"),
            ("Help me clarify this ambiguous request before choosing a workflow.", "deep-interview"),
            ("Create a PRD and prioritize the roadmap for this product change.", "product-brief"),
            ("Build and ship this feature as a pull request.", "idea-to-deploy"),
            ("Write a social post about our product launch.", "content-operator"),
            ("Analyze this supplied cohort retention table.", "data-analysis"),
            ("Send this outreach email to the prospect.", "connector-operator"),
            ("Update the Salesforce opportunity stage to Closed Won.", "connector-operator"),
            (
                "Create a sales discovery plan and qualification questions for this single account.",
                "sales-development",
            ),
            ("Explain the budget variance and month-end close for September.", "finance-analysis"),
            ("What's the weather forecast for Seoul tomorrow?", "live-info-operator"),
        )
        new_workflows = {
            "decision-prototype",
            "lifecycle-growth",
            "product-discovery-validation",
            "sales-pipeline-review",
        }

        for message, expected_skill in cases:
            with self.subTest(message=message):
                recommendation = recommend_skills(message, limit=1)[0]
                route = route_chat_message(message, source="generic")

                self.assertEqual(recommendation["skill"], expected_skill)
                self.assertEqual(route["selected_skill"], expected_skill)
                self.assertNotIn(recommendation["skill"], new_workflows)
                self.assertNotIn(route["selected_skill"], new_workflows)

    def test_completed_work_stays_with_the_downstream_sibling(self) -> None:
        cases = (
            (
                "Write a PRD from these already validated customer discovery findings.",
                "product-brief",
                "prepare_product_brief",
            ),
            (
                "Validate this API schema before we write a PRD.",
                "product-brief",
                "prepare_product_brief",
            ),
            (
                "Write only the onboarding email copy for this approved lifecycle growth experiment.",
                "content-operator",
                "prepare_content_operator_card",
            ),
            (
                "Design an approved onboarding journey experiment and email copy with consent and frequency limits.",
                "lifecycle-growth",
                "prepare_lifecycle_growth",
            ),
        )

        for message, expected_skill, expected_action in cases:
            with self.subTest(message=message):
                recommendation = recommend_skills(message, limit=1)[0]
                route = route_chat_message(message, source="generic")

                self.assertEqual(
                    (recommendation["skill"], recommendation["next_action"]),
                    (expected_skill, expected_action),
                )
                self.assertEqual(
                    (route["selected_skill"], route["recommendations"][0]["next_action"]),
                    (expected_skill, expected_action),
                )


    def test_new_workflows_have_matching_awareness_hints(self) -> None:
        cases = (
            (
                "decision-prototype: test one uncertain cache choice before planning",
                "decision-prototype",
                "prepare_decision_prototype",
            ),
            (
                "lifecycle-growth: prepare an onboarding journey and holdout experiment",
                "lifecycle-growth",
                "prepare_lifecycle_growth",
            ),
            (
                "product-discovery-validation: test whether this customer problem deserves a PRD",
                "product-discovery-validation",
                "prepare_product_discovery_validation",
            ),
            (
                "sales-pipeline-review: review this pipeline export for stale deals and forecast calibration",
                "sales-pipeline-review",
                "prepare_sales_pipeline_review",
            ),
        )

        for message, expected_skill, expected_action in cases:
            with self.subTest(message=message):
                route = route_chat_message(message, source="generic")
                hint = awareness_route_hint(message)

                self.assertTrue(awareness_context_matches_message(message))
                self.assertEqual(route["selected_skill"], expected_skill)
                self.assertEqual(hint["primary_workflow"], expected_skill)
                self.assertEqual(hint["primary_next_action"], expected_action)


if __name__ == "__main__":
    unittest.main()
