from __future__ import annotations

import unittest

from _local_package import load_local_package
from specialist_procedure_contract_support import TARGETS

load_local_package()
from omh.skills.catalog import builtin_definitions
from omh.skills.catalog_types import ProcedureStep
from omh.skills.procedure_validation import procedure_violation_ids


class SpecialistProcedureContractTests(unittest.TestCase):
    def setUp(self) -> None:
        self.definitions = {definition.name: definition for definition in builtin_definitions()}
        self.finance = self.definitions["finance-analysis"]

    def test_locked_targets_have_complete_machine_contracts(self) -> None:
        procedure_targets = {
            name for name, definition in self.definitions.items() if definition.procedure_steps
        }
        self.assertEqual(procedure_targets, TARGETS)

        for name in sorted(TARGETS):
            definition = self.definitions[name]
            with self.subTest(name=name):
                self.assertEqual(procedure_violation_ids(definition), [])
                self.assertTrue(all(isinstance(step, ProcedureStep) for step in definition.procedure_steps))
                self.assertEqual(
                    {question.required_input for question in definition.expert_questions},
                    set(definition.required_inputs),
                )
                self.assertEqual(
                    {ref for step in definition.procedure_steps for ref in step.input_refs},
                    set(definition.required_inputs),
                )
                self.assertEqual(
                    {ref for step in definition.procedure_steps for ref in step.output_refs},
                    set(definition.expected_outputs),
                )
                self.assertTrue(any(step.kind == "validation" for step in definition.procedure_steps))


if __name__ == "__main__":
    unittest.main()
