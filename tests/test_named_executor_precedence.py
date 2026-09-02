from __future__ import annotations

import unittest

from _local_package import load_local_package

load_local_package()

from omh.plugin_bundle.omh.awareness import awareness_route_hint
from omh.routing.chat import route_chat_message
from omh.routing.localization import normalized_phrase, prepare_routing_text, routing_tokens
from omh.routing.policy import named_coding_agent_delivery_requested
from omh.routing.route_plan import public_workflow_identifier
from omh.wrapper.contract import build_chat_interaction_payload


CODING_HANDOFF_SKILL = "ultrawork"
EXECUTOR_READINESS_SKILL = "executor-runtime-readiness"
CODING_LANES = frozenset({CODING_HANDOFF_SKILL, EXECUTOR_READINESS_SKILL})

# The awareness surface emits the identifier the installed skill answers to,
# while the chat decision keeps the catalog key (#1249). The matrix therefore
# carries the public spelling in its awareness column and the lane membership
# check below is done in the matching vocabulary.
PUBLIC_CODING_HANDOFF_WORKFLOW = public_workflow_identifier(CODING_HANDOFF_SKILL)
PUBLIC_CODING_LANES = frozenset(public_workflow_identifier(skill) for skill in CODING_LANES)

# One shared message set for both public routing surfaces. Each entry is
# (language, message, expected chat skill, expected awareness workflow). The
# "have codex" row keeps its existing delegation-phrasing owner on the chat side;
# both surfaces still stay on a coding lane instead of advisor or feedback triage.
NAMED_EXECUTOR_DELIVERY_MATRIX: tuple[tuple[str, str, str, str], ...] = (
    ("ko", "claude code로 이 이슈 해결해줘", CODING_HANDOFF_SKILL, PUBLIC_CODING_HANDOFF_WORKFLOW),
    ("ko", "codex로 이 버그 고쳐줘", CODING_HANDOFF_SKILL, PUBLIC_CODING_HANDOFF_WORKFLOW),
    ("ko", "헤르메스 코딩으로 이 이슈 구현해줘", CODING_HANDOFF_SKILL, PUBLIC_CODING_HANDOFF_WORKFLOW),
    ("en", "use claude code to fix this issue", CODING_HANDOFF_SKILL, PUBLIC_CODING_HANDOFF_WORKFLOW),
    ("en", "use codex to fix the login bug", CODING_HANDOFF_SKILL, PUBLIC_CODING_HANDOFF_WORKFLOW),
    ("en", "get codex to open a pr for this fix", CODING_HANDOFF_SKILL, PUBLIC_CODING_HANDOFF_WORKFLOW),
    ("en", "have codex implement this fix", EXECUTOR_READINESS_SKILL, PUBLIC_CODING_HANDOFF_WORKFLOW),
    ("ja", "codexでこの問題を修正して", CODING_HANDOFF_SKILL, PUBLIC_CODING_HANDOFF_WORKFLOW),
    ("ja", "claude codeでこのissueを解決して", CODING_HANDOFF_SKILL, PUBLIC_CODING_HANDOFF_WORKFLOW),
    ("zh", "用 codex 修复这个问题", CODING_HANDOFF_SKILL, PUBLIC_CODING_HANDOFF_WORKFLOW),
    ("zh", "用 claude code 实现这个功能", CODING_HANDOFF_SKILL, PUBLIC_CODING_HANDOFF_WORKFLOW),
)

# Overroute guards: advisor requests that name Claude without any coding-delivery
# signal. Awareness has no advisor lane, so only the chat router is asserted here.
BARE_ADVISOR_MESSAGES: tuple[tuple[str, str], ...] = (
    ("en", "consult claude about this architecture decision"),
    ("en", "get an external advisor opinion from claude"),
    ("en", "ask claude for a second opinion on this plan"),
    ("ko", "claude 의견 좀 들어보자"),
)

# Overroute guards: issue/bug wording with no named coding agent and no delivery
# request stays customer-signal work on both surfaces.
CUSTOMER_SIGNAL_MESSAGES: tuple[tuple[str, str], ...] = (
    ("en", "this issue keeps happening for our customers"),
    ("en", "users report a bug in the checkout page"),
    ("ko", "고객들이 결제 실패 이슈를 계속 제보해요"),
    ("zh", "用户一直反馈这个 bug"),
)


def _policy_signal(message: str) -> bool:
    normalized_query = normalized_phrase(prepare_routing_text(message).scoring_text)
    return named_coding_agent_delivery_requested(normalized_query, routing_tokens(normalized_query))


class NamedExecutorPrecedenceTests(unittest.TestCase):
    def test_named_executor_delivery_selects_coding_lane_on_both_surfaces(self) -> None:
        for language, message, chat_skill, hint_workflow in NAMED_EXECUTOR_DELIVERY_MATRIX:
            with self.subTest(language=language, message=message):
                route = route_chat_message(message)
                hint = awareness_route_hint(message)

                self.assertEqual(route["selected_skill"], chat_skill)
                self.assertEqual(route["confidence"], "high")
                self.assertEqual(hint["selected_workflow"], hint_workflow)
                self.assertIn(route["selected_skill"], CODING_LANES)
                self.assertIn(hint["selected_workflow"], PUBLIC_CODING_LANES)

    def test_named_executor_delivery_never_selects_advisor_or_feedback_lanes(self) -> None:
        for language, message, _chat_skill, _hint_workflow in NAMED_EXECUTOR_DELIVERY_MATRIX:
            with self.subTest(language=language, message=message):
                self.assertNotIn(route_chat_message(message)["selected_skill"], {"ask", "feedback-triage"})
                self.assertNotEqual(awareness_route_hint(message)["selected_workflow"], "feedback-triage")

    def test_reproduced_request_selects_the_same_lane_on_both_surfaces(self) -> None:
        message = "claude code로 이 이슈 해결해줘"
        route = route_chat_message(message)
        hint = awareness_route_hint(message)

        # Same lane, two vocabularies: the route keeps the catalog key and the
        # hint emits the installed-skill identifier (#1249).
        self.assertEqual(public_workflow_identifier(route["selected_skill"]), hint["selected_workflow"])
        self.assertEqual(route["selected_skill"], CODING_HANDOFF_SKILL)
        self.assertEqual(hint["selected_workflow"], PUBLIC_CODING_HANDOFF_WORKFLOW)
        self.assertIn("guard:named_coding_agent_delivery", route["recommendations"][0]["matched"])
        self.assertEqual(hint["primary_next_action"], "prepare_one_cycle_delivery")
        self.assertNotEqual(hint["hints"][0]["id"], "customer_signal")

    def test_bare_claude_advisor_requests_still_select_ask(self) -> None:
        for language, message in BARE_ADVISOR_MESSAGES:
            with self.subTest(language=language, message=message):
                self.assertFalse(_policy_signal(message))
                self.assertEqual(route_chat_message(message)["selected_skill"], "ask")

    def test_issue_and_bug_without_a_named_executor_stay_feedback_triage(self) -> None:
        for language, message in CUSTOMER_SIGNAL_MESSAGES:
            with self.subTest(language=language, message=message):
                self.assertFalse(_policy_signal(message))
                self.assertEqual(awareness_route_hint(message)["selected_workflow"], "feedback-triage")

    def test_customer_report_language_still_triages_before_named_executor_delivery(self) -> None:
        message = "결제 실패 이슈가 자주 나와. 재현 계획 세우고 codex로 고쳐서 리뷰까지 추적해줘"

        self.assertTrue(_policy_signal(message))
        self.assertEqual(route_chat_message(message)["selected_skill"], "feedback-triage")

    def test_executor_runtime_choice_still_wins_over_delivery_precedence(self) -> None:
        message = "should i use codex or claude code for this?"

        self.assertEqual(route_chat_message(message)["selected_skill"], EXECUTOR_READINESS_SKILL)
        self.assertEqual(awareness_route_hint(message)["selected_workflow"], EXECUTOR_READINESS_SKILL)

    def test_named_executor_delivery_stays_prepared_and_never_dispatches(self) -> None:
        for language, message, chat_skill, _hint_workflow in NAMED_EXECUTOR_DELIVERY_MATRIX:
            with self.subTest(language=language, message=message):
                hint = awareness_route_hint(message)

                self.assertTrue(hint["hints"][0]["not_evidence_yet"])
                self.assertIn("not workflow execution", hint["claim_boundary"])

                if chat_skill != CODING_HANDOFF_SKILL:
                    continue
                delegation = build_chat_interaction_payload(message, source="discord")["delegation"]
                # Both statuses are pre-probe states; "ready" would require observed evidence.
                self.assertIn(delegation["executor_readiness"]["status"], {"not_observed", "choice_required"})
                self.assertIn(delegation["dispatch_policy"], {"prepare_only", "ask_before_dispatch"})


if __name__ == "__main__":  # pragma: no cover - unittest entry point
    unittest.main()
