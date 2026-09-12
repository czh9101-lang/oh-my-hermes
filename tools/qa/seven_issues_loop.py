#!/usr/bin/env python3
# /// script
# requires-python = ">=3.11"
# dependencies = []
# ///
# How to run: uv sync --group lint; uv run python tools/qa/seven_issues_loop.py --output-dir /tmp/loop-evidence
# Uses this checkout's installed package; never launches Hermes or an executor.
"""Real local CLI scenarios with synthetic adapter evidence, not live provider execution."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
from tempfile import TemporaryDirectory
import uuid

from omh.coding.executor_capability_snapshots import JsonValue, build_executor_capability_snapshot, write_executor_capability_snapshot
from omh.coding_lifecycle import start_codex_delegation_lifecycle, record_codex_dispatch, record_codex_result, record_codex_verification
from omh.goal_ledger import create_goal_ledger, record_goal_checkpoint, record_goal_quality_gate
from omh.goal_loop import loop_cycle_path
from omh.paths import resolve_paths
from omh.wrapper.briefing import build_coding_briefing

OBSERVED_AT = "2026-09-01T12:00:00Z"


class ScenarioFailure(RuntimeError):
    pass


def require(condition: bool, code: str) -> None:
    if not condition:
        raise ScenarioFailure(code)


class SurfaceRun:
    """Invocation-local mutable collection of bounded command receipts."""
    def __init__(self, root: Path, output: Path):
        self.paths = resolve_paths(root / "omh", root / "hermes")
        self.env = {**os.environ, "OMH_HOME": str(self.paths.omh_home), "HERMES_HOME": str(self.paths.hermes_home)}
        self.output = output
        self.receipts: list[JsonValue] = []
        self.prefix = "qa-loop-" + uuid.uuid4().hex[:12]

    def cli(self, arguments: list[str], expected: int = 0):
        argv = [sys.executable, "-P", "-m", "omh.cli", "loop", *arguments]
        result = subprocess.run(argv, env=self.env, capture_output=True, text=True, timeout=30, check=False)
        receipt: dict[str, JsonValue] = {"argv": list(argv), "exit": result.returncode,
                   "stdout_sha256": hashlib.sha256(result.stdout.encode()).hexdigest(),
                   "stderr": result.stderr[:600]}
        self.receipts.append(receipt)
        require(result.returncode == expected, f"cli_exit:{arguments[0]}:{result.returncode}:{result.stderr[:200]}")
        if expected:
            return {}
        require(len(result.stdout) <= 500_000, "cli_output_bound")
        payload = json.loads(result.stdout)
        card = payload.get("status_card")
        if card:
            receipt["observed"] = {"driver": {k: card["driver"][k] for k in (
                "kind", "owner", "session_ref", "observation_state", "observed_at", "warnings", "next_action")},
                "completion_claim_allowed": card["completion_claim_allowed"], "checkpoint_next_action": card["next_action"]}
        if "goal_driver_handoff" in payload:
            handoff = payload["goal_driver_handoff"]
            receipt["observed"] = {"has_native_command": "goal_command" in handoff,
                                   "has_goal_intent": "goal_intent" in handoff, "status": handoff["status"]}
        return payload

    def file(self, name: str, value):
        path = self.output / name
        path.write_text(json.dumps(value, indent=2) + "\n", encoding="utf-8")
        return str(path)

    def start(self, suffix: str, extra: list[str]):
        return self.cli(["start", "--loop-id", self.prefix + suffix,
                         "--goal-summary", "Improve fixture correctness", "--goal-reframe", "Verify the bounded fixture",
                         "--criterion", "Observed fixture verification passes", "--allow-unloopable", *extra])["loop"]

    def observe(self, cycle, state: str, sequence: int):
        driver = cycle["driver"]
        value = {"schema_version": "loop_executor_goal_observation/v1", "loop_id": cycle["loop_id"],
                 "driver_id": driver["driver_id"], "owner": driver["owner"], "session_ref": driver["session_ref"],
                 "objective_sha256": "b" * 64 if state == "objective_different" else driver["objective_sha256"],
                 "sequence": sequence, "observation_id": f"fixture-observation-{sequence}", "observed_at": OBSERVED_AT,
                 "status": "active" if state == "objective_different" else state, "evidence_refs": ["fixture:driver-state"]}
        path = self.file(f"{state}-{sequence}.json", value)
        return self.cli(["goal-driver-observe", "--loop", cycle["loop_id"], "--observation-json", path])

    def exercise(self) -> dict[str, JsonValue]:
        capability = build_executor_capability_snapshot(executor="codex", recorded_at=OBSERVED_AT, capabilities={
            "resumable_goal": {"status": "host_observed", "observed_at": OBSERVED_AT,
                "scope": {"executor": "codex", "environment": "fixture", "session_ref": "executor_fixture_1"},
                "evidence_ref": "fixture:goal-controls"}})
        cap_path = self.output / "capability.json"
        write_executor_capability_snapshot(cap_path, capability)
        external = ["--executor", "codex", "--work-kind", "coding", "--capability-json", str(cap_path),
                    "--executor-session-ref", "executor_fixture_1"]
        create_goal_ledger(self.paths, "Fixture goal", ["Fixture result"], goal_id="fixture-goal")
        cycle = self.start("-external", [*external, "--linked-goal", "fixture-goal"])
        driver = cycle["driver"]
        require((driver["kind"], driver["owner"], driver["session_ref"], driver["observation_state"]) ==
                ("external_executor_goal", "codex", "executor_fixture_1", "prepared"), "G1_G2_driver_selection")
        handoff = self.cli(["goal-driver-handoff", "--loop", cycle["loop_id"]])["goal_driver_handoff"]
        require("goal_command" not in handoff and handoff["goal_intent"]["owner"] == "codex", "G1_one_goal")
        missing = self.cli(["status", "--loop", cycle["loop_id"]])["status_card"]
        require(missing["driver"]["warnings"] == ["driver_missing"] and not missing["completion_claim_allowed"], "G3_G4_missing")
        matrix = (("active", [], "observe_progress"), ("paused", ["driver_paused"], "resume_after_confirmation"),
                  ("budget_limited", ["driver_budget_limited"], "review_budget"),
                  ("closed", ["driver_closed_early"], "resume_for_missing_evidence"),
                  ("objective_different", ["driver_objective_mismatch"], "reconcile_objective"))
        for index, (state, warnings, action) in enumerate(matrix, 1):
            payload = self.observe(cycle, state, index)
            card = payload["status_card"]
            require((card["driver"]["warnings"], card["driver"]["next_action"]) == (warnings, action), "G3_" + state)
            require(not card["completion_claim_allowed"], "G4_" + state)
        active = {**json.loads((self.output / "active-1.json").read_text()), "loop_id": "qa-external"}
        self.file("active.json", active)
        invalid = {**active, "loop_id": cycle["loop_id"], "prompt": "RAW_PROMPT_SENTINEL"}
        self.cli(["goal-driver-observe", "--loop", cycle["loop_id"], "--observation-json", self.file("malformed.json", invalid)], 2)
        self.cli(["goal-driver-observe", "--loop", cycle["loop_id"], "--observation-json", str(self.output / "active-1.json")])
        # Public runtime methods record synthetic adapter evidence; none dispatches a process.
        runtime = start_codex_delegation_lifecycle(self.paths, "diagnose installation health")
        runtime_run = runtime["run"]
        assert isinstance(runtime_run, dict)
        run_id = str(runtime_run["run_id"])
        create_goal_ledger(self.paths, "Observed fixture", ["Fixture result"], goal_id="passing-goal", linked_runtime_runs=[run_id])
        record_goal_checkpoint(self.paths, "passing-goal", "Fixture result recorded", criteria_refs=["AC001"], evidence_refs=["fixture:result"])
        passing = self.start("-passing", [*external, "--linked-goal", "passing-goal"])
        require(not self.cli(["status", "--loop", passing["loop_id"]])["status_card"]["completion_claim_allowed"], "G4_runtime_absent")
        record_codex_dispatch(self.paths, run_id)
        record_codex_result(self.paths, run_id, result="completed", evidence_refs=["fixture:result"])
        verified = record_codex_verification(self.paths, run_id)
        record_goal_quality_gate(self.paths, "passing-goal", "Fixture quality gate", evidence_refs=["fixture:check"])
        verified_status = verified["status"]
        assert isinstance(verified_status, dict)
        require(verified_status["next_action"] == "report_completion_with_evidence", "runtime_fixture_recorded")
        ready = self.cli(["status", "--loop", passing["loop_id"]])["status_card"]
        require(ready["completion_claim_allowed"], "G5_missing_advisory_no_veto")
        for index, state in enumerate(("active", "closed", "objective_different"), 1):
            card = self.observe(passing, state, index)["status_card"]
            require(card["completion_claim_allowed"] and card["next_action"] == ready["next_action"], "G5_" + state)
        briefing = build_coding_briefing({}, runtime_status={"loop_status_card": ready})
        require(briefing["loop_driver"] == ready["driver"], "G8_wrapper_projection")
        for suffix, flags in (("-hermes", []), ("-noncoding", [*external, "--work-kind", "non_coding"]),
                              ("-unknown", ["--executor", "codex", "--work-kind", "coding"])):
            native = self.start(suffix, flags)
            fallback = self.cli(["goal-driver-handoff", "--loop", native["loop_id"]])["goal_driver_handoff"]
            require(fallback["driver"]["kind"] == "hermes_goal" and fallback["goal_command"].startswith("/goal "), "G6" + suffix)
        native_id = self.prefix + "-hermes"
        path = loop_cycle_path(self.paths, native_id)
        legacy = json.loads(path.read_text())
        for key in ("driver", "executor_capability_snapshot", "executor_goal_observations"):
            legacy.pop(key)
        legacy["schema_version"] = "loop_cycle/v1"
        path.write_text(json.dumps(legacy), encoding="utf-8")
        before = path.read_bytes()
        card = self.cli(["status", "--loop", native_id])["status_card"]
        require(card["driver"]["compatibility"] == "legacy_native_driver" and before == path.read_bytes(), "G7_read_only")
        migrated = self.cli(["migrate-driver", "--loop", native_id, "--apply"])["loop"]
        replay = self.cli(["migrate-driver", "--loop", native_id, "--apply"])["loop"]
        require(migrated == replay and migrated["driver_migration"]["source_revision"] == legacy["record_revision"], "G7_migration")
        binding: dict[str, JsonValue] = {"selection": {"executor": "codex", "work_kind": "coding", "capability_snapshot": capability,
                                  "session_ref": "executor_fixture_1"}, "previous_driver": None}
        self.cli(["driver-bind", "--loop", native_id, "--input", self.file("binding-missing.json", binding)], 2)
        binding["previous_driver"] = {"driver_id": migrated["driver"]["driver_id"], "status": "absent",
                                      "observed_at": OBSERVED_AT, "evidence_refs": ["fixture:driver-absent"]}
        bound = self.cli(["driver-bind", "--loop", native_id, "--input", self.file("binding.json", binding)])["loop"]
        require(bound["driver"]["kind"] == "external_executor_goal" and len(bound["driver_history"]) == 1, "G7_transfer")
        return {"acceptance_ids": [f"G{i}" for i in range(1, 9)], "commands": self.receipts,
                "runtime_fixture": {"next_action": verified_status["next_action"], "input_class": "synthetic_adapter_evidence"},
                "wrapper_driver": briefing["loop_driver"], "live_executor_execution": "not_run"}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path)
    args = parser.parse_args()
    report: dict[str, JsonValue] = {}
    status = 0
    with TemporaryDirectory(prefix="omh-loop-qa-") as scratch:
        root = Path(scratch)
        output = args.output_dir or root / "evidence"
        output.mkdir(parents=True, exist_ok=True)
        runner = SurfaceRun(root, output)
        try:
            report = runner.exercise()
        except (ScenarioFailure, OSError, ValueError, KeyError, subprocess.TimeoutExpired) as exc:
            status = 1
            report = {"error": f"{type(exc).__name__}:{str(exc)[:400]}", "commands": runner.receipts}
    cleanup = not Path(scratch).exists()
    report["cleanup"] = {"verified_absent": cleanup, "owned_workers": 0, "owned_registrations": 0}
    report["status"] = "passed" if status == 0 and cleanup else "failed"
    if args.output_dir:
        (args.output_dir / "result.json").write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(report, sort_keys=True))
    return status if cleanup else 1


if __name__ == "__main__":
    raise SystemExit(main())
