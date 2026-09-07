from __future__ import annotations

from dataclasses import replace
import unittest

from tests._local_package import load_local_package
from tests.test_sales_pipeline_review import review_input

load_local_package()

from omh.workflows.sales_pipeline_review import (
    CurrencyConversionBasis,
    OutcomeObservation,
    PriorForecastSnapshot,
    evaluate_sales_pipeline_review,
    prepare_sales_pipeline_review,
    validate_sales_pipeline_artifact,
)


class SalesPipelineForecastTests(unittest.TestCase):
    def test_forecast_preserves_seller_scenario_and_buyer_states_separately(self) -> None:
        # Given seller states, supplied scenario mappings, and observed buyer evidence.
        request = review_input()

        # When the forecast assessment is produced.
        forecast = evaluate_sales_pipeline_review(request).forecast

        # Then no stage-derived probability or buyer commitment is manufactured.
        self.assertIsNotNone(forecast)
        self.assertEqual(forecast["seller_states"][0]["category_id"], "commit")
        self.assertEqual(forecast["seller_states"][0]["probability_basis_points"], 8000)
        self.assertIsNone(forecast["seller_states"][1]["probability_basis_points"])
        self.assertEqual(forecast["scenarios"][0]["amount_minor"], 700_000)
        self.assertEqual(forecast["scenarios"][1]["amount_minor"], 1_000_000)
        self.assertFalse(forecast["scenarios"][0]["buyer_commitment_inferred"])
        self.assertEqual(forecast["buyer_commitments"][0]["state"], "next_step_observed")
        self.assertEqual(forecast["buyer_commitments"][1]["state"], "none_observed")
        self.assertEqual(forecast["weighted_seller_scenario"]["amount_minor"], 560_000)
        self.assertEqual(forecast["weighted_seller_scenario"]["excluded_opportunity_ids"], ["opp_beta_02"])

    def test_zero_supplied_probability_remains_zero_instead_of_unavailable(self) -> None:
        # Given one explicit zero probability and one unavailable probability.
        base = review_input()
        request = replace(
            base,
            opportunities=(replace(base.opportunities[0], seller_probability_basis_points=0), base.opportunities[1]),
        )

        # When the forecast is evaluated.
        forecast = evaluate_sales_pipeline_review(request).forecast

        # Then the supported zero is preserved and only the unavailable row is excluded.
        self.assertIsNotNone(forecast)
        self.assertEqual(forecast["weighted_seller_scenario"]["amount_minor"], 0)
        self.assertEqual(forecast["weighted_seller_scenario"]["excluded_opportunity_ids"], ["opp_beta_02"])

    def test_unsupported_probability_holds_before_forecast_calculation(self) -> None:
        # Given a seller probability outside the supported basis-point range.
        base = review_input()
        request = replace(
            base,
            opportunities=(replace(base.opportunities[0], seller_probability_basis_points=10_001), base.opportunities[1]),
        )

        # When preparation validates metadata.
        preparation = prepare_sales_pipeline_review(request)

        # Then the review holds and names the affected opaque opportunity.
        self.assertEqual(preparation.status, "HOLD")
        self.assertIn("unsupported_probability:opp_alpha_01", preparation.errors)

    def test_calibration_is_unavailable_without_matching_observed_cohort(self) -> None:
        # Given prior and outcome evidence for different horizons.
        base = review_input()
        prior = PriorForecastSnapshot(
            "prior_snapshot_q3", "2026-08-01T00:00:00Z", base.cohort_id, base.horizon_end,
            "opp_alpha_01", "commit", 8000, 700_000, "USD", ("ev_prior_alpha",),
        )
        outcome = OutcomeObservation(
            "outcome_snapshot_q4", "2026-10-01T00:00:00Z", base.cohort_id, "2026-12-31",
            "opp_alpha_01", "won", "reason_product_fit", 700_000, "USD", ("ev_outcome_alpha",),
        )
        request = replace(base, prior_forecasts=(prior,), observed_outcomes=(outcome,))

        # When the forecast is evaluated.
        forecast = evaluate_sales_pipeline_review(request).forecast

        # Then calibration is explicitly unavailable rather than cross-cohort.
        self.assertIsNotNone(forecast)
        self.assertEqual(forecast["calibration"]["availability"], "unavailable")
        self.assertEqual(forecast["calibration"]["matched_opportunity_ids"], [])

    def test_calibration_uses_only_matching_prior_and_observed_outcomes(self) -> None:
        # Given one matching observed cohort and one unmatched outcome.
        base = review_input()
        prior = PriorForecastSnapshot(
            "prior_snapshot_q3", "2026-08-01T00:00:00Z", base.cohort_id, base.horizon_end,
            "opp_alpha_01", "commit", 8000, 700_000, "USD", ("ev_prior_alpha",),
        )
        matching = OutcomeObservation(
            "outcome_snapshot_q3", "2026-10-01T00:00:00Z", base.cohort_id, base.horizon_end,
            "opp_alpha_01", "won", "reason_product_fit", 650_000, "USD", ("ev_outcome_alpha",),
        )
        unmatched = replace(matching, opportunity_id="opp_unmatched_99", amount_minor=900_000)
        request = replace(base, prior_forecasts=(prior,), observed_outcomes=(matching, unmatched))

        # When the forecast is evaluated.
        forecast = evaluate_sales_pipeline_review(request).forecast

        # Then calibration reports only the matched observation and its exact error.
        self.assertIsNotNone(forecast)
        self.assertEqual(forecast["calibration"]["availability"], "available")
        self.assertEqual(forecast["calibration"]["matched_opportunity_ids"], ["opp_alpha_01"])
        self.assertEqual(forecast["calibration"]["observed_won_amount_minor"], 650_000)
        self.assertEqual(forecast["calibration"]["absolute_error_minor"], 50_000)
        self.assertEqual(validate_sales_pipeline_artifact(forecast), [])

    def test_forecast_validator_rejects_incomplete_nested_seller_state(self) -> None:
        # Given a forecast whose nested seller row lost its supplied category field.
        forecast = evaluate_sales_pipeline_review(review_input()).forecast
        self.assertIsNotNone(forecast)
        malformed = dict(forecast)
        malformed["seller_states"] = [dict(forecast["seller_states"][0])]
        malformed["seller_states"][0].pop("category_id")

        # When the public validator checks the nested contract.
        errors = validate_sales_pipeline_artifact(malformed)

        # Then the incomplete seller state is rejected.
        self.assertIn("sales_forecast_assessment seller_states rows are invalid", errors)

    def test_observed_conversion_basis_enables_mixed_currency_calculation(self) -> None:
        # Given an observed rational EUR-to-USD basis.
        base = review_input()
        request = replace(
            base,
            opportunities=(base.opportunities[0], replace(base.opportunities[1], currency="EUR")),
            conversion_bases=(CurrencyConversionBasis("EUR", "USD", 11, 10, "ev_fx_20260901", "2026-09-01T12:00:00Z"),),
        )

        # When the review is evaluated.
        evaluation = evaluate_sales_pipeline_review(request)

        # Then calculations use that basis and retain it in bounded scope metadata.
        self.assertEqual(evaluation.status, "READY")
        self.assertEqual(evaluation.health["concentration"]["total_amount_minor"], 1_030_000)
        self.assertEqual(evaluation.scope["conversion_bases"][0]["evidence_ref"], "ev_fx_20260901")


if __name__ == "__main__":
    _ = unittest.main()
