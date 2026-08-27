from __future__ import annotations

from dataclasses import replace
import unittest

from _local_package import load_local_package

load_local_package()
from omh.skills.catalog import builtin_definitions
from omh.skills.catalog_types import SkillDefinition
from omh.skills.procedure_validation import procedure_violation_ids
from omh.skills.validation import validate_skill_definition_contract


class SpecialistProcedureControlFormatSecurityTests(unittest.TestCase):
    finance: SkillDefinition

    @classmethod
    def setUpClass(cls) -> None:
        cls.finance = next(
            definition
            for definition in builtin_definitions()
            if definition.name == "finance-analysis"
        )

    def _with_placeholder(self, target: str, value: str) -> tuple[SkillDefinition, str]:
        first_check = self.finance.procedure_checks[0]
        first_step = self.finance.procedure_steps[0]
        if target == "check_instruction":
            return (
                replace(
                    self.finance,
                    procedure_checks=(
                        replace(first_check, instruction=value),
                        *self.finance.procedure_checks[1:],
                    ),
                ),
                "procedure_placeholder_check_instruction",
            )
        if target == "result_field":
            return (
                replace(
                    self.finance,
                    procedure_checks=(
                        replace(first_check, required_result_fields=(value,)),
                        *self.finance.procedure_checks[1:],
                    ),
                ),
                "procedure_placeholder_check_result_field",
            )
        return (
            replace(
                self.finance,
                procedure_steps=(
                    replace(first_step, instruction=value),
                    *self.finance.procedure_steps[1:],
                ),
            ),
            "procedure_placeholder_step_instruction",
        )

    def _assert_placeholder_rejected(self, value: str) -> None:
        for target in ("check_instruction", "result_field", "step_instruction"):
            with self.subTest(value=value, target=target):
                # Given
                definition, expected = self._with_placeholder(target, value)

                # When
                direct = procedure_violation_ids(definition)
                public = validate_skill_definition_contract(definition)

                # Then
                self.assertEqual(direct, [expected])
                self.assertIn(f"skill finance-analysis {expected}", public)

    def test_internal_format_characters_at_every_marker_boundary_are_rejected(self) -> None:
        # Given
        format_characters = (
            "\u200b",  # zero-width space
            "\u200c",  # zero-width non-joiner
            "\u200d",  # zero-width joiner
            "\u2060",  # word joiner
            "\ufeff",  # byte-order mark
            "\u200e",  # left-to-right mark
            "\u200f",  # right-to-left mark
            "\u061c",  # Arabic letter mark
        )

        for marker in ("TODO", "TBD"):
            for character in format_characters:
                for boundary in range(1, len(marker)):
                    # When / Then
                    self._assert_placeholder_rejected(
                        f"{marker[:boundary]}{character}{marker[boundary:]} complete"
                    )

    def test_leading_and_internal_controls_are_rejected(self) -> None:
        # Given
        controls = ("\0", "\x1b", "\x7f")

        for marker in ("TODO", "TBD"):
            for control in controls:
                # When / Then
                self._assert_placeholder_rejected(f"{control}{marker} complete")
                for boundary in range(1, len(marker)):
                    self._assert_placeholder_rejected(
                        f"{marker[:boundary]}{control}{marker[boundary:]} complete"
                    )

    def test_wrapped_case_and_mixed_script_obfuscation_is_rejected(self) -> None:
        # Given / When / Then
        for value in (
            "[t\u200bΟ\x1bD\u2060o] complete",
            "***\x1bТ\u200cвD*** complete",
            "\ufeff`To\u200dᎠO` complete",
            "\0(~t\u200fbd~) complete",
        ):
            self._assert_placeholder_rejected(value)

    def test_tabs_and_newlines_remain_marker_separators(self) -> None:
        # Given
        first_step = self.finance.procedure_steps[0]

        for value in ("T\tODO complete", "TO\nDO complete", "T\rBD complete"):
            with self.subTest(value=value):
                definition = replace(
                    self.finance,
                    procedure_steps=(
                        replace(first_step, instruction=value),
                        *self.finance.procedure_steps[1:],
                    ),
                )

                # When
                direct = procedure_violation_ids(definition)

                # Then
                self.assertEqual(direct, [])

        for value in ("\tTODO complete", "\nTBD complete", "\r\n[TODO] complete"):
            self._assert_placeholder_rejected(value)

    def test_control_and_format_policy_preserves_non_placeholder_prose(self) -> None:
        # Given
        first_step = self.finance.procedure_steps[0]
        values = (
            "분석 결과를 검토하고 TODO 표기를 설명합니다.",
            "Évaluer les preuves avant de discuter TBD.",
            "Discuss T\u200bODO only as an embedded example.",
            "Document the embedded T\x1bBD discussion without accepting it.",
            "To\u200bdoist records the supplied task.",
            "tb\0dx is an ordinary identifier.",
            "T\u200bopic evidence is complete.",
            "Con\x7ftrol provenance is documented.",
            "[Deliver reviewed evidence with citations.]",
            "`Produce the final risk register.`",
        )

        for value in values:
            with self.subTest(value=value):
                definition = replace(
                    self.finance,
                    procedure_steps=(
                        replace(first_step, instruction=value),
                        *self.finance.procedure_steps[1:],
                    ),
                )

                # When
                direct = procedure_violation_ids(definition)
                public = validate_skill_definition_contract(definition)

                # Then
                self.assertEqual(direct, [])
                self.assertNotIn(
                    "skill finance-analysis procedure_placeholder_step_instruction",
                    public,
                )


if __name__ == "__main__":
    unittest.main()
