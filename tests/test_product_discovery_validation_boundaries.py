from __future__ import annotations

from copy import deepcopy
import unittest

from _local_package import load_local_package
from test_product_discovery_validation import _ledger, _prepared_artifacts

load_local_package()
from omh.workflows.product_discovery_validation import (  # noqa: E402
    build_assumption_test_portfolio,
    build_discovery_evidence_ledger,
    evaluate_product_discovery,
    prepare_product_discovery,
    validate_product_discovery_artifact,
)


class ProductDiscoveryBoundaryTests(unittest.TestCase):
    def test_validator_returns_errors_for_incomplete_extra_wrong_and_nested_json(self) -> None:
        # Given: one valid instance of every versioned artifact.
        artifacts = _prepared_artifacts()
        receipt = evaluate_product_discovery(
            prepare_product_discovery(**artifacts, ledger=_ledger()), now="2030-01-01T02:00:00+00:00"
        )
        records = [artifacts["frame"], _ledger(), artifacts["plan"], artifacts["portfolio"], artifacts["gtm"], receipt]
        nested_fields = (
            ("alternative_refs", [1]),
            ("entries", [{}]),
            ("bias_control_refs", [{}]),
            ("assumptions", [{}]),
            ("learning_metric_refs", [{}]),
            ("eligible_evidence_refs", [{}]),
        )

        # When: untrusted JSON is incomplete, has extra keys, wrong scalar types, or malformed nested rows.
        for record, (nested_field, nested_value) in zip(records, nested_fields, strict=True):
            with self.subTest(schema=record["schema_version"], shape="incomplete"):
                errors = validate_product_discovery_artifact({"schema_version": record["schema_version"]})
                self.assertTrue(errors)
            with self.subTest(schema=record["schema_version"], shape="extra"):
                errors = validate_product_discovery_artifact({**record, "unexpected": "value"})
                self.assertTrue(errors)
            with self.subTest(schema=record["schema_version"], shape="wrong_type"):
                errors = validate_product_discovery_artifact({**record, "artifact_id": 1})
                self.assertTrue(errors)
            with self.subTest(schema=record["schema_version"], shape="nested"):
                errors = validate_product_discovery_artifact({**record, nested_field: nested_value})
                self.assertTrue(errors)

        # Then: every malformed JSON input has a list of errors, never a constructor exception.

    def test_evaluation_time_rejects_future_evidence_and_precommit_but_allows_completed_window_evidence(self) -> None:
        # Given: one completed test and a decision time before its observation.
        artifacts = _prepared_artifacts()
        future_observation = evaluate_product_discovery(
            prepare_product_discovery(**artifacts, ledger=_ledger()), now="2030-01-01T00:30:00+00:00"
        )
        future_assumption = dict(artifacts["portfolio"]["assumptions"][0])
        future_assumption.pop("priority")
        future_assumption["precommitted_at"] = "2030-01-01T03:00:00+00:00"
        artifacts["portfolio"] = build_assumption_test_portfolio(
            discovery_id=artifacts["frame"]["discovery_id"], assumptions=[future_assumption]
        )
        future_precommit = evaluate_product_discovery(
            prepare_product_discovery(**artifacts, ledger=_ledger(observed_at="2030-01-01T04:00:00+00:00")),
            now="2030-01-01T02:00:00+00:00",
        )

        # When: evaluation happens after a completed window with the original package.
        completed_after_deadline = evaluate_product_discovery(
            prepare_product_discovery(**_prepared_artifacts(), ledger=_ledger()), now="2030-01-03T00:00:00+00:00"
        )
        expired_unexecuted = evaluate_product_discovery(
            prepare_product_discovery(
                **_prepared_artifacts(), ledger=_ledger(observation_kind="source_pointer")
            ),
            now="2030-01-03T00:00:00+00:00",
        )

        # Then: future work cannot validate, while completed and unexecuted expired tests remain distinct.
        self.assertEqual(future_observation["decision"], "inconclusive")
        self.assertEqual(future_precommit["decision"], "inconclusive")
        self.assertEqual(completed_after_deadline["decision"], "persevere")
        self.assertEqual(expired_unexecuted["decision"], "inconclusive")
        self.assertIn("risk-test-deadline-expired", expired_unexecuted["residual_risk_refs"])

    def test_unresolved_precommitted_evidence_blocks_promotion_and_duplicate_sources_are_rejected(self) -> None:
        # Given: support plus a matching precommitted inconclusive observation.
        artifacts = _prepared_artifacts()
        support = _ledger()["entries"][0]
        unresolved = {**support, "evidence_id": "evidence-unresolved", "source_ref": "source-unresolved", "direction": "unresolved", "criterion_ref": "criterion-insufficient-sample"}
        ledger = build_discovery_evidence_ledger(
            discovery_id=artifacts["frame"]["discovery_id"], entries=[support, unresolved]
        )
        duplicate = {**support, "evidence_id": "evidence-copy"}

        # When: the same source is re-entered twice or an inconclusive outcome accompanies support.
        with self.assertRaisesRegex(ValueError, "duplicate"):
            build_discovery_evidence_ledger(
                discovery_id=artifacts["frame"]["discovery_id"], entries=[support, duplicate]
            )
        receipt = evaluate_product_discovery(
            prepare_product_discovery(**artifacts, ledger=ledger), now="2030-01-01T02:00:00+00:00"
        )

        # Then: neither copied sample counts nor unresolved criteria can promote the problem.
        self.assertEqual(receipt["decision"], "inconclusive")
        tampered = deepcopy(_ledger())
        tampered["entries"].append(duplicate)
        self.assertTrue(validate_product_discovery_artifact(tampered))

    def test_only_contradicted_hypotheses_select_kill_or_pivot(self) -> None:
        # Given: a kill-designated hypothesis with support and a pivot-designated hypothesis with contradiction.
        artifacts = _prepared_artifacts(failure_decision="kill")
        kill_assumption = dict(artifacts["portfolio"]["assumptions"][0])
        kill_assumption.pop("priority")
        pivot_assumption = {
            **kill_assumption,
            "assumption_id": "assumption-pivot",
            "test_id": "test-pivot",
            "failure_decision": "pivot",
        }
        artifacts["portfolio"] = build_assumption_test_portfolio(
            discovery_id=artifacts["frame"]["discovery_id"], assumptions=[kill_assumption, pivot_assumption]
        )
        support = _ledger()["entries"][0]
        contradiction = {
            **support,
            "evidence_id": "evidence-pivot",
            "test_id": "test-pivot",
            "source_ref": "source-pivot",
            "direction": "contradicts",
            "criterion_ref": "criterion-no-repeated-problem",
        }
        ledger = build_discovery_evidence_ledger(
            discovery_id=artifacts["frame"]["discovery_id"], entries=[support, contradiction]
        )

        # When: only the pivot-designated hypothesis is contradicted.
        receipt = evaluate_product_discovery(
            prepare_product_discovery(**artifacts, ledger=ledger), now="2030-01-01T02:00:00+00:00"
        )

        # Then: the receipt pivots and rejects only that hypothesis.
        self.assertEqual(receipt["decision"], "pivot")
        self.assertEqual(receipt["rejected_hypothesis_ids"], ["assumption-pivot"])


if __name__ == "__main__":
    unittest.main()
