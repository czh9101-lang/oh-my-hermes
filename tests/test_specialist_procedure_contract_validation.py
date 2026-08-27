from __future__ import annotations

from dataclasses import replace
import unittest

from _local_package import load_local_package

load_local_package()
from omh.skills.catalog import builtin_definitions
from omh.skills.catalog_types import ExpertQuestion, ProcedureCheck, SkillDefinition
from omh.skills.procedure_validation import procedure_violation_ids
from omh.skills.validation import validate_skill_definition_contract


class SpecialistProcedureContractTests(unittest.TestCase):
    def setUp(self) -> None:
        self.definitions = {definition.name: definition for definition in builtin_definitions()}
        self.finance = self.definitions["finance-analysis"]

    def test_missing_required_input_reference_fails_independently(self) -> None:
        omitted = self.finance.required_inputs[-1]
        steps = tuple(
            replace(step, input_refs=tuple(ref for ref in step.input_refs if ref != omitted))
            for step in self.finance.procedure_steps
        )
        self.assertEqual(
            procedure_violation_ids(replace(self.finance, procedure_steps=steps)),
            ["procedure_missing_required_input_ref"],
        )

    def test_missing_expected_output_reference_fails_independently(self) -> None:
        omitted = self.finance.expected_outputs[-1]
        fallback = self.finance.expected_outputs[0]
        steps = tuple(
            replace(
                step,
                output_refs=tuple(ref for ref in step.output_refs if ref != omitted) or (fallback,),
            )
            for step in self.finance.procedure_steps
        )
        self.assertEqual(
            procedure_violation_ids(replace(self.finance, procedure_steps=steps)),
            ["procedure_missing_expected_output_ref"],
        )

    def test_duplicate_step_id_fails_independently(self) -> None:
        steps = (
            self.finance.procedure_steps[0],
            replace(
                self.finance.procedure_steps[1],
                step_id=self.finance.procedure_steps[0].step_id,
            ),
            *self.finance.procedure_steps[2:],
        )
        self.assertEqual(
            procedure_violation_ids(replace(self.finance, procedure_steps=steps)),
            ["procedure_duplicate_step_id"],
        )

    def test_unknown_input_output_and_check_refs_fail_independently(self) -> None:
        first = self.finance.procedure_steps[0]
        mutations = (
            (
                replace(first, input_refs=(*first.input_refs, "unknown-input")),
                "procedure_unknown_input_ref",
            ),
            (
                replace(first, output_refs=(*first.output_refs, "unknown-output")),
                "procedure_unknown_output_ref",
            ),
            (
                replace(first, check_ids=(*first.check_ids, "unknown-check")),
                "procedure_unknown_check_id",
            ),
        )
        for mutated, expected in mutations:
            with self.subTest(expected=expected):
                steps = (mutated, *self.finance.procedure_steps[1:])
                self.assertEqual(
                    procedure_violation_ids(replace(self.finance, procedure_steps=steps)),
                    [expected],
                )

    def test_missing_required_input_question_coverage_fails_independently(self) -> None:
        self.assertEqual(
            procedure_violation_ids(
                replace(self.finance, expert_questions=self.finance.expert_questions[:-1])
            ),
            ["procedure_missing_required_input_question"],
        )

    def test_unknown_required_input_question_fails_independently(self) -> None:
        questions = (
            *self.finance.expert_questions,
            ExpertQuestion("unknown-input", "Unknown?", "알 수 없나요?"),
        )
        self.assertEqual(
            procedure_violation_ids(replace(self.finance, expert_questions=questions)),
            ["procedure_unknown_required_input_question"],
        )

    def test_no_validation_step_fails_independently(self) -> None:
        steps = tuple(replace(step, kind="analysis") for step in self.finance.procedure_steps)
        self.assertEqual(
            procedure_violation_ids(replace(self.finance, procedure_steps=steps)),
            ["procedure_missing_validation_step"],
        )

    def test_check_value_mutations_fail_independently(self) -> None:
        first_check = self.finance.procedure_checks[0]
        first_step = self.finance.procedure_steps[0]

        def with_check(check: ProcedureCheck) -> SkillDefinition:
            return replace(
                self.finance,
                procedure_checks=(check, *self.finance.procedure_checks[1:]),
            )

        fixtures = (
            (
                with_check(replace(first_check, required_result_fields=())),
                "procedure_check_result_fields_required",
            ),
            (
                with_check(
                    replace(
                        first_check,
                        required_result_fields=(
                            first_check.required_result_fields[0],
                            first_check.required_result_fields[0],
                        ),
                    )
                ),
                "procedure_duplicate_or_invalid_check_result_field",
            ),
            (
                with_check(replace(first_check, instruction="TODO")),
                "procedure_placeholder_check_instruction",
            ),
            (
                with_check(replace(first_check, instruction="  todo: add criterion")),
                "procedure_placeholder_check_instruction",
            ),
            (
                with_check(replace(first_check, instruction="\tTbD - choose criterion")),
                "procedure_placeholder_check_instruction",
            ),
            (
                replace(
                    self.finance,
                    procedure_steps=(
                        replace(first_step, instruction=" TBD "),
                        *self.finance.procedure_steps[1:],
                    ),
                ),
                "procedure_placeholder_step_instruction",
            ),
            (
                replace(
                    self.finance,
                    procedure_steps=(
                        replace(first_step, instruction="todo: implement this step"),
                        *self.finance.procedure_steps[1:],
                    ),
                ),
                "procedure_placeholder_step_instruction",
            ),
            (
                with_check(replace(first_check, required_result_fields=("todo",))),
                "procedure_placeholder_check_result_field",
            ),
            (
                with_check(replace(first_check, required_result_fields=("ToDo_status",))),
                "procedure_placeholder_check_result_field",
            ),
            (
                with_check(replace(first_check, required_result_fields=("  tBd_owner  ",))),
                "procedure_placeholder_check_result_field",
            ),
        )

        for mutated, expected in fixtures:
            with self.subTest(expected=expected, mutation=mutated):
                self.assertEqual(procedure_violation_ids(mutated), [expected])
                self.assertIn(
                    f"skill finance-analysis {expected}",
                    validate_skill_definition_contract(mutated),
                )

        valid = replace(
            self.finance,
            procedure_checks=(
                replace(
                    first_check,
                    instruction="Track TODO items reported by the supplied source without inventing values.",
                    required_result_fields=("methodology_status",),
                ),
                *self.finance.procedure_checks[1:],
            ),
            procedure_steps=(
                replace(
                    first_step,
                    instruction="Record whether the supplied system has a TODO queue.",
                ),
                *self.finance.procedure_steps[1:],
            ),
        )
        self.assertEqual(procedure_violation_ids(valid), [])


if __name__ == "__main__":
    unittest.main()
