from __future__ import annotations

from copy import deepcopy
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from _local_package import load_local_package

load_local_package()
from omh.workflows.product_discovery_validation import (  # noqa: E402
    append_product_discovery_artifact,
    build_assumption_test_portfolio,
    build_customer_discovery_plan,
    build_discovery_decision_frame,
    build_discovery_evidence_ledger,
    build_initial_gtm_hypothesis,
    evaluate_product_discovery,
    prepare_product_discovery,
    product_brief_consumption,
    read_product_discovery_artifacts,
    validate_product_discovery_artifact,
)
from omh.system.paths import resolve_paths  # noqa: E402


DISCOVERY_ID = "discovery-0123456789abcdef"
DEADLINE = "2030-01-02T00:00:00+00:00"
PRECOMMITTED_AT = "2030-01-01T00:00:00+00:00"


def _prepared_artifacts(*, failure_decision: str = "pivot") -> dict[str, dict[str, object]]:
    # Given: the bounded decision frame and the precommitted customer test.
    frame = build_discovery_decision_frame(
        discovery_id=DISCOVERY_ID,
        problem_ref="problem-onboarding-dropoff",
        segment_ref="segment-new-teams",
        alternative_refs=["alternative-spreadsheet"],
        decision_owner_ref="owner-product",
        learning_budget_ref="budget-discovery-1",
        deadline_at=DEADLINE,
        kill_criteria_refs=["criterion-no-repeated-problem"],
    )
    plan = build_customer_discovery_plan(
        discovery_id=DISCOVERY_ID,
        participant_criteria_ref="participants-new-teams",
        consent_privacy_ref="consent-policy-1",
        bias_control_refs=["bias-past-behavior"],
        human_task_ref="human-recruitment-1",
    )
    portfolio = build_assumption_test_portfolio(
        discovery_id=DISCOVERY_ID,
        assumptions=[
            {
                "assumption_id": "assumption-repeated-dropoff",
                "category": "value",
                "decision_impact": 5,
                "evidence_gap": 5,
                "test_id": "test-problem-interviews",
                "smallest_test_ref": "test-past-behavior-interviews",
                "success_criterion_ref": "criterion-repeated-problem",
                "failure_criterion_ref": "criterion-no-repeated-problem",
                "inconclusive_criterion_ref": "criterion-insufficient-sample",
                "failure_decision": failure_decision,
                "scope_segment_ref": "segment-new-teams",
                "sample_target": 2,
                "deadline_at": DEADLINE,
                "cost_cap_ref": "cost-cap-1",
                "owner_ref": "owner-product",
                "required_evidence_classes": ["external_human"],
                "precommitted_at": PRECOMMITTED_AT,
            }
        ],
    )
    gtm = build_initial_gtm_hypothesis(
        discovery_id=DISCOVERY_ID,
        beachhead_segment_ref="segment-new-teams",
        buyer_ref="buyer-ops-lead",
        user_ref="user-operator",
        current_alternative_ref="alternative-spreadsheet",
        value_proposition_ref="value-reduce-manual-work",
        pricing_hypothesis_ref="pricing-time-saved",
        initial_channel_ref="channel-community",
        first_cohort_ref="cohort-five-teams",
        learning_metric_refs=["metric-repeated-problem"],
    )
    return {"frame": frame, "plan": plan, "portfolio": portfolio, "gtm": gtm}


def _ledger(
    *,
    source_class: str = "external_human",
    direction: str = "supports",
    reentry: str = "reentered",
    observed_at: str = "2030-01-01T01:00:00+00:00",
    representative: bool = True,
    test_id: str = "test-problem-interviews",
    observation_kind: str = "past_behavior",
    criterion_ref: str | None = None,
) -> dict[str, object]:
    criterion_by_direction = {
        "supports": "criterion-repeated-problem",
        "contradicts": "criterion-no-repeated-problem",
        "unresolved": "criterion-insufficient-sample",
    }
    return build_discovery_evidence_ledger(
        discovery_id=DISCOVERY_ID,
        entries=[
            {
                "evidence_id": "evidence-interviews-1",
                "test_id": test_id,
                "source_class": source_class,
                "source_ref": "source-interview-batch-1",
                "criterion_ref": criterion_ref or criterion_by_direction[direction],
                "observed_at": observed_at,
                "segment_ref": "segment-new-teams",
                "direction": direction,
                "observation_kind": observation_kind,
                "sample_count": 2,
                "representative": representative,
                "reentry": reentry,
                "confidence_limit": "bounded",
            }
        ],
    )


class ProductDiscoveryValidationTests(unittest.TestCase):
    def test_persevere_receipt_requires_precommitted_reentered_external_evidence(self) -> None:
        # Given: a valid discovery package with representative customer evidence.
        artifacts = _prepared_artifacts()
        ledger = _ledger()

        # When: the gate evaluates the complete package before its deadline.
        receipt = evaluate_product_discovery(
            prepare_product_discovery(**artifacts, ledger=ledger), now="2030-01-01T02:00:00+00:00"
        )

        # Then: only the validated problem may feed the compact product-brief input.
        self.assertEqual((receipt["decision"], receipt["problem_gate"]), ("persevere", "validated"))
        consumed = product_brief_consumption(receipt)
        self.assertEqual(consumed["problem_ref"], "problem-onboarding-dropoff")
        self.assertEqual(consumed["target_segment_ref"], "segment-new-teams")

    def test_invalid_artifacts_and_source_pointers_are_rejected(self) -> None:
        # Given: a valid frame carrying only opaque metadata references.
        frame = _prepared_artifacts()["frame"]
        malformed = deepcopy(frame)
        malformed["problem_ref"] = "https://private.example/problem"

        # When: an untrusted record is validated.
        errors = validate_product_discovery_artifact(malformed)

        # Then: the source pointer cannot carry a navigable or raw reference.
        self.assertTrue(any("opaque identifier" in error for error in errors))

    def test_synthetic_secondary_and_unrepresentative_evidence_cannot_validate_a_problem(self) -> None:
        # Given: a prepared package whose apparent support is not customer evidence.
        artifacts = _prepared_artifacts()
        for source_class, representative, observation_kind in (
            ("synthetic", True, "past_behavior"),
            ("secondary_research", True, "past_behavior"),
            ("external_human", False, "past_behavior"),
            ("external_human", True, "prototype_completion"),
        ):
            with self.subTest(
                source_class=source_class,
                representative=representative,
                observation_kind=observation_kind,
            ):
                # When: the source is evaluated as though it supported the hypothesis.
                receipt = evaluate_product_discovery(
                    prepare_product_discovery(
                        **artifacts,
                        ledger=_ledger(
                            source_class=source_class,
                            representative=representative,
                            observation_kind=observation_kind,
                        ),
                    ),
                    now="2030-01-01T02:00:00+00:00",
                )

                # Then: source class alone cannot promote the decision.
                self.assertEqual((receipt["decision"], receipt["problem_gate"]), ("inconclusive", "inconclusive"))

    def test_contradiction_and_missing_reentry_propagate_without_overwriting_history(self) -> None:
        # Given: a precommitted test with a customer contradiction and a missing re-entry.
        artifacts = _prepared_artifacts()
        contradiction = evaluate_product_discovery(
            prepare_product_discovery(**artifacts, ledger=_ledger(direction="contradicts")),
            now="2030-01-01T02:00:00+00:00",
        )
        no_reentry = evaluate_product_discovery(
            prepare_product_discovery(**artifacts, ledger=_ledger(reentry="not_reentered")),
            now="2030-01-01T02:00:00+00:00",
        )

        # When/Then: contradiction drives pivot history; unreentered work holds.
        self.assertEqual((contradiction["decision"], contradiction["problem_gate"]), ("pivot", "refuted"))
        self.assertEqual(contradiction["rejected_hypothesis_ids"], ["assumption-repeated-dropoff"])
        self.assertEqual((no_reentry["decision"], no_reentry["problem_gate"]), ("inconclusive", "inconclusive"))

    def test_precommitted_failure_can_kill_without_erasing_the_rejected_hypothesis(self) -> None:
        # Given: a test whose precommitted failure decision is kill.
        artifacts = _prepared_artifacts(failure_decision="kill")

        # When: representative re-entered customer evidence contradicts it.
        receipt = evaluate_product_discovery(
            prepare_product_discovery(**artifacts, ledger=_ledger(direction="contradicts")),
            now="2030-01-01T02:00:00+00:00",
        )

        # Then: the receipt records a refuted problem and retains the hypothesis identity.
        self.assertEqual((receipt["decision"], receipt["problem_gate"]), ("kill", "refuted"))
        self.assertEqual(receipt["rejected_hypothesis_ids"], ["assumption-repeated-dropoff"])

    def test_expired_or_unprecommitted_tests_remain_inconclusive(self) -> None:
        # Given: valid-looking evidence after the deadline or outside the portfolio.
        artifacts = _prepared_artifacts()
        expired = evaluate_product_discovery(
            prepare_product_discovery(**artifacts, ledger=_ledger(observed_at="2030-01-03T01:00:00+00:00")),
            now="2030-01-03T02:00:00+00:00",
        )
        unknown_test = _ledger(test_id="test-not-precommitted")
        wrong_criterion = _ledger(criterion_ref="criterion-uncommitted")

        # When: no in-scope accepted evidence exists.
        unprecommitted = evaluate_product_discovery(
            prepare_product_discovery(**artifacts, ledger=unknown_test), now="2030-01-01T02:00:00+00:00"
        )
        criterion_mismatch = evaluate_product_discovery(
            prepare_product_discovery(**artifacts, ledger=wrong_criterion), now="2030-01-01T02:00:00+00:00"
        )

        # Then: neither case can create a positive decision.
        self.assertEqual(expired["decision"], "inconclusive")
        self.assertEqual(unprecommitted["decision"], "inconclusive")
        self.assertEqual(criterion_mismatch["decision"], "inconclusive")

    def test_receipts_survive_append_only_reentry_and_invalid_receipts_cannot_feed_product_brief(self) -> None:
        # Given: a valid evaluated receipt and a fresh local store path.
        artifacts = _prepared_artifacts()
        receipt = evaluate_product_discovery(
            prepare_product_discovery(**artifacts, ledger=_ledger()), now="2030-01-01T02:00:00+00:00"
        )
        with TemporaryDirectory() as tmp:
            paths = resolve_paths(omh_home=Path(tmp) / "omh", hermes_home=Path(tmp) / "hermes")

            # When: the receipt is persisted and read through a new paths instance.
            append_product_discovery_artifact(paths, receipt)
            reread = read_product_discovery_artifacts(paths, discovery_id=DISCOVERY_ID)

            # Then: durable re-entry preserves the receipt, while an invalid gate has no handoff.
            self.assertEqual(reread, [receipt])
            self.assertEqual(product_brief_consumption({**receipt, "problem_gate": "inconclusive"}), {})


if __name__ == "__main__":
    unittest.main()
