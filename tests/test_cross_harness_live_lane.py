from __future__ import annotations

from contextlib import redirect_stdout
import importlib.util
import io
import json
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from _local_package import load_local_package

load_local_package()

from omh.quality.cross_harness_benchmark import (  # noqa: E402
    evaluate_submission,
    parse_corpus,
    score_submission,
)

ROOT = Path(__file__).resolve().parents[1]
LIVE_BASE = ROOT / "benchmarks" / "cross-harness" / "live"
CORPUS_PATH = ROOT / "benchmarks" / "cross-harness" / "v1" / "manifest.json"
PASSING_INPUT_PATH = ROOT / "benchmarks" / "cross-harness" / "v1" / "example-passing-submission.json"
sys.path.insert(0, str(LIVE_BASE / "lib"))

import controller  # noqa: E402
import receipt as receipt_module  # noqa: E402


def _fake_command_observation(**overrides: object) -> controller.Observation:
    fields: dict[str, object] = {
        "observation_id": "obs-command-binding",
        "kind": "command_binding",
        "cwd_class": "repository_root",
        "argv_digest": "0" * 64,
        "status": "completed",
        "observed_exit": 0,
        "observed_semantic_result": "validated",
        "duration_ms": 12,
    }
    fields.update(overrides)
    return controller.Observation(**fields)  # type: ignore[arg-type]


def _fake_dispatch_observation(**overrides: object) -> controller.Observation:
    fields: dict[str, object] = {
        "observation_id": "obs-hermes-child-dispatch",
        "kind": "hermes_child_dispatch",
        "cwd_class": "isolated_temporary",
        "argv_digest": "1" * 64,
        "status": "completed",
        "observed_exit": 0,
        "observed_semantic_result": "completed",
        "duration_ms": 4200,
        "tokens": 1024,
        "cost_usd": 0.0031,
        "tools": 3,
    }
    fields.update(overrides)
    return controller.Observation(**fields)  # type: ignore[arg-type]


def _run(**overrides: object) -> dict[str, object]:
    arguments: dict[str, object] = {
        "corpus_path": CORPUS_PATH,
        "repository_root": ROOT,
        "mode": "fake",
    }
    arguments.update(overrides)
    return controller.run(**arguments)  # type: ignore[arg-type]


def _evaluate(envelope: dict[str, object]) -> object:
    corpus = parse_corpus(envelope["corpus"])  # type: ignore[arg-type]
    return evaluate_submission(envelope["submission"], corpus)  # type: ignore[arg-type]


class LiveLaneDefaultsTests(unittest.TestCase):
    def test_doctor_reports_offline_default_and_untouched_v1_corpus(self) -> None:
        report = controller.doctor(CORPUS_PATH)
        self.assertEqual(report["default_mode"], "fake")
        self.assertIs(report["v1_corpus_mutated"], False)
        self.assertEqual(
            report["dispatch_boundary"], "omh coding hermes-child dispatch --confirm-dispatch"
        )
        on_disk = json.loads(CORPUS_PATH.read_text(encoding="utf-8"))
        self.assertEqual(report["corpus_digest"], on_disk["corpus_digest"])

    def test_emitted_envelope_carries_the_corpus_verbatim(self) -> None:
        envelope = _run()["envelope"]
        self.assertEqual(envelope["corpus"], json.loads(CORPUS_PATH.read_text(encoding="utf-8")))
        self.assertEqual(
            set(envelope), {"schema_version", "corpus", "submission"}
        )
        self.assertEqual(envelope["schema_version"], "cross_harness_benchmark_cli_input/v1")


class FakeModeTests(unittest.TestCase):
    def test_fake_mode_envelope_passes_the_real_v1_parser_and_evaluator(self) -> None:
        result = _run()
        report = _evaluate(result["envelope"])  # type: ignore[arg-type]
        statuses = {outcome.fixture_id: outcome.status for outcome in report.outcomes}
        self.assertEqual(len(statuses), 15)
        for fixture_id in (*controller.COMMAND_FIXTURES, *controller.DISPATCH_FIXTURES):
            self.assertEqual(statuses[fixture_id], "partial")

    def test_fake_mode_can_never_pass_a_fixture_or_certify(self) -> None:
        result = _run()
        corpus = parse_corpus(result["envelope"]["corpus"])  # type: ignore[index, arg-type]
        score = score_submission(result["envelope"]["submission"], corpus)  # type: ignore[index, arg-type]
        self.assertFalse(score.contract_certified)
        self.assertEqual(score.evidence_authenticity, "unverified_submission")
        self.assertFalse(score.execution_verified)
        report = _evaluate(result["envelope"])  # type: ignore[arg-type]
        self.assertNotIn("pass", {outcome.status for outcome in report.outcomes})

    def test_fake_mode_receipt_declares_the_fake_tier_and_no_observation(self) -> None:
        receipt = _run()["receipt"]
        self.assertEqual(receipt["evidence_authenticity"], "fake_adapter")
        self.assertEqual(receipt["controller_observed_fixture_ids"], [])
        self.assertEqual(receipt["observations"], [])
        self.assertEqual(
            sorted(receipt["simulated_fixture_ids"]),  # type: ignore[arg-type]
            sorted((*controller.COMMAND_FIXTURES, *controller.DISPATCH_FIXTURES)),
        )

    def test_fake_mode_starts_no_subprocess(self) -> None:
        with patch.object(controller.subprocess, "run") as spawn:
            _run()
        spawn.assert_not_called()

    def test_fake_mode_is_reproducible(self) -> None:
        first, second = _run(), _run()
        self.assertEqual(first["envelope"], second["envelope"])
        self.assertEqual(first["receipt"]["envelope_digest"], second["receipt"]["envelope_digest"])  # type: ignore[index]


class RefusalTests(unittest.TestCase):
    def test_dispatch_without_allow_paid_live_refuses(self) -> None:
        with self.assertRaises(controller.ControllerError) as caught:
            _run(mode="dispatch", model="m", provider="p")
        self.assertEqual(str(caught.exception), "paid_live_not_allowed")

    def test_dispatch_without_paid_budget_refuses(self) -> None:
        with self.assertRaises(controller.ControllerError) as caught:
            _run(mode="dispatch", model="m", provider="p", allow_paid_live=True, max_paid_calls=0)
        self.assertEqual(str(caught.exception), "paid_call_budget_exceeded")

    def test_dispatch_without_model_metadata_refuses(self) -> None:
        with self.assertRaises(controller.ControllerError) as caught:
            _run(mode="dispatch", allow_paid_live=True, max_paid_calls=1)
        self.assertEqual(str(caught.exception), "dispatch_requires_model_and_provider")

    def test_paid_flag_outside_dispatch_refuses(self) -> None:
        with self.assertRaises(controller.ControllerError) as caught:
            _run(mode="probe", allow_paid_live=True, max_paid_calls=1)
        self.assertEqual(str(caught.exception), "paid_live_flag_without_dispatch")

    def test_refusal_happens_before_any_subprocess(self) -> None:
        with patch.object(controller.subprocess, "run") as spawn:
            with self.assertRaises(controller.ControllerError):
                _run(mode="dispatch", model="m", provider="p")
        spawn.assert_not_called()

    def test_child_dispatch_requires_explicit_confirmation(self) -> None:
        with TemporaryDirectory() as workspace, patch.object(controller.subprocess, "run") as spawn:
            with self.assertRaises(controller.ControllerError):
                controller.execute_child_dispatch(
                    omh_executable="omh",
                    hermes_executable="hermes",
                    workspace=Path(workspace),
                    home_root=Path(workspace),
                    model="m",
                    provider="p",
                    reasoning="medium",
                    timeout=5,
                )
        spawn.assert_not_called()

    def test_invalid_timeout_refuses(self) -> None:
        with self.assertRaises(controller.ControllerError) as caught:
            _run(timeout=0)
        self.assertEqual(str(caught.exception), "invalid_timeout")


class DispatchBoundaryTests(unittest.TestCase):
    def test_dispatch_argv_uses_the_approved_explicit_boundary(self) -> None:
        argv = controller.child_dispatch_argv(
            "omh",
            hermes_executable="hermes",
            workspace=Path("/tmp/workspace"),
            model="qwen3-coder-next",
            provider="ultrafast",
            reasoning="high",
            parent_run_id="parent",
            run_id="child",
            timeout=120,
        )
        self.assertEqual(argv[:5], ["omh", "coding", "hermes-child", "dispatch", "--confirm-dispatch"])
        self.assertIn("--json", argv)
        self.assertNotIn(controller.DISPATCH_TASK, argv)

    def test_observed_run_reports_controller_tier_and_efficiency(self) -> None:
        with (
            patch.object(controller, "execute_command_binding", return_value=_fake_command_observation()),
            patch.object(controller, "execute_child_dispatch", return_value=_fake_dispatch_observation()),
        ):
            result = _run(mode="dispatch", model="m", provider="p", allow_paid_live=True, max_paid_calls=1)
        receipt = result["receipt"]
        self.assertEqual(receipt["evidence_authenticity"], "controller_observed")
        self.assertEqual(
            sorted(receipt["controller_observed_fixture_ids"]),  # type: ignore[arg-type]
            sorted((*controller.COMMAND_FIXTURES, *controller.DISPATCH_FIXTURES)),
        )
        self.assertEqual(receipt["efficiency"]["tokens"], 1024)  # type: ignore[index]
        self.assertEqual(receipt["efficiency"]["duration_ms"], 4212)  # type: ignore[index]
        self.assertEqual(result["paid_calls_launched"], 1)
        report = _evaluate(result["envelope"])  # type: ignore[arg-type]
        observed = {
            outcome.fixture_id: outcome.status
            for outcome in report.outcomes
            if outcome.fixture_id in set(receipt["controller_observed_fixture_ids"])  # type: ignore[arg-type]
        }
        self.assertEqual(set(observed.values()), {"pass"})

    def test_failed_dispatch_becomes_an_observed_failure_not_a_pass(self) -> None:
        failed = _fake_dispatch_observation(
            status="failed", observed_exit=1, observed_semantic_result=None, failure_code="nonzero_exit"
        )
        with (
            patch.object(controller, "execute_command_binding", return_value=_fake_command_observation()),
            patch.object(controller, "execute_child_dispatch", return_value=failed),
        ):
            result = _run(mode="dispatch", model="m", provider="p", allow_paid_live=True, max_paid_calls=1)
        self.assertFalse(result["ok"])
        report = _evaluate(result["envelope"])  # type: ignore[arg-type]
        statuses = {outcome.fixture_id: outcome.status for outcome in report.outcomes}
        for fixture_id in controller.DISPATCH_FIXTURES:
            self.assertEqual(statuses[fixture_id], "fail")

    def test_routing_observation_metrics_reject_an_unvalidated_payload(self) -> None:
        self.assertIsNone(controller._routing_observation_metrics("not json"))
        self.assertIsNone(controller._routing_observation_metrics(json.dumps({"schema_version": "wrong"})))


class BaseEnvelopeTests(unittest.TestCase):
    def test_carried_results_downgrade_the_tier_and_claim_no_observation(self) -> None:
        with patch.object(controller, "execute_command_binding", return_value=_fake_command_observation()):
            result = _run(mode="probe", base_path=PASSING_INPUT_PATH)
        receipt = result["receipt"]
        self.assertEqual(receipt["evidence_authenticity"], "mixed_controller_and_submitted")
        self.assertEqual(receipt["controller_observed_fixture_ids"], ["evidence-command-binding"])
        self.assertEqual(len(receipt["carried_fixture_ids"]), 14)  # type: ignore[arg-type]
        self.assertEqual(receipt["unsupported_fixture_ids"], [])
        for binding in receipt["fixture_bindings"]:  # type: ignore[attr-defined]
            if binding["provenance"] == "carried_from_base":
                self.assertEqual(binding["observation_ids"], [])

    def test_certified_carried_envelope_still_scores_as_unverified(self) -> None:
        with patch.object(controller, "execute_command_binding", return_value=_fake_command_observation()):
            result = _run(mode="probe", base_path=PASSING_INPUT_PATH)
        corpus = parse_corpus(result["envelope"]["corpus"])  # type: ignore[index, arg-type]
        score = score_submission(result["envelope"]["submission"], corpus)  # type: ignore[index, arg-type]
        self.assertTrue(score.contract_certified)
        self.assertEqual(score.evidence_authenticity, "unverified_submission")
        self.assertFalse(score.execution_verified)

    def test_base_from_a_foreign_corpus_is_refused(self) -> None:
        with TemporaryDirectory() as root:
            path = Path(root) / "base.json"
            payload = json.loads(PASSING_INPUT_PATH.read_text(encoding="utf-8"))
            payload["submission"]["corpus_digest"] = "f" * 64
            path.write_text(json.dumps(payload), encoding="utf-8")
            with self.assertRaises(controller.ControllerError) as caught:
                _run(base_path=path)
        self.assertEqual(str(caught.exception), "base_corpus_mismatch")


class ReceiptSchemaTests(unittest.TestCase):
    def test_emitted_receipt_validates(self) -> None:
        self.assertEqual(receipt_module.validate_receipt(_run()["receipt"]), ())

    def test_unknown_field_is_rejected(self) -> None:
        payload = dict(_run()["receipt"])  # type: ignore[arg-type]
        payload["extra"] = 1
        self.assertIn("invalid_receipt_shape", receipt_module.validate_receipt(payload))

    def test_fake_mode_may_not_claim_controller_observation(self) -> None:
        payload = dict(_run()["receipt"])  # type: ignore[arg-type]
        payload["controller_observed_fixture_ids"] = ["evidence-command-binding"]
        payload["simulated_fixture_ids"] = []
        payload["evidence_authenticity"] = "controller_observed"
        self.assertIn("fake_mode_claims_observation", receipt_module.validate_receipt(payload))

    def test_observed_tier_requires_an_observed_fixture(self) -> None:
        payload = dict(_run()["receipt"])  # type: ignore[arg-type]
        payload["evidence_authenticity"] = "mixed_controller_and_submitted"
        self.assertIn("authenticity_tier_overclaims", receipt_module.validate_receipt(payload))

    def test_carried_result_may_not_reference_an_observation(self) -> None:
        with patch.object(controller, "execute_command_binding", return_value=_fake_command_observation()):
            payload = dict(_run(mode="probe", base_path=PASSING_INPUT_PATH)["receipt"])  # type: ignore[arg-type]
        bindings = [dict(item) for item in payload["fixture_bindings"]]
        for binding in bindings:
            if binding["provenance"] == "carried_from_base":
                binding["observation_ids"] = ["obs-command-binding"]
                break
        payload["fixture_bindings"] = bindings
        self.assertIn("carried_result_claims_observation", receipt_module.validate_receipt(payload))

    def test_binding_must_reference_a_recorded_observation(self) -> None:
        with patch.object(controller, "execute_command_binding", return_value=_fake_command_observation()):
            payload = dict(_run(mode="probe")["receipt"])  # type: ignore[arg-type]
        payload["observations"] = []
        self.assertIn("unknown_binding_observation_id", receipt_module.validate_receipt(payload))

    def test_authenticity_tiers_are_ordered_weakest_first(self) -> None:
        self.assertEqual(receipt_module.AUTHENTICITY_TIERS[0], "fake_adapter")
        self.assertEqual(receipt_module.AUTHENTICITY_TIERS[-1], "controller_observed")
        self.assertIn("unverified_submission", receipt_module.AUTHENTICITY_TIERS)


class EfficiencyTests(unittest.TestCase):
    def test_missing_telemetry_stays_null_and_is_never_estimated(self) -> None:
        payloads = [
            _fake_command_observation().payload(),
            _fake_dispatch_observation(tokens=None, cost_usd=None).payload(),
        ]
        efficiency = receipt_module.aggregate_efficiency(payloads)
        self.assertIsNone(efficiency["tokens"])
        self.assertIsNone(efficiency["cost_usd"])
        self.assertEqual(efficiency["duration_ms"], 4212)
        self.assertEqual(efficiency["observations_reporting_tokens"], 0)
        self.assertFalse(efficiency["complete"])

    def test_partially_reported_telemetry_exposes_its_reporting_count(self) -> None:
        payloads = [
            _fake_command_observation().payload(),
            _fake_dispatch_observation().payload(),
        ]
        efficiency = receipt_module.aggregate_efficiency(payloads)
        self.assertEqual(efficiency["tokens"], 1024)
        self.assertEqual(efficiency["observations_total"], 2)
        self.assertEqual(efficiency["observations_reporting_tokens"], 1)
        self.assertFalse(efficiency["complete"])

    def test_efficiency_is_absent_from_the_scored_envelope(self) -> None:
        with (
            patch.object(controller, "execute_command_binding", return_value=_fake_command_observation()),
            patch.object(controller, "execute_child_dispatch", return_value=_fake_dispatch_observation()),
        ):
            result = _run(mode="dispatch", model="m", provider="p", allow_paid_live=True, max_paid_calls=1)
        serialized = json.dumps(result["envelope"])
        for marker in ("duration_ms", "tokens", "cost_usd", "efficiency"):
            self.assertNotIn(marker, serialized)


class BenchEntryPointTests(unittest.TestCase):
    @staticmethod
    def _bench() -> object:
        spec = importlib.util.spec_from_file_location("cross_harness_live_bench", LIVE_BASE / "bench.py")
        assert spec and spec.loader
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module

    def _invoke(self, argv: list[str]) -> tuple[int, dict[str, object]]:
        stream = io.StringIO()
        with redirect_stdout(stream):
            status = self._bench().main(argv)  # type: ignore[attr-defined]
        return status, json.loads(stream.getvalue())

    def test_fake_run_is_the_default_and_exits_zero(self) -> None:
        status, payload = self._invoke(["run"])
        self.assertEqual(status, 0)
        self.assertEqual(payload["mode"], "fake")
        self.assertEqual(payload["paid_calls_launched"], 0)
        self.assertEqual(payload["receipt"]["evidence_authenticity"], "fake_adapter")  # type: ignore[index]

    def test_dispatch_without_explicit_flags_exits_two_with_a_reason_code(self) -> None:
        status, payload = self._invoke(["run", "--mode", "dispatch", "--model", "m", "--provider", "p"])
        self.assertEqual(status, 2)
        self.assertEqual(payload["reason_code"], "paid_live_not_allowed")

    def test_written_envelope_scores_through_the_production_command(self) -> None:
        with TemporaryDirectory() as root:
            envelope_path = Path(root) / "envelope.json"
            status, _ = self._invoke(["run", "--envelope-output", str(envelope_path)])
            self.assertEqual(status, 0)
            envelope = json.loads(envelope_path.read_text(encoding="utf-8"))
        corpus = parse_corpus(envelope["corpus"])
        self.assertFalse(score_submission(envelope["submission"], corpus).contract_certified)


class PrivacyTests(unittest.TestCase):
    def test_run_payload_holds_no_prompt_body_or_absolute_path(self) -> None:
        with (
            patch.object(controller, "execute_command_binding", return_value=_fake_command_observation()),
            patch.object(controller, "execute_child_dispatch", return_value=_fake_dispatch_observation()),
        ):
            result = _run(mode="dispatch", model="m", provider="p", allow_paid_live=True, max_paid_calls=1)
        serialized = json.dumps(result)
        self.assertNotIn(controller.DISPATCH_TASK, serialized)
        self.assertNotIn(str(ROOT), serialized)
        self.assertNotIn("/Users/", serialized)


if __name__ == "__main__":
    unittest.main()
