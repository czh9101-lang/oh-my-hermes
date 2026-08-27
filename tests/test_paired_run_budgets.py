from __future__ import annotations

from dataclasses import replace
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from omh.quality.paired_run_decision import (
    build_paired_run_decision,
    parse_paired_run_decision,
)
from omh.quality.paired_run_model import (
    ArmRole,
    ArmSpec,
    BehaviorVerdict,
    InfrastructureStatus,
    PairedRunRequest,
    PairedRunValidationError,
    RunResultInput,
    TaskSpec,
)
from paired_run_support import paired_evaluation_binding, write_observed_receipt
from test_paired_run_decision_contract import _INPUT_DIGEST, _receipt


class PairedRunBudgetTests(unittest.TestCase):
    def test_budget_counts_only_attempted_rows_across_full_matrix(self) -> None:
        with TemporaryDirectory(prefix="omh-budget-") as raw:
            home = (Path(raw) / ".omh").resolve()
            write_observed_receipt(
                home,
                "failed-run",
                "failed",
                evaluation_binding=paired_evaluation_binding(
                    task_id="task-a",
                    criteria_ref="criteria-a",
                    input_digest="c" * 64,
                    arm="baseline",
                    executor="hermes",
                    model="model-a",
                    exposed_skills=(),
                    execution_revision="rev-budget",
                ),
            )
            failed = _receipt(home, "failed-run")
            tasks = (
                TaskSpec("task-b", "criteria-b", "b" * 64),
                TaskSpec("task-a", "criteria-a", "c" * 64),
            )
            rows = (
                RunResultInput(
                    "task-b",
                    ArmRole.VARIANT,
                    InfrastructureStatus.NOT_OBSERVED,
                    BehaviorVerdict.NOT_OBSERVED,
                    None,
                ),
                RunResultInput(
                    "task-a",
                    ArmRole.BASELINE,
                    InfrastructureStatus.INFRA_ERROR,
                    BehaviorVerdict.NOT_OBSERVED,
                    failed,
                ),
                RunResultInput(
                    "task-b",
                    ArmRole.BASELINE,
                    InfrastructureStatus.NOT_OBSERVED,
                    BehaviorVerdict.NOT_OBSERVED,
                    None,
                ),
                RunResultInput(
                    "task-a",
                    ArmRole.VARIANT,
                    InfrastructureStatus.NOT_OBSERVED,
                    BehaviorVerdict.NOT_OBSERVED,
                    None,
                ),
            )
            request = PairedRunRequest(
                "budget-1",
                None,
                ArmSpec("base", "hermes", "model-a", ()),
                ArmSpec("variant", "hermes", "model-b", ("skill-b", "skill-a")),
                tasks,
                1,
                900,
                "rev-budget",
                "2026-08-27T00:00:00Z",
                rows,
            )
            record = build_paired_run_decision(request, home)
            parsed = parse_paired_run_decision(record.to_json(), home)
        self.assertEqual(len(parsed.results), 4)
        self.assertEqual(parsed.aggregate.baseline.infra_error, 1)
        self.assertEqual(parsed.aggregate.variant.not_observed, 2)

    def test_budget_rejects_attempts_over_cap_in_builder_and_parser(self) -> None:
        with TemporaryDirectory(prefix="omh-budget-cap-") as raw:
            home = (Path(raw) / ".omh").resolve()
            receipts = []
            for run_id, arm, model in (
                ("failed-1", "baseline", "model-a"),
                ("failed-2", "variant", "model-b"),
            ):
                write_observed_receipt(
                    home,
                    run_id,
                    "failed",
                    evaluation_binding=paired_evaluation_binding(
                        task_id="task-a",
                        criteria_ref="criteria-a",
                        input_digest=_INPUT_DIGEST,
                        arm=arm,
                        executor="hermes",
                        model=model,
                        exposed_skills=(),
                        execution_revision="rev-budget",
                    ),
                )
                receipts.append(_receipt(home, run_id))
            request = PairedRunRequest(
                "budget-2",
                None,
                ArmSpec("base", "hermes", "model-a", ()),
                ArmSpec("variant", "hermes", "model-b", ()),
                (TaskSpec("task-a", "criteria-a", _INPUT_DIGEST),),
                1,
                900,
                "rev-budget",
                "2026-08-27T00:00:00Z",
                (
                    RunResultInput(
                        "task-a",
                        ArmRole.BASELINE,
                        InfrastructureStatus.INFRA_ERROR,
                        BehaviorVerdict.NOT_OBSERVED,
                        receipts[0],
                    ),
                    RunResultInput(
                        "task-a",
                        ArmRole.VARIANT,
                        InfrastructureStatus.INFRA_ERROR,
                        BehaviorVerdict.NOT_OBSERVED,
                        receipts[1],
                    ),
                ),
            )
            with self.assertRaises(PairedRunValidationError):
                build_paired_run_decision(request)
            valid = build_paired_run_decision(
                replace(request, max_total_runs=2),
                home,
            )
            payload = json.loads(valid.to_json())
            payload["max_total_runs"] = 1
            with self.assertRaises(PairedRunValidationError):
                parse_paired_run_decision(json.dumps(payload))


if __name__ == "__main__":
    unittest.main()
