from __future__ import annotations

import unittest

from _local_package import load_local_package

load_local_package()

from omh.coding.coding_delegation import (  # noqa: E402
    _coding_status_request_applies,
    build_coding_delegation_payload,
)


class CodingStatusAgentTermTests(unittest.TestCase):
    """pi-family executor names reach the coding status board classification.

    `_CODING_STATUS_AGENT_TERMS` matches by substring on the lowered message,
    and bare "pi" hides inside "api" and "pipeline" while the token itself is
    owned by Raspberry-Pi physical-device routing — so pi only counts through
    right-bounded forms matched at word boundaries ("raspi status" hides
    "pi status"), and never in raspberry/api context.
    """

    POSITIVE = (
        "how far along is senpi?",
        "pi 진행상황?",
        "pi 세션 상태 알려줘",
        "opencode 진행상황 알려줘",
        "omo runtime status?",
        # The incumbent names keep working alongside the pi family.
        "how far along is codex?",
        "claude code 작업 어디까지 됐어?",
    )
    NEGATIVE = (
        "raspberry pi 진행상황?",
        "raspberry pi status check",
        "api 진행상황 알려줘",
        # Word-boundary guard: "raspi status" and "spi status" contain
        # "pi status" as a raw substring without any raspberry/api blocker term.
        "raspi status check",
        "check spi status",
    )

    def test_pi_family_status_questions_apply_on_the_status_workflow(self) -> None:
        for message in self.POSITIVE:
            with self.subTest(message=message):
                self.assertTrue(_coding_status_request_applies(message.lower(), "ultrawork"))

    def test_raspberry_pi_and_api_context_never_applies(self) -> None:
        for message in self.NEGATIVE:
            with self.subTest(message=message):
                self.assertFalse(_coding_status_request_applies(message.lower(), "ultrawork"))

    def test_status_terms_only_apply_on_the_status_workflow(self) -> None:
        self.assertFalse(_coding_status_request_applies("how far along is senpi?", "loop"))

    def test_an_agent_name_without_a_status_request_never_applies(self) -> None:
        self.assertFalse(_coding_status_request_applies("senpi is a nice tool", "ultraprocess"))


class NamedCodingAgentDelegationTests(unittest.TestCase):
    """The ask bare-token retirement's prerequisite fix, now executor-neutral.

    `ask`'s bare `claude`/`gemini` catalog triggers used to be the only reason a
    request naming Claude Code outranked the retained `executor-runtime-readiness`
    workflow and produced action=delegate. Dropping those tokens without a
    replacement would silently downgrade "Claude Code로 바로 열어줘" to
    action=clarify. The delegation path now detects the named executor directly
    through `routing.coding_route_actions.named_executor_owners` -- and only
    when a single `EXTERNAL_CLI_PROFILES` member (Claude Code or Codex) is the
    sole named owner -- independent of any catalog trigger score. A user who
    names one external CLI with an imperative has already made the explicit
    owner choice, so Codex now reaches the same delegate outcome Claude Code
    always has (#1163's Directive). Naming two owners, a runtime owner such as
    Hermes coding or the omo-runtime family (pi/senpi/opencode), or asking a
    status/diagnostic question about the named executor all keep the clarify
    outcome.
    """

    def test_naming_codex_now_delegates(self) -> None:
        # Renamed from `test_naming_codex_still_clarifies`: naming Codex alone
        # with an imperative now reaches the same delegate outcome naming
        # Claude Code alone always has.
        payload = build_coding_delegation_payload("Codex로 바로 열어줘")
        delegation = payload["delegation"]
        self.assertEqual(delegation["action"], "delegate")
        self.assertEqual(delegation["intent"], "coding")

    def test_naming_codex_with_a_coding_verb_now_delegates(self) -> None:
        payload = build_coding_delegation_payload("codex로 구현해줘")
        delegation = payload["delegation"]
        self.assertEqual(delegation["action"], "delegate")
        self.assertEqual(delegation["intent"], "coding")

    def test_naming_two_owners_still_clarifies(self) -> None:
        payload = build_coding_delegation_payload("claude code랑 codex 중에 골라서 열어줘")
        self.assertEqual(payload["delegation"]["action"], "clarify")

    def test_naming_runtime_owner_still_clarifies(self) -> None:
        # Runtime owners (Hermes coding, the omo-runtime family) are not
        # external CLIs -- `EXTERNAL_CLI_PROFILES` is only `("claude-code",
        # "codex")` -- so naming one alone keeps the retained-workflow clarify
        # outcome even with an identical imperative shape to the Codex/Claude
        # Code cases above.
        hermes_payload = build_coding_delegation_payload("Hermes coding으로 구현해줘")
        self.assertEqual(hermes_payload["delegation"]["action"], "clarify")
        omo_runtime_payload = build_coding_delegation_payload("omo runtime으로 구현해줘")
        self.assertEqual(omo_runtime_payload["delegation"]["action"], "clarify")

    def test_status_question_about_named_codex_still_clarifies(self) -> None:
        # A status/diagnostic question about the named executor is not an
        # imperative delivery request, so it must not gain the delegate
        # outcome the imperative cases above now get.
        for message in (
            "is codex broken",
            "codex 상태 어때",
            "코덱스가 지금 어디까지 했는지 알려줘",
        ):
            with self.subTest(message=message):
                payload = build_coding_delegation_payload(message)
                self.assertEqual(payload["delegation"]["action"], "clarify")

    def test_naming_claude_code_without_a_code_reference_still_delegates(self) -> None:
        payload = build_coding_delegation_payload("Claude Code로 바로 열어줘")
        delegation = payload["delegation"]
        self.assertEqual(delegation["action"], "delegate")
        self.assertEqual(delegation["intent"], "coding")
        self.assertEqual(delegation["recommended_workflow"], "plan")

    def test_naming_claude_code_with_a_coding_verb_still_delegates(self) -> None:
        payload = build_coding_delegation_payload("claude code로 구현해줘")
        delegation = payload["delegation"]
        self.assertEqual(delegation["action"], "delegate")
        self.assertEqual(delegation["intent"], "coding")
        self.assertEqual(delegation["recommended_workflow"], "plan")

    def test_advisor_phrase_triggers_still_reach_ask(self) -> None:
        payload = build_coding_delegation_payload("ask claude about this design")
        delegation = payload["delegation"]
        self.assertEqual(delegation["action"], "delegate")
        self.assertEqual(delegation["recommended_workflow"], "ask")

    def test_claude_code_hyphenated_folder_name_never_delegates(self) -> None:
        # #1163 review P3-6: plain containment on "claude-code"/"claudecode"
        # fired inside an ordinary hyphenated word, so a message that merely
        # mentions a folder name reached action=delegate off a false owner
        # detection. `named_executor_owners` now boundary-matches this group.
        payload = build_coding_delegation_payload("my repo has a folder called claudecode-notes")
        self.assertEqual(payload["delegation"]["action"], "clarify")

    def test_claude_code_dotted_filename_never_delegates(self) -> None:
        payload = build_coding_delegation_payload("read the claude-code.md file")
        self.assertEqual(payload["delegation"]["action"], "clarify")

    def test_bare_claude_word_never_delegates(self) -> None:
        # #1163 PR risk note: with `ask`'s bare `claude`/`gemini` triggers
        # retired, a bare one-word "claude" message no longer inflates a
        # score that used to outrank the retained workflow. Bare "claude" is
        # not the sole-named-Claude-Code-owner case (`named_executor_owners`
        # requires "claude code"/"claude-code"/"claudecode", not bare
        # "claude"), so the retained-workflow clarify outcome applies.
        payload = build_coding_delegation_payload("claude")
        self.assertEqual(payload["delegation"]["action"], "clarify")

    def test_bare_gemini_word_never_delegates(self) -> None:
        payload = build_coding_delegation_payload("gemini")
        self.assertEqual(payload["delegation"]["action"], "clarify")


class CategoryPropagationTests(unittest.TestCase):
    def test_natural_ulw_category_reaches_root_and_hermes_handoff(self) -> None:
        payload = build_coding_delegation_payload(
            "Use ulw-visual-engineering to implement the dashboard",
            executor_target="hermes",
        )

        self.assertEqual(payload["model_route_category"], "visual-engineering")
        self.assertEqual(
            payload["runtime_handoff"]["model_route_category"],
            "visual-engineering",
        )

    def test_natural_alias_reaches_external_handoff_without_becoming_a_role(self) -> None:
        payload = build_coding_delegation_payload(
            "Implement a risky documentation refactor with /ulw-write",
            executor_target="codex",
        )

        self.assertEqual(payload["model_route_category"], "writing")
        self.assertEqual(payload["executor_handoff"]["model_route_category"], "writing")


class HermesNativeModelBindingTests(unittest.TestCase):
    def test_resolved_recommendation_binds_native_alias_kanban_and_delegate_metadata(self) -> None:
        recommendation = {
            "schema_version": "model_recommendation_resolution/v2",
            "owner": "hermes",
            "status": "resolved",
            "source": "recommendation_chain",
            "selected": {
                "model_alias": "qwen3-coder",
                "provider": "qwen-oauth",
                "model_id": "qwen3-coder",
                "recommendation_source": "shipped_catalog",
            },
            "projection": {
                "kind": "hermes_native_binding",
                "alias": "deep",
                "provider": "qwen-oauth",
                "model_id": "qwen3-coder",
                "binding": "qwen-oauth/qwen3-coder",
                "apply_state": "approval_required",
            },
        }

        payload = build_coding_delegation_payload(
            "implement a risky refactor with Hermes",
            executor_target="hermes",
            model_recommendation=recommendation,
        )

        handoff = payload["runtime_handoff"]
        binding = handoff["hermes_native_model_binding"]
        self.assertEqual(binding["status"], "prepared_not_observed")
        self.assertEqual(binding["alias"], "deep")
        self.assertEqual(binding["provider"], "qwen-oauth")
        self.assertEqual(binding["model_id"], "qwen3-coder")
        self.assertEqual(binding["binding"], "qwen-oauth/qwen3-coder")
        self.assertEqual(binding["provenance"], "shipped_catalog")
        self.assertEqual(binding["kanban_task_override"]["command"], "set-model qwen-oauth/qwen3-coder")
        self.assertEqual(
            binding["delegate_task_override"],
            {
                "model": "qwen-oauth/qwen3-coder",
                "status": "prepared_not_observed",
            },
        )
        self.assertNotIn("maestro", str(payload).casefold())
        self.assertIn("runtime observation", binding["claim_boundary"].casefold())

    def test_last_resort_resolution_provenance_reaches_native_handoff(self) -> None:
        recommendation = {
            "schema_version": "model_recommendation_resolution/v3",
            "owner": "hermes",
            "status": "resolved",
            "source": "last_resort_chain",
            "selected": {
                "model_alias": "claude-opus-5",
                "provider": "ccapi",
                "model_id": "claude-opus-5",
                "recommendation_source": "shipped_editorial",
            },
            "projection": {
                "kind": "hermes_native_binding",
                "alias": "quick",
                "provider": "ccapi",
                "model_id": "claude-opus-5",
                "binding": "ccapi/claude-opus-5",
                "apply_state": "approval_required",
            },
        }

        payload = build_coding_delegation_payload(
            "implement a risky refactor with Hermes",
            executor_target="hermes",
            model_recommendation=recommendation,
        )
        binding = payload["runtime_handoff"]["hermes_native_model_binding"]
        self.assertEqual(binding["provenance"], "last_resort_chain")

    def test_owner_default_recommendation_keeps_native_default_without_model_pin(self) -> None:
        recommendation = {
            "schema_version": "model_recommendation_resolution/v2",
            "owner": "hermes",
            "status": "owner_default",
            "source": "owner_default",
            "selected": None,
            "projection": None,
            "inactive_candidates": ["gemini-3.1-pro"],
        }

        payload = build_coding_delegation_payload(
            "implement a risky refactor with Hermes",
            executor_target="hermes",
            model_recommendation=recommendation,
        )

        binding = payload["runtime_handoff"]["hermes_native_model_binding"]
        self.assertEqual(binding["status"], "owner_default")
        self.assertEqual(binding["next_action"], "use_hermes_default_model")
        self.assertEqual(binding["inactive_candidates"], ["gemini-3.1-pro"])
        self.assertNotIn("kanban_task_override", binding)
        self.assertNotIn("delegate_task_override", binding)

    def test_explicit_unavailable_recommendation_still_requires_native_setup(self) -> None:
        recommendation = {
            "schema_version": "model_recommendation_resolution/v2",
            "owner": "hermes",
            "status": "choice_required",
            "source": "explicit_model",
            "selected": None,
            "projection": None,
            "inactive_candidates": ["gpt-5.6-sol"],
        }

        payload = build_coding_delegation_payload(
            "implement a risky refactor with Hermes",
            executor_target="hermes",
            model_recommendation=recommendation,
        )

        binding = payload["runtime_handoff"]["hermes_native_model_binding"]
        self.assertEqual(binding["status"], "choice_required")
        self.assertEqual(binding["next_action"], "configure_hermes_native_alias")


if __name__ == "__main__":  # pragma: no cover - unittest entry point
    unittest.main()
