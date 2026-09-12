from __future__ import annotations

import copy
import inspect
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from _local_package import load_local_package

load_local_package()
from omh.coding.executor_capability_snapshots import (
    JsonValue, complete_executor_capability_snapshot,
    validate_executor_capability_snapshot,
)
from omh.goal_ledger import create_goal_ledger, record_goal_checkpoint
from omh.goal_loop import (
    build_loop_goal_driver_handoff,
    build_loop_status_card,
    create_loop_cycle,
    loop_cycle_path,
    read_loop_cycle,
    record_loop_goal_driver_observation,
)
from omh.paths import resolve_paths
from omh.wrapper.briefing import build_coding_briefing

OBSERVED_AT = "2026-09-01T12:00:00Z"


def capability() -> dict[str, JsonValue]:
    return {
        "schema_version": "executor_capability_snapshot/v3",
        "executor": "codex", "recorded_at": OBSERVED_AT,
        "capabilities": {"resumable_goal": {
            "status": "host_observed", "observed_at": OBSERVED_AT,
            "evidence_ref": "fixture:goal-controls",
            "scope": {"executor": "codex", "environment": "fixture", "session_ref": "executor_fixture_1"},
        }},
    }


def selection() -> dict[str, JsonValue]:
    return {"executor": "codex", "work_kind": "coding", "capability_snapshot": capability(),
            "session_ref": "executor_fixture_1"}


def observation(cycle, status="active", sequence=1):
    driver = cycle["driver"]
    return {
        "schema_version": "loop_executor_goal_observation/v1",
        "loop_id": cycle["loop_id"], "driver_id": driver["driver_id"],
        "owner": driver["owner"], "session_ref": driver["session_ref"],
        "objective_sha256": driver["objective_sha256"],
        "sequence": sequence, "observation_id": f"fixture-observation-{sequence}",
        "observed_at": OBSERVED_AT, "status": status, "evidence_refs": ["fixture:driver-state"],
    }


class LoopExecutorDriverTests(unittest.TestCase):
    def __init__(self, methodName: str = "runTest") -> None:
        super().__init__(methodName)
        self.home = TemporaryDirectory()
        self.addCleanup(self.home.cleanup)
        self.paths = resolve_paths(Path(self.home.name) / "omh", Path(self.home.name) / "hermes")

    def start(self, selected=None, linked_goal_id=""):
        self.assertIn("driver_selection", inspect.signature(create_loop_cycle).parameters)
        return create_loop_cycle(
            self.paths, goal_summary="Improve fixture correctness", goal_reframe="Verify the bounded fixture",
            success_criteria=["Observed fixture verification passes"], allow_unloopable=True,
            loop_id=f"fixture-loop-{len(list(self.paths.loops_dir.glob('*/cycle.json')))}", linked_goal_id=linked_goal_id,
            driver_selection=selection() if selected is None else selected,
        )

    def test_capability_is_valid_when_goal_controls_are_host_observed(self):
        # Given / When / Then: the closed v3 row is a supported capability, not static routing.
        self.assertEqual(validate_executor_capability_snapshot(capability()), [])

    def test_goal_row_is_unknown_when_reading_legacy_snapshots(self):
        # Given
        for version in ("v1", "v2"):
            with self.subTest(version=version):
                legacy: dict[str, JsonValue] = {**capability(), "schema_version": f"executor_capability_snapshot/{version}",
                          "capabilities": {"worktree_isolation": {"status": "prepared"}}}
                # When
                projected = complete_executor_capability_snapshot(legacy)
                # Then
                rows = projected["capabilities"]
                assert isinstance(rows, dict)
                self.assertEqual(rows.get("resumable_goal"), {"status": "unknown"})

    def test_one_executor_goal_is_prepared_when_selected_owner_has_controls(self):
        # Given
        cycle = self.start()
        # When
        handoff = build_loop_goal_driver_handoff(self.paths, cycle["loop_id"])
        # Then
        self.assertNotIn("goal_command", handoff)
        self.assertEqual(handoff["goal_intent"]["owner"], "codex")
        self.assertEqual(handoff["goal_intent"]["session_ref"], "executor_fixture_1")
        self.assertEqual(handoff["status"], "prepared_not_observed")
        self.assertEqual(handoff["observation_contract"]["schema_version"], "loop_executor_goal_observation/v1")

    def test_status_identifies_preparation_when_no_snapshot_exists(self):
        # Given
        cycle = self.start()
        # When
        driver = build_loop_status_card(self.paths, cycle["loop_id"])["driver"]
        # Then
        self.assertEqual((driver["kind"], driver["owner"], driver["session_ref"], driver["observation_state"]),
                         ("external_executor_goal", "codex", "executor_fixture_1", "prepared"))
        self.assertEqual(driver["warnings"], ["driver_missing"])
        self.assertEqual(driver["next_action"], "observe_or_resume_selected_executor")

    def test_recovery_is_deterministic_when_driver_state_is_observed(self):
        # Given
        cycle = self.start()
        rows = (("active", [], "observe_progress"),
                ("paused", ["driver_paused"], "resume_after_confirmation"),
                ("budget_limited", ["driver_budget_limited"], "review_budget"),
                ("closed", ["driver_closed_early"], "resume_for_missing_evidence"))
        for sequence, (status, warnings, action) in enumerate(rows, 1):
            with self.subTest(status=status):
                # When
                record_loop_goal_driver_observation(self.paths, cycle["loop_id"], observation(cycle, status, sequence))
                # Then
                driver = build_loop_status_card(self.paths, cycle["loop_id"])["driver"]
                self.assertEqual((driver["warnings"], driver["next_action"]), (warnings, action))
                self.assertEqual(driver["observation_state"], "observed")
                self.assertEqual(driver["observed_at"], OBSERVED_AT)

    def test_objective_reconciliation_wins_when_budget_is_also_limited(self):
        # Given
        cycle = self.start()
        snapshot = {**observation(cycle, "budget_limited"), "objective_sha256": "b" * 64}
        # When
        record_loop_goal_driver_observation(self.paths, cycle["loop_id"], snapshot)
        # Then
        driver = build_loop_status_card(self.paths, cycle["loop_id"])["driver"]
        self.assertEqual(driver["warnings"], ["driver_objective_mismatch", "driver_budget_limited"])
        self.assertEqual(driver["next_action"], "reconcile_objective")

    def test_closed_driver_cannot_complete_when_criteria_are_pending(self):
        # Given
        create_goal_ledger(self.paths, "Fixture", ["Verified"], goal_id="fixture-goal")
        cycle = self.start(linked_goal_id="fixture-goal")
        # When
        record_loop_goal_driver_observation(self.paths, cycle["loop_id"], observation(cycle, "closed"))
        # Then
        self.assertFalse(build_loop_status_card(self.paths, cycle["loop_id"])["completion_claim_allowed"])

    def test_closed_driver_cannot_complete_when_runtime_evidence_is_absent(self):
        # Given
        create_goal_ledger(self.paths, "Fixture", ["Verified"], goal_id="fixture-goal", linked_runtime_runs=["missing-run"])
        record_goal_checkpoint(self.paths, "fixture-goal", "Fixture result recorded", criteria_refs=["AC001"], evidence_refs=["fixture:check"])
        cycle = self.start(linked_goal_id="fixture-goal")
        # When
        record_loop_goal_driver_observation(self.paths, cycle["loop_id"], observation(cycle, "closed"))
        # Then
        self.assertFalse(build_loop_status_card(self.paths, cycle["loop_id"])["completion_claim_allowed"])

    def test_native_fallback_is_preserved_when_external_controls_are_ineligible(self):
        # Given
        variants = [dict(selection(), executor="hermes"), dict(selection(), work_kind="non_coding"),
                    dict(selection(), capability_snapshot=None), dict(selection(), session_ref="other-session")]
        for state in ("unknown", "prepared", "unavailable"):
            snapshot = capability()
            rows = snapshot["capabilities"]
            assert isinstance(rows, dict)
            rows["resumable_goal"] = {"status": state}
            variants.append(dict(selection(), capability_snapshot=snapshot))
        for selected in variants:
            with self.subTest(selected=selected):
                cycle = self.start(selected)
                # When
                handoff = build_loop_goal_driver_handoff(self.paths, cycle["loop_id"])
                # Then
                self.assertTrue(handoff["goal_command"].startswith("/goal "))
                self.assertEqual(handoff["driver"]["kind"], "hermes_goal")
                self.assertTrue(handoff["driver"]["fallback_reason"])

    def test_legacy_read_is_explicit_when_cycle_has_no_companion(self):
        # Given
        cycle = create_loop_cycle(self.paths, goal_summary="Fixture", goal_reframe="Verify fixture",
                                  success_criteria=["Verified"], allow_unloopable=True, loop_id="legacy")
        legacy = {key: value for key, value in cycle.items() if key not in {"driver", "executor_capability_snapshot", "executor_goal_observations"}}
        legacy["schema_version"] = "loop_cycle/v1"
        path = loop_cycle_path(self.paths, "legacy")
        path.write_text(json.dumps(legacy), encoding="utf-8")
        before = path.read_bytes()
        # When
        card = build_loop_status_card(self.paths, "legacy")
        # Then
        self.assertEqual(card.get("driver", {}).get("compatibility"), "legacy_native_driver")
        self.assertEqual(path.read_bytes(), before)
        self.assertEqual(read_loop_cycle(self.paths, "legacy")["record_revision"], legacy["record_revision"])

    def test_wrapper_projects_driver_when_loop_status_is_supplied(self):
        # Given
        cycle = self.start()
        card = build_loop_status_card(self.paths, cycle["loop_id"])
        # When
        briefing = build_coding_briefing({}, runtime_status={"loop_status_card": card})
        # Then
        self.assertEqual(briefing.get("loop_driver"), card["driver"])

    def test_observation_is_idempotent_when_identical_input_is_replayed(self):
        # Given
        cycle = self.start()
        snapshot = observation(cycle)
        first = record_loop_goal_driver_observation(self.paths, cycle["loop_id"], snapshot)
        # When
        second = record_loop_goal_driver_observation(self.paths, cycle["loop_id"], copy.deepcopy(snapshot))
        # Then
        self.assertEqual(second, first)
