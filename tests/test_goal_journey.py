from __future__ import annotations

import json
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from tempfile import TemporaryDirectory

from _cli_harness import run_cli
from _local_package import load_local_package

load_local_package()
from omh.goal_ledger import (
    build_goal_completion_gate,
    cancel_goal_ledger,
    complete_goal_ledger,
    create_goal_ledger,
    goal_ledger_path,
    record_goal_blocker,
    record_goal_checkpoint,
)
from omh.paths import OmhPaths, resolve_paths
from omh.wrapper.sessions import (
    create_or_resume_wrapper_session,
    prepare_wrapper_session_handoff,
    record_plan_decision,
    select_wrapper_session_executor,
)
from omh.workflows.goal_journey import (
    GOAL_JOURNEY_CHECKPOINT_LIMIT,
    GOAL_JOURNEY_CLAIM_BOUNDARY,
    GOAL_JOURNEY_FRESH_WINDOW_SECONDS,
    GOAL_JOURNEY_SCHEMA_VERSION,
    build_goal_journey,
    render_goal_journey_text,
    validate_goal_journey,
)


PINNED_NOW = "2026-08-09T00:00:00Z"
# The message this repo's wrapper-session tests use to reach `plan_presented`,
# which is the only status `record_plan_decision("accept")` will take.
HANDOFF_MESSAGE = "risky refactor with private-token-123"


def _paths(root: Path) -> OmhPaths:
    return resolve_paths(root / ".omh", root / ".hermes")


def _base(root: Path) -> list[str]:
    return ["--omh-home", str(root / ".omh"), "--hermes-home", str(root / ".hermes")]


def _prepared_handoff_run(paths: OmhPaths) -> tuple[str, str]:
    """A real wrapper session carried to a prepared handoff, and the run it owns.

    Built through the actual writers rather than by planting records: the
    projection claims to read the artifacts the product already produces, and a
    fixture that forges them would not test that claim.
    """
    started = create_or_resume_wrapper_session(
        paths,
        HANDOFF_MESSAGE,
        source="discord",
        source_metadata={"source_event_id": "m1", "channel_ref": "c1"},
    )
    session_id = str(started["session"]["session_id"])
    record_plan_decision(paths, session_id, "accept")
    select_wrapper_session_executor(paths, session_id, "codex")
    prepared = prepare_wrapper_session_handoff(paths, session_id, HANDOFF_MESSAGE)
    return session_id, str(prepared["session"]["current_run_id"])


def _tree_snapshot(root: Path) -> dict[str, bytes]:
    return {
        str(path.relative_to(root)): path.read_bytes()
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


def _criterion(journey: dict[str, object], criterion_id: str) -> dict[str, object]:
    return next(item for item in journey["criteria"] if item["id"] == criterion_id)


class GoalJourneyProjectionTests(unittest.TestCase):
    def test_a_later_session_reconstructs_the_journey_from_local_metadata(self) -> None:
        # Acceptance criterion 1. Nothing is handed in but the goal id: the
        # session, plan, handoff, owner, and run edges are all re-derived from
        # what is already on disk.
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            paths = _paths(root)
            session_id, run_id = _prepared_handoff_run(paths)
            create_goal_ledger(
                paths,
                "Carry an evidence-backed goal across conversations",
                ["Journey links the work that advanced the goal"],
                goal_id="goal-journey",
                linked_runtime_runs=[run_id],
            )
            record_goal_checkpoint(
                paths,
                "goal-journey",
                "Projection landed",
                criteria_refs=["AC001"],
                evidence_refs=["tests/test_goal_journey.py"],
                linked_runtime_run_id=run_id,
            )

            # A separate resolve_paths stands in for the later conversation:
            # no in-memory state survives, only the local store.
            journey = build_goal_journey(_paths(root), "goal-journey", now=PINNED_NOW)

            self.assertEqual(journey["schema_version"], GOAL_JOURNEY_SCHEMA_VERSION)
            self.assertEqual(validate_goal_journey(journey), [])
            self.assertEqual(journey["goal_id"], "goal-journey")
            self.assertEqual([item["session_id"] for item in journey["sessions"]], [session_id])
            self.assertEqual([item["linked_run_id"] for item in journey["sessions"]], [run_id])
            self.assertEqual([item["session_id"] for item in journey["plans"]], [session_id])
            self.assertEqual([item["run_id"] for item in journey["runs"]], [run_id])
            self.assertEqual([item["owner"] for item in journey["owners"]], ["codex"])
            self.assertEqual(
                sorted(journey["owners"][0]["refs"]), [f"run:{run_id}", f"session:{session_id}"]
            )
            self.assertEqual(
                [(item["origin"], item["source_ref"]) for item in journey["handoffs"]],
                [("runtime_run", run_id)],
            )
            self.assertEqual([item["criteria_refs"] for item in journey["checkpoints"]], [["AC001"]])
            self.assertEqual(journey["resume"]["continue_command"], "omh goal continue --goal goal-journey")

    def test_the_journey_carries_the_stable_goal_id_without_any_transcript(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            paths = _paths(root)
            _session_id, run_id = _prepared_handoff_run(paths)
            objective = "Finish the long goal, private detail SECRET-JOURNEY-1"
            goal = create_goal_ledger(
                paths,
                objective,
                ["Journey stays metadata only"],
                goal_id="goal-private",
                linked_runtime_runs=[run_id],
            )

            journey = build_goal_journey(paths, "goal-private", now=PINNED_NOW)

            serialized = json.dumps(journey)
            self.assertEqual(journey["objective_hash"], goal["objective_hash"])
            self.assertEqual(journey["resume"]["objective_hash"], goal["objective_hash"])
            self.assertNotIn(objective, serialized)
            self.assertNotIn("SECRET-JOURNEY-1", serialized)
            self.assertNotIn(HANDOFF_MESSAGE, serialized)
            self.assertNotIn("private-token-123", serialized)

    def test_a_criterion_marked_satisfied_without_a_checkpoint_trail_stays_pending(self) -> None:
        # Acceptance criterion 2. The ledger's own gate accepts a criterion that
        # merely *says* satisfied and carries refs; the journey refuses it,
        # because no done checkpoint ever accepted evidence for it.
        #
        # The forged ref has to be one the completion-integrity classifier
        # admits, or the ledger gate would refuse it for the wrong reason and
        # the "journey is stricter than the ledger" contract under test would
        # stop being exercised at all.
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            paths = _paths(root)
            create_goal_ledger(paths, "Finish the goal", ["Criterion one"], goal_id="goal-forged")
            path = goal_ledger_path(paths, "goal-forged")
            stored = json.loads(path.read_text(encoding="utf-8"))
            stored["acceptance_criteria"][0]["status"] = "satisfied"
            stored["acceptance_criteria"][0]["evidence_refs"] = ["observed:suite-green"]
            path.write_text(json.dumps(stored, sort_keys=True), encoding="utf-8")

            journey = build_goal_journey(paths, "goal-forged", now=PINNED_NOW)

            criterion = _criterion(journey, "AC001")
            self.assertEqual(criterion["ledger_status"], "satisfied")
            self.assertEqual(criterion["journey_status"], "pending")
            self.assertFalse(criterion["evidence_accepted"])
            self.assertEqual(criterion["satisfied_by_checkpoints"], [])
            # The ledger gate is ready and the journey still is not: the
            # projection may be stricter than the ledger, never looser.
            self.assertTrue(build_goal_completion_gate(paths, "goal-forged")["ready"])
            self.assertTrue(journey["completion"]["ledger_gate_ready"])
            self.assertFalse(journey["completion"]["ready"])
            self.assertEqual(journey["completion"]["blocking_gate_ids"], ["criterion:AC001"])
            self.assertEqual(validate_goal_journey(journey), [])

    def test_completion_stays_blocked_while_a_required_gate_lacks_evidence(self) -> None:
        # Acceptance criterion 3. Every acceptance criterion is satisfied with
        # accepted evidence, and completion is still blocked because the linked
        # runtime run carries no observed evidence.
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            paths = _paths(root)
            _session_id, run_id = _prepared_handoff_run(paths)
            create_goal_ledger(
                paths,
                "Finish the delegated goal",
                ["Criterion one"],
                goal_id="goal-runtime-gate",
                linked_runtime_runs=[run_id],
            )
            record_goal_checkpoint(
                paths,
                "goal-runtime-gate",
                "Implementation landed",
                criteria_refs=["AC001"],
                evidence_refs=["unit"],
            )

            journey = build_goal_journey(paths, "goal-runtime-gate", now=PINNED_NOW)

            self.assertTrue(_criterion(journey, "AC001")["evidence_accepted"])
            self.assertEqual(journey["completion"]["blocking_gate_ids"], [f"runtime_run:{run_id}"])
            self.assertFalse(journey["completion"]["ready"])
            self.assertFalse(journey["completion"]["ledger_gate_ready"])
            self.assertEqual(journey["completion"]["unsatisfied_required_gates"], 1)
            gate_kinds = {item["gate_id"]: item["kind"] for item in journey["required_gates"]}
            self.assertEqual(gate_kinds[f"runtime_run:{run_id}"], "linked_runtime_run")
            self.assertEqual(gate_kinds["criterion:AC001"], "acceptance_criterion")
            # The ledger refuses the completion the journey reports as blocked.
            self.assertFalse(
                complete_goal_ledger(paths, "goal-runtime-gate", evidence_refs=["unit"])["completed"]
            )

    def test_a_generic_done_checkpoint_does_not_satisfy_a_specific_criterion(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            paths = _paths(root)
            create_goal_ledger(
                paths,
                "Finish two separate criteria",
                ["Parser is fixed", "Docs are updated"],
                goal_id="goal-generic-done",
            )
            # A "done, everything works" checkpoint that names no criterion, and
            # a real one that names exactly one.
            record_goal_checkpoint(
                paths, "goal-generic-done", "Everything is done", evidence_refs=["looks fine"]
            )
            record_goal_checkpoint(
                paths,
                "goal-generic-done",
                "Parser fix landed",
                criteria_refs=["AC001"],
                evidence_refs=["tests/test_parser.py"],
            )

            journey = build_goal_journey(paths, "goal-generic-done", now=PINNED_NOW)

            self.assertEqual(_criterion(journey, "AC001")["journey_status"], "satisfied")
            self.assertEqual(_criterion(journey, "AC002")["journey_status"], "pending")
            self.assertEqual(journey["completion"]["blocking_gate_ids"], ["criterion:AC002"])
            self.assertFalse(journey["completion"]["ready"])

    def test_a_complete_ledger_with_an_unproven_criterion_reads_as_blocked(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            paths = _paths(root)
            create_goal_ledger(paths, "Finish the goal", ["Criterion one"], goal_id="goal-overclaim")
            record_goal_checkpoint(
                paths, "goal-overclaim", "Landed", criteria_refs=["AC001"], evidence_refs=["unit"]
            )
            complete_goal_ledger(paths, "goal-overclaim", evidence_refs=["unit"])
            path = goal_ledger_path(paths, "goal-overclaim")
            stored = json.loads(path.read_text(encoding="utf-8"))
            # Strip the evidence the checkpoint carried, leaving a ledger that
            # still says complete.
            stored["checkpoints"][0]["evidence_refs"] = []
            path.write_text(json.dumps(stored, sort_keys=True), encoding="utf-8")

            journey = build_goal_journey(paths, "goal-overclaim", now=PINNED_NOW)

            self.assertEqual(journey["goal_status"], "complete")
            self.assertEqual(journey["stage"], "blocked")
            self.assertFalse(journey["completion"]["ready"])
            self.assertEqual(validate_goal_journey(journey), [])

    def test_stage_names_intent_preparation_activity_blocked_and_verified_complete(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            paths = _paths(root)
            create_goal_ledger(paths, "Plain goal", ["Criterion one"], goal_id="goal-intent")
            self.assertEqual(build_goal_journey(paths, "goal-intent")["stage"], "intent")

            _session_id, run_id = _prepared_handoff_run(paths)
            create_goal_ledger(
                paths, "Prepared goal", ["Criterion one"], goal_id="goal-prep", linked_runtime_runs=[run_id]
            )
            self.assertEqual(build_goal_journey(paths, "goal-prep")["stage"], "preparation")

            record_goal_checkpoint(paths, "goal-prep", "Started", status="in_progress")
            self.assertEqual(build_goal_journey(paths, "goal-prep")["stage"], "activity")

            record_goal_blocker(paths, "goal-prep", "Waiting on review authority", mark_goal_blocked=True)
            self.assertEqual(build_goal_journey(paths, "goal-prep")["stage"], "blocked")

            create_goal_ledger(paths, "Cancelled goal", ["Criterion one"], goal_id="goal-cancelled")
            cancel_goal_ledger(paths, "goal-cancelled", reason="Superseded")
            self.assertEqual(build_goal_journey(paths, "goal-cancelled")["stage"], "cancelled")

            create_goal_ledger(paths, "Finished goal", ["Criterion one"], goal_id="goal-done")
            record_goal_checkpoint(
                paths, "goal-done", "Landed", criteria_refs=["AC001"], evidence_refs=["unit"]
            )
            complete_goal_ledger(paths, "goal-done", evidence_refs=["unit"])
            finished = build_goal_journey(paths, "goal-done", now=PINNED_NOW)
            self.assertEqual(finished["stage"], "verified_complete")
            self.assertTrue(finished["completion"]["ready"])
            self.assertEqual(finished["completion"]["blocking_gate_ids"], [])

    def test_building_the_journey_writes_nothing_to_the_local_store(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            paths = _paths(root)
            _session_id, run_id = _prepared_handoff_run(paths)
            create_goal_ledger(
                paths,
                "Read-only projection",
                ["Criterion one"],
                goal_id="goal-readonly",
                linked_runtime_runs=[run_id],
            )
            record_goal_checkpoint(
                paths, "goal-readonly", "Landed", criteria_refs=["AC001"], evidence_refs=["unit"]
            )
            before = _tree_snapshot(paths.omh_home)

            build_goal_journey(paths, "goal-readonly", now=PINNED_NOW)

            self.assertEqual(_tree_snapshot(paths.omh_home), before)

    def test_two_projections_of_an_unchanged_goal_are_identical(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            paths = _paths(root)
            create_goal_ledger(paths, "Deterministic goal", ["Criterion one"], goal_id="goal-stable")
            record_goal_checkpoint(
                paths, "goal-stable", "Landed", criteria_refs=["AC001"], evidence_refs=["unit"]
            )

            first = build_goal_journey(paths, "goal-stable", now=PINNED_NOW)
            second = build_goal_journey(paths, "goal-stable", now=PINNED_NOW)

            self.assertEqual(json.dumps(first, sort_keys=True), json.dumps(second, sort_keys=True))
            # No wall clock inside the payload: omitting `now` is also stable.
            self.assertEqual(
                json.dumps(build_goal_journey(paths, "goal-stable"), sort_keys=True),
                json.dumps(build_goal_journey(paths, "goal-stable"), sort_keys=True),
            )

    def test_evidence_freshness_is_unknown_without_now_and_stale_past_the_window(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            paths = _paths(root)
            create_goal_ledger(paths, "Aging goal", ["Criterion one"], goal_id="goal-fresh")
            record_goal_checkpoint(
                paths, "goal-fresh", "Landed", criteria_refs=["AC001"], evidence_refs=["unit"]
            )
            undated = build_goal_journey(paths, "goal-fresh")
            recorded_at = str(_criterion(undated, "AC001")["evidence_recorded_at"])
            recorded = datetime.fromisoformat(recorded_at.replace("Z", "+00:00"))

            self.assertEqual(_criterion(undated, "AC001")["evidence_freshness"], "unknown")
            self.assertIsNone(_criterion(undated, "AC001")["evidence_age_seconds"])

            fresh = build_goal_journey(paths, "goal-fresh", now=_stamp(recorded + timedelta(hours=1)))
            self.assertEqual(_criterion(fresh, "AC001")["evidence_freshness"], "fresh")
            self.assertEqual(_criterion(fresh, "AC001")["evidence_age_seconds"], 3600)

            stale_at = recorded + timedelta(seconds=GOAL_JOURNEY_FRESH_WINDOW_SECONDS + 60)
            stale = build_goal_journey(paths, "goal-fresh", now=_stamp(stale_at))
            self.assertEqual(_criterion(stale, "AC001")["evidence_freshness"], "stale")

            # A clock that disagrees is reported as unknown, never as fresh.
            skewed = build_goal_journey(paths, "goal-fresh", now=_stamp(recorded - timedelta(hours=1)))
            self.assertEqual(_criterion(skewed, "AC001")["evidence_freshness"], "unknown")

    def test_checkpoint_history_is_tail_bounded_without_changing_the_verdict(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            paths = _paths(root)
            create_goal_ledger(paths, "Long goal", ["Criterion one"], goal_id="goal-long")
            satisfying = record_goal_checkpoint(
                paths, "goal-long", "Landed", criteria_refs=["AC001"], evidence_refs=["unit"]
            )["checkpoints"][0]["checkpoint_id"]
            extra = GOAL_JOURNEY_CHECKPOINT_LIMIT + 5
            for index in range(extra):
                record_goal_checkpoint(
                    paths, "goal-long", f"Filler {index}", status="in_progress", mutation_id=f"cp-{index:03d}"
                )

            journey = build_goal_journey(paths, "goal-long", now=PINNED_NOW)

            self.assertEqual(len(journey["checkpoints"]), GOAL_JOURNEY_CHECKPOINT_LIMIT)
            self.assertEqual(journey["checkpoint_history"]["total"], extra + 1)
            self.assertTrue(journey["checkpoint_history"]["truncated"])
            # The satisfying checkpoint fell off the emitted tail and the
            # criterion is still satisfied: bounding output must never move a
            # verdict.
            self.assertNotIn(satisfying, [item["checkpoint_id"] for item in journey["checkpoints"]])
            self.assertEqual(_criterion(journey, "AC001")["satisfied_by_checkpoints"], [satisfying])
            self.assertEqual(_criterion(journey, "AC001")["journey_status"], "satisfied")

    def test_a_deleted_linked_run_is_a_missing_edge_not_a_crash(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            paths = _paths(root)
            create_goal_ledger(
                paths,
                "Goal with a vanished run",
                ["Criterion one"],
                goal_id="goal-missing-run",
                linked_runtime_runs=["missing-run"],
            )

            journey = build_goal_journey(paths, "goal-missing-run", now=PINNED_NOW)

            self.assertEqual([item["found"] for item in journey["runs"]], [False])
            self.assertEqual(journey["handoffs"], [])
            self.assertEqual(journey["owners"], [])
            self.assertIn("runtime_run:missing-run", journey["completion"]["blocking_gate_ids"])
            self.assertEqual(validate_goal_journey(journey), [])


class GoalJourneyValidationTests(unittest.TestCase):
    def _journey(self, root: Path) -> dict[str, object]:
        paths = _paths(root)
        create_goal_ledger(paths, "Validated goal", ["Criterion one"], goal_id="goal-valid")
        record_goal_checkpoint(
            paths, "goal-valid", "Landed", criteria_refs=["AC001"], evidence_refs=["unit"]
        )
        return build_goal_journey(paths, "goal-valid", now=PINNED_NOW)

    def test_validator_accepts_a_built_journey(self) -> None:
        with TemporaryDirectory() as tmp:
            self.assertEqual(validate_goal_journey(self._journey(Path(tmp))), [])

    def test_validator_refuses_a_criterion_satisfied_without_accepted_evidence(self) -> None:
        with TemporaryDirectory() as tmp:
            journey = self._journey(Path(tmp))
            journey["criteria"][0]["satisfied_by_checkpoints"] = []

            errors = validate_goal_journey(journey)

            self.assertIn("criteria[1] is satisfied without accepted evidence", errors)

    def test_validator_refuses_completion_ready_while_a_gate_lacks_evidence(self) -> None:
        with TemporaryDirectory() as tmp:
            journey = self._journey(Path(tmp))
            journey["required_gates"][0]["evidence_accepted"] = False
            journey["completion"]["blocking_gate_ids"] = ["criterion:AC001"]

            errors = validate_goal_journey(journey)

            self.assertIn("completion.ready must be false while a required gate lacks evidence", errors)
            self.assertIn("completion.unsatisfied_required_gates must count the blocking gates", errors)

    def test_validator_refuses_a_raw_objective_and_a_weakened_claim_boundary(self) -> None:
        with TemporaryDirectory() as tmp:
            journey = self._journey(Path(tmp))
            journey["objective"] = "the raw text"
            journey["claim_boundary"] = "This proves the work is done."
            journey["not_evidence"] = ["goal_completion"]

            errors = validate_goal_journey(journey)

            self.assertIn("raw objective field is not allowed", errors)
            self.assertIn(
                "claim_boundary must deny that the projection is execution, review, CI, or merge evidence",
                errors,
            )
            self.assertIn("not_evidence must include all goal journey boundaries", errors)

    def test_validator_refuses_a_foreign_schema_and_an_unsupported_stage(self) -> None:
        errors = validate_goal_journey({"schema_version": "goal_status_card/v1", "stage": "shipped"})

        self.assertIn("schema_version must be goal_journey/v1", errors)
        self.assertIn("stage is unsupported", errors)
        self.assertIn("goal_id is required", errors)
        self.assertIn("objective_hash must be a sha256 hex digest", errors)


class GoalJourneyRenderTests(unittest.TestCase):
    def test_rendered_lines_use_dashes_and_end_with_the_claim_boundary(self) -> None:
        with TemporaryDirectory() as tmp:
            paths = _paths(Path(tmp))
            create_goal_ledger(
                paths, "Rendered goal", ["Criterion one", "Criterion two"], goal_id="goal-render"
            )
            record_goal_checkpoint(
                paths, "goal-render", "Landed", criteria_refs=["AC001"], evidence_refs=["unit"]
            )

            text = render_goal_journey_text(build_goal_journey(paths, "goal-render", now=PINNED_NOW))

            self.assertTrue(text.startswith("Goal goal-render — activity"))
            self.assertIn("- AC001: Criterion one — satisfied, evidence accepted", text)
            self.assertIn("- AC002: Criterion two — pending, no accepted evidence", text)
            self.assertIn("- criterion:AC002 (acceptance_criterion)", text)
            self.assertTrue(text.endswith(GOAL_JOURNEY_CLAIM_BOUNDARY))
            # Messenger surfaces drop markdown tables, so the renderer never
            # emits one.
            self.assertNotIn("|", text)


class GoalJourneyCliTests(unittest.TestCase):
    def _create(self, root: Path) -> None:
        paths = _paths(root)
        create_goal_ledger(paths, "CLI goal", ["Criterion one"], goal_id="goal-cli-journey")
        record_goal_checkpoint(
            paths, "goal-cli-journey", "Landed", criteria_refs=["AC001"], evidence_refs=["unit"]
        )

    def test_journey_defaults_to_plain_text_and_opts_into_json(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            self._create(root)
            command = _base(root) + ["goal", "journey", "--goal", "goal-cli-journey"]

            status, stdout, stderr = run_cli(command, output_json=False)

            self.assertEqual(status, 0, stderr)
            self.assertTrue(stdout.startswith("Goal goal-cli-journey"), stdout)
            self.assertIn(GOAL_JOURNEY_CLAIM_BOUNDARY, stdout)

            json_status, json_stdout, json_stderr = run_cli(command + ["--json"], output_json=False)

            self.assertEqual(json_status, 0, json_stderr)
            payload = json.loads(json_stdout)["journey"]
            self.assertEqual(payload["schema_version"], GOAL_JOURNEY_SCHEMA_VERSION)
            self.assertEqual(validate_goal_journey(payload), [])

    def test_omh_output_json_reaches_the_machine_payload_without_the_flag(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            self._create(root)

            # run_cli sets OMH_OUTPUT=json, which is how wrappers ask for the
            # payload without spelling --json on every call.
            status, stdout, stderr = run_cli(_base(root) + ["goal", "journey", "--goal", "goal-cli-journey"])

            self.assertEqual(status, 0, stderr)
            self.assertEqual(json.loads(stdout)["journey"]["goal_id"], "goal-cli-journey")

    def test_missing_goal_reports_a_readable_error_not_a_traceback(self) -> None:
        with TemporaryDirectory() as tmp:
            status, stdout, stderr = run_cli(
                _base(Path(tmp)) + ["goal", "journey", "--goal", "no-such-goal"], output_json=False
            )

            self.assertEqual(status, 2)
            self.assertEqual(stdout, "")
            self.assertTrue(stderr.startswith("omh: "), stderr)
            self.assertNotIn("Traceback", stderr)


def _stamp(value: datetime) -> str:
    return value.astimezone(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


if __name__ == "__main__":
    unittest.main()
