"""Canonical ULW identifiers on the public routing surface (issue #1249).

The catalog keeps legacy internal keys (`ralplan`, `ultraqa`, `ultrawork`)
because every trigger table, capability map, and lifecycle row is built from
them. What the user sees is a different contract: the installed skills are
`ulw-plan`, `ulw-qa`, and `ulw-work`, so a route plan naming `ralplan` hands
the reader an identifier no installed skill answers to.

These tests pin the boundary rule: legacy names stay accepted as INPUT, and the
public route plan emits canonical `ulw-*` identifiers only.
"""

from __future__ import annotations

import unittest

from _local_package import load_local_package

load_local_package()
from omh.chat_router import public_route_payload, route_chat_message
from omh.skills.catalog_types import ULW_ENGINE_SKILL_NAMES, omh_skill_display_name

# The internal catalog keys whose public identifier is a `ulw-` label. A legacy
# key surfacing in public routing output is exactly the #1249 defect.
LEGACY_ULW_KEYS = frozenset(
    name for name in ULW_ENGINE_SKILL_NAMES if omh_skill_display_name(name) != name
)

# One message per legacy input spelling the issue names, plus the bare `ulw`
# alias. Each must stay routable; none may produce a legacy identifier.
LEGACY_INPUT_MESSAGES = (
    "use ralplan to plan this rollout",
    "ultraqa the setup wizard with hostile install paths",
    "ultrawork this refactor until the tests pass",
    "ulw this refactor until the tests pass",
)

# The shape that builds a full research -> plan -> deliver -> review plan, so
# more than one ULW engine lands in the same emitted payload.
MULTI_STAGE_MESSAGE = (
    "Research current install friction, make a reviewed plan, implement with Codex, "
    "run code review, and sync docs for a PR."
)


def _route_plan(message: str) -> dict[str, object] | None:
    decision = route_chat_message(message, source="discord", limit=8)
    plan = public_route_payload(decision).get("workflow_route_plan")
    return plan if isinstance(plan, dict) else None


def _public_selected_workflow(message: str) -> str:
    """The workflow identifier the public route explanation hands a wrapper.

    `decision["selected_skill"]` is deliberately NOT this value: it is the
    catalog key that `primary_harness_for_skill`, task cards, and the
    specialist lookup all resolve against. The public projection is where the
    catalog key becomes a name the user can invoke.
    """
    decision = route_chat_message(message, source="discord", limit=8)
    return str(public_route_payload(decision)["route_explanation"]["selected_workflow"])


def _plan_identifiers(plan: dict[str, object]) -> list[str]:
    """Every workflow identifier the plan hands a reader, `next_action` included.

    `next_action` is `start_with_<skill>`, so its suffix is an identifier too:
    it is the field a wrapper renders as the button label.
    """
    steps = plan.get("steps", [])
    identifiers = [str(step["skill"]) for step in steps if isinstance(step, dict)]
    identifiers.append(str(plan.get("primary_skill", "")))
    next_action = str(plan.get("next_action", ""))
    identifiers.append(next_action.removeprefix("start_with_"))
    return [value for value in identifiers if value]


class CanonicalUlwWorkflowNameTests(unittest.TestCase):
    def test_route_plan_emits_only_canonical_ulw_names(self) -> None:
        # Given: a request that spans research, planning, delivery, and review,
        # so several ULW engines appear in one emitted route plan.
        plan = _route_plan(MULTI_STAGE_MESSAGE)
        self.assertIsNotNone(plan, "the multi-stage request must build a route plan")
        assert plan is not None

        # When: the public payload is read the way a wrapper reads it.
        identifiers = _plan_identifiers(plan)

        # Then: every ULW identifier is the canonical `ulw-` label, and the
        # legacy catalog keys never reach the public surface.
        stage_by_skill = {
            str(step["stage"]): str(step["skill"])
            for step in plan["steps"]  # type: ignore[index]
            if isinstance(step, dict)
        }
        self.assertEqual(stage_by_skill.get("plan"), "ulw-plan")
        self.assertEqual(stage_by_skill.get("deliver"), "ulw-work")
        self.assertEqual(plan["primary_skill"], "ulw-work")
        # Exact comparison, not substring: `research` is itself a legacy key
        # whose public label is `ulw-research`, so a substring check would
        # flag the very canonicalization it is meant to prove.
        for identifier in identifiers:
            self.assertNotIn(
                identifier,
                LEGACY_ULW_KEYS,
                f"public route plan emitted legacy ULW name {identifier!r}",
            )

    def test_legacy_ulw_names_stay_accepted_as_input(self) -> None:
        # Given: the legacy spellings users and stale docs still type.
        for message in LEGACY_INPUT_MESSAGES:
            with self.subTest(message=message):
                # When: each one is routed through the real chat surface.
                selected = _public_selected_workflow(message)

                # Then: it still resolves to a ULW engine, and the identifier
                # handed back is the canonical label rather than the input word.
                self.assertTrue(
                    selected.startswith("ulw-"),
                    f"{message!r} resolved to {selected!r}, not a canonical ULW name",
                )
                self.assertNotIn(selected, LEGACY_ULW_KEYS)

    def test_verify_stage_emits_the_canonical_qa_name(self) -> None:
        # Given: a request that asks for adversarial verification by its legacy
        # name, which is the `ultraqa` -> `ulw-qa` pair from the issue.
        # When: the public selection identifier is read.
        selected = _public_selected_workflow(
            "ultraqa the setup wizard with hostile install paths and stale config"
        )

        # Then: it is the canonical QA label.
        self.assertEqual(selected, "ulw-qa")


if __name__ == "__main__":
    unittest.main()
