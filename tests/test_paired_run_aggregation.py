from __future__ import annotations

from pathlib import Path
import random
from tempfile import TemporaryDirectory
import unittest

from omh.coding.hermes_child_receipts import load_hermes_child_receipt
from omh.quality.paired_run_decision import (
    aggregate_results,
    build_paired_run_decision,
)
from omh.quality.paired_run_model import (
    ArmRole,
    BehaviorVerdict,
    InfrastructureStatus,
    RunResultInput,
)
from paired_run_support import write_observed_receipt
from test_paired_run_decision_contract import _request


class PairedRunAggregationTests(unittest.TestCase):
    def test_per_arm_aggregate_carries_scope_and_comparable_count(self) -> None:
        with TemporaryDirectory(prefix="omh-aggregate-") as raw:
            home = (Path(raw) / ".omh").resolve()
            record = build_paired_run_decision(
                _request(
                    home,
                    (BehaviorVerdict.PASS, BehaviorVerdict.FAIL),
                ),
                home,
            )
        self.assertEqual(record.aggregate.baseline.observed_pass, 1)
        self.assertEqual(record.aggregate.variant.observed_fail, 1)
        self.assertEqual(record.aggregate.comparable_task_count, 1)
        self.assertEqual(record.aggregate.task_set_digest, record.task_set_digest)
        self.assertEqual(
            record.aggregate.baseline_exposure_digest,
            record.to_record()["baseline"]["exposure_digest"],
        )

    def test_all_pareto_outcomes_are_task_keyed_and_order_independent(self) -> None:
        cases = {
            "variant_dominates": (("t1", "fail", "pass"),),
            "baseline_dominates": (("t1", "pass", "fail"),),
            "tradeoff": (("t1", "pass", "fail"), ("t2", "fail", "pass")),
            "no_observed_difference": (
                ("t1", "pass", "pass"),
                ("t2", "fail", "fail"),
            ),
            "inconclusive": (("t1", "not_observed", "not_observed"),),
        }
        with TemporaryDirectory(prefix="omh-pareto-") as raw:
            home = (Path(raw) / ".omh").resolve()
            for expected, task_rows in cases.items():
                rows = []
                for task_id, baseline, variant in task_rows:
                    for role, verdict in (
                        (ArmRole.BASELINE, baseline),
                        (ArmRole.VARIANT, variant),
                    ):
                        status = (
                            InfrastructureStatus.OBSERVED
                            if verdict != "not_observed"
                            else InfrastructureStatus.NOT_OBSERVED
                        )
                        receipt = None
                        if status is InfrastructureStatus.OBSERVED:
                            run_id = f"{expected}-{task_id}-{role.value}"
                            write_observed_receipt(home, run_id)
                            receipt = load_hermes_child_receipt(home, run_id)
                        rows.append(
                            RunResultInput(
                                task_id,
                                role,
                                status,
                                BehaviorVerdict(verdict),
                                receipt,
                            )
                        )
                for seed in range(5):
                    shuffled = list(rows)
                    random.Random(seed).shuffle(shuffled)
                    with self.subTest(expected=expected, seed=seed):
                        self.assertEqual(
                            aggregate_results(tuple(shuffled)).outcome,
                            expected,
                        )


if __name__ == "__main__":
    unittest.main()
