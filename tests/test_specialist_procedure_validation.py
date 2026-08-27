from __future__ import annotations

from dataclasses import replace
import unittest

from _local_package import load_local_package

load_local_package()
from omh.skills.catalog import builtin_definitions
from omh.skills.catalog_types import SkillDefinition
from omh.skills.procedure_validation import procedure_violation_ids
from omh.skills.validation import validate_skill_definition_contract


class SpecialistProcedureInputValidationTests(unittest.TestCase):
    finance: SkillDefinition

    @classmethod
    def setUpClass(cls) -> None:
        cls.finance = next(
            definition
            for definition in builtin_definitions()
            if definition.name == "finance-analysis"
        )

    def test_placeholder_prefix_normalizes_confusables_and_wrappers(self) -> None:
        first_check = self.finance.procedure_checks[0]
        first_step = self.finance.procedure_steps[0]
        placeholder_values = (
            "ＴＯＤＯ complete criterion",
            "\ufeffTODO complete criterion",
            "\u200bTBD complete criterion",
            "[TODO] complete criterion",
            "`TBD` complete criterion",
        )

        for value in placeholder_values:
            with self.subTest(value=value, target="check"):
                mutated = replace(
                    self.finance,
                    procedure_checks=(
                        replace(first_check, instruction=value),
                        *self.finance.procedure_checks[1:],
                    ),
                )
                self.assertEqual(
                    procedure_violation_ids(mutated),
                    ["procedure_placeholder_check_instruction"],
                )

            with self.subTest(value=value, target="step"):
                mutated = replace(
                    self.finance,
                    procedure_steps=(
                        replace(first_step, instruction=value),
                        *self.finance.procedure_steps[1:],
                    ),
                )
                self.assertEqual(
                    procedure_violation_ids(mutated),
                    ["procedure_placeholder_step_instruction"],
                )

        field_values = (
            "ＴＯＤＯ_status",
            "\ufeffTODO_status",
            "[TBD]_status",
            "`TODO`_status",
        )
        for value in field_values:
            with self.subTest(value=value, target="result_field"):
                mutated = replace(
                    self.finance,
                    procedure_checks=(
                        replace(first_check, required_result_fields=(value,)),
                        *self.finance.procedure_checks[1:],
                    ),
                )
                self.assertEqual(
                    procedure_violation_ids(mutated),
                    ["procedure_placeholder_check_result_field"],
                )

    def test_malformed_procedure_members_return_stable_violations(self) -> None:
        first_check = replace(self.finance.procedure_checks[0])
        object.__setattr__(first_check, "required_result_fields", ({},))

        first_step = self.finance.procedure_steps[0]
        input_step = replace(first_step)
        object.__setattr__(input_step, "input_refs", (*first_step.input_refs, {}))
        output_step = replace(first_step)
        object.__setattr__(output_step, "output_refs", (*first_step.output_refs, []))
        check_step = replace(first_step)
        object.__setattr__(check_step, "check_ids", (*first_step.check_ids, {}))

        mutations = (
            (
                replace(
                    self.finance,
                    procedure_checks=(first_check, *self.finance.procedure_checks[1:]),
                ),
                "procedure_duplicate_or_invalid_check_result_field",
            ),
            (
                replace(
                    self.finance,
                    procedure_steps=(input_step, *self.finance.procedure_steps[1:]),
                ),
                "procedure_unknown_input_ref",
            ),
            (
                replace(
                    self.finance,
                    procedure_steps=(output_step, *self.finance.procedure_steps[1:]),
                ),
                "procedure_unknown_output_ref",
            ),
            (
                replace(
                    self.finance,
                    procedure_steps=(check_step, *self.finance.procedure_steps[1:]),
                ),
                "procedure_unknown_check_id",
            ),
        )

        for mutated, expected in mutations:
            with self.subTest(expected=expected):
                self.assertEqual(procedure_violation_ids(mutated), [expected])
                self.assertIn(
                    f"skill finance-analysis {expected}",
                    validate_skill_definition_contract(mutated),
                )


if __name__ == "__main__":
    unittest.main()
