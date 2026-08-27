from __future__ import annotations

from dataclasses import replace
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from omh.coding.hermes_child_receipts import (
    hermes_child_run_dir,
    load_hermes_child_receipt,
)
from omh.quality.paired_run_decision import (
    build_paired_run_decision,
    parse_paired_run_decision,
    validate_paired_run_decision,
)
from omh.quality.paired_run_decision_store import (
    PairedRunStoreError,
    append_paired_run_decision,
    latest_paired_run_decision,
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
from paired_run_support import resign_observation, write_observed_receipt

_INPUT_DIGEST = "a" * 64
_BASE_EXPOSURE = "4f53cda18c2baa0c0354bb5f9a3ecbe5ed12ab4d8e11ba873c2f11161202b945"
_VARIANT_EXPOSURE = "7fd7311edc4eda848725b186236f9cb3da90c26c92460ebca679600cb7403273"


def _binding(role: str, model: str, exposure: str) -> dict[str, str | int]:
    return {
        "task_id": "task-1",
        "acceptance_criteria_ref": "criteria-1",
        "input_digest": _INPUT_DIGEST,
        "arm": role,
        "executor": "hermes",
        "model": model,
        "exposure_digest": exposure,
        "execution_revision": "rev-1",
        "timeout_seconds": 900,
    }


def _request(home: Path) -> PairedRunRequest:
    receipts = []
    for run_id, role, model, exposure in (
        ("base-run", "baseline", "model-a", _BASE_EXPOSURE),
        ("variant-run", "variant", "model-b", _VARIANT_EXPOSURE),
    ):
        write_observed_receipt(
            home,
            run_id,
            evaluation_binding=_binding(role, model, exposure),
        )
        receipts.append(load_hermes_child_receipt(home, run_id))
    return PairedRunRequest(
        "decision-1",
        None,
        ArmSpec("base", "hermes", "model-a", ()),
        ArmSpec("variant", "hermes", "model-b", ("skill-a", "skill-b")),
        (TaskSpec("task-1", "criteria-1", _INPUT_DIGEST),),
        2,
        900,
        "rev-1",
        "2026-08-27T00:01:00Z",
        (
            RunResultInput("task-1", ArmRole.BASELINE, InfrastructureStatus.OBSERVED, BehaviorVerdict.PASS, receipts[0]),
            RunResultInput("task-1", ArmRole.VARIANT, InfrastructureStatus.OBSERVED, BehaviorVerdict.FAIL, receipts[1]),
        ),
    )


class PairedRunReceiptBindingTests(unittest.TestCase):
    def test_two_distinct_matching_receipts_round_trip(self) -> None:
        with TemporaryDirectory(prefix="omh-binding-positive-") as raw:
            home = (Path(raw) / ".omh").resolve()
            decision = build_paired_run_decision(_request(home), home)
            parsed = parse_paired_run_decision(decision.to_json(), home)
        self.assertEqual(parsed.outcome, "baseline_dominates")

    def test_one_receipt_cannot_back_both_arms_or_opposite_verdicts(self) -> None:
        with TemporaryDirectory(prefix="omh-binding-reuse-") as raw:
            home = (Path(raw) / ".omh").resolve()
            request = _request(home)
            reused = replace(
                request,
                results=(request.results[0], replace(request.results[1], receipt=request.results[0].receipt)),
            )
            with self.assertRaises(PairedRunValidationError):
                build_paired_run_decision(reused)

    def test_builder_reloads_persisted_receipt_before_authorizing_row(self) -> None:
        with TemporaryDirectory(prefix="omh-binding-builder-reload-") as raw:
            home = (Path(raw) / ".omh").resolve()
            request = _request(home)
            with self.assertRaises(PairedRunValidationError):
                build_paired_run_decision(request)
            receipt = request.results[0].receipt
            self.assertIsNotNone(receipt)
            forged = replace(
                receipt,
                run_id="invented-run",
                receipt_ref=f"hermes-child:invented-run:{'0' * 64}",
            )
            rows = (
                replace(request.results[0], receipt=forged),
                request.results[1],
            )
            with self.assertRaises(PairedRunValidationError):
                build_paired_run_decision(
                    replace(request, results=rows),
                    receipt_context=home,
                )
            run_dir = hermes_child_run_dir(
                home,
                receipt.run_id,
                create_root=False,
            )
            observation_path = run_dir / "observation.json"
            payload = json.loads(observation_path.read_text(encoding="utf-8"))
            payload["evaluation_binding"] = None
            observation_path.write_text(json.dumps(payload), encoding="utf-8")
            resign_observation(run_dir)
            with self.assertRaises(PairedRunValidationError):
                build_paired_run_decision(request, receipt_context=home)

    def test_builder_rejects_missing_or_wrong_binding_dimensions(self) -> None:
        mutations = {
            "task_id": "other-task",
            "acceptance_criteria_ref": "other-criteria",
            "input_digest": "b" * 64,
            "arm": "variant",
            "executor": "other-executor",
            "model": "other-model",
            "exposure_digest": "c" * 64,
            "execution_revision": "rev-2",
            "timeout_seconds": 899,
        }
        with TemporaryDirectory(prefix="omh-binding-builder-") as raw:
            home = (Path(raw) / ".omh").resolve()
            request = _request(home)
            for field, value in mutations.items():
                with self.subTest(field=field):
                    run_id = f"wrong-{field}"
                    binding = _binding("baseline", "model-a", _BASE_EXPOSURE)
                    binding[field] = value
                    write_observed_receipt(home, run_id, evaluation_binding=binding)
                    receipt = load_hermes_child_receipt(home, run_id)
                    rows = (replace(request.results[0], receipt=receipt), request.results[1])
                    with self.assertRaises(PairedRunValidationError):
                        build_paired_run_decision(replace(request, results=rows))
            write_observed_receipt(home, "missing-binding")
            receipt = load_hermes_child_receipt(home, "missing-binding")
            with self.assertRaises(PairedRunValidationError):
                build_paired_run_decision(replace(
                    request,
                    results=(replace(request.results[0], receipt=receipt), request.results[1]),
                ))

    def test_public_surfaces_reject_reused_receipt_reference(self) -> None:
        with TemporaryDirectory(prefix="omh-binding-public-reuse-") as raw:
            root = Path(raw)
            home = (root / ".omh").resolve()
            decision = build_paired_run_decision(_request(home), home)
            reused_row = replace(
                decision.results[1],
                receipt_ref=decision.results[0].receipt_ref,
                receipt_run_id=decision.results[0].receipt_run_id,
                receipt_observed_at=decision.results[0].receipt_observed_at,
                receipt_status=decision.results[0].receipt_status,
            )
            reused = replace(decision, results=(decision.results[0], reused_row))
            with self.assertRaises(PairedRunValidationError):
                parse_paired_run_decision(reused.to_json(), home)
            self.assertTrue(validate_paired_run_decision(reused.to_json(), home))
            path = root / "reused.jsonl"
            with self.assertRaises(PairedRunStoreError):
                append_paired_run_decision(path, reused, home)
            path.write_text(reused.to_json() + "\n", encoding="utf-8")
            with self.assertRaises(PairedRunStoreError):
                latest_paired_run_decision(path, "decision-1", home)

    def test_public_parse_store_and_latest_reject_binding_drift(self) -> None:
        with TemporaryDirectory(prefix="omh-binding-admission-") as raw:
            root = Path(raw)
            home = (root / ".omh").resolve()
            decision = build_paired_run_decision(_request(home), home)
            run_dir = home / "coding" / "hermes-child" / "base-run"
            observation = json.loads((run_dir / "observation.json").read_text(encoding="utf-8"))
            for label in ("wrong", "missing"):
                candidate = json.loads(json.dumps(observation))
                if label == "wrong":
                    candidate["evaluation_binding"]["model"] = "wrong-model"
                else:
                    candidate.pop("evaluation_binding")
                (run_dir / "observation.json").write_text(
                    json.dumps(candidate), encoding="utf-8"
                )
                resign_observation(run_dir)
                with self.subTest(label=label):
                    with self.assertRaises(PairedRunValidationError):
                        parse_paired_run_decision(decision.to_json(), home)
                    self.assertTrue(validate_paired_run_decision(decision.to_json(), home))
                    path = root / f"{label}.jsonl"
                    with self.assertRaises(PairedRunStoreError):
                        append_paired_run_decision(path, decision, home)
                    path.write_text(decision.to_json() + "\n", encoding="utf-8")
                    with self.assertRaises(PairedRunStoreError):
                        latest_paired_run_decision(path, "decision-1", home)


if __name__ == "__main__":
    unittest.main()
