from __future__ import annotations

from dataclasses import replace
import unittest

from tests._local_package import load_local_package
from tests.test_sales_pipeline_review import review_input

load_local_package()

from omh.workflows.sales_pipeline_review import (
    AccountFollowUp,
    OutcomeObservation,
    ProposedCrmCorrection,
    RenewalSignal,
    SalesPipelineContractError,
    SalesPipelineHandoffInput,
    evaluate_sales_pipeline_review,
    prepare_sales_pipeline_handoff,
    prepare_sales_pipeline_review,
    validate_sales_pipeline_artifact,
)


def observed_outcome(opportunity_id: str, outcome: str, reason_id: str, *, horizon: str = "2026-09-30") -> OutcomeObservation:
    return OutcomeObservation(
        source_ref="outcome_snapshot_q3",
        observed_at="2026-10-01T00:00:00Z",
        cohort_id="new_business_2026q3",
        horizon_end=horizon,
        opportunity_id=opportunity_id,
        outcome=outcome,
        reason_id=reason_id,
        amount_minor=650_000,
        currency="USD",
        evidence_refs=(f"ev_{opportunity_id}_{outcome}",),
    )


class SalesPipelineAnnexTests(unittest.TestCase):
    def test_outcome_annex_uses_only_bounded_cohort_and_reports_contradictions(self) -> None:
        # Given contradictory matching observations and one observation outside the review horizon.
        base = review_input()
        request = replace(
            base,
            observed_outcomes=(
                observed_outcome("opp_alpha_01", "won", "reason_product_fit"),
                observed_outcome("opp_alpha_01", "lost", "reason_budget"),
                observed_outcome("opp_outside_99", "won", "reason_timing", horizon="2026-12-31"),
            ),
        )

        # When the optional learning annex is evaluated.
        annex = evaluate_sales_pipeline_review(request).outcome_annex

        # Then unmatched evidence is excluded and conflicting observed states remain explicit.
        self.assertIsNotNone(annex)
        self.assertEqual(annex["outcome_counts"], [{"outcome": "lost", "count": 1}, {"outcome": "won", "count": 1}])
        self.assertEqual(annex["contradictions"][0]["opportunity_id"], "opp_alpha_01")
        self.assertEqual(annex["research_follow_ups"][0]["action_id"], "resolve_outcome_contradiction")
        self.assertFalse(annex["causal_claims_established"])

    def test_outcome_annex_is_absent_without_evidence_for_the_bounded_cohort(self) -> None:
        # Given only an observed outcome outside the declared review horizon.
        request = replace(
            review_input(),
            observed_outcomes=(observed_outcome("opp_outside_99", "won", "reason_timing", horizon="2026-12-31"),),
        )

        # When the review is evaluated.
        evaluation = evaluate_sales_pipeline_review(request)

        # Then the conditional annex is absent rather than presenting an empty learning result.
        self.assertIsNone(evaluation.outcome_annex)

    def test_outcome_annex_marks_missing_reason_as_evidence_gap(self) -> None:
        # Given a bounded observed loss without a supplied reason.
        request = replace(review_input(), observed_outcomes=(observed_outcome("opp_beta_02", "lost", ""),))

        # When the optional learning annex is evaluated.
        annex = evaluate_sales_pipeline_review(request).outcome_annex

        # Then the missing reason remains a research gap rather than an invented explanation.
        self.assertIsNotNone(annex)
        self.assertEqual(annex["evidence_gaps"], [{"opportunity_id": "opp_beta_02", "gap": "missing_reason"}])
        self.assertEqual(annex["reason_counts"], [])

    def test_renewal_annex_preserves_gaps_risks_contradictions_and_hypotheses(self) -> None:
        # Given two conflicting observed signal rows with incomplete renewal evidence.
        signal = RenewalSignal(
            "renewal_snapshot_q3", "acct_alpha_01", "owner_team_east", "2026-09-25",
            "observed_positive", "unknown", "observed_negative", "unknown", "observed_neutral",
            ("ev_renewal_alpha",), "expand_seats_alpha",
        )
        conflicting = replace(signal, health_state="observed_negative", evidence_refs=("ev_renewal_alpha_late",))
        request = replace(review_input(), renewal_signals=(signal, conflicting))

        # When the optional renewal annex is evaluated.
        annex = evaluate_sales_pipeline_review(request).renewal_annex

        # Then observed risk, unknown gaps, contradiction, expansion hypothesis, and owner stay separate.
        self.assertIsNotNone(annex)
        self.assertEqual(annex["open_risks"][0]["signal"], "support")
        self.assertIn({"account_id": "acct_alpha_01", "signal": "utilization"}, annex["evidence_gaps"])
        self.assertEqual(annex["contradictions"][0]["signal"], "health")
        self.assertEqual(annex["expansion_hypotheses"][0]["state"], "hypothesis_not_observed")
        self.assertEqual(annex["signals"][0]["owner_id"], "owner_team_east")

    def test_handoff_contains_complete_proposals_but_no_connector_claim(self) -> None:
        # Given a READY review, one owned follow-up, and one approval-pending correction.
        evaluation = evaluate_sales_pipeline_review(review_input())
        request = SalesPipelineHandoffInput(
            evaluation=evaluation,
            selected_follow_ups=(
                AccountFollowUp(
                    "opp_alpha_01", "owner_team_east", "2026-09-06", "confirm_buyer_review",
                    "buyer_review", ("ev_buyer_alpha",),
                ),
            ),
            proposed_corrections=(
                ProposedCrmCorrection(
                    "opp_alpha_01", "opportunity", "expected_close_on", "2026-09-20",
                    ("ev_next_alpha",), "owner_team_east", "pending",
                ),
            ),
            decision_owner_id="owner_sales_lead",
        )

        # When the public handoff API is called.
        handoff = prepare_sales_pipeline_handoff(request)

        # Then every correction field is present while mutation and delivery remain unavailable.
        self.assertEqual(
            set(handoff["proposed_crm_corrections"][0]),
            {"object_id", "object_type", "field_id", "proposed_value", "evidence_refs", "owner_id", "approval_state"},
        )
        self.assertEqual(handoff["connector"], {
            "availability": "unavailable", "mutation_observed": False, "delivery_observed": False,
        })
        self.assertEqual(handoff["selected_account_follow_ups"][0]["state"], "proposed_not_delivered")
        self.assertEqual(validate_sales_pipeline_artifact(handoff), [])

    def test_handoff_rejects_hold_reviews_and_incomplete_evidence(self) -> None:
        # Given a HOLD review and a READY review with a follow-up lacking evidence.
        hold = evaluate_sales_pipeline_review(replace(review_input(), amount_semantics=""))
        ready = evaluate_sales_pipeline_review(review_input())
        hold_request = SalesPipelineHandoffInput(hold, (), (), "owner_sales_lead")
        incomplete = SalesPipelineHandoffInput(
            ready,
            (AccountFollowUp("opp_alpha_01", "owner_team_east", "2026-09-06", "confirm_review", "buyer_review", ()),),
            (),
            "owner_sales_lead",
        )

        # When either invalid handoff is prepared, Then it is refused before any connector claim.
        with self.assertRaisesRegex(SalesPipelineContractError, "requires a READY"):
            prepare_sales_pipeline_handoff(hold_request)
        with self.assertRaisesRegex(SalesPipelineContractError, "requires due date and evidence"):
            prepare_sales_pipeline_handoff(incomplete)

    def test_all_six_artifact_validators_reject_missing_required_fields(self) -> None:
        # Given all six valid artifacts from one review and handoff.
        outcome = observed_outcome("opp_alpha_01", "won", "reason_product_fit")
        renewal = RenewalSignal(
            "renewal_snapshot_q3", "acct_alpha_01", "owner_team_east", "2026-09-25",
            "observed_positive", "observed_neutral", "observed_neutral", "observed_positive", "observed_neutral",
            ("ev_renewal_alpha",),
        )
        evaluation = evaluate_sales_pipeline_review(replace(review_input(), observed_outcomes=(outcome,), renewal_signals=(renewal,)))
        handoff = prepare_sales_pipeline_handoff(SalesPipelineHandoffInput(evaluation, (), (), "owner_sales_lead"))
        artifacts = (
            evaluation.scope, evaluation.health, evaluation.forecast, evaluation.outcome_annex, evaluation.renewal_annex, handoff,
        )

        for artifact in artifacts:
            with self.subTest(schema=artifact["schema_version"]):
                # When a required field is removed.
                malformed = dict(artifact)
                malformed.pop("claim_boundary")

                # Then the public dispatcher rejects that artifact shape.
                self.assertTrue(validate_sales_pipeline_artifact(malformed))

    def test_unsafe_identifiers_and_unobserved_buyer_commitments_hold(self) -> None:
        base = review_input()
        cases = (
            replace(base, source_ref="person@example.com"),
            replace(
                base,
                opportunities=(replace(base.opportunities[0], buyer_commitment_evidence_refs=()), base.opportunities[1]),
            ),
        )

        for request in cases:
            with self.subTest(source=request.source_ref):
                # Given unsafe or unsupported boundary metadata, When preparation runs.
                preparation = prepare_sales_pipeline_review(request)

                # Then the review is held before calculation.
                self.assertEqual(preparation.status, "HOLD")
                self.assertTrue(preparation.errors)


if __name__ == "__main__":
    _ = unittest.main()
