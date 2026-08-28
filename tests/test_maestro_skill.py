"""Contract tests for the `maestro` (display name `ulw-maestro`) skill.

Pins the surfaces the implementation spec (`.omc/plans/ulw-maestro-spec.md`)
requires: ULW membership agreement across the three coupled tables, the
mechanical display-name derivation, the `ultrawork` Hermes-category carve-out
and install path, trigger disjointness from the named-coding-agent phrase
tables and the retired Codex owner-choice cues, the `HERMES_HARNESS_DEFAULT_WORDING`
substance and both handoff-mode names in the quality bar, executor-neutral
wording (no "default" co-located with a CLI name), and the absence of
Hermes-mixture worker/team vocabulary in maestro's own copy.
"""

from __future__ import annotations

import unittest

from _local_package import load_local_package

load_local_package()

from omh.coding.orchestration_vocabulary import HERMES_HARNESS_DEFAULT_WORDING
from omh.routing.executor_cues import NAMED_CODING_AGENT_PHRASES
from omh.routing.ulw_alias import CODEX_OWNER_CHOICE_CUES
from omh.skills.catalog import (
    ULTRAWORK_HERMES_CATEGORY,
    _ULW_ENGINE_ORDER,
    _ULW_ENGINE_PRESENTATIONS,
    builtin_definitions,
    hermes_skill_category,
    omh_skill_install_path,
)
from omh.skills.catalog_types import (
    OMH_SKILL_DISPLAY_NAME_OVERRIDES,
    ULW_ENGINE_SKILL_NAMES,
    omh_skill_display_name,
)

# Same tokens `tests/test_orchestration_vocabulary.py` forbids in Hermes
# mixture/Maestro vocabulary copy: Maestro hands work to a chosen external
# CLI, never to a "worker" or "team", or the two paths blur back together.
_WORKER_TEAM_TOKENS = {"worker", "workers", "team", "teams"}

# Mirrors `owner_neutrality_findings` in `tests/test_orchestration_vocabulary.py`:
# copy is a defect (`owner_neutrality_lost`) when it co-locates the word
# "default" with a specific external CLI's name, promoting that CLI to the
# implicit default coding owner.
_EXTERNAL_CLI_NAME_MENTIONS = ("codex", "claude code", "claude-code")


def _maestro_definition():
    return next(item for item in builtin_definitions() if item.name == "maestro")


def _owner_neutrality_findings(lines: tuple[str, ...]) -> list[str]:
    findings = []
    for line in lines:
        folded = line.casefold()
        if "default" in folded and any(name in folded for name in _EXTERNAL_CLI_NAME_MENTIONS):
            findings.append(line)
    return findings


class MaestroUlwMembershipTests(unittest.TestCase):
    def test_the_three_coupled_ulw_tables_agree_on_maestro(self) -> None:
        self.assertIn("maestro", ULW_ENGINE_SKILL_NAMES)
        self.assertIn("maestro", _ULW_ENGINE_ORDER)
        self.assertIn("maestro", _ULW_ENGINE_PRESENTATIONS)
        # Import-time gate in catalog.py already enforces set/length equality
        # between _ULW_ENGINE_ORDER and ULW_ENGINE_SKILL_NAMES; re-assert it
        # here as the maestro-specific regression pin rather than relying on
        # the package having imported cleanly.
        self.assertEqual(set(_ULW_ENGINE_ORDER), set(ULW_ENGINE_SKILL_NAMES))
        self.assertEqual(len(_ULW_ENGINE_ORDER), len(ULW_ENGINE_SKILL_NAMES))

    def test_maestro_is_registered_as_a_builtin_skill_definition(self) -> None:
        definition = _maestro_definition()
        self.assertEqual(definition.name, "maestro")


class MaestroDisplayNameAndCategoryTests(unittest.TestCase):
    def test_display_name_is_mechanically_prefixed_with_no_override(self) -> None:
        self.assertNotIn("maestro", OMH_SKILL_DISPLAY_NAME_OVERRIDES)
        self.assertEqual(omh_skill_display_name("maestro"), "ulw-maestro")

    def test_hermes_category_is_the_ultrawork_carve_out(self) -> None:
        self.assertEqual(hermes_skill_category("maestro"), ULTRAWORK_HERMES_CATEGORY)

    def test_install_path_nests_under_ultrawork(self) -> None:
        self.assertEqual(omh_skill_install_path("maestro"), "ultrawork/ulw-maestro")


class MaestroTriggerDisjointnessTests(unittest.TestCase):
    """§5.1: no maestro trigger may contain a bare CLI name or a retired
    owner-choice cue -- those are owner-selection signals, not engine
    signals, and reclaiming them for an engine is the Q9 defect."""

    def test_no_trigger_contains_a_named_coding_agent_phrase(self) -> None:
        definition = _maestro_definition()
        for trigger in definition.triggers:
            folded = trigger.casefold()
            for phrase in NAMED_CODING_AGENT_PHRASES:
                with self.subTest(trigger=trigger, phrase=phrase):
                    self.assertNotIn(phrase.casefold(), folded)

    def test_no_trigger_contains_a_retired_codex_owner_choice_cue(self) -> None:
        definition = _maestro_definition()
        for trigger in definition.triggers:
            folded = trigger.casefold()
            for cue in CODEX_OWNER_CHOICE_CUES:
                with self.subTest(trigger=trigger, cue=cue):
                    self.assertNotIn(cue.casefold(), folded)

    def test_no_trigger_is_the_bare_ambiguous_maestro_token(self) -> None:
        definition = _maestro_definition()
        self.assertNotIn("maestro", definition.triggers)


class MaestroQualityBarContentTests(unittest.TestCase):
    def test_quality_bar_carries_the_hermes_harness_default_wording(self) -> None:
        definition = _maestro_definition()
        combined = " ".join(definition.quality_bar) + " " + definition.why_this_exists
        self.assertIn(HERMES_HARNESS_DEFAULT_WORDING, combined)

    def test_quality_bar_names_both_handoff_modes(self) -> None:
        definition = _maestro_definition()
        combined = " ".join(definition.quality_bar)
        self.assertIn("prompt-only", combined)
        self.assertIn("dispatchable", combined)
        # The schema identifiers must be the real constants, not shorthand:
        # a truncated identifier in the mode-statement rule is exactly the
        # defect review caught, so assert against the source of truth.
        from omh.coding.executors import (
            EXECUTOR_HANDOFF_SCHEMA_VERSION,
            PROMPT_HANDOFF_SCHEMA_VERSION,
            RUNTIME_HANDOFF_SCHEMA_VERSION,
        )
        for schema in (
            PROMPT_HANDOFF_SCHEMA_VERSION,
            EXECUTOR_HANDOFF_SCHEMA_VERSION,
            RUNTIME_HANDOFF_SCHEMA_VERSION,
        ):
            self.assertIn(f"`{schema}`", combined)


class MaestroExecutorNeutralityTests(unittest.TestCase):
    def test_no_quality_bar_or_why_this_exists_line_co_locates_default_with_a_cli_name(self) -> None:
        definition = _maestro_definition()
        # Scan every prose surface the definition ships, not just the quality
        # bar: review found the one violating string living in handoff_policy,
        # the surface the narrower scan missed.
        lines = (
            tuple(definition.quality_bar)
            + tuple(definition.safety_rules)
            + tuple(definition.do_not_use_when)
            + (definition.why_this_exists, definition.handoff_policy)
        )
        findings = _owner_neutrality_findings(lines)
        self.assertEqual(findings, [], f"owner_neutrality_lost findings: {findings}")

    def test_maestro_copy_never_uses_hermes_mixture_worker_team_vocabulary(self) -> None:
        definition = _maestro_definition()
        surfaces = (
            definition.description,
            definition.use_when,
            definition.why_this_exists,
            *definition.quality_bar,
            *definition.safety_rules,
            *definition.do_not_use_when,
        )
        for text in surfaces:
            words = set(text.casefold().replace("-", " ").split())
            overlap = words & _WORKER_TEAM_TOKENS
            with self.subTest(text=text):
                self.assertEqual(overlap, set())


if __name__ == "__main__":
    unittest.main()
