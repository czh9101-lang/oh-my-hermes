"""Contract tests for compact decision receipt handoffs."""

from __future__ import annotations

from copy import deepcopy
import json
import unittest

from _local_package import load_local_package
from test_decision_prototypes import _observation, _proposal
from test_product_discovery_validation import _ledger, _prepared_artifacts

load_local_package()
from omh.workflows.decision_prototypes import observe_decision_prototype, prepare_decision_prototype  # noqa: E402
from omh.workflows.decision_receipt_handoffs import (  # noqa: E402
    DecisionReceiptHandoffError,
    build_decision_receipt_handoff,
)
from omh.workflows.hermes_context import build_product_brief_context  # noqa: E402
from omh.workflows.hermes_planning import build_hermes_plan_payload  # noqa: E402
from omh.workflows.product_discovery_validation import evaluate_product_discovery, prepare_product_discovery  # noqa: E402


def _observed_prototype():
    return observe_decision_prototype(prepare_decision_prototype(_proposal()), _observation())


def _discovery_receipt(
    *,
    direction: str = "supports",
    observed_at: str = "2030-01-01T01:00:00+00:00",
    reentry: str = "reentered",
):
    return evaluate_product_discovery(
        prepare_product_discovery(**_prepared_artifacts(), ledger=_ledger(direction=direction, observed_at=observed_at, reentry=reentry)),
        now="2030-01-01T02:00:00+00:00",
    )


class DecisionReceiptHandoffTests(unittest.TestCase):
    def test_given_an_observed_prototype_when_the_real_plan_builder_receives_it_then_it_carries_compact_resolved_context(self) -> None:
        # Given: one observed empirical context decision with a declared scratch command.
        prototype = _observed_prototype()

        # When: the real Hermes plan builder receives the original prototype artifact.
        payload = build_hermes_plan_payload("Plan the cache backend change.", decision_artifact=prototype)

        # Then: the plan carries only the compact decision context, never the prototype command or measurements.
        handoff = payload["decision_receipt_handoff"]
        self.assertEqual(handoff["target_workflow"], "ralplan")
        self.assertEqual(handoff["decision_state"], "resolved")
        self.assertEqual(handoff["decision"]["context_decision_ref"], "frontier-cache-choice")
        self.assertEqual(handoff["decision"]["supported_option"], "sqlite")
        self.assertEqual(handoff["production_authority"], "none")
        rendered = json.dumps(handoff, sort_keys=True)
        self.assertNotIn("uv run python probe.py", rendered)
        self.assertNotIn("measurements", rendered)
        self.assertNotIn("transcript", rendered)

    def test_given_unobserved_timeout_or_invalid_prototypes_when_handed_to_planning_then_none_becomes_a_resolved_choice(self) -> None:
        # Given: unavailable, timed-out, and malformed prototype artifacts.
        unobserved = prepare_decision_prototype(
            _proposal(executor={"profile": "generic", "available": False, "capability_limits": ["no_browser"]})
        )
        timed_out = observe_decision_prototype(
            prepare_decision_prototype(_proposal()),
            _observation(
                state="timeout",
                supported_option="",
                rejected_options=[],
                decision="keep",
                cleanup_status="failed",
            ),
        )

        # When: the planner asks for their compact handoffs.
        unobserved_handoff = build_decision_receipt_handoff(unobserved, target_workflow="ralplan")
        timeout_handoff = build_decision_receipt_handoff(timed_out, target_workflow="ralplan")

        # Then: evidence limits stay unresolved and malformed input is rejected at the original-artifact boundary.
        self.assertEqual(unobserved_handoff["decision_state"], "unresolved")
        self.assertEqual(timeout_handoff["decision_state"], "unresolved")
        self.assertEqual(unobserved_handoff["decision"]["supported_option"], "")
        self.assertEqual(timeout_handoff["decision"]["supported_option"], "")
        with self.assertRaises(DecisionReceiptHandoffError):
            build_decision_receipt_handoff({}, target_workflow="ralplan")

    def test_given_a_callers_accepted_string_when_planning_consumes_a_prototype_then_it_does_not_grant_production_authority(self) -> None:
        # Given: a valid source artifact whose separate promotion metadata names an accepted plan.
        prototype = _observed_prototype()
        prototype["promotion"] = {
            "production_code_permitted": True,
            "accepted_plan_ref": "plan-accepted-by-caller",
            "accepted_plan_status": "accepted",
            "implementation_handoff_ref": "implementation-handoff-1",
        }

        # When: a caller supplies that artifact to plan construction.
        payload = build_hermes_plan_payload("Plan the cache backend change.", decision_artifact=prototype)

        # Then: it remains prepared decision context rather than production approval.
        handoff = payload["decision_receipt_handoff"]
        self.assertEqual(handoff["prepared_status"], "prepared_context")
        self.assertEqual(handoff["production_authority"], "none")

    def test_given_discovery_receipts_when_product_brief_context_is_built_then_only_validated_persevere_is_available(self) -> None:
        # Given: validated, refuted, expired, and malformed discovery receipts.
        persevered = _discovery_receipt()
        refuted = _discovery_receipt(direction="contradicts")
        expired = _discovery_receipt(observed_at="2030-01-03T01:00:00+00:00")
        inconclusive = _discovery_receipt(reentry="not_reentered")

        # When: the product-brief context consumer receives each original receipt.
        validated = build_product_brief_context(persevered)
        refuted_context = build_product_brief_context(refuted)
        expired_context = build_product_brief_context(expired)
        inconclusive_context = build_product_brief_context(inconclusive)

        # Then: only validated persevere exposes the bounded product input; others remain blocked.
        self.assertEqual(validated["decision_state"], "validated")
        self.assertEqual(validated["product_brief_context"]["problem_ref"], "problem-onboarding-dropoff")
        self.assertEqual(validated["product_brief_context"]["target_segment_ref"], "segment-new-teams")
        self.assertEqual(refuted_context["decision_state"], "blocked")
        self.assertEqual(expired_context["decision_state"], "blocked")
        self.assertEqual(inconclusive_context["decision_state"], "blocked")
        self.assertEqual(refuted_context["product_brief_context"], {})
        self.assertEqual(expired_context["product_brief_context"], {})
        self.assertEqual(inconclusive_context["product_brief_context"], {})
        with self.assertRaises(DecisionReceiptHandoffError):
            build_product_brief_context(deepcopy({"schema_version": "discovery_decision_receipt/v1"}))


if __name__ == "__main__":
    unittest.main()
