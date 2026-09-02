from __future__ import annotations

from copy import deepcopy
import json
import os
import stat
import unittest
from datetime import datetime, timezone
from pathlib import Path
from tempfile import TemporaryDirectory

from _local_package import load_local_package
from _platform_support import requires_posix_permissions

load_local_package()
from omh.paths import resolve_paths
from omh.chat_router import route_chat_message, routing_record_payload
from omh.coding_delegation import build_coding_delegation_payload, coding_delegation_record_payload
from omh.coding.executor_local_workflow import build_executor_local_workflow
from omh.executor_progress import build_progress_binding, build_safe_progress_signal, observe_executor_progress, write_progress_binding
from omh.runtime_artifacts import (
    DEFAULT_RUN_HISTORY_LIMIT,
    append_journal_observation,
    create_prepared_coding_delegation_run,
    create_run,
    export_runtime,
    list_runs,
    new_run_id,
    show_run,
    summarize_delegated_coding_status,
    summarize_runtime_observation_status,
    update_state,
    validate_coding_delegation_record,
    validate_ci_record,
    validate_delegation_record,
    validate_merge_record,
    validate_review_record,
    validate_routing_record,
    validate_runtime,
    validate_runtime_observation_record,
    validate_run_record,
    validate_wrapper_record,
    write_ci_record,
    write_coding_delegation,
    write_delegation,
    write_merge_record,
    write_review_record,
    write_routing_decision,
    write_runtime_observation,
    write_wrapper_contract,
)
from omh.runtime import records as runtime_records
from omh.runtime.records import (
    OPTIONAL_RUNTIME_STORE_VALIDATORS,
    RUNTIME_OBSERVATION_EVENTS,
    build_coding_delegation_record,
    validate_coding_executor_handoff,
    validate_coding_prompt_handoff,
    validate_coding_runtime_handoff,
    validate_executor_prompting_contract,
    validate_isolation_plan,
)


class RuntimeArtifactTests(unittest.TestCase):
    def test_executor_local_workflow_round_trips_all_handoff_lanes(self) -> None:
        cases = (
            ("codex", "executor_handoff", True),
            ("omx-runtime", "runtime_handoff", False),
        )

        for profile, handoff_key, parent_dispatchable in cases:
            with self.subTest(profile=profile):
                payload = build_coding_delegation_payload(
                    "$ultrawork complete the goal",
                    executor_target=profile,
                )
                handoff = payload[handoff_key]
                binding = build_executor_local_workflow(
                    profile=profile,
                    routed_workflow="ultrawork",
                    parent_handoff_dispatchable=parent_dispatchable,
                )
                self.assertIsNotNone(binding)
                handoff["executor_local_workflow"] = binding

                record_input = coding_delegation_record_payload(
                    payload,
                    "$ultrawork complete the goal",
                )
                compacted = build_coding_delegation_record(record_input)
                replayed = json.loads(json.dumps(compacted))

                self.assertEqual(replayed[handoff_key]["executor_local_workflow"], binding)
                validator = validate_coding_executor_handoff if handoff_key == "executor_handoff" else validate_coding_runtime_handoff
                self.assertEqual(validator(replayed[handoff_key]), [])

        prompt = build_coding_delegation_payload(
            "$ultrawork complete the goal",
            executor_target="claude-code",
        )
        compacted_prompt = build_coding_delegation_record(
            coding_delegation_record_payload(prompt, "$ultrawork complete the goal")
        )["prompt_handoff"]
        self.assertNotIn("executor_local_workflow", compacted_prompt)
        self.assertEqual(validate_coding_prompt_handoff(compacted_prompt), [])

    def test_executor_local_workflow_compaction_is_idempotent(self) -> None:
        payload = build_coding_delegation_payload(
            "$ultrawork complete the goal",
            executor_target="codex",
        )
        binding = build_executor_local_workflow(
            profile="codex",
            routed_workflow="ultrawork",
            parent_handoff_dispatchable=True,
        )
        self.assertIsNotNone(binding)
        payload["executor_handoff"]["executor_local_workflow"] = binding

        first = build_coding_delegation_record(
            coding_delegation_record_payload(payload, "$ultrawork complete the goal")
        )
        second = build_coding_delegation_record(first)

        self.assertEqual(second["executor_handoff"], first["executor_handoff"])

    def test_legacy_handoffs_without_local_workflow_still_validate(self) -> None:
        cases = (
            ("codex", "executor_handoff", validate_coding_executor_handoff),
            ("claude-code", "prompt_handoff", validate_coding_prompt_handoff),
            ("omx-runtime", "runtime_handoff", validate_coding_runtime_handoff),
        )

        for profile, handoff_key, validator in cases:
            with self.subTest(profile=profile):
                handoff = build_coding_delegation_payload(
                    "risky refactor",
                    executor_target=profile,
                )[handoff_key]
                handoff.pop("executor_local_workflow", None)

                self.assertNotIn("executor_local_workflow", handoff)
                self.assertEqual(validator(handoff), [])

    def test_executor_local_workflow_rejects_cross_field_forgery(self) -> None:
        executor = build_coding_delegation_payload(
            "$ultrawork complete the goal",
            executor_target="codex",
        )["executor_handoff"]
        executor_binding = build_executor_local_workflow(
            profile="codex",
            routed_workflow="ultrawork",
            parent_handoff_dispatchable=True,
        )
        self.assertIsNotNone(executor_binding)
        executor["executor_local_workflow"] = executor_binding

        mutations = (
            ("profile", lambda value: value["executor_local_workflow"].__setitem__("profile", "hermes"), "executor_local_workflow.profile"),
            (
                "candidate",
                lambda value: value["executor_local_workflow"]["candidate"].__setitem__("skill_id", "ralph"),
                "executor_local_workflow.candidate.skill_id",
            ),
            (
                "availability",
                lambda value: value["executor_local_workflow"].__setitem__("status", "observed_available"),
                "executor_local_workflow.status",
            ),
            (
                "legacy_codex",
                lambda value: value.__setitem__("codex_skill", "$ralph"),
                "executor_local_workflow.routed_workflow",
            ),
            (
                "extra_key",
                lambda value: value["executor_local_workflow"]["candidate"].__setitem__("dispatch", True),
                "executor_local_workflow.candidate",
            ),
            (
                "malformed",
                lambda value: value["executor_local_workflow"].__setitem__("candidate", []),
                "executor_local_workflow.candidate",
            ),
        )
        for name, mutate, expected_path in mutations:
            with self.subTest(name=name):
                forged = deepcopy(executor)
                mutate(forged)
                self.assertIn(expected_path, json.dumps(validate_coding_executor_handoff(forged)))

        runtime = build_coding_delegation_payload(
            "$ultrawork complete the goal",
            executor_target="omx-runtime",
        )["runtime_handoff"]
        runtime_binding = build_executor_local_workflow(
            profile="omx-runtime",
            routed_workflow="ultrawork",
            parent_handoff_dispatchable=False,
        )
        self.assertIsNotNone(runtime_binding)
        runtime["executor_local_workflow"] = runtime_binding
        runtime["executor_local_workflow"]["dispatchability"]["candidate_invocation_dispatchable"] = True

        runtime_errors = json.dumps(validate_coding_runtime_handoff(runtime))
        self.assertIn("executor_local_workflow.dispatchability.candidate_invocation_dispatchable", runtime_errors)

        prompt = build_coding_delegation_payload(
            "$ultrawork complete the goal",
            executor_target="claude-code",
        )["prompt_handoff"]
        prompt["executor_local_workflow"] = deepcopy(executor_binding)
        self.assertIn(
            "executor_local_workflow.profile",
            json.dumps(validate_coding_prompt_handoff(prompt)),
        )

    def test_executor_handoff_rejects_dispatch_template_when_candidate_is_not_dispatchable(self) -> None:
        handoff = build_coding_delegation_payload(
            "$ultrawork complete the goal",
            executor_target="codex",
        )["executor_handoff"]
        self.assertFalse(
            handoff["executor_local_workflow"]["dispatchability"]["candidate_invocation_dispatchable"]
        )
        handoff["codex_invocation"]["dispatch_text_template"] = "$ai-slop-cleaner {message}"

        errors = validate_coding_executor_handoff(handoff)

        self.assertTrue(any("codex_invocation.dispatch_text_template" in error for error in errors), errors)

    def test_executor_handoff_rejects_observed_binding_without_matching_snapshot_evidence(self) -> None:
        handoff = build_coding_delegation_payload(
            "$ultrawork complete the goal",
            executor_target="codex",
        )["executor_handoff"]
        binding = build_executor_local_workflow(
            profile="codex",
            routed_workflow="ultrawork",
            parent_handoff_dispatchable=True,
            availability_evidence={
                "status": "host_observed",
                "scope": {
                    "profile": "codex",
                    "skill_id": "ultrawork",
                    "environment": "local",
                },
                "recorded_at": "2026-08-02T12:00:01+09:00",
                "observed_at": "2026-08-02T12:00:00+09:00",
                "evidence_ref": "operator:local-workflow-check",
            },
        )
        self.assertIsNotNone(binding)
        handoff["executor_local_workflow"] = binding
        handoff["codex_invocation"]["dispatch_text_template"] = "$ultrawork {message}"

        errors = validate_coding_executor_handoff(handoff)

        self.assertTrue(any("executor_capability_snapshot" in error for error in errors), errors)

    def test_executor_local_workflow_rejects_truthy_non_booleans(self) -> None:
        cases = (
            ("codex", "executor_handoff", True, validate_coding_executor_handoff),
            ("omx-runtime", "runtime_handoff", False, validate_coding_runtime_handoff),
        )
        mutations = (
            ("handoff_dispatchable", 1),
            ("candidate_invocation_dispatchable", "false"),
        )

        for profile, handoff_key, parent_dispatchable, validator in cases:
            for field, forged_value in mutations:
                with self.subTest(profile=profile, field=field):
                    handoff = build_coding_delegation_payload(
                        "$ultrawork complete the goal",
                        executor_target=profile,
                    )[handoff_key]
                    binding = build_executor_local_workflow(
                        profile=profile,
                        routed_workflow="ultrawork",
                        parent_handoff_dispatchable=parent_dispatchable,
                    )
                    self.assertIsNotNone(binding)
                    binding["dispatchability"][field] = forged_value
                    handoff["executor_local_workflow"] = binding

                    errors = json.dumps(validator(handoff))

                    self.assertIn(f"executor_local_workflow.dispatchability.{field}", errors)

    def test_descriptor_only_runtime_profiles_allow_no_executable_templates(self) -> None:
        for profile in ("omo-runtime", "omc-runtime"):
            with self.subTest(profile=profile):
                handoff = build_coding_delegation_payload(
                    "$ultrawork complete the goal",
                    executor_target=profile,
                )["runtime_handoff"]

                self.assertEqual(handoff["runtime_templates"], [])
                self.assertEqual(validate_coding_runtime_handoff(handoff), [])

        for profile in ("hermes", "omx-runtime"):
            with self.subTest(profile=profile):
                handoff = build_coding_delegation_payload(
                    "$ultrawork complete the goal",
                    executor_target=profile,
                )["runtime_handoff"]
                handoff["runtime_templates"] = []

                self.assertIn(
                    "runtime_templates",
                    json.dumps(validate_coding_runtime_handoff(handoff)),
                )

    def test_new_run_id_is_stable_and_slugged(self) -> None:
        now = datetime(2026, 6, 4, 12, 1, 2, tzinfo=timezone.utc)

        self.assertEqual(new_run_id(now, "Coding Handling!"), "20260604T120102000000Z-coding-handling")

    def test_create_run_writes_run_events_and_state(self) -> None:
        with TemporaryDirectory() as tmp:
            paths = resolve_paths(Path(tmp) / ".omh", Path(tmp) / ".hermes")

            run = create_run(paths, {"skill": "oh-my-hermes", "harness": "coding-handling", "status": "started"})

            run_dir = paths.runtime_runs_dir / run["run_id"]
            self.assertTrue((run_dir / "run.json").exists())
            self.assertTrue((run_dir / "events.jsonl").exists())
            self.assertTrue((run_dir / "evidence").is_dir())
            self.assertEqual(json.loads(paths.runtime_state_path.read_text(encoding="utf-8"))["last_run_id"], run["run_id"])
            self.assertEqual(list_runs(paths)[0]["run_id"], run["run_id"])
            self.assertEqual(validate_run_record(run), [])

    def test_create_prepared_coding_delegation_run_has_explicit_boundary(self) -> None:
        with TemporaryDirectory() as tmp:
            paths = resolve_paths(Path(tmp) / ".omh", Path(tmp) / ".hermes")

            run = create_prepared_coding_delegation_run(
                paths,
                {"skill": "ai-slop-cleaner", "harness": "coding-handling", "trigger": "coding:discord:delegate"},
            )

            self.assertEqual(run["status"], "prepared")
            self.assertEqual(run["artifact_kind"], "prepared_coding_delegation")
            self.assertEqual(run["phase"], "prepared")
            self.assertEqual(run["observation_status"], "prepared_not_observed")
            self.assertEqual(validate_run_record(run), [])
            shown = show_run(paths, run["run_id"])
            self.assertTrue(shown["lifecycle"]["prepared_handoff"])
            self.assertEqual(shown["lifecycle"]["observation_status"], "prepared_not_observed")
            self.assertEqual(shown["lifecycle"]["latest_event"]["event"], "prepared_handoff_created")

    def test_create_run_does_not_collide_for_rapid_same_harness_records(self) -> None:
        with TemporaryDirectory() as tmp:
            paths = resolve_paths(Path(tmp) / ".omh", Path(tmp) / ".hermes")

            first = create_run(paths, {"skill": "oh-my-hermes", "harness": "coding-handling", "status": "started"})
            second = create_run(paths, {"skill": "oh-my-hermes", "harness": "coding-handling", "status": "started"})

            self.assertNotEqual(first["run_id"], second["run_id"])
            self.assertEqual(len(list_runs(paths)), 2)

    def test_show_and_export_report_malformed_jsonl_without_crashing(self) -> None:
        with TemporaryDirectory() as tmp:
            paths = resolve_paths(Path(tmp) / ".omh", Path(tmp) / ".hermes")
            run = create_run(paths, {"skill": "oh-my-hermes", "harness": "coding-handling", "status": "started"})
            events_path = paths.runtime_runs_dir / run["run_id"] / "events.jsonl"
            with events_path.open("a", encoding="utf-8") as handle:
                handle.write("{not json\n")

            shown = show_run(paths, run["run_id"])
            exported = export_runtime(paths, redacted=False)
            validation = validate_runtime(paths, run["run_id"])

            self.assertIn("event_errors", shown)
            self.assertTrue(any("Expecting property name" in error for error in shown["event_errors"]))
            self.assertEqual(exported["runs"][0]["event_errors"], shown["event_errors"])
            self.assertFalse(validation["ok"])
            self.assertTrue(any("Expecting property name" in error for error in validation["runs"][0]["errors"]))

    def _run_with_journal_history(self, paths, count: int) -> dict[str, object]:
        run = create_run(paths, {"skill": "oh-my-hermes", "harness": "coding-handling", "status": "started"})
        for index in range(count):
            append_journal_observation(
                paths,
                {
                    "target_type": "run",
                    "target_id": run["run_id"],
                    "run_id": run["run_id"],
                    "event": "executor_dispatch" if index == 0 else "runtime_start",
                    "status": "observed",
                    "summary": f"observation {index}",
                },
            )
        return run

    def test_show_run_tail_bounds_history_and_reports_totals(self) -> None:
        with TemporaryDirectory() as tmp:
            paths = resolve_paths(Path(tmp) / ".omh", Path(tmp) / ".hermes")
            run = self._run_with_journal_history(paths, 25)

            shown = show_run(paths, run["run_id"])

            self.assertEqual(len(shown["journal_events"]), DEFAULT_RUN_HISTORY_LIMIT)
            self.assertTrue(shown["history"]["truncated"])
            self.assertEqual(shown["history"]["limit"], DEFAULT_RUN_HISTORY_LIMIT)
            self.assertEqual(shown["history"]["journal_events"], {"total": 25, "shown": 20, "omitted": 5})
            self.assertEqual(shown["journal_events"][-1]["summary"], "observation 24")
            self.assertTrue(shown["history"]["full_history_command"].endswith("--full"))
            self.assertIn("journal", shown["history"]["full_history_artifacts"]["journal_events"])

    def test_show_run_repeated_calls_cost_the_same_bounded_output(self) -> None:
        with TemporaryDirectory() as tmp:
            paths = resolve_paths(Path(tmp) / ".omh", Path(tmp) / ".hermes")
            run = self._run_with_journal_history(paths, 40)
            first = len(json.dumps(show_run(paths, run["run_id"]), sort_keys=True))

            for index in range(40):
                append_journal_observation(
                    paths,
                    {
                        "target_type": "run",
                        "target_id": run["run_id"],
                        "run_id": run["run_id"],
                        "event": "runtime_start",
                        "status": "observed",
                        "summary": f"later observation {index}",
                    },
                )
            second_shown = show_run(paths, run["run_id"])

            self.assertEqual(second_shown["history"]["journal_events"]["total"], 80)
            self.assertEqual(len(second_shown["journal_events"]), DEFAULT_RUN_HISTORY_LIMIT)
            # Doubling history must not double emitted context.
            self.assertLess(len(json.dumps(second_shown, sort_keys=True)), first * 2)

    def test_show_run_full_history_is_available_through_explicit_opt_out(self) -> None:
        with TemporaryDirectory() as tmp:
            paths = resolve_paths(Path(tmp) / ".omh", Path(tmp) / ".hermes")
            run = self._run_with_journal_history(paths, 25)

            shown = show_run(paths, run["run_id"], history_limit=None)

            self.assertEqual(len(shown["journal_events"]), 25)
            self.assertFalse(shown["history"]["truncated"])
            self.assertIsNone(shown["history"]["limit"])

    def test_bounded_show_run_never_changes_lifecycle_conclusions(self) -> None:
        with TemporaryDirectory() as tmp:
            paths = resolve_paths(Path(tmp) / ".omh", Path(tmp) / ".hermes")
            run = self._run_with_journal_history(paths, 40)

            bounded = show_run(paths, run["run_id"])
            unbounded = show_run(paths, run["run_id"], history_limit=None)

            # The dispatch observation is older than the emitted tail; the
            # lifecycle projection must still see it.
            self.assertIn("executor_dispatch_observed", [event["event"] for event in unbounded["journal_events"]])
            self.assertNotIn("executor_dispatch_observed", [event["event"] for event in bounded["journal_events"]])
            self.assertTrue(bounded["lifecycle"]["prompt_dispatched"])
            self.assertEqual(bounded["lifecycle"], unbounded["lifecycle"])
            self.assertEqual(bounded["lifecycle"]["journal_event_count"], 40)

    def test_summary_surfaces_keep_reading_full_history(self) -> None:
        with TemporaryDirectory() as tmp:
            paths = resolve_paths(Path(tmp) / ".omh", Path(tmp) / ".hermes")
            run = self._run_with_journal_history(paths, 40)

            exported = export_runtime(paths, redacted=False)

            exported_run = next(
                entry for entry in exported["runs"] if entry["run"]["run_id"] == run["run_id"]
            )
            self.assertEqual(len(exported_run["journal_events"]), 40)
            self.assertFalse(exported_run["history"]["truncated"])

    def test_runtime_listing_and_summary_export_can_be_bounded(self) -> None:
        with TemporaryDirectory() as tmp:
            paths = resolve_paths(Path(tmp) / ".omh", Path(tmp) / ".hermes")
            runs = [
                create_run(paths, {"run_id": "20260604T000001000000Z-one", "skill": "oh-my-hermes", "harness": "coding-handling"}),
                create_run(paths, {"run_id": "20260604T000002000000Z-two", "skill": "oh-my-hermes", "harness": "coding-handling"}),
                create_run(paths, {"run_id": "20260604T000003000000Z-three", "skill": "oh-my-hermes", "harness": "coding-handling"}),
            ]

            self.assertEqual([run["run_id"] for run in list_runs(paths, limit=2)], [runs[1]["run_id"], runs[2]["run_id"]])

            exported = export_runtime(paths, redacted=False, limit=1, full=False)

            self.assertEqual(exported["export"]["limit"], 1)
            self.assertFalse(exported["export"]["full"])
            self.assertEqual([run["run_id"] for run in exported["runs"]], [runs[2]["run_id"]])
            self.assertNotIn("events", exported["runs"][0])

    @requires_posix_permissions
    def test_runtime_artifacts_are_private_even_with_permissive_umask(self) -> None:
        with TemporaryDirectory() as tmp:
            old_umask = os.umask(0o022)
            try:
                paths = resolve_paths(Path(tmp) / ".omh", Path(tmp) / ".hermes")
                run = create_run(paths, {"skill": "oh-my-hermes", "harness": "coding-handling", "status": "started"})
            finally:
                os.umask(old_umask)

            run_dir = paths.runtime_runs_dir / run["run_id"]
            self.assertEqual(stat.S_IMODE(paths.runtime_dir.stat().st_mode), 0o700)
            self.assertEqual(stat.S_IMODE(run_dir.stat().st_mode), 0o700)
            self.assertEqual(stat.S_IMODE((run_dir / "run.json").stat().st_mode), 0o600)
            self.assertEqual(stat.S_IMODE((run_dir / "events.jsonl").stat().st_mode), 0o600)

    def test_write_delegation_preserves_observed_boundary(self) -> None:
        with TemporaryDirectory() as tmp:
            paths = resolve_paths(Path(tmp) / ".omh", Path(tmp) / ".hermes")
            run = create_run(paths, {"skill": "oh-my-hermes", "harness": "critic", "status": "completed"})

            delegation = write_delegation(
                paths.runtime_runs_dir / run["run_id"],
                {"requested": True, "observed": False, "result": "not_observed", "evidence_refs": ["run.json"]},
            )

            self.assertTrue(delegation["requested"])
            self.assertFalse(delegation["observed"])
            shown = show_run(paths, run["run_id"])
            self.assertEqual(shown["delegation"]["result"], "not_observed")

    def test_write_delegation_rejects_contradictory_observation(self) -> None:
        with TemporaryDirectory() as tmp:
            paths = resolve_paths(Path(tmp) / ".omh", Path(tmp) / ".hermes")
            run = create_run(paths, {"skill": "oh-my-hermes", "harness": "critic", "status": "completed"})

            with self.assertRaises(ValueError):
                write_delegation(paths.runtime_runs_dir / run["run_id"], {"observed": True, "result": "not_observed"})
            with self.assertRaises(ValueError):
                write_delegation(paths.runtime_runs_dir / run["run_id"], {"observed": False, "result": "completed"})

    def test_write_wrapper_contract_records_observed_boundaries(self) -> None:
        with TemporaryDirectory() as tmp:
            paths = resolve_paths(Path(tmp) / ".omh", Path(tmp) / ".hermes")
            run = create_run(paths, {"skill": "oh-my-hermes", "harness": "coding-handling", "status": "started"})

            wrapper = write_wrapper_contract(
                paths.runtime_runs_dir / run["run_id"],
                {
                    "prompt_dispatched": True,
                    "hermes_response_observed": True,
                    "verification_observed": False,
                    "completion_status": "blocked",
                    "unobserved_gaps": ["separate specialist lane not exposed"],
                },
            )

            self.assertTrue(wrapper["prompt_dispatched"])
            self.assertFalse(wrapper["verification_observed"])
            shown = show_run(paths, run["run_id"])
            self.assertEqual(shown["wrapper"]["completion_status"], "blocked")
            self.assertIn("wrapper_contract_recorded", {event["event"] for event in shown["events"]})

    def test_write_routing_decision_records_pre_dispatch_metadata(self) -> None:
        with TemporaryDirectory() as tmp:
            paths = resolve_paths(Path(tmp) / ".omh", Path(tmp) / ".hermes")
            run = create_run(paths, {"skill": "ai-slop-cleaner", "harness": "coding-handling", "status": "started"})
            message = "Research current install friction, make a reviewed plan, implement with Codex, and run code review."
            decision = route_chat_message(message, source="discord")

            routing = write_routing_decision(
                paths.runtime_runs_dir / run["run_id"],
                routing_record_payload(decision, message, source_event_id="m1"),
            )

            self.assertEqual(routing["selected_skill"], "ultrawork")
            self.assertEqual(routing["source_event_id"], "m1")
            self.assertEqual(routing["workflow_route_plan"]["schema_version"], "workflow_route_plan/v1")
            # The persisted plan records the canonical public labels (#1249);
            # `selected_skill` above stays the catalog key the router resolved.
            self.assertEqual(
                [(step["stage"], step["skill"]) for step in routing["workflow_route_plan"]["steps"]],
                [
                    ("research", "ulw-research"),
                    ("plan", "ulw-plan"),
                    ("deliver", "ulw-work"),
                    ("review", "code-review"),
                ],
            )
            self.assertNotIn(message, json.dumps(routing["workflow_route_plan"]))
            self.assertTrue(validate_runtime(paths, run["run_id"])["ok"])
            shown = show_run(paths, run["run_id"])
            self.assertEqual(shown["routing"]["action"], "dispatch")
            self.assertIn("routing_decision_recorded", {event["event"] for event in shown["events"]})

    def test_write_routing_decision_sanitizes_full_route_decision(self) -> None:
        with TemporaryDirectory() as tmp:
            paths = resolve_paths(Path(tmp) / ".omh", Path(tmp) / ".hermes")
            run = create_run(paths, {"skill": "ai-slop-cleaner", "harness": "coding-handling", "status": "started"})
            secret_message = "risky refactor with private-token-123"

            routing = write_routing_decision(
                paths.runtime_runs_dir / run["run_id"],
                route_chat_message(secret_message, source="discord"),
            )

            serialized = json.dumps(routing)
            self.assertNotIn(secret_message, serialized)
            self.assertNotIn("suggested_prompt", serialized)
            self.assertEqual(
                set(routing["recommendations"][0]),
                {"skill", "score", "confidence", "matched", "reasoning_demand"},
            )
            self.assertIn(routing["recommendations"][0]["reasoning_demand"], {"light", "standard", "heavy"})
            legacy_routing = deepcopy(routing)
            legacy_routing["recommendations"][0].pop("reasoning_demand")
            self.assertEqual(validate_routing_record(legacy_routing), [])

    def test_write_coding_delegation_sanitizes_full_payload(self) -> None:
        with TemporaryDirectory() as tmp:
            paths = resolve_paths(Path(tmp) / ".omh", Path(tmp) / ".hermes")
            run = create_run(paths, {"skill": "ai-slop-cleaner", "harness": "coding-handling", "status": "started"})
            secret_message = "risky refactor with private-token-123"
            payload = build_coding_delegation_payload(
                secret_message,
                source="discord",
                include_message=True,
                source_metadata={"source_event_id": "m1", "unsupported": "drop-me"},
            )
            payload["suggested_prompt"] = "do not store"

            coding_delegation = write_coding_delegation(paths.runtime_runs_dir / run["run_id"], payload)

            serialized = json.dumps(coding_delegation)
            self.assertNotIn(secret_message, serialized)
            self.assertNotIn("delegation_prompt", serialized)
            self.assertNotIn("suggested_prompt", serialized)
            self.assertNotIn("drop-me", serialized)
            self.assertEqual(coding_delegation["message_length"], len(secret_message))
            self.assertEqual(coding_delegation["source_metadata"], {"source_event_id": "m1"})
            self.assertEqual(
                set(coding_delegation["recommendation_evidence"][0]),
                {"skill", "score", "confidence", "matched", "reasoning_demand"},
            )
            self.assertIn(
                coding_delegation["recommendation_evidence"][0]["reasoning_demand"],
                {"light", "standard", "heavy"},
            )
            legacy_delegation = deepcopy(coding_delegation)
            legacy_delegation["recommendation_evidence"][0].pop("reasoning_demand")
            self.assertEqual(validate_coding_delegation_record(legacy_delegation), [])
            self.assertEqual(coding_delegation["harness_quality"]["schema_version"], "harness_quality/v1")
            self.assertEqual(coding_delegation["harness_quality"]["harness"], "coding-handling")
            self.assertIn("coding_delegation_prepared", coding_delegation["harness_quality"]["evidence_ladder"])
            self.assertEqual(
                coding_delegation["harness_quality"]["wrapper_actions"],
                ["show_prompt_handoff", "copy_prompt_handoff", "choose_executor", "show_status"],
            )
            self.assertTrue(coding_delegation["acceptance_criteria"])
            self.assertTrue(coding_delegation["verification"])
            self.assertTrue(validate_runtime(paths, run["run_id"])["ok"])

    def test_write_coding_delegation_preserves_prompting_contract_across_profiles(self) -> None:
        with TemporaryDirectory() as tmp:
            paths = resolve_paths(Path(tmp) / ".omh", Path(tmp) / ".hermes")
            cases = (
                ("codex", "executor_handoff"),
                ("claude-code", "prompt_handoff"),
                ("hermes", "runtime_handoff"),
                ("omx-runtime", "runtime_handoff"),
            )
            message = "Safely refactor src/example.py and add focused tests."

            for executor, handoff_key in cases:
                with self.subTest(executor=executor):
                    run = create_run(
                        paths,
                        {
                            "skill": "safe-feature-change",
                            "harness": "coding-handling",
                            "status": "started",
                        },
                    )
                    payload = build_coding_delegation_payload(
                        message,
                        executor_target=executor,
                    )

                    record = write_coding_delegation(
                        paths.runtime_runs_dir / run["run_id"],
                        coding_delegation_record_payload(payload, message),
                    )

                    prompting_contract = record[handoff_key]["executor_prompting_contract"]
                    self.assertEqual(prompting_contract["profile"], executor)
                    self.assertEqual(prompting_contract["strategy"], "risk_aware_change")
                    self.assertEqual(prompting_contract["status"], "prepared_not_observed")
                    self.assertIn(
                        "{required_action}",
                        prompting_contract["steering_delta_template"],
                    )

    def test_validate_coding_delegation_rejects_top_level_raw_prompt(self) -> None:
        with TemporaryDirectory() as tmp:
            paths = resolve_paths(Path(tmp) / ".omh", Path(tmp) / ".hermes")
            run = create_prepared_coding_delegation_run(
                paths,
                {"skill": "ai-slop-cleaner", "harness": "coding-handling"},
            )
            record = write_coding_delegation(
                paths.runtime_runs_dir / run["run_id"],
                build_coding_delegation_payload("risky refactor", source="discord", executor_target="codex", include_message=True),
            )
            record["message"] = "risky refactor"

            errors = validate_coding_delegation_record(record)

            self.assertTrue(any("unsupported keys" in error and "message" in error for error in errors))

    def test_v1_handoff_validators_accept_legacy_records_without_new_optional_contracts(self) -> None:
        executor = build_coding_delegation_payload("risky refactor", executor_target="codex")["executor_handoff"]
        prompt = build_coding_delegation_payload("risky refactor", executor_target="claude-code")["prompt_handoff"]
        runtime = build_coding_delegation_payload("risky refactor", executor_target="omx-runtime")["runtime_handoff"]

        for handoff, validator in (
            (executor, validate_coding_executor_handoff),
            (prompt, validate_coding_prompt_handoff),
            (runtime, validate_coding_runtime_handoff),
        ):
            legacy = deepcopy(handoff)
            del legacy["executor_local_capability_strategy"]
            del legacy["executor_capability_snapshot"]
            del legacy["task_prompt_contract"]
            del legacy["executor_prompting_contract"]
            if "local_capability_report_contract" in legacy:
                del legacy["local_capability_report_contract"]
            if "session_observation_contract" in legacy:
                del legacy["session_observation_contract"]

            self.assertEqual(validator(legacy), [])

    def test_v1_handoff_validators_reject_invalid_capability_snapshots_when_present(self) -> None:
        executor = build_coding_delegation_payload("risky refactor", executor_target="codex")["executor_handoff"]
        prompt = build_coding_delegation_payload("risky refactor", executor_target="claude-code")["prompt_handoff"]
        runtime = build_coding_delegation_payload("risky refactor", executor_target="omx-runtime")["runtime_handoff"]

        for handoff, validator in (
            (executor, validate_coding_executor_handoff),
            (prompt, validate_coding_prompt_handoff),
            (runtime, validate_coding_runtime_handoff),
        ):
            invalid_shape = deepcopy(handoff)
            invalid_shape["executor_capability_snapshot"] = None
            self.assertIn("executor_capability_snapshot must be an object", json.dumps(validator(invalid_shape)))

            mismatched_executor = deepcopy(handoff)
            mismatched_executor["executor_capability_snapshot"]["executor"] = "wrong-profile"
            self.assertIn("executor must match selected executor profile", json.dumps(validator(mismatched_executor)))

            unobserved_claim = deepcopy(handoff)
            unobserved_claim["executor_capability_snapshot"]["capabilities"]["parallel_agents"] = {
                "status": "host_observed"
            }
            self.assertIn("host_observed capability requires", json.dumps(validator(unobserved_claim)))

    def test_write_coding_delegation_preserves_legacy_executor_handoff_without_local_capability_strategy(self) -> None:
        with TemporaryDirectory() as tmp:
            paths = resolve_paths(Path(tmp) / ".omh", Path(tmp) / ".hermes")
            run = create_prepared_coding_delegation_run(
                paths,
                {"skill": "ai-slop-cleaner", "harness": "coding-handling"},
            )
            payload = build_coding_delegation_payload(
                "risky refactor",
                source="discord",
                executor_target="codex",
                include_message=True,
            )
            del payload["executor_handoff"]["executor_local_capability_strategy"]

            record = write_coding_delegation(paths.runtime_runs_dir / run["run_id"], payload)

            self.assertNotIn("executor_local_capability_strategy", record["executor_handoff"])
            self.assertEqual(validate_coding_delegation_record(record), [])
            self.assertTrue(validate_runtime(paths, run["run_id"])["ok"])

    def test_v1_handoff_validators_still_reject_invalid_local_capability_strategy_when_present(self) -> None:
        executor = build_coding_delegation_payload("risky refactor", executor_target="codex")["executor_handoff"]
        prompt = build_coding_delegation_payload("risky refactor", executor_target="claude-code")["prompt_handoff"]
        runtime = build_coding_delegation_payload("risky refactor", executor_target="omx-runtime")["runtime_handoff"]

        for handoff, validator in (
            (executor, validate_coding_executor_handoff),
            (prompt, validate_coding_prompt_handoff),
            (runtime, validate_coding_runtime_handoff),
        ):
            non_object = deepcopy(handoff)
            non_object["executor_local_capability_strategy"] = None
            self.assertIn("executor_local_capability_strategy must be an object", json.dumps(validator(non_object)))

            invalid_schema = deepcopy(handoff)
            invalid_schema["executor_local_capability_strategy"]["schema_version"] = "executor_local_capability_strategy/v0"
            self.assertIn("executor_local_capability_strategy schema_version is invalid", json.dumps(validator(invalid_schema)))

            invalid_boundary = deepcopy(handoff)
            invalid_boundary["executor_local_capability_strategy"]["claim_boundary"] = "Use local capability output as evidence."
            self.assertIn(
                "executor_local_capability_strategy claim_boundary must preserve evidence boundary",
                json.dumps(validator(invalid_boundary)),
            )

    def test_v1_handoff_validators_reject_invalid_local_capability_report_contract_when_present(self) -> None:
        executor = build_coding_delegation_payload("risky refactor", executor_target="codex")["executor_handoff"]
        prompt = build_coding_delegation_payload("risky refactor", executor_target="claude-code")["prompt_handoff"]
        runtime = build_coding_delegation_payload("risky refactor", executor_target="omx-runtime")["runtime_handoff"]

        for handoff, validator in (
            (executor, validate_coding_executor_handoff),
            (prompt, validate_coding_prompt_handoff),
            (runtime, validate_coding_runtime_handoff),
        ):
            non_object = deepcopy(handoff)
            non_object["local_capability_report_contract"] = None
            self.assertIn("local_capability_report_contract must be an object", json.dumps(validator(non_object)))

            invalid_schema = deepcopy(handoff)
            invalid_schema["local_capability_report_contract"]["schema_version"] = "executor_local_capability_report_contract/v0"
            self.assertIn("local_capability_report_contract schema_version is invalid", json.dumps(validator(invalid_schema)))

            missing_usage = deepcopy(handoff)
            missing_usage["local_capability_report_contract"]["required_fields"].remove("local_capabilities_used")
            self.assertIn("required_fields must include required values", json.dumps(validator(missing_usage)))

            invalid_boundary = deepcopy(handoff)
            invalid_boundary["local_capability_report_contract"]["claim_boundary"] = "Use reported capabilities as proof."
            self.assertIn(
                "local_capability_report_contract claim_boundary must preserve prepared-only evidence boundary",
                json.dumps(validator(invalid_boundary)),
            )

    def test_coding_handoff_contract_validators_accept_prompt_and_codex_observation_contracts(self) -> None:
        executor = build_coding_delegation_payload("risky refactor", executor_target="codex")["executor_handoff"]
        prompt = build_coding_delegation_payload("risky refactor", executor_target="claude-code")["prompt_handoff"]
        generic_prompt = build_coding_delegation_payload("risky refactor", executor_target="generic")["prompt_handoff"]
        runtime = build_coding_delegation_payload("risky refactor", executor_target="omx-runtime")["runtime_handoff"]

        self.assertEqual(validate_coding_executor_handoff(executor), [])
        self.assertEqual(validate_coding_prompt_handoff(prompt), [])
        self.assertEqual(validate_coding_prompt_handoff(generic_prompt), [])
        self.assertEqual(validate_coding_runtime_handoff(runtime), [])
        self.assertEqual(executor["task_prompt_contract"]["required_sections"], ["Goal", "Do", "Don't", "Expected result", "Test"])
        self.assertEqual(executor["executor_prompting_contract"]["profile"], "codex")
        self.assertEqual(prompt["executor_prompting_contract"]["profile"], "claude-code")
        self.assertEqual(runtime["executor_prompting_contract"]["profile"], "omx-runtime")
        self.assertIn("Known context", executor["executor_prompting_contract"]["required_sections"])
        self.assertIn("{required_action}", executor["executor_prompting_contract"]["steering_delta_template"])
        self.assertEqual(prompt["task_prompt_contract"]["profile"], "claude-code")
        self.assertEqual(runtime["task_prompt_contract"]["profile"], "omx-runtime")
        self.assertEqual(executor["local_capability_report_contract"]["profile"], "codex")
        self.assertEqual(prompt["local_capability_report_contract"]["profile"], "claude-code")
        self.assertEqual(generic_prompt["local_capability_report_contract"]["profile"], "generic")
        self.assertEqual(runtime["local_capability_report_contract"]["profile"], "omx-runtime")
        self.assertIn("local_capabilities_used", runtime["local_capability_report_contract"]["required_fields"])
        self.assertEqual(executor["session_observation_contract"]["completion_statuses"], ["completed"])
        self.assertIn("waitingOnApproval", executor["session_observation_contract"]["blocker_statuses"])
        self.assertIn("waitingOnUserInput", executor["session_observation_contract"]["blocker_statuses"])
        self.assertEqual(prompt["session_observation_contract"]["schema_version"], "claude_code_session_observation_contract/v1")
        self.assertEqual(prompt["session_observation_contract"]["profile"], "claude-code")
        self.assertIn("session_id", prompt["session_observation_contract"]["identity_fields"])
        self.assertIn("tool_use_status", prompt["session_observation_contract"]["status_fields"])
        self.assertNotIn("session_observation_contract", generic_prompt)
        self.assertNotIn("session_observation_contract", runtime)

        missing_section = deepcopy(executor)
        missing_section["task_prompt_contract"]["required_sections"].remove("Test")
        self.assertIn("required_sections must include required sections", json.dumps(validate_coding_executor_handoff(missing_section)))

        missing_prompt_section = deepcopy(executor["executor_prompting_contract"])
        missing_prompt_section["required_sections"].remove("Evidence boundary")
        self.assertIn(
            "required_sections must include required sections",
            json.dumps(validate_executor_prompting_contract(missing_prompt_section, "prompting", expected_profile="codex")),
        )

        mismatched_executor_source = deepcopy(executor)
        mismatched_executor_source["executor_prompting_contract"]["task_source"] = "accepted_plan_artifact"
        self.assertIn(
            "executor_prompting_contract.task_source must match execution_brief.task_source",
            json.dumps(validate_coding_executor_handoff(mismatched_executor_source)),
        )

        mismatched_runtime_source = deepcopy(runtime)
        mismatched_runtime_source["executor_prompting_contract"]["task_source"] = "accepted_plan_artifact"
        self.assertIn(
            "executor_prompting_contract.task_source must match runtime_brief.task_source",
            json.dumps(validate_coding_runtime_handoff(mismatched_runtime_source)),
        )

        leaked_prompt = deepcopy(generic_prompt)
        leaked_prompt["session_observation_contract"] = deepcopy(prompt["session_observation_contract"])
        leaked_prompt_errors = json.dumps(validate_coding_prompt_handoff(leaked_prompt))
        self.assertIn("session_observation_contract is only valid for claude-code", leaked_prompt_errors)

        wrong_claude_contract = deepcopy(prompt)
        wrong_claude_contract["session_observation_contract"] = deepcopy(executor["session_observation_contract"])
        wrong_claude_errors = json.dumps(validate_coding_prompt_handoff(wrong_claude_contract))
        self.assertIn("session_observation_contract schema_version is invalid", wrong_claude_errors)
        self.assertIn("session_observation_contract profile must be claude-code", wrong_claude_errors)

        leaked_runtime = deepcopy(runtime)
        leaked_runtime["session_observation_contract"] = deepcopy(executor["session_observation_contract"])
        leaked_runtime_errors = json.dumps(validate_coding_runtime_handoff(leaked_runtime))
        self.assertIn("unsupported keys", leaked_runtime_errors)
        self.assertIn("must not contain session_observation_contract", leaked_runtime_errors)

        missing_approval_blocker = deepcopy(executor)
        missing_approval_blocker["session_observation_contract"]["blocker_statuses"].remove("waitingOnApproval")
        self.assertIn("blocker_statuses must include required values", json.dumps(validate_coding_executor_handoff(missing_approval_blocker)))

        truncated_final = deepcopy(executor)
        truncated_final["session_observation_contract"]["final_answer_rule"] = "Use any preview text."
        self.assertIn("final_answer_rule must reject truncated previews", json.dumps(validate_coding_executor_handoff(truncated_final)))

    def test_validate_runtime_accepts_legacy_executor_handoff_without_local_capability_strategy(self) -> None:
        with TemporaryDirectory() as tmp:
            paths = resolve_paths(Path(tmp) / ".omh", Path(tmp) / ".hermes")
            run = create_prepared_coding_delegation_run(
                paths,
                {"skill": "ai-slop-cleaner", "harness": "coding-handling"},
            )
            run_dir = paths.runtime_runs_dir / run["run_id"]
            payload = build_coding_delegation_payload("risky refactor", source="discord", executor_target="codex")
            record = write_coding_delegation(
                run_dir,
                coding_delegation_record_payload(payload, "risky refactor"),
            )
            del record["executor_handoff"]["executor_local_capability_strategy"]
            (run_dir / "coding_delegation.json").write_text(json.dumps(record, sort_keys=True), encoding="utf-8")

            result = validate_runtime(paths, run["run_id"])

            self.assertTrue(result["ok"], result)
            self.assertEqual(summarize_delegated_coding_status(paths, run["run_id"])["prepared"]["status"], "prepared_not_observed")

    def test_validate_runtime_handoff_requires_boundary_evidence_contract(self) -> None:
        handoff = build_coding_delegation_payload("risky refactor", executor_target="omx-runtime")["runtime_handoff"]

        self.assertEqual(validate_coding_runtime_handoff(handoff), [])
        self.assertEqual(validate_isolation_plan(handoff["isolation_plan"], "isolation"), [])
        self.assertEqual(handoff["isolation_plan"]["strategy"], "worktree_recommended")
        self.assertIn("runtime_templates", handoff)
        self.assertIn("$ultrawork {message}", {template["command_template"] for template in handoff["runtime_templates"]})
        self.assertEqual(handoff["observation_contract"]["record_schema"], "runtime_observation/v1")
        self.assertIn("worker_result", handoff["observation_contract"]["status_ladder"])

        missing_key = deepcopy(handoff)
        del missing_key["evidence_contract"]["prepared_is_not"]
        self.assertIn("missing keys", json.dumps(validate_coding_runtime_handoff(missing_key)))

        unknown_key = deepcopy(handoff)
        unknown_key["evidence_contract"]["anything"] = ["runtime_start"]
        self.assertIn("unsupported keys", json.dumps(validate_coding_runtime_handoff(unknown_key)))

        empty_boundary = deepcopy(handoff)
        empty_boundary["evidence_contract"]["prepared_is_not"] = []
        self.assertIn("must not be empty", json.dumps(validate_coding_runtime_handoff(empty_boundary)))

        missing_required_boundary = deepcopy(handoff)
        missing_required_boundary["evidence_contract"]["observed_required_for"].remove("worker_dispatch")
        self.assertIn("must include required boundaries", json.dumps(validate_coding_runtime_handoff(missing_required_boundary)))

        missing_worktree_action = deepcopy(handoff["isolation_plan"])
        missing_worktree_action["wrapper_actions"].remove("prepare_worktree")
        self.assertIn("prepare_worktree", json.dumps(validate_isolation_plan(missing_worktree_action, "isolation")))

    def test_validate_hermes_team_path_requires_full_public_contract(self) -> None:
        handoff = build_coding_delegation_payload("coordinate a safe coding team", executor_target="hermes")["runtime_handoff"]

        self.assertEqual(validate_coding_runtime_handoff(handoff), [])
        self.assertEqual(handoff["hermes_coding_harness"]["schema_version"], "hermes_coding_harness/v1")
        self.assertEqual(
            [stage["id"] for stage in handoff["hermes_coding_harness"]["workflow_graph"]],
            ["intake", "scope", "plan", "workspace", "build", "verify", "review", "docs_sync", "pr_prep", "handover"],
        )
        self.assertEqual(
            [lane["id"] for lane in handoff["hermes_coding_harness"]["lanes"]],
            ["builder_lane", "verifier_lane", "reviewer_lane", "docs_lane", "pr_lane"],
        )
        self.assertIn("read-only projection", handoff["hermes_coding_harness"]["claim_boundary"])

        missing_harness_stage = deepcopy(handoff)
        missing_harness_stage["hermes_coding_harness"]["workflow_graph"] = [
            stage
            for stage in missing_harness_stage["hermes_coding_harness"]["workflow_graph"]
            if stage["id"] != "pr_prep"
        ]
        self.assertIn("canonical ordered stages", json.dumps(validate_coding_runtime_handoff(missing_harness_stage)))

        missing_harness_lane = deepcopy(handoff)
        missing_harness_lane["hermes_coding_harness"]["lanes"] = [
            lane
            for lane in missing_harness_lane["hermes_coding_harness"]["lanes"]
            if lane["id"] != "reviewer_lane"
        ]
        self.assertIn("builder/verifier/reviewer/docs/pr lanes", json.dumps(validate_coding_runtime_handoff(missing_harness_lane)))

        missing_durable_goal = deepcopy(handoff)
        missing_durable_goal["hermes_coding_team_path"]["start_modes"] = [
            mode
            for mode in missing_durable_goal["hermes_coding_team_path"]["start_modes"]
            if mode["id"] != "durable_goal"
        ]
        self.assertIn("durable_goal", json.dumps(validate_coding_runtime_handoff(missing_durable_goal)))

        for event_type in RUNTIME_OBSERVATION_EVENTS:
            missing_event = deepcopy(handoff)
            missing_event["hermes_coding_team_path"]["status_ladder"] = [
                event
                for event in missing_event["hermes_coding_team_path"]["status_ladder"]
                if event != event_type
            ]
            self.assertIn(
                "full Hermes coding team ladder",
                json.dumps(validate_coding_runtime_handoff(missing_event)),
                event_type,
            )

        invalid_start_event = deepcopy(handoff)
        invalid_start_event["hermes_coding_team_path"]["start_modes"][0]["first_observed_event"] = "not_a_runtime_event"
        self.assertIn(
            "first_observed_event is unsupported",
            json.dumps(validate_coding_runtime_handoff(invalid_start_event)),
        )

        invalid_action = deepcopy(handoff)
        invalid_action["hermes_coding_team_path"]["wrapper_actions"] = ["claim_done_without_evidence"]
        self.assertIn(
            "wrapper_actions must match the Hermes coding team action contract",
            json.dumps(validate_coding_runtime_handoff(invalid_action)),
        )

    def test_runtime_observation_records_status_ladder_without_claiming_missing_steps(self) -> None:
        with TemporaryDirectory() as tmp:
            paths = resolve_paths(Path(tmp) / ".omh", Path(tmp) / ".hermes")
            run = create_run(paths, {"skill": "oh-my-hermes", "harness": "coding-handling", "status": "started"})
            run_dir = paths.runtime_runs_dir / run["run_id"]

            observation = write_runtime_observation(
                run_dir,
                {
                    "target_type": "run",
                    "target_id": run["run_id"],
                    "runtime_profile": "omx-runtime",
                    "event_type": "runtime_start",
                    "status": "observed",
                    "participants": ["leader"],
                    "summary": "operator started the selected runtime",
                },
            )

            self.assertEqual(validate_runtime_observation_record(observation), [])
            shown = show_run(paths, run["run_id"])
            self.assertEqual(shown["runtime_observations"][0]["event_type"], "runtime_start")
            summary = summarize_runtime_observation_status(shown["runtime_observations"])
            self.assertEqual(summary["observed_events"], ["runtime_start"])
            self.assertEqual(summary["next_action"], "record_runtime_observation:worktree_creation")
            self.assertIn("worktree_creation", summary["missing_events"])

            blocked_worktree = write_runtime_observation(
                run_dir,
                {
                    "target_type": "run",
                    "target_id": run["run_id"],
                    "runtime_profile": "omx-runtime",
                    "event_type": "worktree_creation",
                    "status": "blocked",
                    "summary": "worktree creation blocked before allocation",
                },
            )
            self.assertEqual(blocked_worktree["status"], "blocked")

            with self.assertRaises(ValueError):
                write_runtime_observation(
                    run_dir,
                    {
                        "target_type": "run",
                        "target_id": run["run_id"],
                        "runtime_profile": "omx-runtime",
                        "event_type": "worktree_creation",
                        "status": "observed",
                        "summary": "missing worktree ref",
                    },
                )

            all_not_observed = [
                {
                    "target_type": "run",
                    "target_id": run["run_id"],
                    "runtime_profile": "omx-runtime",
                    "event_type": event_type,
                    "status": "not_observed",
                    "summary": "",
                }
                for event_type in RUNTIME_OBSERVATION_EVENTS
            ]
            not_observed_summary = summarize_runtime_observation_status(all_not_observed)
            self.assertEqual(not_observed_summary["observed_events"], [])
            self.assertEqual(not_observed_summary["next_action"], "record_runtime_observation:runtime_start")
            self.assertIn("runtime_start", not_observed_summary["unsatisfied_events"])

    def test_journal_events_project_run_lifecycle_without_legacy_observation_file(self) -> None:
        with TemporaryDirectory() as tmp:
            paths = resolve_paths(Path(tmp) / ".omh", Path(tmp) / ".hermes")
            run = create_prepared_coding_delegation_run(
                paths,
                {"skill": "ai-slop-cleaner", "harness": "coding-handling"},
            )
            run_dir = paths.runtime_runs_dir / run["run_id"]
            message = "implement safe runtime feature in src/omh/runtime/artifacts.py without overclaiming"
            payload = build_coding_delegation_payload(
                message,
                source="discord",
                executor_target="codex",
            )
            write_coding_delegation(
                run_dir,
                coding_delegation_record_payload(payload, message),
            )

            append_journal_observation(
                paths,
                {
                    "target_type": "run",
                    "target_id": run["run_id"],
                    "run_id": run["run_id"],
                    "event": "executor_dispatch",
                    "status": "observed",
                    "summary": "wrapper dispatched the accepted handoff",
                    "evidence_refs": ["executor-session.json"],
                },
            )
            append_journal_observation(
                paths,
                {
                    "target_type": "run",
                    "target_id": run["run_id"],
                    "run_id": run["run_id"],
                    "event": "executor_result",
                    "status": "observed",
                    "summary": "executor reported completion",
                    "evidence_refs": ["executor-result.json"],
                },
            )

            shown = show_run(paths, run["run_id"])
            status = summarize_delegated_coding_status(paths, run["run_id"])
            exported = export_runtime(paths, redacted=False, run_id=run["run_id"])

            self.assertFalse((run_dir / "runtime_observations.jsonl").exists())
            self.assertEqual(shown["lifecycle"]["journal_event_count"], 3)
            self.assertTrue(shown["lifecycle"]["prompt_dispatched"])
            self.assertTrue(shown["lifecycle"]["execution_observed"])
            self.assertEqual(shown["lifecycle"]["observation_status"], "execution_observed")
            self.assertEqual(shown["lifecycle"]["latest_event"]["event"], "executor_result_observed")
            self.assertTrue(status["execution"]["observed"])
            self.assertTrue(status["wrapper"]["prompt_dispatched"])
            self.assertEqual(exported["export"]["journal_event_count"], 3)
            self.assertEqual({event["run_id"] for event in exported["journal"]["events"]}, {run["run_id"]})
            self.assertTrue(validate_runtime(paths, run["run_id"])["ok"])

    def test_delegated_status_uses_journal_lifecycle_for_runtime_observation_projection(self) -> None:
        with TemporaryDirectory() as tmp:
            paths = resolve_paths(Path(tmp) / ".omh", Path(tmp) / ".hermes")
            run = create_prepared_coding_delegation_run(
                paths,
                {
                    "skill": "coding",
                    "harness": "delegate",
                    "trigger": "test",
                    "privacy": "metadata_only",
                    "inputs_summary": "prepared handoff",
                    "outputs_summary": "prepared",
                    "verification_summary": "prepared_not_observed",
                },
            )
            run_dir = paths.runtime_runs_dir / run["run_id"]
            message = "implement safe runtime feature in src/omh/runtime/artifacts.py without overclaiming"
            payload = build_coding_delegation_payload(message, source="discord", executor_target="codex")
            write_coding_delegation(run_dir, coding_delegation_record_payload(payload, message))

            for event, summary in (
                ("executor_dispatch", "wrapper dispatched the handoff"),
                ("executor_result", "executor reported completion"),
                ("verification", "verification passed"),
            ):
                append_journal_observation(
                    paths,
                    {
                        "target_type": "run",
                        "target_id": run["run_id"],
                        "run_id": run["run_id"],
                        "event": event,
                        "status": "observed",
                        "summary": summary,
                    },
                )

            status = summarize_delegated_coding_status(paths, run["run_id"])

            self.assertEqual(status["next_action"], "record_review_evidence")
            self.assertEqual(status["runtime_observation"]["source"], "lifecycle_projection")
            self.assertEqual(status["runtime_observation"]["next_action"], "record_review_evidence")
            self.assertEqual(
                status["runtime_observation"]["observed_events"],
                ["worker_dispatch", "worker_result", "verification"],
            )
            self.assertNotIn("runtime_start", status["runtime_observation"]["missing_events"])

    def test_validate_runtime_rejects_out_of_order_journal_lifecycle_events(self) -> None:
        with TemporaryDirectory() as tmp:
            paths = resolve_paths(Path(tmp) / ".omh", Path(tmp) / ".hermes")
            run = create_prepared_coding_delegation_run(
                paths,
                {
                    "skill": "coding",
                    "harness": "delegate",
                    "trigger": "test",
                    "privacy": "metadata_only",
                    "inputs_summary": "prepared handoff",
                    "outputs_summary": "prepared",
                    "verification_summary": "prepared_not_observed",
                },
            )
            run_dir = paths.runtime_runs_dir / run["run_id"]
            message = "implement safe runtime feature in src/omh/runtime/artifacts.py without overclaiming"
            payload = build_coding_delegation_payload(message, source="discord", executor_target="codex")
            write_coding_delegation(run_dir, coding_delegation_record_payload(payload, message))

            append_journal_observation(
                paths,
                {
                    "target_type": "run",
                    "target_id": run["run_id"],
                    "run_id": run["run_id"],
                    "event": "executor_dispatch",
                    "status": "observed",
                    "summary": "dispatch observed",
                },
            )
            append_journal_observation(
                paths,
                {
                    "target_type": "run",
                    "target_id": run["run_id"],
                    "run_id": run["run_id"],
                    "event": "executor_result",
                    "status": "observed",
                    "summary": "executor result observed",
                },
            )
            with paths.runtime_journal_events_path.open("a", encoding="utf-8") as handle:
                handle.write(
                    json.dumps(
                        {
                            "schema_version": "omh_observation_event/v1",
                            "event_id": "bad-merge",
                            "target_type": "run",
                            "target_id": run["run_id"],
                            "run_id": run["run_id"],
                            "workflow": "coding",
                            "harness": "delegate",
                            "phase": "prepared",
                            "event": "merge_observed",
                            "status": "observed",
                            "observed_at": "2026-06-24T00:00:00Z",
                            "source": "test",
                            "actor": "",
                            "runtime_profile": "hermes",
                            "evidence_refs": [],
                            "summary": "merge without verification",
                            "privacy": "metadata_only",
                        },
                        sort_keys=True,
                    )
                    + "\n"
                )

            result = validate_runtime(paths, run["run_id"])

            self.assertFalse(result["ok"])
            self.assertFalse(result["journal"]["ok"])
            errors = "\n".join(result["journal"]["errors"])
            self.assertIn("merge_observed requires verification_result_observed", errors)

    def test_validate_runtime_rejects_malformed_journal_records(self) -> None:
        with TemporaryDirectory() as tmp:
            paths = resolve_paths(Path(tmp) / ".omh", Path(tmp) / ".hermes")
            run = create_prepared_coding_delegation_run(
                paths,
                {"skill": "ai-slop-cleaner", "harness": "coding-handling"},
            )
            message = "implement safe runtime feature in src/omh/runtime/artifacts.py without overclaiming"
            payload = build_coding_delegation_payload(
                message,
                source="discord",
                executor_target="codex",
            )
            write_coding_delegation(
                paths.runtime_runs_dir / run["run_id"],
                coding_delegation_record_payload(payload, message),
            )
            with paths.runtime_journal_events_path.open("a", encoding="utf-8") as handle:
                handle.write(
                    json.dumps(
                        {
                            "schema_version": "omh_observation_event/v1",
                            "event_id": "bad-event",
                            "target_type": "run",
                            "target_id": run["run_id"],
                            "run_id": run["run_id"],
                            "event": "not-supported",
                            "status": "observed",
                            "observed_at": "2026-06-24T00:00:00Z",
                            "privacy": "metadata_only",
                            "evidence_refs": [],
                            "summary": "invalid event",
                        },
                        sort_keys=True,
                    )
                    + "\n"
                )

            result = validate_runtime(paths, run["run_id"])

            self.assertFalse(result["ok"])
            self.assertFalse(result["journal"]["ok"])
            self.assertIn("observation_event event is unsupported", "\n".join(result["journal"]["errors"]))

    def test_validate_runtime_requires_coding_delegation_for_prepared_runs(self) -> None:
        with TemporaryDirectory() as tmp:
            paths = resolve_paths(Path(tmp) / ".omh", Path(tmp) / ".hermes")
            run = create_prepared_coding_delegation_run(
                paths,
                {"skill": "ai-slop-cleaner", "harness": "coding-handling"},
            )

            result = validate_runtime(paths, run["run_id"])

            self.assertFalse(result["ok"])
            self.assertTrue(any("missing coding_delegation.json" in error for error in result["runs"][0]["errors"]))

    def test_validate_runtime_rejects_runtime_observations_on_plain_runs(self) -> None:
        with TemporaryDirectory() as tmp:
            paths = resolve_paths(Path(tmp) / ".omh", Path(tmp) / ".hermes")
            run = create_run(paths, {"skill": "oh-my-hermes", "harness": "coding-handling", "status": "started"})
            write_runtime_observation(
                paths.runtime_runs_dir / run["run_id"],
                {
                    "target_type": "run",
                    "target_id": run["run_id"],
                    "runtime_profile": "omx-runtime",
                    "event_type": "runtime_start",
                    "status": "observed",
                    "summary": "manually injected observation",
                },
            )

            result = validate_runtime(paths, run["run_id"])

            self.assertFalse(result["ok"])
            errors = "\n".join(result["runs"][0]["errors"])
            self.assertIn("runtime observations are not valid for a non-runtime coding delegation run", errors)

    def test_validate_runtime_rejects_run_observation_target_mismatch(self) -> None:
        with TemporaryDirectory() as tmp:
            paths = resolve_paths(Path(tmp) / ".omh", Path(tmp) / ".hermes")
            run = create_run(paths, {"skill": "oh-my-hermes", "harness": "coding-handling", "status": "started"})
            run_dir = paths.runtime_runs_dir / run["run_id"]
            write_runtime_observation(
                run_dir,
                {
                    "target_type": "wrapper_session",
                    "target_id": "not-this-run",
                    "runtime_profile": "omx-runtime",
                    "event_type": "runtime_start",
                    "status": "observed",
                    "summary": "misattached observation",
                },
            )

            result = validate_runtime(paths, run["run_id"])

            self.assertFalse(result["ok"])
            errors = "\n".join(result["runs"][0]["errors"])
            self.assertIn("target_type must match containing target 'run'", errors)
            self.assertIn(f"target_id must match containing target '{run['run_id']}'", errors)

    def test_validate_runtime_rejects_prompt_only_handoff_in_prepared_run(self) -> None:
        with TemporaryDirectory() as tmp:
            paths = resolve_paths(Path(tmp) / ".omh", Path(tmp) / ".hermes")
            run = create_prepared_coding_delegation_run(
                paths,
                {"skill": "ai-slop-cleaner", "harness": "coding-handling"},
            )
            run_dir = paths.runtime_runs_dir / run["run_id"]
            payload = build_coding_delegation_payload("risky refactor", source="discord", executor_target="claude-code")
            write_coding_delegation(
                run_dir,
                coding_delegation_record_payload(payload, "risky refactor"),
            )

            result = validate_runtime(paths, run["run_id"])

            self.assertFalse(result["ok"])
            self.assertIn("prompt-only handoff must not be stored as a prepared runtime run", "\n".join(result["runs"][0]["errors"]))

    def test_validate_runtime_rejects_choice_required_payload_in_prepared_run(self) -> None:
        with TemporaryDirectory() as tmp:
            paths = resolve_paths(Path(tmp) / ".omh", Path(tmp) / ".hermes")
            run = create_prepared_coding_delegation_run(
                paths,
                {"skill": "ai-slop-cleaner", "harness": "coding-handling"},
            )
            run_dir = paths.runtime_runs_dir / run["run_id"]
            payload = build_coding_delegation_payload("risky refactor", source="discord", executor_target="choose")
            write_coding_delegation(
                run_dir,
                coding_delegation_record_payload(payload, "risky refactor"),
            )

            result = validate_runtime(paths, run["run_id"])

            self.assertFalse(result["ok"])
            errors = "\n".join(result["runs"][0]["errors"])
            self.assertIn("executor choice must not be stored as a prepared runtime run", errors)
            self.assertIn(
                "prepared runtime run rejected because selected_executor_profile None "
                "has no run-backed executor handoff lifecycle",
                errors,
            )
            self.assertIn("selected_executor_profile in (codex)", errors)

    def test_validate_runtime_rejects_raw_top_level_coding_delegation_key(self) -> None:
        with TemporaryDirectory() as tmp:
            paths = resolve_paths(Path(tmp) / ".omh", Path(tmp) / ".hermes")
            run = create_prepared_coding_delegation_run(
                paths,
                {"skill": "ai-slop-cleaner", "harness": "coding-handling"},
            )
            run_dir = paths.runtime_runs_dir / run["run_id"]
            write_coding_delegation(
                run_dir,
                build_coding_delegation_payload("risky refactor", source="discord", executor_target="codex", include_message=True),
            )
            artifact_path = run_dir / "coding_delegation.json"
            artifact = json.loads(artifact_path.read_text(encoding="utf-8"))
            artifact["message"] = "risky refactor"
            artifact_path.write_text(json.dumps(artifact), encoding="utf-8")

            result = validate_runtime(paths, run["run_id"])

            self.assertFalse(result["ok"])
            self.assertTrue(any("unsupported keys" in error and "message" in error for error in result["runs"][0]["errors"]))

    def test_validate_runtime_rejects_missing_and_invalid_artifacts(self) -> None:
        with TemporaryDirectory() as tmp:
            paths = resolve_paths(Path(tmp) / ".omh", Path(tmp) / ".hermes")
            good = create_run(paths, {"skill": "oh-my-hermes", "harness": "coding-handling", "status": "started"})

            self.assertTrue(validate_runtime(paths, good["run_id"])["ok"])

            bad_dir = paths.runtime_runs_dir / "bad-run"
            bad_dir.mkdir(parents=True)
            (bad_dir / "run.json").write_text('{"schema_version": 1, "run_id": "bad-run", "status": "bogus"}', encoding="utf-8")
            (bad_dir / "events.jsonl").write_text('{"schema_version": 1, "timestamp": "now", "event": "x", "level": "bad", "message": ""}\n', encoding="utf-8")

            result = validate_runtime(paths)
            self.assertFalse(result["ok"])
            bad = next(item for item in result["runs"] if item["run_id"] == "bad-run")
            self.assertTrue(any("status is invalid" in error for error in bad["errors"]))
            self.assertTrue(any("event level is invalid" in error for error in bad["errors"]))

    def test_record_validators_remain_available_from_runtime_artifacts(self) -> None:
        delegation_errors = validate_delegation_record(
            {
                "schema_version": 1,
                "requested": True,
                "observed": False,
                "participants": [],
                "evidence_refs": [],
                "result": "completed",
            }
        )
        wrapper_errors = validate_wrapper_record(
            {
                "schema_version": 1,
                "prompt_dispatched": True,
                "hermes_response_observed": False,
                "verification_observed": False,
                "completion_status": "missing",
                "unobserved_gaps": [],
            }
        )
        routing_errors = validate_routing_record({"schema_version": 1, "action": "missing", "recommendations": []})
        coding_errors = validate_coding_delegation_record(
            {
                "schema_version": "coding_delegation/v1",
                "record_type": "coding_delegation",
                "updated_at": "now",
                "source": "discord",
                "action": "missing",
                "intent": "cleanup",
                "recommended_workflow": "ai-slop-cleaner",
                "recommended_harness": "coding-handling",
                "executor_profile": "coding-agent",
                "review_required": True,
                "review_workflow": "code-review",
                "message_sha256": "",
                "message_length": 13,
                "source_metadata": {"raw_message": "nope"},
                "recommendation_evidence": [],
                "status": "prepared_not_observed",
            }
        )

        self.assertIn("unobserved delegation requires result not_available or not_observed", delegation_errors)
        self.assertTrue(any("completion_status is invalid" in error for error in wrapper_errors))
        self.assertTrue(any("routing action is invalid" in error for error in routing_errors))
        self.assertTrue(any("coding_delegation action is invalid" in error for error in coding_errors))
        self.assertTrue(any("source_metadata has unsupported keys" in error for error in coding_errors))

    def test_review_ci_merge_records_validate_and_show_under_runtime_run(self) -> None:
        with TemporaryDirectory() as tmp:
            paths = resolve_paths(Path(tmp) / ".omh", Path(tmp) / ".hermes")
            run = create_run(paths, {"skill": "oh-my-hermes", "harness": "coding-handling", "status": "started"})
            run_dir = paths.runtime_runs_dir / run["run_id"]

            review = write_review_record(
                run_dir,
                {"status": "pending", "reviewer": "code-review", "summary": "waiting for review"},
            )
            ci = write_ci_record(
                run_dir,
                {"status": "pending", "provider": "local", "checks": ["unit:pending"]},
            )
            merge = write_merge_record(
                run_dir,
                {"status": "not_observed", "target_branch": "main"},
            )

            self.assertEqual(validate_review_record(review), [])
            self.assertEqual(validate_ci_record(ci), [])
            self.assertEqual(validate_merge_record(merge), [])
            shown = show_run(paths, run["run_id"])
            self.assertEqual(shown["review"]["status"], "pending")
            self.assertEqual(shown["ci"]["checks"][0]["name"], "unit")
            self.assertEqual(shown["merge"]["status"], "not_observed")
            self.assertIn("review_recorded", {event["event"] for event in shown["events"]})
            self.assertIn("ci_recorded", {event["event"] for event in shown["events"]})
            self.assertIn("merge_recorded", {event["event"] for event in shown["events"]})

    def test_show_run_includes_executor_progress_without_gate_evidence(self) -> None:
        with TemporaryDirectory() as tmp:
            paths = resolve_paths(Path(tmp) / ".omh", Path(tmp) / ".hermes")
            run = create_run(paths, {"skill": "oh-my-hermes", "harness": "coding-handling", "status": "started"})
            binding = write_progress_binding(
                paths,
                build_progress_binding(
                    target_type="run",
                    target_id=run["run_id"],
                    executor_profile="codex",
                    codex_session_ref="codex-session-1",
                    now="2026-06-24T00:00:00Z",
                ),
            )
            signal = build_safe_progress_signal(
                executor_profile="codex",
                explicit_event_type="diff_started",
                explicit_summary="Codex started editing files.",
            )
            observe_executor_progress(paths, binding, signal, observed_at="2026-06-24T00:01:00Z")

            shown = show_run(paths, run["run_id"])

            self.assertEqual(shown["executor_progress"]["binding"]["binding_id"], f"run:{run['run_id']}:codex")
            self.assertEqual(shown["executor_progress"]["latest_event"]["event_type"], "diff_started")
            self.assertEqual(shown["executor_progress"]["latest_report"]["event_type"], "diff_started")
            self.assertNotIn("verification", shown["executor_progress"]["binding"])
            self.assertIn("not result", shown["executor_progress"]["latest_event"]["claim_boundary"])

    def test_redacted_export_removes_executor_progress_refs_and_summaries(self) -> None:
        with TemporaryDirectory() as tmp:
            paths = resolve_paths(Path(tmp) / ".omh", Path(tmp) / ".hermes")
            run = create_run(paths, {"skill": "oh-my-hermes", "harness": "coding-handling", "status": "started"})
            binding = write_progress_binding(
                paths,
                build_progress_binding(
                    target_type="run",
                    target_id=run["run_id"],
                    executor_profile="codex",
                    codex_session_ref="codex-secret-session",
                    codex_thread_ref="codex-secret-thread",
                    process_session_id="process-secret",
                    pid=4242,
                    worktree="/tmp/secret-worktree",
                    branch="secret-branch",
                    source="discord-secret-source",
                    channel_ref="secret-channel",
                    thread_ref="secret-thread",
                    delivery_target="secret-delivery",
                    evidence_refs=["secret-evidence"],
                ),
            )
            observe_executor_progress(
                paths,
                binding,
                build_safe_progress_signal(
                    executor_profile="codex",
                    explicit_event_type="diff_started",
                    explicit_summary="secret progress summary",
                    evidence_refs=["secret-evidence"],
                ),
            )

            exported = export_runtime(paths, redacted=True)
            rendered = json.dumps(exported)

            for leaked in (
                "codex-secret-session",
                "codex-secret-thread",
                "process-secret",
                "secret-worktree",
                "secret-branch",
                "discord-secret-source",
                "secret-channel",
                "secret-thread",
                "secret-delivery",
                "secret-evidence",
                "secret progress summary",
            ):
                self.assertNotIn(leaked, rendered)
            self.assertIn("[redacted]", rendered)

    def test_show_run_and_export_drop_invalid_executor_progress_payloads(self) -> None:
        with TemporaryDirectory() as tmp:
            paths = resolve_paths(Path(tmp) / ".omh", Path(tmp) / ".hermes")
            run = create_run(paths, {"skill": "oh-my-hermes", "harness": "coding-handling", "status": "started"})
            binding = write_progress_binding(
                paths,
                build_progress_binding(
                    target_type="run",
                    target_id=run["run_id"],
                    executor_profile="codex",
                    codex_session_ref="codex-session-1",
                ),
            )
            progress_dir = paths.runtime_runs_dir / run["run_id"] / "executor_progress"
            (progress_dir / "events.jsonl").write_text(
                json.dumps(
                    {
                        "schema_version": "omh_progress_event/v1",
                        "binding_id": binding["binding_id"],
                        "executor_profile": "codex",
                        "event_type": "diff_started",
                        "status": "running",
                        "summary": "unsafe progress summary",
                        "observed_at": "2026-06-24T00:01:00Z",
                        "transition_fingerprint": "abc",
                        "raw_log": "secret raw executor output",
                        "claim_boundary": "Executor progress is not result evidence.",
                    }
                )
                + "\n",
                encoding="utf-8",
            )
            (progress_dir / "reports.jsonl").write_text(
                json.dumps(
                    {
                        "schema_version": "omh_progress_report/v1",
                        "binding_id": binding["binding_id"],
                        "executor_profile": "codex",
                        "event_type": "diff_started",
                        "status": "running",
                        "summary": "unsafe report summary",
                        "reported_at": "2026-06-24T00:01:00Z",
                        "hidden_reasoning": "secret hidden executor reasoning",
                        "claim_boundary": "Executor progress is not result evidence.",
                    }
                )
                + "\n",
                encoding="utf-8",
            )

            shown = show_run(paths, run["run_id"])
            exported = export_runtime(paths, redacted=True)
            rendered = json.dumps({"shown": shown, "exported": exported})

            self.assertEqual(shown["executor_progress"]["latest_event"], {})
            self.assertEqual(shown["executor_progress"]["latest_report"], {})
            self.assertNotIn("secret raw executor output", rendered)
            self.assertNotIn("secret hidden executor reasoning", rendered)
            self.assertNotIn("unsafe progress summary", rendered)
            self.assertNotIn("unsafe report summary", rendered)

    def test_show_run_and_export_report_malformed_executor_progress_binding(self) -> None:
        with TemporaryDirectory() as tmp:
            paths = resolve_paths(Path(tmp) / ".omh", Path(tmp) / ".hermes")
            run = create_run(paths, {"skill": "oh-my-hermes", "harness": "coding-handling", "status": "started"})
            progress_dir = paths.runtime_runs_dir / run["run_id"] / "executor_progress"
            progress_dir.mkdir(parents=True)
            (progress_dir / "binding.json").write_text("{not json", encoding="utf-8")

            shown = show_run(paths, run["run_id"])
            exported = export_runtime(paths, redacted=True, run_id=run["run_id"])
            rendered = json.dumps({"shown": shown, "exported": exported})

            self.assertEqual(shown["executor_progress"]["state"], "diagnostic_error")
            self.assertEqual(shown["executor_progress"]["binding"], {})
            self.assertIn("binding_errors", shown["executor_progress"])
            self.assertIn("binding_errors", rendered)
            self.assertNotIn("{not json", rendered)

    def test_status_artifact_validators_reject_contradictory_success_claims(self) -> None:
        self.assertIn(
            "review observed=false requires pending or not_observed",
            validate_review_record(
                {
                    "schema_version": 1,
                    "run_id": "run-1",
                    "updated_at": "now",
                    "required": True,
                    "observed": False,
                    "status": "passed",
                    "reviewer": "code-review",
                    "evidence_refs": [],
                    "summary": "",
                }
            ),
        )
        self.assertIn(
            "ci passed status requires all checks to be passed",
            validate_ci_record(
                {
                    "schema_version": 1,
                    "run_id": "run-1",
                    "updated_at": "now",
                    "required": True,
                    "observed": True,
                    "status": "passed",
                    "provider": "local",
                    "checks": [{"name": "unit", "status": "failed"}],
                    "evidence_refs": [],
                    "summary": "",
                }
            ),
        )
        self.assertIn(
            "ci not_required status requires checks to be empty or not_required",
            validate_ci_record(
                {
                    "schema_version": 1,
                    "run_id": "run-1",
                    "updated_at": "now",
                    "required": False,
                    "observed": True,
                    "status": "not_required",
                    "provider": "local",
                    "checks": [{"name": "unit", "status": "failed"}],
                    "evidence_refs": [],
                    "summary": "",
                }
            ),
        )
        self.assertIn(
            "merge merged status requires merge_commit or evidence_refs",
            validate_merge_record(
                {
                    "schema_version": 1,
                    "run_id": "run-1",
                    "updated_at": "now",
                    "observed": True,
                    "ready": True,
                    "merged": True,
                    "status": "merged",
                    "target_branch": "main",
                    "merge_commit": "",
                    "evidence_refs": [],
                    "summary": "",
                }
            ),
        )
        self.assertIn(
            "merge not_ready status requires ready=false",
            validate_merge_record(
                {
                    "schema_version": 1,
                    "run_id": "run-1",
                    "updated_at": "now",
                    "observed": False,
                    "ready": True,
                    "merged": False,
                    "status": "not_ready",
                    "target_branch": "main",
                    "merge_commit": "",
                    "evidence_refs": [],
                    "summary": "",
                }
            ),
        )
        self.assertIn(
            "merge blocked status requires merged=false",
            validate_merge_record(
                {
                    "schema_version": 1,
                    "run_id": "run-1",
                    "updated_at": "now",
                    "observed": True,
                    "ready": False,
                    "merged": True,
                    "status": "blocked",
                    "target_branch": "main",
                    "merge_commit": "",
                    "evidence_refs": [],
                    "summary": "",
                }
            ),
        )

    def test_runtime_validation_rejects_merge_ready_before_upstream_gates(self) -> None:
        with TemporaryDirectory() as tmp:
            paths = resolve_paths(Path(tmp) / ".omh", Path(tmp) / ".hermes")
            run = create_run(paths, {"skill": "oh-my-hermes", "harness": "coding-handling", "status": "started"})
            run_dir = paths.runtime_runs_dir / run["run_id"]
            write_merge_record(run_dir, {"status": "ready", "target_branch": "main"})

            result = validate_runtime(paths, run["run_id"])

            self.assertFalse(result["ok"])
            errors = "\n".join(result["runs"][0]["errors"])
            self.assertIn("merge ready requires completed executor evidence", errors)
            self.assertIn("merge ready requires verification evidence", errors)

    def test_status_reader_does_not_overclaim_contradictory_merge_artifacts(self) -> None:
        with TemporaryDirectory() as tmp:
            paths = resolve_paths(Path(tmp) / ".omh", Path(tmp) / ".hermes")
            run = create_prepared_coding_delegation_run(
                paths,
                {"skill": "ai-slop-cleaner", "harness": "coding-handling"},
            )
            run_dir = paths.runtime_runs_dir / run["run_id"]
            write_coding_delegation(
                run_dir,
                build_coding_delegation_payload("risky refactor", source="discord", executor_target="codex", include_message=True),
            )
            write_wrapper_contract(
                run_dir,
                {
                    "prompt_dispatched": True,
                    "hermes_response_observed": True,
                    "verification_observed": True,
                    "completion_status": "completed",
                },
            )
            write_delegation(run_dir, {"requested": True, "observed": True, "result": "completed"})
            write_review_record(run_dir, {"status": "passed", "reviewer": "code-review", "evidence_refs": ["review"]})
            write_ci_record(run_dir, {"status": "passed", "provider": "local", "checks": ["unit:passed"]})

            for status, ready, merged in (("ready", False, False), ("merged", True, False)):
                (run_dir / "merge.json").write_text(
                    json.dumps(
                        {
                            "schema_version": 1,
                            "run_id": run["run_id"],
                            "updated_at": "now",
                            "observed": True,
                            "ready": ready,
                            "merged": merged,
                            "status": status,
                            "target_branch": "main",
                            "merge_commit": "abc123",
                            "evidence_refs": [],
                            "summary": "",
                        },
                        sort_keys=True,
                    ),
                    encoding="utf-8",
                )

                summary = summarize_delegated_coding_status(paths, run["run_id"])

                self.assertEqual(summary["next_action"], "record_merge_readiness")
                self.assertFalse(summary["merge"]["satisfied"])

    def test_status_reader_preserves_required_review_and_ci_gates(self) -> None:
        with TemporaryDirectory() as tmp:
            paths = resolve_paths(Path(tmp) / ".omh", Path(tmp) / ".hermes")
            run = create_prepared_coding_delegation_run(
                paths,
                {"skill": "ai-slop-cleaner", "harness": "coding-handling"},
            )
            run_dir = paths.runtime_runs_dir / run["run_id"]
            write_coding_delegation(
                run_dir,
                build_coding_delegation_payload("risky refactor", source="discord", executor_target="codex", include_message=True),
            )
            write_wrapper_contract(
                run_dir,
                {
                    "prompt_dispatched": True,
                    "hermes_response_observed": True,
                    "verification_observed": True,
                    "completion_status": "completed",
                },
            )
            write_delegation(run_dir, {"requested": True, "observed": True, "result": "completed"})
            (run_dir / "review.json").write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "run_id": run["run_id"],
                        "updated_at": "now",
                        "required": False,
                        "observed": True,
                        "status": "not_required",
                        "reviewer": "code-review",
                        "evidence_refs": [],
                        "summary": "",
                    },
                    sort_keys=True,
                ),
                encoding="utf-8",
            )

            summary = summarize_delegated_coding_status(paths, run["run_id"])
            result = validate_runtime(paths, run["run_id"])

            self.assertTrue(summary["review"]["required"])
            self.assertFalse(summary["review"]["satisfied"])
            self.assertEqual(summary["next_action"], "record_review_evidence")
            self.assertFalse(result["ok"])
            self.assertIn("review not_required cannot downgrade required review evidence", "\n".join(result["runs"][0]["errors"]))

            write_review_record(run_dir, {"status": "passed", "reviewer": "code-review", "evidence_refs": ["review"]})
            (run_dir / "ci.json").write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "run_id": run["run_id"],
                        "updated_at": "now",
                        "required": False,
                        "observed": True,
                        "status": "not_required",
                        "provider": "local",
                        "checks": [{"name": "unit", "status": "failed"}],
                        "evidence_refs": [],
                        "summary": "",
                    },
                    sort_keys=True,
                ),
                encoding="utf-8",
            )

            summary = summarize_delegated_coding_status(paths, run["run_id"])
            result = validate_runtime(paths, run["run_id"])
            errors = "\n".join(result["runs"][0]["errors"])

            self.assertTrue(summary["ci"]["required"])
            self.assertFalse(summary["ci"]["satisfied"])
            self.assertEqual(summary["next_action"], "record_ci_evidence")
            self.assertFalse(result["ok"])
            self.assertIn("ci not_required cannot downgrade required CI evidence", errors)
            self.assertIn("ci not_required status requires checks to be empty or not_required", errors)

    def test_status_reader_calculates_harness_quality_ladder_progress(self) -> None:
        with TemporaryDirectory() as tmp:
            paths = resolve_paths(Path(tmp) / ".omh", Path(tmp) / ".hermes")
            run = create_prepared_coding_delegation_run(
                paths,
                {"skill": "ai-slop-cleaner", "harness": "coding-handling"},
            )
            run_dir = paths.runtime_runs_dir / run["run_id"]
            write_coding_delegation(
                run_dir,
                build_coding_delegation_payload("risky refactor", source="discord", executor_target="codex", include_message=True),
            )

            summary = summarize_delegated_coding_status(paths, run["run_id"])
            progress = summary["harness_progress"]
            states = {step["id"]: step["state"] for step in progress["steps"]}

            self.assertEqual(progress["schema_version"], "harness_progress/v1")
            self.assertEqual(progress["harness"], "coding-handling")
            self.assertEqual(progress["completed"], 1)
            self.assertFalse(progress["complete"])
            self.assertEqual(progress["next_step"], "executor_dispatch_observed")
            self.assertEqual(states["coding_delegation_prepared"], "complete")
            self.assertEqual(states["executor_dispatch_observed"], "pending")
            self.assertEqual(states["review_ci_merge_recorded_when_required"], "pending")

            write_wrapper_contract(
                run_dir,
                {
                    "prompt_dispatched": True,
                    "hermes_response_observed": True,
                    "verification_observed": True,
                    "completion_status": "completed",
                },
            )
            write_delegation(run_dir, {"requested": True, "observed": True, "result": "completed"})
            write_review_record(run_dir, {"status": "passed", "reviewer": "code-review", "evidence_refs": ["review"]})
            write_ci_record(run_dir, {"status": "passed", "provider": "local", "checks": ["unit:passed"]})
            write_merge_record(run_dir, {"status": "ready", "target_branch": "main", "evidence_refs": ["ci"], "summary": "ready"})

            summary = summarize_delegated_coding_status(paths, run["run_id"])
            progress = summary["harness_progress"]
            states = {step["id"]: step["state"] for step in progress["steps"]}

            self.assertTrue(progress["complete"])
            self.assertEqual(progress["completed"], progress["total"])
            self.assertEqual(progress["next_step"], "")
            self.assertEqual(states["executor_dispatch_observed"], "complete")
            self.assertEqual(states["executor_result_observed"], "complete")
            self.assertEqual(states["verification_recorded"], "complete")
            self.assertEqual(states["review_ci_merge_recorded_when_required"], "complete")

    def test_export_runtime_redacts_sensitive_text_and_preserves_evidence_booleans(self) -> None:
        with TemporaryDirectory() as tmp:
            paths = resolve_paths(Path(tmp) / ".omh", Path(tmp) / ".hermes")
            run = create_run(paths, {"skill": "oh-my-hermes", "harness": "coding-handling", "status": "started"})
            write_wrapper_contract(
                paths.runtime_runs_dir / run["run_id"],
                {
                    "prompt_dispatched": True,
                    "hermes_response_observed": True,
                    "verification_observed": False,
                    "completion_status": "completed",
                    "message": "private-token-123 raw prompt",
                    "prompt_body": "do not export",
                },
            )
            wrapper_path = paths.runtime_runs_dir / run["run_id"] / "wrapper.json"
            wrapper_record = json.loads(wrapper_path.read_text(encoding="utf-8"))
            wrapper_record["prompt_body"] = "do not export"
            wrapper_path.write_text(json.dumps(wrapper_record), encoding="utf-8")

            exported = export_runtime(paths, redacted=True)

            self.assertTrue(exported["redacted"])
            wrapper = exported["runs"][0]["wrapper"]
            self.assertEqual(wrapper["message"], "[redacted]")
            self.assertEqual(wrapper["prompt_body"], "[redacted]")
            self.assertTrue(wrapper["prompt_dispatched"])
            self.assertTrue(wrapper["hermes_response_observed"])
            self.assertFalse(wrapper["verification_observed"])
            self.assertEqual(wrapper["completion_status"], "completed")
            serialized = json.dumps(exported)
            self.assertNotIn("private-token-123", serialized)
            self.assertNotIn("do not export", serialized)

    def test_update_state_merges_patch(self) -> None:
        with TemporaryDirectory() as tmp:
            paths = resolve_paths(Path(tmp) / ".omh", Path(tmp) / ".hermes")

            update_state(paths, {"installed_skills": 18})
            state = update_state(paths, {"last_run_id": "r1"})

            self.assertEqual(state["installed_skills"], 18)
            self.assertEqual(state["last_run_id"], "r1")


_STORE_SCHEMA_VERSIONS = {
    "approval_receipts.jsonl": "approval_receipt/v1",
    "blocked_work_records.jsonl": "blocked_work_record/v1",
    "decision_gates.jsonl": "decision_gate/v1",
    "external_effect_receipts.jsonl": "external_effect_receipt/v1",
    "run_lineage_checkpoints.jsonl": "run_lineage_checkpoint/v1",
    "workspace_bindings.jsonl": "workspace_binding_guard/v1",
}


class OptionalRuntimeStoreValidatorRegistryTests(unittest.TestCase):
    """#846: one registry keyed by store, one consumer that dispatches per store.

    The registry used to be an unkeyed tuple whose consumer read a single store
    and applied every entry to it. A second family could not join without
    validating the first family's records against its own schema, so each new
    store grew a sibling tuple and a near-duplicate reader instead.
    """

    def test_the_registry_names_each_store_once_and_left_no_sibling_tuples(self) -> None:
        names = [entry.store_name for entry in OPTIONAL_RUNTIME_STORE_VALIDATORS]
        self.assertEqual(sorted(names), sorted(set(names)))
        self.assertEqual(set(names), set(_STORE_SCHEMA_VERSIONS))
        forked = sorted(
            name
            for name in vars(runtime_records)
            if name.startswith("OPTIONAL_") and name.endswith("_STORE_VALIDATORS")
        )
        self.assertEqual(forked, ["OPTIONAL_RUNTIME_STORE_VALIDATORS"])

    def test_no_validator_accepts_a_sibling_stores_record(self) -> None:
        # Why the dispatch below has to be per store. If any validator were
        # indifferent to a sibling's records, one shared reader would be safe.
        for entry in OPTIONAL_RUNTIME_STORE_VALIDATORS:
            for sibling in OPTIONAL_RUNTIME_STORE_VALIDATORS:
                if sibling.store_name == entry.store_name:
                    continue
                record = {"schema_version": _STORE_SCHEMA_VERSIONS[sibling.store_name]}
                with self.subTest(validator=entry.store_name, record=sibling.store_name):
                    self.assertTrue(entry.validator(record))

    def test_a_malformed_record_faults_its_own_store_and_no_sibling(self) -> None:
        for entry in OPTIONAL_RUNTIME_STORE_VALIDATORS:
            with self.subTest(store=entry.store_name), TemporaryDirectory() as tmp:
                paths = resolve_paths(Path(tmp) / ".omh", Path(tmp) / ".hermes")
                run_id = create_run(paths, {"skill": "plan"})["run_id"]
                store_path = paths.runtime_journal_dir / entry.store_name
                store_path.parent.mkdir(parents=True, exist_ok=True)
                store_path.write_text(
                    json.dumps({"schema_version": "nonsense", "run_id": run_id, entry.record_id_key: "x-1"})
                    + "\n",
                    encoding="utf-8",
                )

                errors = validate_runtime(paths, run_id)["runs"][0]["errors"]

                self.assertTrue(errors)
                for error in errors:
                    self.assertTrue(error.startswith(f"{store_path}[x-1]: "), error)
                for sibling in OPTIONAL_RUNTIME_STORE_VALIDATORS:
                    if sibling.store_name == entry.store_name:
                        continue
                    for error in errors:
                        self.assertNotIn(sibling.store_name, error)

    def test_a_malformed_record_in_one_store_leaves_an_unrelated_run_clean(self) -> None:
        with TemporaryDirectory() as tmp:
            paths = resolve_paths(Path(tmp) / ".omh", Path(tmp) / ".hermes")
            faulted = create_run(paths, {"skill": "plan"})["run_id"]
            clean = create_run(paths, {"skill": "plan"})["run_id"]
            self.assertNotEqual(faulted, clean)
            store_path = paths.runtime_approval_receipts_path
            store_path.parent.mkdir(parents=True, exist_ok=True)
            store_path.write_text(
                json.dumps({"schema_version": "nonsense", "run_id": faulted, "receipt_id": "x-1"}) + "\n",
                encoding="utf-8",
            )

            by_run = {run["run_id"]: run for run in validate_runtime(paths)["runs"]}

            self.assertFalse(by_run[faulted]["ok"])
            self.assertTrue(by_run[clean]["ok"], by_run[clean]["errors"])


if __name__ == "__main__":
    unittest.main()
