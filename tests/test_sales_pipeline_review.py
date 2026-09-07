from __future__ import annotations

from dataclasses import replace
import unittest
from unittest.mock import patch

from tests._local_package import load_local_package

load_local_package()

from omh.workflows.sales_pipeline_review import (
    CriterionEvidence,
    ForecastCategoryDefinition,
    NextStep,
    OpportunitySnapshot,
    SalesPipelineReviewInput,
    ScenarioDefinition,
    StageDefinition,
    evaluate_sales_pipeline_review,
    prepare_sales_pipeline_review,
    validate_sales_pipeline_artifact,
)


def review_input() -> SalesPipelineReviewInput:
    return SalesPipelineReviewInput(
        source_ref="crm_snapshot_2026q3_a1b2c3d4",
        snapshot_as_of="2026-09-01T12:00:00Z",
        reviewed_at="2026-09-03T12:00:00Z",
        horizon_end="2026-09-30",
        cohort_id="new_business_2026q3",
        included_motions=("new_business",),
        included_owner_ids=("owner_team_east", "owner_team_west"),
        reporting_currency="USD",
        amount_semantics="annual_contract_value_minor_units",
        stage_definitions=(
            StageDefinition("discovery", "discovery evidence collected", ("buyer_problem",), 14),
            StageDefinition("proposal", "proposal supplied to buyer", ("buyer_review",), 10),
        ),
        forecast_definitions=(
            ForecastCategoryDefinition("pipeline", "seller pipeline", ("upside",)),
            ForecastCategoryDefinition("commit", "seller commit", ("base", "upside")),
        ),
        scenario_definitions=(
            ScenarioDefinition("base", "categories supplied for base scenario"),
            ScenarioDefinition("upside", "categories supplied for upside scenario"),
        ),
        opportunities=(
            OpportunitySnapshot(
                opportunity_id="opp_alpha_01",
                account_id="acct_alpha_01",
                owner_id="owner_team_east",
                motion_id="new_business",
                stage_id="proposal",
                seller_forecast_category_id="commit",
                seller_probability_basis_points=8000,
                amount_minor=700_000,
                currency="USD",
                stage_entered_on="2026-08-10",
                expected_close_on="2026-09-20",
                previous_stage_id="discovery",
                previous_close_on="2026-09-10",
                exit_criteria=(CriterionEvidence("buyer_review", "unknown"),),
                next_step=NextStep("legal_review", "2026-09-05", ("ev_next_alpha",)),
                buyer_commitment_state="next_step_observed",
                buyer_commitment_evidence_refs=("ev_buyer_alpha",),
            ),
            OpportunitySnapshot(
                opportunity_id="opp_beta_02",
                account_id="acct_beta_02",
                owner_id="owner_team_west",
                motion_id="new_business",
                stage_id="discovery",
                seller_forecast_category_id="pipeline",
                seller_probability_basis_points=None,
                amount_minor=300_000,
                currency="USD",
                stage_entered_on="2026-08-01",
                expected_close_on="2026-09-28",
                exit_criteria=(CriterionEvidence("buyer_problem", "met", ("ev_problem_beta",)),),
                buyer_commitment_state="none_observed",
            ),
        ),
        decision_owner_id="owner_sales_lead",
        freshness_limit_days=7,
        concentration_limit_basis_points=6000,
    )


class SalesPipelineReviewTests(unittest.TestCase):
    def test_callable_pipeline_prepares_valid_scope_before_evaluation(self) -> None:
        # Given a bounded CRM snapshot with supplied portfolio semantics.
        request = review_input()

        # When the public preparation and evaluation APIs are called.
        preparation = prepare_sales_pipeline_review(request)
        evaluation = evaluate_sales_pipeline_review(request)

        # Then every calculated artifact is valid and no raw export is retained.
        self.assertEqual(preparation.status, "READY")
        self.assertEqual(preparation.errors, ())
        self.assertEqual(validate_sales_pipeline_artifact(preparation.scope), [])
        self.assertEqual(evaluation.status, "READY")
        self.assertIsNotNone(evaluation.health)
        self.assertIsNotNone(evaluation.forecast)
        self.assertEqual(validate_sales_pipeline_artifact(evaluation.health), [])
        self.assertEqual(validate_sales_pipeline_artifact(evaluation.forecast), [])
        self.assertFalse(preparation.scope["raw_export_retained"])

    def test_health_reports_movement_stalls_slips_exit_gaps_next_steps_and_concentration(self) -> None:
        # Given one concentrated slipped deal and one stalled deal.
        request = review_input()

        # When the supplied records are evaluated.
        health = evaluate_sales_pipeline_review(request).health

        # Then portfolio exceptions are calculated only from those records.
        self.assertIsNotNone(health)
        self.assertEqual(health["movement"][0]["opportunity_id"], "opp_alpha_01")
        self.assertEqual(health["slips"][0]["slip_days"], 10)
        self.assertEqual(health["stalls"][0]["opportunity_id"], "opp_alpha_01")
        self.assertEqual(health["exit_criteria_gaps"][0]["criterion_id"], "buyer_review")
        self.assertEqual(health["next_step_quality"][0]["quality"], "evidence_backed")
        self.assertEqual(health["next_step_quality"][1]["quality"], "missing")
        self.assertEqual(health["concentration"]["exceptions"][0]["account_id"], "acct_alpha_01")

    def test_each_precalculation_metadata_failure_returns_hold(self) -> None:
        base = review_input()
        cases = {
            "missing_as_of": replace(base, snapshot_as_of=""),
            "stale_snapshot": replace(base, reviewed_at="2026-09-20T12:00:00Z"),
            "invalid_horizon": replace(base, horizon_end="2026-08-31"),
            "undefined_stages": replace(base, stage_definitions=()),
            "undefined_forecasts": replace(base, forecast_definitions=()),
            "undefined_amount": replace(base, amount_semantics=""),
            "duplicate_opportunities": replace(base, opportunities=(base.opportunities[0], base.opportunities[0])),
            "missing_owner": replace(
                base,
                opportunities=(replace(base.opportunities[0], owner_id=""), base.opportunities[1]),
            ),
        }

        for name, request in cases.items():
            with self.subTest(name=name):
                # Given one invalid item of required metadata, When evaluation is requested.
                evaluation = evaluate_sales_pipeline_review(request)

                # Then calculation is held and no calculated artifact is emitted.
                self.assertEqual(evaluation.status, "HOLD")
                self.assertTrue(evaluation.errors)
                self.assertIsNone(evaluation.health)
                self.assertIsNone(evaluation.forecast)

    def test_hold_gate_prevents_health_and_forecast_calculation(self) -> None:
        # Given invalid stage metadata and calculation seams that must remain unreachable.
        request = replace(review_input(), stage_definitions=())

        # When evaluation runs behind the metadata gate.
        with (
            patch("omh.workflows.sales_pipeline_review.build_sales_pipeline_health") as build_health,
            patch("omh.workflows.sales_pipeline_review.build_sales_forecast_assessment") as build_forecast,
        ):
            evaluation = evaluate_sales_pipeline_review(request)

        # Then the gate holds before either calculator is called.
        self.assertEqual(evaluation.status, "HOLD")
        build_health.assert_not_called()
        build_forecast.assert_not_called()

    def test_mixed_currency_requires_observed_conversion_basis(self) -> None:
        # Given a EUR opportunity in a USD portfolio without a conversion observation.
        base = review_input()
        request = replace(
            base,
            opportunities=(base.opportunities[0], replace(base.opportunities[1], currency="EUR")),
        )

        # When the review is prepared.
        preparation = prepare_sales_pipeline_review(request)

        # Then mixed-currency calculations are held rather than fabricated.
        self.assertEqual(preparation.status, "HOLD")
        self.assertIn("mixed_currency_without_observed_conversion:EUR", preparation.errors)

    def test_malformed_artifact_is_rejected_by_public_dispatcher(self) -> None:
        # Given an otherwise valid scope with an unsupported field.
        scope = dict(prepare_sales_pipeline_review(review_input()).scope)
        scope["raw_export"] = "forbidden"

        # When the public artifact validator receives it.
        errors = validate_sales_pipeline_artifact(scope)

        # Then the malformed shape is rejected.
        self.assertIn("sales_pipeline_scope keys are invalid", errors)


if __name__ == "__main__":
    _ = unittest.main()
