"""Catalog registration contract for issues #1371 and #1373-#1376."""

from __future__ import annotations

import unittest

from omh.catalogs.specialists import specialist_for_skill
from omh.skills.catalog import builtin_definitions, primary_harness_for_skill
from omh.skills.llm_app_references import (
    LLM_APP_DEV_CONDITIONAL_CONTRACTS,
    LLM_APP_DEV_STATEFUL_CONTRACTS_REFERENCE_PATH,
)
from omh.skills.packaging import builtin_skill_reference_templates
from omh.skills.procedure_validation import procedure_violation_ids
from omh.skills.validation import validate_skill_definition_contract


class IssueWorkflowCatalogTests(unittest.TestCase):
    def test_canonical_workflows_are_installable_with_existing_role_harness_and_specialist_mappings(self) -> None:
        definitions = {definition.name: definition for definition in builtin_definitions()}
        expected = {
            "decision-prototype": ("planner", "coding-handling", "product-planning"),
            "lifecycle-growth": ("operator", "planning", "operations-data"),
            "product-discovery-validation": ("planner", "strategy-synthesis", "product-planning"),
            "sales-pipeline-review": ("operator", "ops-review", "operations-data"),
        }

        for name, (role, harness, specialist_id) in expected.items():
            with self.subTest(name=name):
                definition = definitions[name]
                self.assertEqual(role, definition.hermes_role)
                self.assertEqual(harness, primary_harness_for_skill(name))
                specialist = specialist_for_skill(name)
                self.assertIsNotNone(specialist)
                self.assertEqual(specialist_id, specialist.id)
                self.assertEqual([], procedure_violation_ids(definition))
                self.assertEqual([], validate_skill_definition_contract(definition))

    def test_llm_stateful_contracts_are_conditional_and_discoverable(self) -> None:
        definitions = {definition.name: definition for definition in builtin_definitions()}
        reference_paths = {
            (template.skill_name, template.relative_path)
            for template in builtin_skill_reference_templates()
        }

        self.assertEqual(
            (
                ("presents or acts on business records", "record authority and presentation receipts"),
                ("enforces a cumulative business limit", "shared resulting-state limits"),
                ("stores facts about a person", "user-memory lifecycle"),
            ),
            LLM_APP_DEV_CONDITIONAL_CONTRACTS,
        )
        self.assertIn(("llm-app-dev", LLM_APP_DEV_STATEFUL_CONTRACTS_REFERENCE_PATH), reference_paths)
        self.assertIn(LLM_APP_DEV_STATEFUL_CONTRACTS_REFERENCE_PATH, "\n".join(definitions["llm-app-dev"].quality_bar))


if __name__ == "__main__":
    unittest.main()
