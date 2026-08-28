from __future__ import annotations

import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from _local_package import load_local_package

load_local_package()

from omh.coding.prompting import select_executor_prompting_strategy
from omh.coding_delegation import build_coding_delegation_payload, coding_delegation_record_payload
from omh.coding.executor_capability_snapshots import (
    build_executor_capability_snapshot,
    executor_capability_snapshot_path,
    write_executor_capability_snapshot,
)
from omh.runtime.records import (
    validate_coding_executor_handoff,
    validate_coding_prompt_handoff,
    validate_coding_runtime_handoff,
    validate_executor_prompting_contract,
)


class ExecutorPromptingTests(unittest.TestCase):
    def test_observed_codex_candidate_is_ask_before_dispatch(self) -> None:
        # Given: an exact observed-available Codex workflow snapshot.
        with TemporaryDirectory() as tmp:
            directory = Path(tmp)
            snapshot = build_executor_capability_snapshot(
                executor="codex",
                recorded_at="2026-08-02T12:00:01+09:00",
                capabilities={
                    "local_workflow": {
                        "status": "host_observed",
                        "scope": {
                            "profile": "codex",
                            "skill_id": "ai-slop-cleaner",
                            "environment": "test-host",
                        },
                        "observed_at": "2026-08-02T12:00:00+09:00",
                        "evidence_ref": "operator:task3-prompting",
                    }
                },
            )
            write_executor_capability_snapshot(
                executor_capability_snapshot_path(directory, "codex"),
                snapshot,
            )

            # When: the executor handoff is constructed.
            payload = build_coding_delegation_payload(
                "risky refactor src/example.py",
                executor_target="codex",
                preferred_workflow="ai-slop-cleaner",
                preferred_workflow_score=10,
                force_coding_handoff=True,
                capability_snapshot_directory=directory,
            )

            # Then: the candidate is invocable only behind the parent ask-before-dispatch policy.
            handoff = payload["executor_handoff"]
            binding = handoff["executor_local_workflow"]
            self.assertEqual(binding["status"], "observed_available")
            self.assertTrue(binding["dispatchability"]["candidate_invocation_dispatchable"])
            self.assertEqual(handoff["dispatch_policy"], "ask_before_dispatch")
            self.assertTrue(handoff["dispatchable"])
            self.assertEqual(
                handoff["codex_invocation"]["dispatch_text_template"],
                "$ai-slop-cleaner {message}",
            )
            self.assertEqual(validate_coding_executor_handoff(handoff), [])

    def test_noncatalog_preferred_workflow_cannot_replace_guarded_recommendation(self) -> None:
        payload = build_coding_delegation_payload(
            "risky refactor src/example.py",
            executor_target="codex",
            preferred_workflow="rm-rf",
            preferred_workflow_score=10,
            force_coding_handoff=True,
        )

        self.assertEqual(
            (
                payload["delegation"]["recommended_workflow"],
                payload["executor_handoff"]["codex_skill"],
                payload["executor_handoff"]["executor_local_workflow"]["candidate"]["skill_id"],
            ),
            ("ai-slop-cleaner", "$ai-slop-cleaner", "ai-slop-cleaner"),
        )

    def test_runtime_and_prompt_candidates_remain_prepare_only(self) -> None:
        # Given: final guarded workflow routing across mapped runtime and omitted prompt-only profiles.
        cases = (
            ("hermes", "runtime_handoff", "/ulw-work {message}"),
            ("omx-runtime", "runtime_handoff", "$ultrawork {message}"),
            ("omo-runtime", "runtime_handoff", ""),
            ("omc-runtime", "runtime_handoff", ""),
        )
        for profile, handoff_key, candidate_template in cases:
            with self.subTest(profile=profile):
                # When: the mapped handoff is built without matching observed evidence.
                payload = build_coding_delegation_payload(
                    "risky refactor src/example.py",
                    executor_target=profile,
                    preferred_workflow="ultrawork",
                    preferred_workflow_score=10,
                    force_coding_handoff=True,
                )

                # Then: display/reference metadata never becomes runtime invocation authority.
                handoff = payload[handoff_key]
                binding = handoff["executor_local_workflow"]
                self.assertEqual(binding["candidate"]["invocation"]["template"], candidate_template)
                self.assertFalse(binding["dispatchability"]["candidate_invocation_dispatchable"])
                self.assertFalse(handoff["dispatchable"])
                self.assertNotEqual(handoff["invocation"]["dispatch_text_template"], candidate_template)
                if candidate_template:
                    self.assertNotIn(candidate_template, handoff["prompt_template"])

        for profile in ("claude-code", "generic"):
            with self.subTest(profile=profile):
                prompt = build_coding_delegation_payload(
                    "risky refactor src/example.py",
                    executor_target=profile,
                    preferred_workflow="ultrawork",
                    preferred_workflow_score=10,
                    force_coding_handoff=True,
                )["prompt_handoff"]
                self.assertNotIn("executor_local_workflow", prompt)

    def test_handoffs_share_canonical_prompt_sections_across_executor_paths(self) -> None:
        cases = (
            ("codex", "executor_handoff", validate_coding_executor_handoff),
            ("claude-code", "prompt_handoff", validate_coding_prompt_handoff),
            ("hermes", "runtime_handoff", validate_coding_runtime_handoff),
            ("omx-runtime", "runtime_handoff", validate_coding_runtime_handoff),
        )
        expected_sections = (
            "Goal",
            "Do",
            "Don't",
            "Known context",
            "Unknowns and decision rule",
            "Expected result",
            "Test",
            "Progress and blockers",
            "Evidence boundary",
            "Task",
        )

        for executor, key, validator in cases:
            with self.subTest(executor=executor):
                payload = build_coding_delegation_payload(
                    "Safely refactor src/example.py and add focused tests.",
                    executor_target=executor,
                )
                handoff = payload[key]

                self.assertEqual(validator(handoff), [])
                contract = handoff["executor_prompting_contract"]
                self.assertEqual(contract["profile"], executor)
                self.assertEqual(contract["status"], "prepared_not_observed")
                self.assertEqual(contract["strategy"], "risk_aware_change")
                self.assertEqual(contract["required_sections"], list(expected_sections))
                self.assertIn("{changed_constraint}", contract["steering_delta_template"])
                self.assertIn("{verification_target_changed}", contract["steering_delta_template"])
                for section in expected_sections[:-1]:
                    self.assertIn(f"{section}\n", handoff["prompt_template"])
                self.assertIn("Task:\n{message}", handoff["prompt_template"])
                self.assertIn(payload["delegation"]["acceptance_criteria"][0], handoff["prompt_template"])
                self.assertIn(payload["delegation"]["verification"][0], handoff["prompt_template"])

    def test_gpt_sol_codex_handoff_adds_parallel_execution_overlay(self) -> None:
        payload = build_coding_delegation_payload(
            "Implement src/example.py",
            executor_target="codex",
            main_agent_model="gpt-5.6-sol",
        )
        handoff = payload["executor_handoff"]

        self.assertEqual(
            handoff["executor_prompting_contract"]["throughput_overlay"]["mode"],
            "gpt_sol_codex_handoff",
        )
        for rule in handoff["executor_prompting_contract"]["throughput_overlay"]["rules"]:
            self.assertIn(rule, handoff["prompt_template"])
        self.assertEqual(validate_coding_executor_handoff(handoff), [])

    def test_gpt_sol_codex_handoff_adds_eval_batching_guidance(self) -> None:
        payload = build_coding_delegation_payload(
            "Implement src/example.py",
            executor_target="codex",
            main_agent_model="gpt-5.6-sol",
        )
        overlay = payload["executor_handoff"]["executor_prompting_contract"]["throughput_overlay"]

        self.assertEqual(overlay["mode"], "gpt_sol_codex_handoff")
        self.assertEqual(overlay["eval_strategy"], "single_cell_internal_parallel")

    def test_gpt_sol_hermes_ulw_handoff_adds_parallel_execution_overlay(self) -> None:
        payload = build_coding_delegation_payload(
            "Run this goal to completion.",
            executor_target="hermes",
            preferred_workflow="ultrawork",
            preferred_workflow_score=10,
            force_coding_handoff=True,
            main_agent_model="openai/gpt-5.6-sol",
        )
        handoff = payload["runtime_handoff"]

        self.assertEqual(payload["delegation"]["recommended_workflow"], "ultrawork")
        self.assertEqual(
            handoff["executor_prompting_contract"]["throughput_overlay"]["mode"],
            "gpt_hermes_ulw",
        )
        for rule in handoff["executor_prompting_contract"]["throughput_overlay"]["rules"]:
            self.assertIn(rule, handoff["prompt_template"])
        self.assertEqual(validate_coding_runtime_handoff(handoff), [])

    def test_parallel_execution_overlay_is_scoped_to_gpt_sol_routes(self) -> None:
        cases = (
            (
                build_coding_delegation_payload(
                    "Implement src/example.py",
                    executor_target="codex",
                    main_agent_model="claude-fable-5",
                )["executor_handoff"],
                "claude Codex handoff",
            ),
            (
                build_coding_delegation_payload(
                    "Review src/example.py.",
                    executor_target="hermes",
                    preferred_workflow="code-review",
                    preferred_workflow_score=10,
                    force_coding_handoff=True,
                    main_agent_model="gpt-5.6-sol",
                )["runtime_handoff"],
                "GPT Hermes non-ULW handoff",
            ),
            (
                build_coding_delegation_payload(
                    "Run this goal to completion.",
                    executor_target="hermes",
                    preferred_workflow="ultrawork",
                    preferred_workflow_score=10,
                    force_coding_handoff=True,
                    main_agent_model="claude-fable-5",
                )["runtime_handoff"],
                "Claude Hermes ULW handoff",
            ),
        )

        for handoff, label in cases:
            with self.subTest(label=label):
                overlay = handoff["executor_prompting_contract"]["throughput_overlay"]
                self.assertEqual(overlay["mode"], "parallel_handoff")
                self.assertNotIn("eval_strategy", overlay)
                for rule in overlay["rules"]:
                    self.assertIn(rule, handoff["prompt_template"])

    def test_claude_code_handoff_adds_measured_advanced_rules(self) -> None:
        # Given: a Claude-family model on the measured Claude Code surface.
        payload = build_coding_delegation_payload(
            "Implement src/example.py",
            executor_target="claude-code",
            main_agent_model="claude-fable-5",
        )

        # When: the portable prompt handoff is prepared.
        handoff = payload["prompt_handoff"]
        overlay = handoff["executor_prompting_contract"]["throughput_overlay"]

        # Then: Claude gets advanced handoff discipline without an eval strategy.
        self.assertEqual(overlay["mode"], "claude_code_handoff")
        self.assertGreater(len(overlay["rules"]), 2)
        self.assertNotIn("eval_strategy", overlay)
        for rule in overlay["rules"]:
            self.assertIn(rule, handoff["prompt_template"])
        self.assertEqual(validate_coding_prompt_handoff(handoff), [])

    def test_unmeasured_non_gpt_handoffs_keep_base_parallel_guidance(self) -> None:
        # Given: families without a completed advanced-overlay comparison.
        cases = (
            ("generic", "", "unknown"),
            ("hermes", "moonshotai/kimi-k3-ultrafast", "kimi"),
            ("hermes", "gemini-3.1-pro", "gemini"),
        )

        for profile, model, family in cases:
            with self.subTest(family=family):
                # When: a handoff is prepared for the unmeasured surface.
                payload = build_coding_delegation_payload(
                    "Implement src/example.py",
                    executor_target=profile,
                    preferred_workflow="ultrawork" if profile == "hermes" else "",
                    preferred_workflow_score=10 if profile == "hermes" else 0,
                    force_coding_handoff=profile == "hermes",
                    main_agent_model=model,
                )
                handoff = payload["runtime_handoff" if profile == "hermes" else "prompt_handoff"]
                overlay = handoff["executor_prompting_contract"]["throughput_overlay"]

                # Then: no unsupported advanced or eval claim is prepared.
                self.assertEqual(overlay["mode"], "parallel_handoff")
                self.assertEqual(overlay["model_family"], family)
                self.assertEqual(len(overlay["rules"]), 2)
                self.assertNotIn("eval_strategy", overlay)

    def test_strategy_selection_distinguishes_plan_risk_and_repair(self) -> None:
        self.assertEqual(
            select_executor_prompting_strategy(
                intent="coding",
                message="Implement src/example.py validation.",
                has_plan_artifact=False,
                isolation_plan={"risk_level": "low"},
            ),
            "direct_change",
        )
        self.assertEqual(
            select_executor_prompting_strategy(
                intent="coding",
                message="Safely refactor src/payments.py.",
                has_plan_artifact=False,
                isolation_plan={"risk_level": "medium"},
            ),
            "risk_aware_change",
        )
        self.assertEqual(
            select_executor_prompting_strategy(
                intent="review",
                message="Review src/example.py.",
                has_plan_artifact=False,
                isolation_plan={},
            ),
            "review_or_repair",
        )
        payload = build_coding_delegation_payload(
            "Implement the accepted plan in src/example.py.",
            executor_target="codex",
            plan_artifact={"path": ".omh/plans/example.json", "status": "accepted"},
        )
        contract = payload["executor_handoff"]["executor_prompting_contract"]
        self.assertEqual(contract["strategy"], "plan_backed_change")
        self.assertEqual(contract["task_source"], "accepted_plan_artifact")

        draft_payload = build_coding_delegation_payload(
            "Implement the draft plan in src/example.py.",
            executor_target="codex",
            plan_artifact={"path": ".omh/plans/example.json", "status": "draft"},
        )
        draft_contract = draft_payload["executor_handoff"]["executor_prompting_contract"]
        self.assertEqual(draft_contract["strategy"], "plan_backed_change")
        self.assertEqual(draft_contract["task_source"], "draft_plan_artifact")

    def test_docs_consulted_is_a_required_named_artifact_across_handoff_paths(self) -> None:
        # Given: every prepared-handoff lane that carries the prompting contract.
        cases = (
            ("codex", "executor_handoff"),
            ("claude-code", "prompt_handoff"),
            ("hermes", "runtime_handoff"),
        )

        for executor, key in cases:
            with self.subTest(executor=executor):
                # When: the handoff is prepared for SDK-agnostic dispatch.
                handoff = build_coding_delegation_payload(
                    "Implement the payments SDK integration in src/example.py.",
                    executor_target=executor,
                )[key]
                contract = handoff["executor_prompting_contract"]

                # Then: the contract names the checkable report artifact, not an advisory phrase.
                policy = contract["docs_consulted_policy"]
                self.assertIn("Docs consulted:", policy)
                self.assertIn("llms.txt", policy)
                self.assertIn("version", policy)
                self.assertIn("incomplete", policy)
                self.assertIn("none (no SDK/framework surface touched)", policy)
                # And the rendered prompt carries both the rule and the named deliverable.
                self.assertIn(policy, handoff["prompt_template"])
                self.assertIn(
                    "A `Docs consulted:` block with exact URL+version entries",
                    handoff["prompt_template"],
                )

    def test_contract_rejects_missing_or_advisory_docs_consulted_policy(self) -> None:
        payload = build_coding_delegation_payload(
            "Implement the payments SDK integration in src/example.py.",
            executor_target="codex",
        )
        contract = dict(payload["executor_handoff"]["executor_prompting_contract"])

        missing = dict(contract)
        del missing["docs_consulted_policy"]
        errors = validate_executor_prompting_contract(missing, "prompting", expected_profile="codex")
        self.assertTrue(any("missing keys" in error and "docs_consulted_policy" in error for error in errors))

        advisory = dict(contract)
        advisory["docs_consulted_policy"] = "Consult official docs before using an SDK."
        errors = validate_executor_prompting_contract(advisory, "prompting", expected_profile="codex")
        self.assertTrue(
            any("docs_consulted_policy must require the named Docs consulted report block" in error for error in errors)
        )

    def test_contract_rejects_missing_steering_field_and_persists_no_raw_task(self) -> None:
        raw_task = "do not persist this exact raw task in a durable artifact"
        payload = build_coding_delegation_payload(raw_task, executor_target="codex", include_message=True)
        contract = dict(payload["executor_handoff"]["executor_prompting_contract"])
        contract["steering_delta_template"] = contract["steering_delta_template"].replace("{new_evidence}", "")
        errors = validate_executor_prompting_contract(contract, "prompting", expected_profile="codex")
        self.assertTrue(any("steering_delta_template must include {new_evidence}" in error for error in errors))

        record = coding_delegation_record_payload(payload, raw_task)
        self.assertNotIn(raw_task, json.dumps(record))
        self.assertEqual(
            record["executor_handoff"]["executor_prompting_contract"]["task_source"],
            "original_message_at_dispatch_time",
        )


if __name__ == "__main__":
    unittest.main()
