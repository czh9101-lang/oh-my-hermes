from __future__ import annotations

from dataclasses import replace
from pathlib import Path
import unittest

from _local_package import load_local_package

load_local_package()
from omh.skills.catalog import builtin_definitions
from omh.skills.catalog_types import ExpertQuestion, ProcedureStep
from omh.skills.packaging import builtin_skill_reference_templates
from omh.skills.procedure_validation import procedure_violation_ids
from omh.skills.render import builtin_skill_templates, workflow_reference_payload


TARGETS = frozenset(
    {
        "finance-analysis",
        "legal-compliance-review",
        "sales-development",
        "curriculum-design",
    }
)


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

    def test_machine_payload_and_shipped_target_bytes_match_catalog(self) -> None:
        payloads = {item["name"]: item for item in workflow_reference_payload()["skills"]}
        templates = {template.name: template.content for template in builtin_skill_templates()}
        references = {
            template.skill_name: template.content
            for template in builtin_skill_reference_templates()
            if template.relative_path == "references/procedure.md"
        }
        self.assertEqual(set(references), TARGETS)

        for name in sorted(TARGETS):
            definition = self.definitions[name]
            expected_steps = [
                {
                    "step_id": step.step_id,
                    "kind": step.kind,
                    "input_refs": list(step.input_refs),
                    "output_refs": list(step.output_refs),
                    "check_ids": list(step.check_ids),
                    "instruction": step.instruction,
                }
                for step in definition.procedure_steps
            ]
            with self.subTest(name=name):
                self.assertEqual(payloads[name]["procedure_checks"], list(definition.procedure_checks))
                self.assertEqual(payloads[name]["procedure_steps"], expected_steps)
                shipped = Path(f"skills/omh-{name}/SKILL.md").read_text(encoding="utf-8")
                shipped_reference = Path(f"skills/omh-{name}/references/procedure.md").read_text(encoding="utf-8")
                self.assertEqual(shipped, templates[name])
                self.assertEqual(shipped_reference, references[name])


if __name__ == "__main__":
    unittest.main()
