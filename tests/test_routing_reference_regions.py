from __future__ import annotations

import hashlib
from typing import ClassVar
import unittest

from omh.plugin_bundle.omh.awareness import awareness_route_hint
from omh.routing.chat import explicit_skill_invocation, public_chat_route_payload, route_chat_message
from omh.routing.intent import classify_workflow_intent
from omh.routing.policy import explicit_skill_invocation as policy_invocation
from omh.routing.recommend import has_strong_named_catalog_owner, recommend_skills
from omh.routing.reference_regions import executable_routing_text, reference_regions
from omh.routing.route_plan import build_workflow_route_plan
from omh.routing.task_cards import classify_task
from omh.routing.ulw_alias import resolve_codex_owner_choice_cue, resolve_ulw_alias
from omh.skills.catalog import routable_definitions
from omh.wrapper.route_hints import build_chat_route_hint_payload


REFERENCE_MESSAGES = (
    'Translate `$ulw-work` to Korean.',
    'Explain "$ulw-work" without running it.',
    "Explain '$ulw-work' without running it.",
    'Explain "$ultrawork execute implement merge".',
    'Show this example:\n```\n$ulw-work fix the build\n```',
    'Show this example:\n```bash\n$ulw-work fix the build\n```',
    'Show this example:\n~~~shell\n$ulw-work fix the build\n~~~',
    'Show this example:\n````bash\n```\n$ulw-work\n```\n````',
    'Explain ``literal `$ulw-work` syntax``.',
    r'Explain "literal \" $ulw-work execute".',
    "Explain 'literal \\' $ulw-work execute'.".replace("\\\\", "\\"),
    'Explain "$ulw-work execute',
    "Explain '$ulw-work execute",
    'Explain `$ulw-work execute',
    'Show this:\n```bash\n$ulw-work execute',
    'Show this:\n~~~bash\n$ulw-work execute',
    '이 문구 "$ulw-work"를 한국어로 번역해줘.',
    '请解释"$ulw-work"的意思。',
    '「$ulw-work」を説明してください。',
    'Explain “$ulw-work”.',
    'Explain ‘$ulw-work’.',
    '"$ulw-work"',
    '`$ulw-work`',
    "'$ultrawork'",
)


class RoutingReferenceRegionTests(unittest.TestCase):
    def test_public_label_intent_agrees_with_direct_scorer(self) -> None:
        for message in ('Use $ulw-work to fix the build.', '$ulw-work "$ultraqa"', '"$ultraqa" $ulw-work'):
            with self.subTest(message=message):
                self.assertTrue(classify_workflow_intent(message).explicit_execution)
        reference = classify_workflow_intent('Explain "$ulw-work".')
        self.assertIn('quoted_known_term', reference.structural_cues)
        self.assertIn('ultrawork', reference.not_executed)

    def test_recommendation_keeps_reference_context_for_the_selected_workflow(self) -> None:
        message = '$ultraqa audit this example: "$ulw-work"'
        recommendation = recommend_skills(message)[0]
        self.assertTrue(recommendation['suggested_prompt'].endswith(message))

    def test_route_plan_does_not_add_stages_from_reference_text(self) -> None:
        message = '$ultraqa'
        rows = recommend_skills(message)
        baseline = build_workflow_route_plan(message, rows, selected_skill='ultraqa', action='dispatch')
        mixed = build_workflow_route_plan(message + ' "research implement review"', rows, selected_skill='ultraqa', action='dispatch')
        self.assertEqual(mixed, baseline)

    def test_inline_backticks_do_not_absorb_adjacent_tildes(self) -> None:
        message = '`~$ulw-work~` $ultraqa'
        self.assertTrue(executable_routing_text(message).endswith(' $ultraqa'))
        route = route_chat_message(message)
        self.assertEqual((route['action'], route['selected_skill']), ('dispatch', 'ultraqa'))

    def test_every_catalog_name_and_metadata_is_inert_in_references(self) -> None:
        baseline = recommend_skills("For reference:", limit=len(routable_definitions()))
        expected = [(row["skill"], row["matched"]) for row in baseline]
        for definition in routable_definitions():
            references = (
                f"'{definition.name}'",
                f'"{definition.name}"',
                f"`{definition.name}`",
                f"```text\n{definition.name} {' '.join(definition.triggers)} {definition.description}\n```",
                f"~~~text\n{definition.name} {' '.join(definition.triggers)} {definition.description}\n~~~",
            )
            for reference in references:
                message = "For reference:\n" + reference
                with self.subTest(skill=definition.name, reference=reference):
                    route = route_chat_message(message, source="discord")
                    self.assertNotEqual(route["action"], "dispatch")
                    rows = recommend_skills(message, limit=len(routable_definitions()))
                    self.assertEqual([(row["skill"], row["matched"]) for row in rows], expected)

    def test_quoted_learning_signals_cannot_override_the_route(self) -> None:
        for reference in (
            'learn this: run git diff --check before opening a PR',
            'from now on prefer concise Korean summaries',
            'make a skill from this: always verify first',
        ):
            with self.subTest(reference=reference):
                route = route_chat_message(f'For reference: "{reference}"')
                self.assertNotEqual(route["action"], "dispatch")
                self.assertIsNone(route["learning_candidate_card"])

    def test_projection_preserves_surrounding_clauses_and_line_boundaries(self) -> None:
        for before, reference, after in (
            ('請', '"$ulw-work"', '解釋，然後 $ultraqa'),
            ('Use $ultraqa; ', "'$ulw-work", ''),
            ('', '「$ulw-work」', '$ultraqa'),
            ('', '“$ulw-work”', '$ultraqa'),
            ('請', "'$ulw-work'", '解釋，然後 $ultraqa'),
            ('請', "'ultrawork'", '解釋，然後 $ultraqa'),
            ('', "‘$ulw-work’", '$ultraqa'),
            ('', '```bash\n$ulw-work\n```', '\n$ultraqa'),
            ('', '~~~bash\n$ulw-work\n~~~~', '\n$ultraqa'),
        ):
            with self.subTest(reference=reference):
                message = before + reference + after
                result = reference_regions(message)
                mask = ''.join(char if char in "\r\n" else ' ' for char in reference)
                self.assertEqual(result.executable_text, before + mask + after)
                self.assertEqual(result.references, (reference,))
                self.assertEqual(executable_routing_text(result.executable_text), result.executable_text)

    def test_escaped_delimiters_and_apostrophes_are_lexical(self) -> None:
        for message in (r'Use \" $ulw-work', r'Use \` $ulw-work', "don't use contractions as quotes", "it's the user's choice", "l'été is not a quote", "don’t hide $ulw-work"):
            with self.subTest(message=message):
                self.assertEqual(executable_routing_text(message), message)
        for message in (r'"escaped \" $ulw-work"', r"'escaped \' $ulw-work'", r'"even \\" $ultraqa'):
            with self.subTest(message=message):
                self.assertNotIn("$ulw-work", executable_routing_text(message))
        self.assertTrue(executable_routing_text(r'"even \\" $ultraqa').endswith(' $ultraqa'))

    def test_reference_only_chat_never_dispatches_referenced_workflow(self) -> None:
        for message in REFERENCE_MESSAGES:
            with self.subTest(message=message):
                route = route_chat_message(message, source="discord")
                self.assertNotEqual((route["action"], route["selected_skill"]), ("dispatch", "ultrawork"))

    def test_reference_only_scoring_has_no_workflow_evidence(self) -> None:
        for message in REFERENCE_MESSAGES:
            with self.subTest(message=message):
                rows = recommend_skills(message, limit=len(routable_definitions()), apply_guardrails=False)
                self.assertFalse(any(row["skill"] == "ultrawork" and row["score"] > 0 for row in rows))

    def test_structural_execution_cues_ignore_references(self) -> None:
        for message in REFERENCE_MESSAGES:
            with self.subTest(message=message):
                intent = classify_workflow_intent(message)
                self.assertFalse(intent.explicit_execution)
                self.assertEqual(intent.execution_cues, ())
                self.assertNotIn("workflow_marker", intent.structural_cues)

    def test_direct_invocation_keeps_precedence_and_apostrophes(self) -> None:
        for message in (
            'Use $ulw-work to fix the build.',
            "$ultrawork fix the build; don't stop at analysis.",
            "Don't stop; use $ulw-work to fix the build.",
            r'Use $ulw-work with literal \" delimiters.',
            r'Use $ulw-work with literal \` delimiters.',
            '$ulw-work fix the build; example: "$ultraqa"',
            '$ulw-work fix the build; example: "$ultraqa execute',
        ):
            with self.subTest(message=message):
                route = route_chat_message(message, source="discord")
                self.assertEqual((route["action"], route["selected_skill"]), ("dispatch", "ultrawork"))
                self.assertEqual(recommend_skills(message)[0]["skill"], "ultrawork")

    def test_mixed_message_uses_only_outside_invocation(self) -> None:
        for message in (
            '$ultraqa audit the dashboard. Example: "$ulw-work execute"',
            '"$ulw-work execute" $ultraqa audit the dashboard.',
            '$ultraqa audit the dashboard.\n```bash\n$ulw-work execute\n```',
            '$ultraqa audit the dashboard. "$ulw-work execute',
            '$ultraqa 请检查界面；参考"$ulw-work execute"。',
        ):
            with self.subTest(message=message):
                route = route_chat_message(message, source="discord")
                self.assertEqual((route["action"], route["selected_skill"]), ("dispatch", "ultraqa"))
                self.assertEqual(recommend_skills(message)[0]["skill"], "ultraqa")
                self.assertTrue(classify_workflow_intent(message).explicit_execution)

    def test_explicit_helpers_do_not_strip_quotes_into_invocations(self) -> None:
        names = {definition.name for definition in routable_definitions()}
        for message in ('"$ultraqa"', 'use "ultraqa"', 'use the "wiki" skill', '`$ultraqa`'):
            with self.subTest(message=message):
                self.assertIsNone(explicit_skill_invocation(message))
                self.assertIsNone(policy_invocation(message, names))

    def test_named_owner_and_task_fast_paths_ignore_reference_evidence(self) -> None:
        self.assertFalse(has_strong_named_catalog_owner('Explain "ultrawork parallel execution".'))
        self.assertIsNone(classify_task('"omh update"'))
        self.assertIsNone(resolve_ulw_alias('Explain "one cycle delivery"', allow_containment=True))
        self.assertIsNone(resolve_codex_owner_choice_cue('Explain "codex session status".'))
        for message, forbidden in (
            ('Explain "codex session status".', 'executor-runtime-readiness'),
            ('"omh update"', 'update'),
            ('"/omh use ultraqa"', 'meta-router'),
            ('Explain "one cycle delivery".', 'ultrawork'),
        ):
            with self.subTest(message=message):
                route = route_chat_message(message)
                self.assertNotEqual((route["action"], route["selected_skill"]), ("dispatch", forbidden))

    def test_public_route_preserves_original_context(self) -> None:
        for message in ('Translate `$ulw-work` to Korean.', '"$ulw-work"'):
            with self.subTest(message=message):
                route = public_chat_route_payload(message, source="discord", include_message=True)
                self.assertEqual(route["routing_prompt"][-len(message):], message)

    def test_route_hints_ignore_reference_only_workflows(self) -> None:
        for message in REFERENCE_MESSAGES:
            with self.subTest(message=message):
                hint = awareness_route_hint(message)
                self.assertNotIn(hint["primary_workflow"], ("ultrawork", "ulw-work"))
                payload = build_chat_route_hint_payload(message, source="discord")
                self.assertNotIn(payload["route_hint"]["primary_workflow"], ("ultrawork", "ulw-work"))


def protected_hint_forms(text: str) -> tuple[str, ...]:
    escaped = text.replace('\\', '\\\\')
    single = escaped.replace("'", "\\'")
    double = escaped.replace('"', '\\"')
    inline = escaped.replace('`', '\\`')
    return (
        f"'{single}'", f'"{double}"', f'`{inline}`',
        f'```text\n{text}\n```', f'~~~text\n{text}\n~~~',
    )


class ContextualDesignReferenceTests(unittest.TestCase):
    context: ClassVar[dict[str, str]] = {
        "iteration_id": "design-direction-iteration-1234567890abcdef",
        "revision_digest": "a" * 64,
    }

    def test_active_context_cannot_turn_references_into_revision_requests(self) -> None:
        for reference in protected_hint_forms("Revise the direction after feedback."):
            for message in (reference, f"Explain this reference:\n{reference}\nwithout acting on it."):
                for surface in (route_chat_message, public_chat_route_payload):
                    with self.subTest(surface=surface.__name__, message=message):
                        route = surface(
                            message, source="discord",
                            active_design_direction_iteration=self.context,
                        )
                        self.assertIn(route["action"], ("fallback", "clarify"))
                        self.assertEqual(route["selected_skill"], "oh-my-hermes")
                        self.assertNotEqual(route.get("route_next_action"), "revise_design_direction_iteration")
                        self.assertNotIn("design_direction_iteration", route)

    def test_direct_and_mixed_revision_requests_keep_binding_and_original_context(self) -> None:
        direct = "Revise the direction after feedback."
        for message in (
            direct,
            direct + ' Example: "$ulw-work execute"',
            '"$ulw-work execute"\n' + direct,
            direct + '\n```text\n$ultraqa audit the dashboard\n```',
        ):
            decision = route_chat_message(
                message, source="discord",
                active_design_direction_iteration=self.context,
            )
            self.assertEqual(decision["recommendations"][0]["suggested_prompt"][-len(message):], message)
            for surface, route in (
                ("route_chat_message", decision),
                ("public_chat_route_payload", public_chat_route_payload(
                    message, source="discord", include_message=True,
                    active_design_direction_iteration=self.context,
                )),
            ):
                with self.subTest(surface=surface, message=message):
                    self.assertEqual((route["action"], route["selected_skill"]), ("dispatch", "design-quality-gate"))
                    self.assertEqual(route["route_next_action"], "revise_design_direction_iteration")
                    self.assertEqual(route["design_direction_iteration"], self.context)
                    self.assertEqual(route["routing_prompt"][-len(message):], message)

    def test_other_genuine_instruction_wins_over_referenced_revision_request(self) -> None:
        for message in (
            '$ultraqa audit the dashboard. Example: "Revise the direction after feedback."',
            '```text\nRevise the direction after feedback.\n```\n$ultraqa audit the dashboard.',
        ):
            for surface in (route_chat_message, public_chat_route_payload):
                with self.subTest(surface=surface.__name__, message=message):
                    route = surface(
                        message, source="discord",
                        active_design_direction_iteration=self.context,
                    )
                    self.assertEqual((route["action"], route["selected_skill"]), ("dispatch", "ultraqa"))
                    self.assertNotEqual(route.get("route_next_action"), "revise_design_direction_iteration")
                    self.assertNotIn("design_direction_iteration", route)


def hint_selection(payload: dict[str, object]) -> list[tuple[object, ...]]:
    hints = payload['hints']
    assert isinstance(hints, list)
    return [
        (hint['id'], hint['workflow'], hint['next_action'], hint['matched_cues'])
        for hint in hints
    ]


HINT_DIRECT_CONTROLS = (
    ('research', 'ulw-research'),
    ('jit-learn', 'jit-learn'),
    ('workflow-learning', 'workflow-learning'),
    ('use ralplan to plan this rollout', 'ulw-plan'),
    ('ultrawork this refactor until the tests pass', 'ulw-work'),
)
HINT_MATCHING_CONTROLS = (
    *(message for message, _ in HINT_DIRECT_CONTROLS),
    'What should I learn next to diagnose this incident before Friday?',
    'summarize this word document',
    'browser interaction qa',
    'improve OMH routing quality',
    'why is the workflow-learning route hint in the log?',
    'loop on this until the flaky test stops failing',
    'implement this with Codex and open a PR',
)


class RoutingReferenceHintTests(unittest.TestCase):
    def test_blocker_workflows_are_inert_on_both_public_surfaces(self) -> None:
        for workflow, public_name in HINT_DIRECT_CONTROLS[:3]:
            message = f'For reference: "{workflow}"'
            for surface, hint in (
                ('awareness', awareness_route_hint(message)),
                ('wrapper', build_chat_route_hint_payload(message)['route_hint']),
            ):
                with self.subTest(surface=surface, workflow=workflow):
                    self.assertNotIn(hint['primary_workflow'], (workflow, public_name))

    def test_all_catalog_hint_inputs_are_inert_in_every_protected_form(self) -> None:
        for definition in routable_definitions():
            text = f'{definition.name} {" ".join(definition.triggers)} {definition.description}'
            for reference in protected_hint_forms(text):
                message = 'For reference:\n' + reference
                for surface, hint in (
                    ('awareness', awareness_route_hint(message)),
                    ('wrapper', build_chat_route_hint_payload(message)['route_hint']),
                ):
                    with self.subTest(surface=surface, workflow=definition.name, reference=reference):
                        self.assertEqual(hint_selection(hint), [])

    def test_direct_hints_keep_their_workflow(self) -> None:
        for message, expected in HINT_DIRECT_CONTROLS:
            with self.subTest(message=message):
                self.assertEqual(awareness_route_hint(message)['primary_workflow'], expected)
                payload = build_chat_route_hint_payload(message)
                self.assertEqual(payload['route_hint']['primary_workflow'], expected)

    def test_every_catalog_direct_hint_ignores_mixed_reference_inputs(self) -> None:
        messages = (*HINT_MATCHING_CONTROLS, *(definition.name for definition in routable_definitions()))
        for outside in messages:
            baseline = hint_selection(awareness_route_hint(outside))
            for reference in protected_hint_forms('research jit-learn workflow-learning $ulw-work'):
                for message in (outside + '\n' + reference, reference + '\n' + outside):
                    for surface, hint in (
                        ('awareness', awareness_route_hint(message)),
                        ('wrapper', build_chat_route_hint_payload(message)['route_hint']),
                    ):
                        with self.subTest(surface=surface, outside=outside, message=message):
                            self.assertEqual(hint_selection(hint), baseline)

    def test_specialist_jit_and_structural_inputs_cannot_leak_from_references(self) -> None:
        for text in HINT_MATCHING_CONTROLS:
            for reference in protected_hint_forms(text):
                for message, outside in ((reference, ''), ('research\n' + reference, 'research')):
                    with self.subTest(message=message):
                        expected = hint_selection(awareness_route_hint(outside))
                        self.assertEqual(hint_selection(awareness_route_hint(message)), expected)
                        payload = build_chat_route_hint_payload(message)
                        self.assertEqual(hint_selection(payload['route_hint']), expected)

    def test_wrapper_catalog_picker_uses_only_outside_text(self) -> None:
        catalog = 'what workflows are available'
        for reference in protected_hint_forms(catalog):
            with self.subTest(reference=reference):
                payload = build_chat_route_hint_payload('For reference:\n' + reference)
                self.assertEqual(payload['route_hint']['hints'], [])
                self.assertFalse(payload['chat_response']['state']['route_hint']['catalog_question'])
        for message in (catalog, catalog + '\n"research"', '"research"\n' + catalog):
            with self.subTest(message=message):
                payload = build_chat_route_hint_payload(message)
                self.assertEqual(payload['route_hint']['primary_workflow'], 'oh-my-hermes')
                self.assertTrue(payload['route_hint']['catalog_question'])

    def test_hints_preserve_original_metadata_and_reference_context(self) -> None:
        for message in ('For reference: "$ulw-work"', 'research\n"$ulw-work"'):
            with self.subTest(message=message):
                payload = build_chat_route_hint_payload(
                    message, source='discord', source_metadata={'message_id': 'synthetic-1396'},
                    include_prompt_context=True,
                )
                hint = payload['route_hint']
                self.assertEqual(hint['message_sha256'], hashlib.sha256(message.encode()).hexdigest())
                self.assertEqual(hint['message_length'], len(message))
                self.assertEqual(payload['message_length'], len(message))
                self.assertEqual(payload['source_metadata'], {'message_id': 'synthetic-1396'})
                self.assertIn('ulw-work', hint['mentioned_workflows'])
                route = public_chat_route_payload(message, include_message=True)
                self.assertTrue(route['routing_prompt'].endswith(message))


if __name__ == "__main__":
    unittest.main()
