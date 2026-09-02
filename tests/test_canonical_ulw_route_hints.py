"""Canonical ULW identifiers on the public route-hint surface (issue #1249).

`tests/test_canonical_ulw_names.py` pins the route plan and the route
explanation. The route hint is the third public surface and the one a wrapper
renders most directly: `awareness_route_hint()` feeds the LLM hook, the chat
tool, the recommend tool, and the context brief. Every workflow identifier in
that payload is read by a human or echoed back as a skill to invoke, so a
legacy catalog key there is the same #1249 defect as in the route plan.

The rule these tests pin is one-directional: legacy spellings stay accepted as
INPUT, and no legacy key is ever EMITTED on a public route-hint field.
"""

from __future__ import annotations

import unittest

from _local_package import load_local_package

load_local_package()
from omh.plugin_bundle.omh.awareness import (
    awareness_generic_tool_checkpoint_payload,
    awareness_route_hint,
    generic_tool_checkpoint_routes,
)
from omh.skills.catalog_types import ULW_ENGINE_SKILL_NAMES, omh_skill_display_name

# The internal catalog keys whose public identifier is a `ulw-` label. This is
# the whole ULW family (13 engines), not the three names the issue happens to
# quote, because the audit the directive asks for is family-wide.
LEGACY_ULW_KEYS = frozenset(
    name for name in ULW_ENGINE_SKILL_NAMES if omh_skill_display_name(name) != name
)

# Legacy spellings a user or a stale doc still types, one message each. Every
# one must keep routing, and none may come back naming the word it was given.
LEGACY_INPUT_MESSAGES = (
    "use ralplan to plan this rollout",
    "ultraqa the setup wizard with hostile install paths",
    "ultrawork this refactor until the tests pass",
    "ulw this refactor until the tests pass",
)

# Messages chosen to reach different route-hint rules, so the sweep sees hints,
# adjacent workflows, and context cards produced by more than one code path.
ROUTE_HINT_SWEEP_MESSAGES = (
    "use ralplan and ultraqa for this release",
    "ultrawork this refactor until the tests pass",
    "research the current install friction and write a reviewed plan",
    "implement this with Codex and open a PR",
    "run code review on the diff I just pushed",
    "loop on this until the flaky test stops failing",
    "my routing keeps picking the wrong workflow, help me fix it",
    "the build is failing after the last merge",
    "I fixed it and the tests pass now",
    "interview me about what this product should do",
)


def _public_workflow_identifiers(payload: dict[str, object]) -> list[str]:
    """Every workflow identifier a reader can pull out of one route hint payload.

    Deliberately exhaustive rather than field-by-field: the point of the audit
    is that a NEW public workflow field must not be able to leak a legacy key
    unnoticed, so this walks the top-level workflow lists, every hint, and the
    workflow IDs inside each hint's context card.
    """
    identifiers: list[str] = []
    for key in (
        "selected_workflow",
        "primary_workflow",
    ):
        value = payload.get(key, "")
        if isinstance(value, str) and value:
            identifiers.append(value)
    for key in ("mentioned_workflows", "adjacent_workflows", "not_executed"):
        values = payload.get(key, [])
        if isinstance(values, list):
            identifiers.extend(str(item) for item in values if str(item))

    hints = payload.get("hints", [])
    for hint in hints if isinstance(hints, list) else []:
        if not isinstance(hint, dict):
            continue
        workflow = hint.get("workflow", "")
        if isinstance(workflow, str) and workflow:
            identifiers.append(workflow)
        # `matched_cues` is excluded on purpose: it reports the words the
        # message matched, so a legacy word there is an observation of the
        # input, not an emitted identifier.
        for key in ("adjacent_workflows", "mentioned_workflows", "not_executed"):
            values = hint.get(key, [])
            if isinstance(values, list):
                identifiers.extend(str(item) for item in values if str(item))
        card = hint.get("workflow_context_card")
        if isinstance(card, dict):
            representative = card.get("representative_workflows", [])
            if isinstance(representative, list):
                identifiers.extend(str(item) for item in representative if str(item))
    return identifiers


class CanonicalUlwRouteHintTests(unittest.TestCase):
    def test_issue_report_message_emits_no_legacy_identifier(self) -> None:
        # Given: the exact message the issue reports, which names two legacy
        # ULW engines in one sentence.
        # When: the route hint a wrapper renders is built.
        payload = awareness_route_hint("use ralplan and ultraqa for this release")

        # Then: the three fields the report calls out carry canonical labels.
        self.assertEqual(payload["selected_workflow"], "ulw-plan")
        self.assertEqual(payload["primary_workflow"], "ulw-plan")
        self.assertEqual(list(payload["mentioned_workflows"]), ["ulw-plan", "ulw-qa"])
        hints = payload["hints"]
        assert isinstance(hints, list)
        self.assertTrue(hints, "the issue message must still produce a hint")
        first_hint = hints[0]
        assert isinstance(first_hint, dict)
        self.assertEqual(first_hint["workflow"], "ulw-plan")

    def test_no_public_route_hint_field_emits_a_legacy_ulw_key(self) -> None:
        # Given: messages that reach several different route-hint rules.
        for message in ROUTE_HINT_SWEEP_MESSAGES:
            with self.subTest(message=message):
                # When: the full public payload is walked, hints and context
                # cards included.
                identifiers = _public_workflow_identifiers(awareness_route_hint(message, max_hints=3))

                # Then: no emitted identifier equals a legacy catalog key.
                # Exact comparison, not substring: `ulw-research` contains the
                # legacy key `research`, so a substring check would flag the
                # canonicalization it is meant to prove.
                for identifier in identifiers:
                    self.assertNotIn(
                        identifier,
                        LEGACY_ULW_KEYS,
                        f"{message!r} emitted legacy ULW name {identifier!r}",
                    )

    def test_legacy_spellings_still_route_through_the_hint_surface(self) -> None:
        # Given: the legacy input spellings the issue promises to keep working.
        for message in LEGACY_INPUT_MESSAGES:
            with self.subTest(message=message):
                # When: each is routed through the real hint surface.
                payload = awareness_route_hint(message)

                # Then: it still produces a hint (input compatibility is not
                # traded away for canonical output), and what comes back is a
                # canonical label rather than the legacy word that was typed.
                self.assertEqual(payload["status"], "hinted")
                selected = str(payload["selected_workflow"])
                self.assertTrue(
                    selected.startswith("ulw-"),
                    f"{message!r} resolved to {selected!r}, not a canonical ULW name",
                )

    def test_generic_tool_checkpoint_routes_emit_canonical_ulw_names(self) -> None:
        # Given: the OMH-first checkpoint card, whose routes tell a reader which
        # workflow to prefer over a generic tool. Those identifiers are as
        # user-facing as a route hint's, so #1249 covers them too.
        routes = generic_tool_checkpoint_routes()
        self.assertTrue(routes, "the checkpoint must publish routes")

        # When: every workflow field on every route is read.
        for route in routes:
            with self.subTest(tool_family=route["tool_family"]):
                identifiers = [str(route["primary_workflow"])]
                identifiers.extend(str(item) for item in route["preferred_workflows"])

                # Then: none of them is a legacy catalog key.
                for identifier in identifiers:
                    self.assertNotIn(
                        identifier,
                        LEGACY_ULW_KEYS,
                        f"checkpoint route {route['tool_family']!r} emitted legacy ULW name {identifier!r}",
                    )

    def test_search_tools_checkpoint_route_names_the_installed_research_skill(self) -> None:
        # Given: the search-tools route, whose primary workflow is the ULW
        # research engine -- the concrete case where the card handed a reader
        # the internal `research` key instead of the installed skill name.
        route = next(
            item for item in generic_tool_checkpoint_routes() if item["tool_family"] == "search_tools"
        )

        # When/Then: the emitted primary workflow is the canonical label, and
        # the preferred list carries it in canonical form too.
        self.assertEqual(route["primary_workflow"], "ulw-research")
        self.assertIn("ulw-research", list(route["preferred_workflows"]))

    def test_checkpoint_payload_preserves_non_workflow_route_fields(self) -> None:
        # Given: the assembled checkpoint payload a wrapper renders.
        payload = awareness_generic_tool_checkpoint_payload()
        routes = payload["routes"]
        assert isinstance(routes, list)

        # When: a route is read whole.
        route = next(item for item in routes if item["tool_family"] == "coding_tools")

        # Then: canonicalizing the workflow fields did not disturb the rest of
        # the card -- the tool family, actions, and their rendered labels are
        # the contract wrappers dispatch on and must survive untouched.
        self.assertEqual(route["tool_family"], "coding_tools")
        self.assertTrue(str(route["primary_next_action"]))
        self.assertTrue(str(route["primary_next_action_label"]))
        self.assertTrue(str(route["fallback_action"]))
        self.assertTrue(list(route["not_evidence_yet"]))

    def test_non_ulw_workflow_identifiers_are_left_alone(self) -> None:
        # Given: a message that routes to a workflow outside the ULW family,
        # whose catalog key is already the name a user invokes.
        # When: the route hint is built.
        payload = awareness_route_hint("run code review on the diff I just pushed")

        # Then: the identifier is emitted unchanged -- canonicalization is
        # scoped to the renamed family, not a blanket relabel of every skill.
        self.assertEqual(payload["selected_workflow"], "code-review")


if __name__ == "__main__":
    unittest.main()
