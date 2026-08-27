from __future__ import annotations

from dataclasses import replace
import unittest

from _local_package import load_local_package

load_local_package()
from omh.skills.catalog import builtin_definitions
from omh.skills.catalog_types import SkillDefinition
from omh.skills.procedure_validation import procedure_violation_ids
from omh.skills.validation import validate_skill_definition_contract


class FalseyContainer:
    def __bool__(self) -> bool:
        return False


class SpecialistProcedureSecurityTests(unittest.TestCase):
    finance: SkillDefinition

    @classmethod
    def setUpClass(cls) -> None:
        cls.finance = next(
            definition
            for definition in builtin_definitions()
            if definition.name == "finance-analysis"
        )

    def _with_field(self, field: str, value: object) -> SkillDefinition:
        mutated = replace(self.finance)
        object.__setattr__(mutated, field, value)
        return mutated

    def test_falsey_malformed_procedure_containers_fail_closed(self) -> None:
        # Given
        cases = (
            ({}, {}),
            (0, False),
            (FalseyContainer(), FalseyContainer()),
            ((), {}),
            ({}, []),
        )

        for steps, checks in cases:
            with self.subTest(steps=steps, checks=checks):
                definition = self._with_field("procedure_steps", steps)
                object.__setattr__(definition, "procedure_checks", checks)

                # When
                direct = procedure_violation_ids(definition)
                public = validate_skill_definition_contract(definition)

                # Then
                self.assertEqual(
                    direct,
                    ["procedure_steps_required", "procedure_checks_required"],
                )
                self.assertIn(
                    "skill finance-analysis procedure_steps_required",
                    public,
                )
                self.assertIn(
                    "skill finance-analysis procedure_checks_required",
                    public,
                )

    def test_supported_empty_procedure_sequences_remain_the_only_opt_out(self) -> None:
        # Given
        cases = (((), ()), ([], []), ((), []), ([], ()))

        for steps, checks in cases:
            with self.subTest(steps=steps, checks=checks):
                definition = self._with_field("procedure_steps", steps)
                object.__setattr__(definition, "procedure_checks", checks)

                # When
                direct = procedure_violation_ids(definition)

                # Then
                self.assertEqual(direct, [])

    def test_malformed_expert_question_containers_return_stable_ids(self) -> None:
        # Given
        cases = (1, {}, set(), object(), FalseyContainer())

        for questions in cases:
            with self.subTest(questions=questions):
                definition = self._with_field("expert_questions", questions)

                # When
                direct = procedure_violation_ids(definition)
                public = validate_skill_definition_contract(definition)

                # Then
                self.assertEqual(
                    direct,
                    [
                        "procedure_invalid_expert_questions",
                        "procedure_missing_required_input_question",
                    ],
                )
                self.assertIn(
                    "skill finance-analysis expert_questions must be a list",
                    public,
                )
                self.assertIn(
                    "skill finance-analysis procedure_invalid_expert_questions",
                    public,
                )

    def test_malformed_expert_question_members_return_stable_ids(self) -> None:
        # Given
        questions = [
            self.finance.expert_questions[0],
            {},
            [],
            set(),
            object(),
            FalseyContainer(),
        ]
        definition = self._with_field("expert_questions", questions)

        # When
        direct = procedure_violation_ids(definition)
        public = validate_skill_definition_contract(definition)

        # Then
        self.assertEqual(
            direct,
            [
                "procedure_invalid_expert_questions",
                "procedure_missing_required_input_question",
            ],
        )
        self.assertIn(
            "skill finance-analysis procedure_invalid_expert_questions",
            public,
        )

    def test_malformed_required_input_containers_return_stable_ids(self) -> None:
        # Given
        cases = (1, {}, set(), object(), FalseyContainer())

        for required_inputs in cases:
            with self.subTest(required_inputs=required_inputs):
                definition = self._with_field("required_inputs", required_inputs)

                # When
                direct = procedure_violation_ids(definition)
                public = validate_skill_definition_contract(definition)

                # Then
                self.assertEqual(
                    direct,
                    [
                        "procedure_invalid_required_inputs",
                        "procedure_unknown_input_ref",
                        "procedure_unknown_required_input_question",
                    ],
                )
                self.assertIn(
                    "skill finance-analysis required_inputs must be a non-empty list",
                    public,
                )
                self.assertIn(
                    "skill finance-analysis procedure_invalid_required_inputs",
                    public,
                )

    def test_malformed_required_input_members_return_stable_ids(self) -> None:
        # Given
        required_inputs = [*self.finance.required_inputs, {}, [], set(), object()]
        definition = self._with_field("required_inputs", required_inputs)

        # When
        direct = procedure_violation_ids(definition)
        public = validate_skill_definition_contract(definition)

        # Then
        self.assertEqual(direct, ["procedure_invalid_required_inputs"])
        self.assertIn(
            "skill finance-analysis procedure_invalid_required_inputs",
            public,
        )

    def test_leading_visual_homoglyph_markers_are_rejected(self) -> None:
        # Given
        first_step = self.finance.procedure_steps[0]
        markers = (
            "ТODO reconcile evidence",
            "TΟDO reconcile evidence",
            "TOᎠO reconcile evidence",
            "ТВD reconcile evidence",
            "[тodo] reconcile evidence",
            "\ufeff`TοDO` reconcile evidence",
            "\u200b(тоꭰо) reconcile evidence",
            "***твd*** reconcile evidence",
        )

        for marker in markers:
            with self.subTest(marker=marker):
                definition = replace(
                    self.finance,
                    procedure_steps=(
                        replace(first_step, instruction=marker),
                        *self.finance.procedure_steps[1:],
                    ),
                )

                # When
                direct = procedure_violation_ids(definition)

                # Then
                self.assertEqual(
                    direct,
                    ["procedure_placeholder_step_instruction"],
                )

    def test_homoglyph_policy_does_not_reject_non_marker_prose(self) -> None:
        # Given
        first_step = self.finance.procedure_steps[0]
        instructions = (
            "Discuss ТODO only as an embedded example.",
            "Todoist records the task without proving completion.",
            "tbdx is an ordinary identifier in this supplied source.",
            "분석 결과를 검토하고 ТODO라는 표기를 설명합니다.",
        )

        for instruction in instructions:
            with self.subTest(instruction=instruction):
                definition = replace(
                    self.finance,
                    procedure_steps=(
                        replace(first_step, instruction=instruction),
                        *self.finance.procedure_steps[1:],
                    ),
                )

                # When
                direct = procedure_violation_ids(definition)

                # Then
                self.assertEqual(direct, [])


if __name__ == "__main__":
    unittest.main()
