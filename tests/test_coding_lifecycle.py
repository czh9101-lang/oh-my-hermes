from __future__ import annotations

import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from _local_package import load_local_package

load_local_package()
from omh.coding_lifecycle import (
    CodingLifecycleError,
    record_codex_dispatch,
    record_codex_result,
    record_codex_verification,
    report_codex_delegation_lifecycle,
    start_codex_delegation_lifecycle,
)
from omh.memory import capture_project_memory_candidate
from project_identity_fixture import memory_paths as resolve_paths
from omh.profiles.setup import write_setup_profile
from omh.coding.executor_capability_snapshots import (
    build_executor_capability_snapshot,
    executor_capability_snapshot_path,
    write_executor_capability_snapshot,
)


def _write_local_workflow_snapshot(directory: Path, recorded: tuple[str, str, str]) -> None:
    profile, skill_id, status = recorded
    snapshot = build_executor_capability_snapshot(
        executor="codex",
        recorded_at="2026-08-02T12:00:01+09:00",
        capabilities={"local_workflow": {
            "status": status,
            "scope": {"profile": profile, "skill_id": skill_id, "environment": "test-host"},
            "observed_at": "2026-08-02T12:00:00+09:00",
            "evidence_ref": "operator:task3-lifecycle",
        }},
    )
    write_executor_capability_snapshot(executor_capability_snapshot_path(directory, "codex"), snapshot)


class CodingLifecycleTests(unittest.TestCase):
    def test_matching_local_workflow_evidence_binds_before_prompt(self) -> None:
        # Given: matching task-scoped Codex evidence recorded before lifecycle preparation.
        with TemporaryDirectory() as tmp:
            paths = resolve_paths(Path(tmp) / ".omh", Path(tmp) / ".hermes")
            _write_local_workflow_snapshot(
                paths.omh_home / "coding" / "executor-capability-snapshots",
                ("codex", "ai-slop-cleaner", "host_observed"),
            )

            # When: the lifecycle builds and persists its prepared handoff.
            payload = start_codex_delegation_lifecycle(
                paths,
                "risky refactor",
                preferred_workflow="ai-slop-cleaner",
                preferred_workflow_score=10,
                force_coding_handoff=True,
            )

            # Then: one candidate authority controls metadata, prompt, and legacy dispatch fields.
            handoff = payload["coding_delegation"]["executor_handoff"]
            binding = handoff["executor_local_workflow"]
            self.assertEqual((binding["status"], binding["dispatchability"]["candidate_invocation_dispatchable"], handoff["dispatch_policy"], handoff["codex_skill"]), ("observed_available", True, "ask_before_dispatch", "$ai-slop-cleaner"))
            self.assertEqual(handoff["codex_invocation"]["skill"], handoff["codex_skill"])
            self.assertEqual(
                handoff["codex_invocation"]["dispatch_text_template"],
                binding["candidate"]["invocation"]["template"],
            )
            self.assertIn(binding["candidate"]["invocation"]["template"], handoff["prompt_template"])
            self.assertNotIn("risky refactor", json.dumps(payload))

    def test_unknown_unavailable_and_mismatched_workflows_never_inject_invocation(self) -> None:
        cases = (
            (None, "unknown"),
            (("codex", "ai-slop-cleaner", "unavailable"), "observed_unavailable"),
            (("hermes", "ai-slop-cleaner", "host_observed"), "unknown"),
            (("codex", "ultraqa", "host_observed"), "unknown"),
        )
        for recorded, expected_status in cases:
            with self.subTest(recorded=recorded), TemporaryDirectory() as tmp:
                # Given: absent, unavailable, wrong-profile, or wrong-skill persisted evidence.
                paths = resolve_paths(Path(tmp) / ".omh", Path(tmp) / ".hermes")
                if recorded is not None:
                    directory = paths.omh_home / "coding" / "executor-capability-snapshots"
                    _write_local_workflow_snapshot(directory, recorded)

                # When: Codex lifecycle preparation resolves the selected profile.
                payload = start_codex_delegation_lifecycle(
                    paths,
                    "risky refactor",
                    preferred_workflow="ai-slop-cleaner",
                    preferred_workflow_score=10,
                    force_coding_handoff=True,
                )

                # Then: candidate metadata remains prepared and dispatch text stays generic.
                handoff = payload["coding_delegation"]["executor_handoff"]
                binding = handoff["executor_local_workflow"]
                actual = (binding["status"], binding["dispatchability"]["candidate_invocation_dispatchable"], handoff["codex_invocation"]["dispatch_text_template"])
                self.assertEqual(actual, (expected_status, False, "{message}"))
                self.assertNotIn("$ai-slop-cleaner", handoff["prompt_template"])

    def test_started_codex_lifecycle_exposes_progress_reporting_policy(self) -> None:
        with TemporaryDirectory() as tmp:
            paths = resolve_paths(Path(tmp) / ".omh", Path(tmp) / ".hermes")

            payload = start_codex_delegation_lifecycle(paths, "diagnose installation health")

            policy = payload["status"]["progress_reporting_policy"]
            self.assertEqual(policy["schema_version"], "coding_progress_reporting_policy/v1")
            self.assertEqual(policy["mode"], "event_triggered")
            self.assertIn("workflow_started", policy["reportable_events"])
            self.assertIn("dispatch_to_executor", policy["reportable_events"])

    def test_progress_policy_guides_lifecycle_transitions_without_final_only_silence(self) -> None:
        with TemporaryDirectory() as tmp:
            paths = resolve_paths(Path(tmp) / ".omh", Path(tmp) / ".hermes")
            started = start_codex_delegation_lifecycle(paths, "diagnose installation health")
            run_id = started["run"]["run_id"]

            prepared_policy = started["status"]["progress_reporting_policy"]
            self.assertTrue(prepared_policy["final_only_silence_rejected"])
            self.assertTrue(prepared_policy["raw_log_dumping_rejected"])
            self.assertEqual(prepared_policy["state_guidance"]["next_action"], "dispatch_to_executor")
            self.assertIn("workflow_started", prepared_policy["state_guidance"]["reportable_events"])
            self.assertIn("dispatch_to_executor", prepared_policy["state_guidance"]["reportable_events"])

            dispatched = record_codex_dispatch(paths, run_id)
            dispatched_policy = dispatched["status"]["progress_reporting_policy"]
            self.assertEqual(dispatched_policy["state_guidance"]["next_action"], "wait_for_executor_evidence")
            self.assertIn("blocker_encountered", dispatched_policy["state_guidance"]["reportable_events"])
            self.assertIn("targeted_tests_failed", dispatched_policy["state_guidance"]["reportable_events"])
            self.assertIn("targeted_tests_passed", dispatched_policy["state_guidance"]["reportable_events"])

            result = record_codex_result(paths, run_id, result="completed", evidence_refs=["codex-log"])
            result_policy = result["status"]["progress_reporting_policy"]
            self.assertEqual(result_policy["state_guidance"]["next_action"], "record_verification_evidence")
            self.assertIn("full_tests_started", result_policy["state_guidance"]["reportable_events"])

            verified = record_codex_verification(paths, run_id)
            verified_policy = verified["status"]["progress_reporting_policy"]
            self.assertEqual(verified_policy["state_guidance"]["next_action"], "report_completion_with_evidence")
            self.assertIn("full_tests_passed", verified_policy["state_guidance"]["reportable_events"])
            self.assertIn("workflow_completed", verified_policy["state_guidance"]["reportable_events"])

    def test_persisted_handoff_keeps_replay_evidence_without_runtime_use_claim(self) -> None:
        with TemporaryDirectory() as tmp:
            paths = resolve_paths(Path(tmp) / ".omh", Path(tmp) / ".hermes")
            write_setup_profile(paths, memory_mode="auto-safe")
            capture_project_memory_candidate(
                paths,
                "Run diagnostics before changing installation health checks",
                record_type="procedure",
                tags=["diagnose", "installation"],
            )

            lifecycle = start_codex_delegation_lifecycle(paths, "diagnose installation health")
            pack = lifecycle["coding_delegation"]["executor_handoff"]["memory_recall_pack"]
            item = pack["included_records"][0]
            evaluation = item["replay_evaluation"]

            self.assertEqual(evaluation["schema_version"], "omh_memory_replay_evaluation/v1")
            self.assertTrue(evaluation["eligible"])
            self.assertEqual(evaluation["reason_code"], "eligible")
            self.assertEqual(item["revision"], 1)
            self.assertEqual(item["admission_mode"], "approved_auto_safe")
            self.assertEqual(item["source_class"], "omh_local")
            self.assertEqual(item["retention_class"], "standard")
            self.assertNotIn("observed", evaluation)
            self.assertIn("not execution", pack["claim_boundary"])

    def test_start_codex_lifecycle_creates_prepared_handoff_without_raw_message(self) -> None:
        with TemporaryDirectory() as tmp:
            paths = resolve_paths(Path(tmp) / ".omh", Path(tmp) / ".hermes")
            message = "risky refactor with private-token-123"

            payload = start_codex_delegation_lifecycle(paths, message, source="discord", source_metadata={"source_event_id": "m1"})

            run_id = payload["run"]["run_id"]
            record = payload["coding_delegation"]
            self.assertEqual(payload["schema_version"], "coding_lifecycle/v1")
            self.assertEqual(payload["status"]["lifecycle_status"], "prepared")
            self.assertEqual(payload["status"]["next_action"], "dispatch_to_executor")
            self.assertEqual(record["executor_handoff"]["executor_target"], "codex")
            strategy = record["executor_handoff"]["executor_local_capability_strategy"]
            self.assertEqual(strategy["schema_version"], "executor_local_capability_strategy/v1")
            self.assertEqual(strategy["profile"], "codex")
            self.assertFalse(strategy["installation_observed"])
            self.assertFalse(strategy["execution_observed"])
            self.assertIn("plain Codex", strategy["fallback"])
            self.assertFalse(payload["status"]["execution"]["observed"])
            self.assertNotIn(message, json.dumps(payload))
            self.assertTrue((paths.runtime_runs_dir / run_id / "coding_delegation.json").exists())

    def test_lifecycle_records_never_persist_raw_task_message(self) -> None:
        with TemporaryDirectory() as tmp:
            paths = resolve_paths(Path(tmp) / ".omh", Path(tmp) / ".hermes")
            message = "diagnose installation health with private-token-123"

            started = start_codex_delegation_lifecycle(paths, message)
            run_id = started["run"]["run_id"]
            record_codex_dispatch(paths, run_id)
            record_codex_result(paths, run_id, result="completed", evidence_refs=["codex-log"])
            record_codex_verification(paths, run_id)

            run_dir = paths.runtime_runs_dir / run_id
            for artifact in ("coding_delegation.json", "wrapper.json", "delegation.json"):
                self.assertNotIn(message, (run_dir / artifact).read_text(encoding="utf-8"))

    def test_record_codex_dispatch_advances_to_waiting_for_executor_evidence(self) -> None:
        with TemporaryDirectory() as tmp:
            paths = resolve_paths(Path(tmp) / ".omh", Path(tmp) / ".hermes")
            started = start_codex_delegation_lifecycle(paths, "diagnose installation health")

            payload = record_codex_dispatch(paths, started["run"]["run_id"])

            self.assertTrue(payload["wrapper"]["prompt_dispatched"])
            self.assertEqual(payload["status"]["next_action"], "wait_for_executor_evidence")
            self.assertEqual(payload["status"]["lifecycle_status"], "dispatched")
            self.assertFalse(payload["status"]["can_report_completion"])

    def test_record_codex_result_requires_dispatch_first(self) -> None:
        with TemporaryDirectory() as tmp:
            paths = resolve_paths(Path(tmp) / ".omh", Path(tmp) / ".hermes")
            started = start_codex_delegation_lifecycle(paths, "diagnose installation health")

            with self.assertRaises(CodingLifecycleError):
                record_codex_result(paths, started["run"]["run_id"], result="completed")

    def test_blocked_codex_result_surfaces_blocker(self) -> None:
        with TemporaryDirectory() as tmp:
            paths = resolve_paths(Path(tmp) / ".omh", Path(tmp) / ".hermes")
            started = start_codex_delegation_lifecycle(paths, "diagnose installation health")
            run_id = started["run"]["run_id"]
            record_codex_dispatch(paths, run_id)

            payload = record_codex_result(paths, run_id, result="blocked", evidence_refs=["codex-log"])

            self.assertEqual(payload["status"]["next_action"], "surface_executor_blocker")
            self.assertEqual(payload["status"]["lifecycle_status"], "blocked")
            self.assertFalse(payload["status"]["can_report_completion"])

    def test_review_required_blocks_completion_even_after_verification(self) -> None:
        with TemporaryDirectory() as tmp:
            paths = resolve_paths(Path(tmp) / ".omh", Path(tmp) / ".hermes")
            started = start_codex_delegation_lifecycle(paths, "risky refactor")
            run_id = started["run"]["run_id"]
            record_codex_dispatch(paths, run_id)
            record_codex_result(paths, run_id, result="completed", evidence_refs=["codex-log"])

            payload = record_codex_verification(paths, run_id)

            self.assertTrue(payload["status"]["review"]["required"])
            self.assertEqual(payload["status"]["next_action"], "record_review_evidence")
            self.assertFalse(payload["status"]["can_report_completion"])
            self.assertIn("review evidence", payload["status"]["blocking_reason"])

    def test_completion_requires_dispatch_execution_and_verification(self) -> None:
        with TemporaryDirectory() as tmp:
            paths = resolve_paths(Path(tmp) / ".omh", Path(tmp) / ".hermes")
            started = start_codex_delegation_lifecycle(paths, "diagnose installation health")
            run_id = started["run"]["run_id"]
            record_codex_dispatch(paths, run_id)
            record_codex_result(paths, run_id, result="completed", evidence_refs=["codex-log"])

            before = report_codex_delegation_lifecycle(paths, run_id)
            self.assertEqual(before["next_action"], "record_verification_evidence")
            self.assertFalse(before["can_report_completion"])

            after = record_codex_verification(paths, run_id)
            self.assertEqual(after["status"]["next_action"], "report_completion_with_evidence")
            self.assertTrue(after["status"]["can_report_completion"])

    def test_failed_or_gapped_verification_is_not_reportable(self) -> None:
        with TemporaryDirectory() as tmp:
            paths = resolve_paths(Path(tmp) / ".omh", Path(tmp) / ".hermes")
            started = start_codex_delegation_lifecycle(paths, "diagnose installation health")
            run_id = started["run"]["run_id"]
            record_codex_dispatch(paths, run_id)
            record_codex_result(paths, run_id, result="completed", evidence_refs=["codex-log"])

            payload = record_codex_verification(paths, run_id, completion_status="failed", gaps=["tests failed"])

            self.assertFalse(payload["wrapper"]["verification_observed"])
            self.assertEqual(payload["wrapper"]["completion_status"], "failed")
            self.assertEqual(payload["status"]["verification"]["status"], "failed")
            self.assertFalse(payload["status"]["verification"]["satisfied"])
            self.assertEqual(payload["status"]["next_action"], "record_verification_evidence")
            self.assertFalse(payload["status"]["can_report_completion"])
            self.assertIn("tests failed", payload["status"]["wrapper"]["unobserved_gaps"])

    def test_verification_before_executor_result_is_rejected(self) -> None:
        with TemporaryDirectory() as tmp:
            paths = resolve_paths(Path(tmp) / ".omh", Path(tmp) / ".hermes")
            started = start_codex_delegation_lifecycle(paths, "diagnose installation health")
            run_id = started["run"]["run_id"]
            record_codex_dispatch(paths, run_id)

            with self.assertRaises(CodingLifecycleError):
                record_codex_verification(paths, run_id)


if __name__ == "__main__":
    unittest.main()
