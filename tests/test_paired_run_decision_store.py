from __future__ import annotations

from dataclasses import replace
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from omh.coding.hermes_child_receipts import VerifiedHermesChildReceipt, load_hermes_child_receipt
from omh.quality.paired_run_decision import build_paired_run_decision, parse_paired_run_decision
from omh.quality.paired_run_decision_store import PairedRunStoreError, append_paired_run_decision, latest_paired_run_decision
from omh.quality.paired_run_model import ArmRole, ArmSpec, BehaviorVerdict, InfrastructureStatus, PairedRunRequest, PairedRunValidationError, RunResultInput, TaskSpec
from paired_run_support import paired_evaluation_binding, write_observed_receipt


def _request(
    identifier: str,
    supersedes: str | None,
    baseline: BehaviorVerdict = BehaviorVerdict.NOT_OBSERVED,
    receipt: VerifiedHermesChildReceipt | None = None,
) -> PairedRunRequest:
    status = InfrastructureStatus.OBSERVED if baseline != BehaviorVerdict.NOT_OBSERVED else InfrastructureStatus.NOT_OBSERVED
    return PairedRunRequest(
        identifier, supersedes,
        ArmSpec("base", "hermes", "model-a", ("skill-a",)),
        ArmSpec("variant", "hermes", "model-b", ("skill-b",)),
        (TaskSpec("task-1", "criteria-1", "a" * 64),),
        2,
        900,
        "rev-1",
        "2026-08-27T00:00:00Z",
        (
            RunResultInput("task-1", ArmRole.BASELINE, status, baseline, receipt),
            RunResultInput("task-1", ArmRole.VARIANT, InfrastructureStatus.NOT_OBSERVED, BehaviorVerdict.NOT_OBSERVED, None),
        ),
    )


class PairedRunDecisionStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = TemporaryDirectory(prefix="omh-paired-store-")
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "explicit" / "decisions.jsonl"
        home = (Path(self.temp.name) / ".omh").resolve()
        self.omh_home = home
        binding = paired_evaluation_binding(
            task_id="task-1", criteria_ref="criteria-1", input_digest="a" * 64,
            arm="baseline", executor="hermes", model="model-a",
            exposed_skills=("skill-a",), execution_revision="rev-1",
        )
        write_observed_receipt(home, "observed-run", evaluation_binding=binding)
        self.receipt = load_hermes_child_receipt(home, "observed-run")
        write_observed_receipt(home, "failed-run", "failed", evaluation_binding=binding)
        self.failed_receipt = load_hermes_child_receipt(home, "failed-run")

    def test_latest_arrival_wins_for_valid_monotonic_supersede(self) -> None:
        first = build_paired_run_decision(_request("d1", None))
        second = build_paired_run_decision(_request("d2", "d1"))
        append_paired_run_decision(self.path, first, self.omh_home)
        append_paired_run_decision(self.path, second, self.omh_home)
        self.assertEqual(latest_paired_run_decision(self.path, "d1", self.omh_home).decision_id, "d2")
        self.assertEqual(len(self.path.read_text().splitlines()), 2)

    def test_rejects_missing_link_pair_drift_downgrade_and_flip(self) -> None:
        first = build_paired_run_decision(
            _request("d1", None, BehaviorVerdict.PASS, self.receipt),
            self.omh_home,
        )
        append_paired_run_decision(self.path, first, self.omh_home)
        requests = (
            _request("d2", None, BehaviorVerdict.PASS, self.receipt),
            replace(_request("d2", "d1", BehaviorVerdict.PASS, self.receipt), variant=ArmSpec("changed", "hermes", "model-b", ("skill-b",))),
            _request("d2", "d1", BehaviorVerdict.NOT_OBSERVED),
            _request("d2", "d1", BehaviorVerdict.FAIL, self.receipt),
        )
        for request in requests:
            with self.subTest(request=request), self.assertRaises(PairedRunStoreError):
                append_paired_run_decision(
                    self.path,
                    build_paired_run_decision(request, self.omh_home),
                    self.omh_home,
                )

    def test_terminal_infrastructure_fact_cannot_flip_to_behavior(self) -> None:
        initial = _request("d1", None)
        infra_rows = (
            RunResultInput("task-1", ArmRole.BASELINE, InfrastructureStatus.INFRA_ERROR, BehaviorVerdict.NOT_OBSERVED, self.failed_receipt),
            initial.results[1],
        )
        append_paired_run_decision(
            self.path,
            build_paired_run_decision(
                replace(initial, results=infra_rows),
                self.omh_home,
            ),
            self.omh_home,
        )
        observed = build_paired_run_decision(
            _request("d2", "d1", BehaviorVerdict.PASS, self.receipt),
            self.omh_home,
        )
        with self.assertRaises(PairedRunStoreError):
            append_paired_run_decision(self.path, observed, self.omh_home)

    def test_rejects_execution_revision_drift_and_duplicate_candidate_id(self) -> None:
        first = build_paired_run_decision(_request("d1", None))
        append_paired_run_decision(self.path, first, self.omh_home)
        revision_drift = build_paired_run_decision(replace(
            _request("d2", "d1"), execution_revision="rev-2",
        ))
        duplicate_id = build_paired_run_decision(_request("d1", "d1"))
        for candidate in (revision_drift, duplicate_id):
            with self.subTest(candidate=candidate), self.assertRaises(PairedRunStoreError):
                append_paired_run_decision(self.path, candidate, self.omh_home)

    def test_rejects_invalid_candidate_before_first_append(self) -> None:
        candidate = replace(
            build_paired_run_decision(_request("d1", None)),
            max_total_runs=0,
        )
        with self.assertRaises(PairedRunStoreError):
            append_paired_run_decision(self.path, candidate, self.omh_home)

    def test_rejects_fork_unknown_predecessor_and_malformed_store(self) -> None:
        append_paired_run_decision(self.path, build_paired_run_decision(_request("d1", None)), self.omh_home)
        append_paired_run_decision(self.path, build_paired_run_decision(_request("d2", "d1")), self.omh_home)
        for identifier, predecessor in (("d3", "d1"), ("d4", "missing")):
            with self.subTest(identifier=identifier), self.assertRaises(PairedRunStoreError):
                append_paired_run_decision(self.path, build_paired_run_decision(_request(identifier, predecessor)), self.omh_home)
        self.path.write_text(self.path.read_text() + "{bad\n")
        with self.assertRaises(PairedRunStoreError):
            latest_paired_run_decision(self.path, "d1", self.omh_home)

    def test_rejects_every_forged_receipt_field_on_admission_and_read(self) -> None:
        observed = build_paired_run_decision(
            _request("d1", None, BehaviorVerdict.PASS, self.receipt),
            self.omh_home,
        )
        initial = _request("d1", None)
        infra = build_paired_run_decision(
            replace(
                initial,
                results=(
                    RunResultInput(
                        "task-1", ArmRole.BASELINE, InfrastructureStatus.INFRA_ERROR,
                        BehaviorVerdict.NOT_OBSERVED, self.failed_receipt,
                    ),
                    initial.results[1],
                ),
            ),
            self.omh_home,
        )
        cases = (
            ("ref", observed, {"receipt_ref": f"hermes-child:observed-run:{'0' * 64}"}),
            (
                "run",
                observed,
                {
                    "receipt_run_id": "invented-run",
                    "receipt_ref": f"hermes-child:invented-run:{'0' * 64}",
                },
            ),
            ("time", observed, {"receipt_observed_at": "2026-08-27T00:00:01Z"}),
            ("status", infra, {"receipt_status": "timed_out"}),
        )
        for label, source, changes in cases:
            with self.subTest(label=label):
                payload = json.loads(source.to_json())
                payload["results"][0].update(changes)
                with self.assertRaises(PairedRunValidationError):
                    parse_paired_run_decision(json.dumps(payload), self.omh_home)
                path = self.path.with_name(f"{label}.jsonl")
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(json.dumps(payload) + "\n", encoding="utf-8")
                with self.assertRaises(PairedRunStoreError):
                    latest_paired_run_decision(path, "d1", self.omh_home)

    def test_rejects_entire_invalid_existing_supersede_graph(self) -> None:
        records = {
            "self-link": (("d1", "d1"),),
            "unknown": (("d1", None), ("d2", "missing")),
            "fork": (("d1", None), ("d2", "d1"), ("d3", "d1")),
            "cycle": (("d1", "d2"), ("d2", "d1")),
        }
        for label, links in records.items():
            with self.subTest(label=label):
                path = self.path.with_name(f"{label}.jsonl")
                path.parent.mkdir(parents=True, exist_ok=True)
                hand_edited = [
                    json.loads(build_paired_run_decision(_request(identifier, predecessor)).to_json())
                    for identifier, predecessor in links
                ]
                path.write_text("".join(json.dumps(item) + "\n" for item in hand_edited), encoding="utf-8")
                with self.assertRaises(PairedRunStoreError):
                    latest_paired_run_decision(path, links[0][0], self.omh_home)


if __name__ == "__main__":
    unittest.main()
