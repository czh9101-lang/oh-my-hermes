from __future__ import annotations

import dataclasses
import unittest
from unittest import mock

from omh.skills.catalog import builtin_definitions
from omh.skills.render import SkillTemplate, workflow_skill_from_definition
from omh.skills.validation import (
    STRUCTURE_LINT_SKILL_BODY_BYTE_CEILING,
    skill_structure_lint_payload,
)


def _definition(name: str):
    return next(item for item in builtin_definitions() if item.name == name)


def _rules(payload: dict[str, object]) -> list[str]:
    violations = payload["violations"]
    assert isinstance(violations, list)
    return [str(item["rule"]) for item in violations]


def _lint_mutant(mutant):
    from omh.skills import render

    definitions = {item.name: item for item in builtin_definitions()}
    definitions[mutant.name] = mutant
    render._definitions_by_name.cache_clear()
    with mock.patch.object(render, "_definitions_by_name", return_value=definitions):
        return skill_structure_lint_payload(definitions=[mutant])


class FrontmatterScalarSecurityTests(unittest.TestCase):
    def test_rejects_host_invalid_emitted_scalar_edges(self) -> None:
        definition = _definition("skill-health")
        cases = (
            "broken: mapping",
            "broken\x00mapping",
            "broken\x1fmapping",
            "value # hidden comment",
            "'unterminated",
            '"unterminated',
            "---",
            "[unterminated",
            "{unterminated",
        )
        for scalar in cases:
            content = f"---\nname: omh-skill-health\ndescription: {scalar}\nmetadata:\n---\n"
            with self.subTest(scalar=repr(scalar)), mock.patch(
                "omh.skills.render.workflow_skill_from_definition",
                return_value=SkillTemplate(definition.name, content),
            ):
                payload = skill_structure_lint_payload(definitions=[definition])
                self.assertIn("SKILL_FRONTMATTER_FIELDS", _rules(payload))

    def test_safely_encodes_yaml_edges_and_preserves_valid_unicode(self) -> None:
        definition = _definition("skill-health")
        for description in ("broken: mapping", "value # comment", "'quoted'", "[edge]", "안전한 설명 😀"):
            with self.subTest(description=description):
                mutant = dataclasses.replace(definition, description=description)
                self.assertNotIn("SKILL_FRONTMATTER_FIELDS", _rules(_lint_mutant(mutant)))

    def test_rejects_decoded_control_characters(self) -> None:
        definition = _definition("skill-health")
        for description in ("broken\x00mapping", "broken\x1fmapping"):
            with self.subTest(description=repr(description)):
                mutant = dataclasses.replace(definition, description=description)
                self.assertIn("SKILL_FRONTMATTER_FIELDS", _rules(_lint_mutant(mutant)))

    def test_canonical_definitions_pass(self) -> None:
        self.assertEqual(skill_structure_lint_payload()["ok"], True)


class Utf8ContextBudgetTests(unittest.TestCase):
    def _at_encoded_size(self, size: int):
        definition = _definition("skill-health")
        seed = dataclasses.replace(definition, use_when="😀")
        rendered = workflow_skill_from_definition(seed, seed.name).content
        overhead = len(rendered.encode("utf-8")) - len("😀".encode("utf-8"))
        payload_size = size - overhead
        emoji_count, ascii_count = divmod(payload_size, len("😀".encode("utf-8")))
        return dataclasses.replace(definition, use_when="😀" * emoji_count + "x" * ascii_count)

    def test_multibyte_body_just_below_ceiling_passes(self) -> None:
        mutant = self._at_encoded_size(STRUCTURE_LINT_SKILL_BODY_BYTE_CEILING)

        payload = _lint_mutant(mutant)

        self.assertNotIn("SKILL_CONTEXT_BUDGET", _rules(payload))

    def test_multibyte_body_just_above_ceiling_reports_actual_bytes(self) -> None:
        expected = STRUCTURE_LINT_SKILL_BODY_BYTE_CEILING + 1
        mutant = self._at_encoded_size(expected)

        payload = _lint_mutant(mutant)

        violations = payload["violations"]
        assert isinstance(violations, list)
        violation = next(item for item in violations if item["rule"] == "SKILL_CONTEXT_BUDGET")
        self.assertIn(f"{expected} bytes", violation["detail"])


class AggregateCatalogIntegrityTests(unittest.TestCase):
    def test_empty_definitions_fail_closed_without_claiming_full_catalog_completeness(self) -> None:
        payload = skill_structure_lint_payload(definitions=[])

        self.assertEqual(payload["ok"], False)
        self.assertEqual(_rules(payload), ["SKILL_CATALOG_NONEMPTY"])
        self.assertEqual(payload["catalog_scope"], "supplied_subset")
        self.assertNotIn("expected_skill_count", payload)

    def test_duplicate_canonical_identity_is_deterministic(self) -> None:
        definition = _definition("skill-health")

        first = skill_structure_lint_payload(definitions=[definition, definition])
        second = skill_structure_lint_payload(definitions=[definition, definition])

        self.assertEqual(first, second)
        self.assertEqual(_rules(first), ["SKILL_CANONICAL_IDENTITY_UNIQUE"])

    def test_duplicate_rendered_identity_fails_closed(self) -> None:
        first = _definition("skill-health")
        second = _definition("skill-scout")
        with mock.patch("omh.skills.render.omh_skill_display_name", return_value="same-name"):
            payload = skill_structure_lint_payload(definitions=[first, second])

        self.assertEqual(_rules(payload), ["SKILL_RENDERED_IDENTITY_UNIQUE"])

    def test_default_call_explicitly_reports_full_catalog_scope(self) -> None:
        payload = skill_structure_lint_payload()

        self.assertEqual(payload["catalog_scope"], "full_catalog")
        self.assertEqual(payload["expected_skill_count"], len(builtin_definitions()))


class SkillHealthArtifactSchemaTests(unittest.TestCase):
    def test_declared_artifact_matches_executable_wrapper_schema(self) -> None:
        payload = skill_structure_lint_payload(definitions=[_definition("skill-health")])

        self.assertNotIn("SKILL_EXECUTABLE_CONSUMER", _rules(payload))

    def test_schema_drift_fails_the_executable_consumer_rule(self) -> None:
        from omh.wrapper import contract

        cards = dict(contract._WORKFLOW_OPERATIONS_CHAT_CARDS)
        cards["skill-health"] = {**cards["skill-health"], "artifact_schema": "drifted/v1"}
        with mock.patch.object(contract, "_WORKFLOW_OPERATIONS_CHAT_CARDS", cards):
            payload = skill_structure_lint_payload(definitions=[_definition("skill-health")])

        self.assertEqual(_rules(payload), ["SKILL_EXECUTABLE_CONSUMER"])


if __name__ == "__main__":
    unittest.main()
