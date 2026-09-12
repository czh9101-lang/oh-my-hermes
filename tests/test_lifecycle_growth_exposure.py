"""Original #1399 audience, exposure and contact decisions through real APIs/CLI."""
from __future__ import annotations

from copy import deepcopy
from collections.abc import Callable
import json
import os
from pathlib import Path
import subprocess
import sys
from tempfile import TemporaryDirectory
from typing import TypeGuard
import unittest

from _local_package import load_local_package

load_local_package()
from omh.workflows.lifecycle_growth_contracts import (
    evaluate_lifecycle_growth, prepare_lifecycle_growth, readout_lifecycle_growth,
    validate_lifecycle_growth_artifact,
)
from omh.workflows.lifecycle_growth_values import CLAIM_BOUNDARY, PREPARED_STATUS

decode_json: Callable[[str], object] = json.loads


def is_record(value: object) -> TypeGuard[dict[str, object]]:
    return isinstance(value, dict)


def record(value: object) -> dict[str, object]:
    assert is_record(value)
    return value


def launch_artifacts() -> dict[str, dict[str, object]]:
    import test_lifecycle_growth_contracts as contracts

    class Fixture(contracts.LifecycleGrowthContractTests):
        def artifacts(self) -> dict[str, dict[str, object]]:
            return self._launch_artifacts()
    return Fixture().artifacts()


def exposure_inputs() -> dict[str, dict[str, object]]:
    # Existing fixtures are data only; importing their module does not collect tests.
    import test_lifecycle_growth_readiness as readiness

    class Fixture(readiness.LifecycleGrowthReadinessTests):
        def inputs(self) -> tuple[dict[str, object], dict[str, object]]:
            return self._experiment(), self._readout(delivered_count=8, displayed_count=8)
    artifacts = launch_artifacts()
    experiment, readout = Fixture().inputs()
    evidence: dict[str, object] = {
        "schema_version": "lifecycle_growth_exposure_evidence/v1", "status": PREPARED_STATUS,
        "claim_boundary": CLAIM_BOUNDARY, "lifecycle_growth_id": experiment["lifecycle_growth_id"],
        "audience": artifacts["audience"], "safety": artifacts["safety"],
        "observation_window_ref": "window_activation_q3",
        **{field: experiment[field] for field in ("assignment_unit", "exposure_unit", "assignment_event_ref", "actual_exposure_event_ref")},
        "eligible_count": 10, "assigned_count": 8, "repeated_contact_count": 0,
        "eligibility_evidence_refs": ["eligible_accounts"], "exclusion_evidence_refs": ["checked_exclusions"],
        "assignment_evidence_refs": ["observed_assignment"],
        "population_reconciliation_state": "reconciled", "population_evidence_refs": ["deduplicated_window_counts"],
        "contact_pressure_state": "within_budget", "contact_evidence_refs": ["checked_contact_window"],
        "overlap_state": "absent", "overlapping_experiment_refs": [], "overlap_evidence_refs": ["checked_concurrent_experiments"],
        "analysis_run_ref": "analysis_run_activation_q3_01",
        "channel_refs": ["surface_in_app"],
        "channels": [{"channel_ref": "surface_in_app", "reachability_state": "reachable",
                      "attempted_count": 8, "delivered_count": 8, "reached_count": 8,
                      "failed_count": 0, "unresolved_count": 0,
                      "failure_reason_refs": [], "evidence_refs": ["observed_display_receipts"]}],
    }
    return {"experiment": experiment, "readout": readout, "exposure_evidence": evidence}


def evaluate(payload: dict[str, dict[str, object]]) -> dict[str, object]:
    from _lifecycle_configuration import bind
    from _lifecycle_metrics import cover
    observed = cover(bind(payload))
    return evaluate_lifecycle_growth(payload["experiment"], payload["readout"], exposure_evidence=payload["exposure_evidence"],
        audience_review=observed["audience_review"], configuration_binding=observed["configuration_binding"],
        metric_coverage=observed["metric_coverage"])


class LifecycleGrowthExposureTests(unittest.TestCase):
    def test_complete_observed_populations_allow_expansion(self) -> None:
        payload = exposure_inputs()
        result = evaluate(payload)
        self.assertEqual((result["interpretation_state"], result["disposition"], result["blocked"]), ("READY", "ship", False))
        self.assertEqual(result["populations"], {"eligible": 10, "assigned": 8, "attempted": 8, "reached": 8, "converted": 1})

    def test_each_missing_observation_holds_instead_of_inventing_zero(self) -> None:
        cases: tuple[tuple[str, object], ...] = (("eligible_count", None), ("assigned_count", None), ("repeated_contact_count", None),
                               ("eligibility_evidence_refs", []), ("exclusion_evidence_refs", []),
                               ("assignment_evidence_refs", []), ("contact_evidence_refs", []),
                               ("overlap_evidence_refs", []), ("population_evidence_refs", []), ("channels", []))
        for field, missing in cases:
            with self.subTest(field=field):
                payload = exposure_inputs()
                payload["exposure_evidence"][field] = missing
                result = evaluate(payload)
                self.assertEqual(result["interpretation_state"], "HOLD")
                self.assertTrue(result["evidence_reason_codes"])

    def test_unknown_audience_contact_and_overlap_hold(self) -> None:
        for field, value in (("contact_pressure_state", "unknown"), ("contact_pressure_state", "exceeded"),
                             ("overlap_state", "unknown"), ("overlap_state", "detected"),
                             ("overlapping_experiment_refs", ["other_experiment"]),
                             ("population_reconciliation_state", "unknown")):
            with self.subTest(field=field, value=value):
                payload = exposure_inputs()
                payload["exposure_evidence"][field] = value
                self.assertEqual(evaluate(payload)["interpretation_state"], "HOLD")

    def test_policy_unknowns_cannot_be_bypassed_by_aggregate_metrics(self) -> None:
        for name, field, value in (("audience", "audience_identity_state", "unknown"),
                                   ("safety", "consent_state", "unknown"), ("safety", "frequency_state", "unknown")):
            with self.subTest(field=field):
                payload = exposure_inputs()
                record(payload["exposure_evidence"][name])[field] = value
                self.assertEqual(evaluate(payload)["interpretation_state"], "HOLD")

    def test_population_and_assignment_identity_mismatch_hold(self) -> None:
        for field, value in (("assigned_count", 7), ("assigned_count", 11), ("eligible_count", 11),
                             ("assignment_event_ref", "foreign_assignment"), ("actual_exposure_event_ref", "foreign_display"),
                             ("assignment_unit", "device"), ("analysis_run_ref", "foreign_run"), ("lifecycle_growth_id", "foreign")):
            with self.subTest(field=field):
                payload = exposure_inputs()
                payload["exposure_evidence"][field] = value
                self.assertEqual(evaluate(payload)["interpretation_state"], "HOLD")

    def test_channel_partial_delivery_requires_review_and_preserves_failures(self) -> None:
        payload = exposure_inputs()
        payload["readout"].update(delivered_count=7, displayed_count=6)
        channels = [{"channel_ref": "surface_in_app", "reachability_state": "reachable",
                     "attempted_count": 8, "delivered_count": 7, "reached_count": 6,
                     "failed_count": 1, "unresolved_count": 0,
                     "failure_reason_refs": ["unreachable_endpoint"], "evidence_refs": ["partial_receipts"]}]
        payload["exposure_evidence"]["channels"] = channels
        result = evaluate(payload)
        self.assertEqual(result["disposition"], "review")
        self.assertEqual(result["channels"], channels)

    def test_aggregate_partial_reach_cannot_hide_behind_reconciled_channel_claims(self) -> None:
        payload = exposure_inputs()
        payload["readout"].update(delivered_count=7, displayed_count=6)
        payload["exposure_evidence"]["channel_refs"] = ["in_app", "email"]
        payload["exposure_evidence"]["channels"] = [
            {"channel_ref": name, "reachability_state": "reachable", "attempted_count": 4,
             "delivered_count": 4, "reached_count": 4, "failed_count": 0, "unresolved_count": 0,
             "failure_reason_refs": [], "evidence_refs": ["receipts"]} for name in ("in_app", "email")]
        self.assertEqual(evaluate(payload)["disposition"], "review")

    def test_unknown_unreachable_duplicate_and_inconsistent_channels_hold(self) -> None:
        cases: tuple[dict[str, object], ...] = ({"reachability_state": "unreachable"}, {"reachability_state": "unknown"},
                        {"evidence_refs": []}, {"attempted_count": None}, {"failed_count": 1},
                        {"channel_ref": "undeclared_channel"}, {"reached_count": 9})
        for changes in cases:
            with self.subTest(changes=changes):
                payload = exposure_inputs()
                payload["exposure_evidence"]["channels"] = [{
                    "channel_ref": "surface_in_app", "reachability_state": "reachable", "attempted_count": 8,
                    "delivered_count": 8, "reached_count": 8, "failed_count": 0, "unresolved_count": 0,
                    "failure_reason_refs": [], "evidence_refs": ["receipts"], **changes}]
                self.assertEqual(evaluate(payload)["interpretation_state"], "HOLD")
        payload = exposure_inputs()
        payload["exposure_evidence"]["channel_refs"] = ["surface_in_app", "surface_in_app"]
        self.assertEqual(evaluate(payload)["interpretation_state"], "HOLD")

    def test_sample_mismatch_and_duplicate_exposure_still_hold(self) -> None:
        for field, value in (("sample_ratio_state", "mismatch"), ("cross_exposure_state", "detected"), ("overlap_state", "detected")):
            payload = exposure_inputs()
            payload["readout"][field] = value
            payload["readout"]["disposition"] = "review"
            self.assertEqual(evaluate(payload)["disposition"], "review")

    def test_justified_rollback_survives_missing_evidence_context_and_short_runtime(self) -> None:
        payload = exposure_inputs()
        payload["readout"].update(guardrail_state="failed", disposition="rollback", runtime_days_observed=1)
        result = evaluate_lifecycle_growth(payload["experiment"], payload["readout"], evaluation_context={
            "experiment_reference_state": "deleted", "baseline_exposure_state": "absent"})
        self.assertEqual(result["disposition"], "rollback")
        self.assertEqual(result["interpretation_state"], "HOLD")

    def test_direct_readout_cannot_bypass_experiment_approval_or_runtime(self) -> None:
        for changes in ({"approval_state": "pending"}, {"minimum_runtime_days": 15}):
            payload = exposure_inputs()
            payload["experiment"].update(changes)
            result = readout_lifecycle_growth(payload["readout"], experiment=payload["experiment"], exposure_evidence=payload["exposure_evidence"])
            self.assertEqual(result["interpretation_state"], "HOLD")

    def test_legacy_readout_validates_but_is_insufficient_for_expansion(self) -> None:
        payload = exposure_inputs()
        self.assertEqual(validate_lifecycle_growth_artifact(payload["readout"]), [])
        result = readout_lifecycle_growth(payload["readout"])
        self.assertEqual(result["disposition"], "insufficient_data")
        self.assertIsNone(record(result["populations"])["assigned"])

    def test_malformed_companion_is_invalid_and_never_launchable(self) -> None:
        cases: tuple[dict[str, object], ...] = ({"assigned_count": True}, {"eligible_count": -1}, {"channel_refs": [{}]},
                        {"contact_pressure_state": []}, {"audience": []}, {"channels": "raw"},
                        {"eligibility_evidence_refs": ["x"] * 9}, {"prompt": "PRIVATE_SENTINEL"})
        for changes in cases:
            with self.subTest(changes=changes):
                payload = exposure_inputs()
                payload["exposure_evidence"].update(changes)
                self.assertTrue(validate_lifecycle_growth_artifact(payload["exposure_evidence"]))
                self.assertEqual(evaluate(payload)["interpretation_state"], "HOLD")

    def test_first_launch_requires_checks_but_not_manufactured_treatment(self) -> None:
        artifacts = launch_artifacts()
        evidence = exposure_inputs()["exposure_evidence"]
        evidence["actual_exposure_event_ref"] = artifacts["experiment"]["actual_exposure_event_ref"]
        evidence.update(assigned_count=None, assignment_evidence_refs=[], analysis_run_ref="",
                        population_reconciliation_state="unknown", population_evidence_refs=[])
        evidence["channels"] = [{"channel_ref": "surface_in_app", "reachability_state": "reachable",
                                "attempted_count": None, "delivered_count": None, "reached_count": None,
                                "failed_count": None, "unresolved_count": None,
                                "failure_reason_refs": [], "evidence_refs": ["audience_reachability_check"]}]
        artifacts["exposure_evidence"] = evidence
        result = prepare_lifecycle_growth(artifacts)
        self.assertEqual(result["verdict"], "READY")
        self.assertIsNone(evidence["assigned_count"])

    def test_prepare_existing_run_uses_same_expansion_gate(self) -> None:
        for field, value in (("contact_pressure_state", "unknown"), ("assigned_count", None)):
            artifacts = launch_artifacts()
            payload = exposure_inputs()
            payload["exposure_evidence"][field] = value
            artifacts.update(payload)
            self.assertEqual(prepare_lifecycle_growth(artifacts)["verdict"], "HOLD")

    def test_omitting_readout_cannot_hide_observed_channel_failures_at_first_launch(self) -> None:
        artifacts = launch_artifacts()
        payload = exposure_inputs()
        evidence = payload["exposure_evidence"]
        evidence["channels"] = [{"channel_ref": "surface_in_app", "reachability_state": "reachable",
                                "attempted_count": 8, "delivered_count": 7, "reached_count": 7,
                                "failed_count": 1, "unresolved_count": 0,
                                "failure_reason_refs": ["endpoint_failure"], "evidence_refs": ["receipts"]}]
        artifacts.update(experiment=payload["experiment"], exposure_evidence=evidence)
        self.assertEqual(prepare_lifecycle_growth(artifacts)["verdict"], "HOLD")

    def test_real_cli_full_unknown_rollback_and_readout(self) -> None:
        root = Path(__file__).resolve().parents[1]
        cli = [sys.executable, "-P", "-c", "from _local_package import load_local_package; load_local_package(); "
               + "from omh.cli import main; raise SystemExit(main())"]
        for operation, mode, expected in (("evaluate", "full", "ship"), ("evaluate", "unknown", "insufficient_data"),
                                           ("evaluate", "rollback", "rollback"), ("readout", "full", "ship")):
            with self.subTest(operation=operation, mode=mode), TemporaryDirectory(prefix="omh-g1-") as home:
                payload = exposure_inputs()
                if mode == "unknown":
                    payload["exposure_evidence"]["assigned_count"] = None
                if mode == "rollback":
                    payload["readout"].update(guardrail_state="failed", disposition="rollback")
                    del payload["exposure_evidence"]
                if "exposure_evidence" in payload:
                    from _lifecycle_configuration import bind
                    from _lifecycle_metrics import cover
                    payload = cover(bind(payload))
                before = deepcopy(payload)
                result = subprocess.run([*cli, "runtime", "workflow-artifact", "lifecycle-growth", operation, "--input", "-"],
                    cwd=root, env={**os.environ, "PYTHONPATH": str(root / "tests"), "PYTHONDONTWRITEBYTECODE": "1",
                                   "HOME": home, "OMH_HOME": str(Path(home) / "omh"), "HERMES_HOME": str(Path(home) / "hermes")},
                    input=json.dumps(payload), text=True, capture_output=True, timeout=30)
                self.assertEqual((result.returncode, result.stderr), (0, ""))
                self.assertEqual(record(decode_json(result.stdout))["operation"], operation)
                self.assertEqual(record(record(decode_json(result.stdout))["result"])["disposition"], expected)
                self.assertEqual(payload, before)
                self.assertFalse((Path(home) / "omh").exists())
