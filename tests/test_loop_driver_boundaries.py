from __future__ import annotations

import copy
import json
import os
from pathlib import Path
import subprocess
import sys
from tempfile import TemporaryDirectory
import unittest

from _local_package import load_local_package

load_local_package()
from omh.coding_lifecycle import (
    start_codex_delegation_lifecycle, record_codex_dispatch, record_codex_result, record_codex_verification,
)
from omh.goal_ledger import create_goal_ledger, record_goal_checkpoint, record_goal_quality_gate
from omh.goal_loop import (create_loop_cycle, build_loop_goal_driver_handoff, build_loop_status_card, loop_cycle_path,
                           record_loop_goal_driver_observation, validate_loop_cycle)
from omh.paths import resolve_paths
from test_loop_executor_driver import OBSERVED_AT, capability, observation, selection
from test_loop_cycle import _goal_driver_observation


class LoopDriverBoundaryTests(unittest.TestCase):
    def __init__(self, methodName: str = "runTest") -> None:
        super().__init__(methodName)
        self.home = TemporaryDirectory()
        self.addCleanup(self.home.cleanup)
        self.root = Path(self.home.name)
        self.paths = resolve_paths(self.root / "omh", self.root / "hermes")
        self.env = {**os.environ, "OMH_HOME": str(self.paths.omh_home), "HERMES_HOME": str(self.paths.hermes_home)}

    def start(self, external=True, linked_goal_id=""):
        return create_loop_cycle(self.paths, goal_summary="Improve fixture correctness", goal_reframe="Verify the bounded fixture",
                                 success_criteria=["Observed fixture verification passes"], loop_id="fixture", allow_unloopable=True,
                                 linked_goal_id=linked_goal_id, driver_selection=selection() if external else None)

    def cli(self, *args):
        return subprocess.run([sys.executable, "-P", "-m", "omh.cli", "loop", *args],
                              env=self.env, text=True, capture_output=True, timeout=30, check=False)

    def test_cli_prepares_external_goal_when_explicit_selection_is_supplied(self):
        # Given
        path = self.root / "capability.json"
        path.write_text(json.dumps(capability()), encoding="utf-8")
        # When
        result = self.cli("start", "--loop-id", "cli-fixture", "--goal-summary", "Improve fixture correctness",
                          "--goal-reframe", "Verify the bounded fixture", "--criterion", "Verified",
                          "--allow-unloopable", "--executor", "codex", "--work-kind", "coding",
                          "--capability-json", str(path), "--executor-session-ref", "executor_fixture_1")
        # Then
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)["loop"]["driver"]["kind"], "external_executor_goal")

    def test_cli_migrates_once_when_legacy_artifact_is_explicitly_applied(self):
        # Given
        cycle = self.start(False)
        handoff = build_loop_goal_driver_handoff(self.paths, "fixture")
        cycle = record_loop_goal_driver_observation(self.paths, "fixture", _goal_driver_observation("fixture", handoff["goal_command_sha256"]))
        legacy = {k: v for k, v in cycle.items() if k not in {"driver", "executor_capability_snapshot", "executor_goal_observations"}}
        legacy["schema_version"] = "loop_cycle/v1"
        path = loop_cycle_path(self.paths, "fixture")
        path.write_text(json.dumps(legacy), encoding="utf-8")
        # When
        result = self.cli("migrate-driver", "--loop", "fixture", "--apply")
        # Then
        self.assertEqual(result.returncode, 0, result.stderr)
        migrated = json.loads(result.stdout)["loop"]
        self.assertEqual(migrated["schema_version"], "loop_cycle/v2")
        self.assertEqual(migrated["driver_migration"]["source_revision"], legacy["record_revision"])
        self.assertEqual(migrated["goal_driver_observations"], legacy["goal_driver_observations"])
        self.assertEqual(migrated["phase_transitions"], legacy["phase_transitions"])
        self.assertEqual(json.loads(self.cli("migrate-driver", "--loop", "fixture", "--apply").stdout)["loop"], migrated)

    def test_cli_refuses_transfer_when_old_driver_stop_is_unobserved(self):
        # Given
        self.start(False)
        path = self.root / "bind.json"
        path.write_text(json.dumps({"selection": selection(), "previous_driver": None}), encoding="utf-8")
        before = loop_cycle_path(self.paths, "fixture").read_bytes()
        # When
        result = self.cli("driver-bind", "--loop", "fixture", "--input", str(path))
        # Then
        self.assertEqual(result.returncode, 2)
        self.assertIn("driver_transfer_requires_reconciliation", result.stderr)
        self.assertEqual(loop_cycle_path(self.paths, "fixture").read_bytes(), before)

    def test_cli_transfers_once_when_old_driver_absence_is_observed(self):
        # Given
        cycle = self.start(False)
        path = self.root / "bind.json"
        path.write_text(json.dumps({"selection": selection(), "previous_driver": {
            "driver_id": cycle["driver"]["driver_id"], "status": "absent", "observed_at": OBSERVED_AT,
            "evidence_refs": ["fixture:old-driver-absent"],
        }}), encoding="utf-8")
        # When
        result = self.cli("driver-bind", "--loop", "fixture", "--input", str(path))
        # Then
        self.assertEqual(result.returncode, 0, result.stderr)
        updated = json.loads(result.stdout)["loop"]
        self.assertEqual(updated["driver"]["kind"], "external_executor_goal")
        self.assertEqual(len(updated["driver_history"]), 1)
        self.assertEqual(json.loads(self.cli("driver-bind", "--loop", "fixture", "--input", str(path)).stdout)["loop"], updated)

    def test_native_companion_rejects_payload_when_free_text_is_injected(self):
        # Given
        cycle = self.start(False)
        for field, value in (("fallback_reason", "RAW_SECRET_SENTINEL"), ("session_ref", "RAW_SECRET_SENTINEL"),
                             ("observation_state", "completed"), ("observed_at", "tomorrow")):
            with self.subTest(field=field):
                tampered = copy.deepcopy(cycle)
                tampered["driver"][field] = value
                # When / Then
                self.assertFalse(validate_loop_cycle(tampered)["ok"], field)

    def test_v1_refuses_external_semantics_when_companion_is_injected(self):
        # Given
        cycle = self.start()
        cycle["schema_version"] = "loop_cycle/v1"
        # When / Then
        self.assertFalse(validate_loop_cycle(cycle)["ok"])

    def test_advisory_cannot_veto_when_criteria_and_observed_runtime_pass(self):
        # Given: real public runtime writers with synthetic adapter evidence, not live Codex.
        runtime = start_codex_delegation_lifecycle(self.paths, "diagnose installation health")
        runtime_run = runtime["run"]
        self.assertIsInstance(runtime_run, dict)
        assert isinstance(runtime_run, dict)
        run_id = str(runtime_run["run_id"])
        record_codex_dispatch(self.paths, run_id)
        record_codex_result(self.paths, run_id, result="completed", evidence_refs=["fixture:result"])
        record_codex_verification(self.paths, run_id)
        create_goal_ledger(self.paths, "Fixture", ["Verified"], goal_id="fixture-goal", linked_runtime_runs=[run_id])
        record_goal_checkpoint(self.paths, "fixture-goal", "Fixture result recorded", criteria_refs=["AC001"], evidence_refs=["fixture:check"])
        record_goal_quality_gate(self.paths, "fixture-goal", "Fixture verification", status="passed", evidence_refs=["fixture:verified"])
        cycle = self.start(linked_goal_id="fixture-goal")
        self.assertTrue(build_loop_status_card(self.paths, "fixture")["linked_goal_completion"]["ready"])
        states = ((None, False), ("active", False), ("closed", False),
                  ("closed", True), ("paused", True), ("budget_limited", True))
        for seq, (status, objective_differs) in enumerate(states, 1):
            with self.subTest(status=status, objective_differs=objective_differs):
                if status is not None:
                    submitted = observation(cycle, status, seq)
                    if objective_differs:
                        submitted["objective_sha256"] = "b" * 64
                    record_loop_goal_driver_observation(self.paths, "fixture", submitted)
                # When: project the actual checkpoint decision for this advisory state.
                card = build_loop_status_card(self.paths, "fixture")
                # Then: both the authority and the user-visible decision remain ready.
                self.assertTrue(card["linked_goal_completion"]["ready"])
                self.assertTrue(card["completion_claim_allowed"])
                self.assertNotEqual(card["next_action"], card["driver"]["next_action"])

    def test_stream_rejects_old_or_conflicting_input_when_an_observation_exists(self):
        # Given
        cycle = self.start()
        record_loop_goal_driver_observation(self.paths, "fixture", observation(cycle, "active", 2))
        samples = ((observation(cycle, "active", 1), "driver_stale_observation"),
                   (observation(cycle, "paused", 2), "driver_conflicting_observation"),
                   ({**observation(cycle, "active", 3), "observed_at": "2026-08-31T12:00:00Z"}, "driver_stale_observation"))
        for sample, code in samples:
            with self.subTest(code=code):
                before = loop_cycle_path(self.paths, "fixture").read_bytes()
                # When / Then
                with self.assertRaisesRegex(ValueError, code):
                    record_loop_goal_driver_observation(self.paths, "fixture", sample)
                self.assertEqual(loop_cycle_path(self.paths, "fixture").read_bytes(), before)

    def test_native_receipt_cannot_be_relabelled_when_external_driver_is_selected(self):
        # Given
        self.start()
        native = _goal_driver_observation("fixture", "a" * 64)
        # When / Then
        with self.assertRaisesRegex(ValueError, "driver_native_observation_requires_native_owner"):
            record_loop_goal_driver_observation(self.paths, "fixture", native)

    def test_start_refuses_transfer_when_cycle_id_already_has_a_driver(self):
        # Given
        self.start(False)
        before = loop_cycle_path(self.paths, "fixture").read_bytes()
        # When / Then
        with self.assertRaisesRegex(ValueError, "loop_already_exists"):
            self.start()
        self.assertEqual(loop_cycle_path(self.paths, "fixture").read_bytes(), before)

    def test_history_rejects_payload_when_archive_fields_are_injected(self):
        # Given
        cycle = self.start()
        cycle["driver_history"] = [{"prompt": "RAW_SECRET_SENTINEL"}]
        # When / Then
        self.assertFalse(validate_loop_cycle(cycle)["ok"])

    def test_invalid_observations_are_rejected_when_boundary_fields_are_malformed(self):
        # Given
        cycle = self.start()
        valid = observation(cycle)
        for field, value in (("sequence", True), ("sequence", 0), ("sequence", []), ("observed_at", "2999-01-01T00:00:00Z"),
                             ("owner", "hermes"), ("session_ref", "other"), ("driver_id", "a" * 64),
                             ("status", "done"), ("objective_sha256", "A" * 64),
                             ("evidence_refs", ["secret:RAW_SECRET_SENTINEL"]), ("prompt", "RAW_PROMPT_SENTINEL")):
            with self.subTest(field=field, value=value):
                submitted = {**valid, field: value}
                before = loop_cycle_path(self.paths, "fixture").read_bytes()
                # When / Then
                with self.assertRaises(ValueError):
                    record_loop_goal_driver_observation(self.paths, "fixture", submitted)
                self.assertEqual(loop_cycle_path(self.paths, "fixture").read_bytes(), before)
