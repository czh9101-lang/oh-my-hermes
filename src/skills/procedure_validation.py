from __future__ import annotations

import re

from .catalog_types import SkillDefinition


PROCEDURE_STEP_KINDS = frozenset({"analysis", "production", "validation"})
_STEP_ID_PATTERN = re.compile(r"^[a-z][a-z0-9]*(?:_[a-z0-9]+)*$")


def procedure_violation_ids(definition: SkillDefinition) -> list[str]:
    """Return stable machine violation IDs for an opt-in procedure contract."""
    steps = definition.procedure_steps
    checks = definition.procedure_checks
    if not steps and not checks:
        return []

    violations: list[str] = []
    if not isinstance(steps, (tuple, list)) or not steps:
        return ["procedure_steps_required"]
    declared_checks: set[str] = set()
    if not isinstance(checks, (tuple, list)) or not checks:
        violations.append("procedure_checks_required")
    else:
        for check in checks:
            check_id = getattr(check, "check_id", None)
            result_fields = getattr(check, "required_result_fields", None)
            instruction = getattr(check, "instruction", None)
            if not isinstance(check_id, str) or not _STEP_ID_PATTERN.fullmatch(check_id):
                violations.append("procedure_invalid_check_id")
            elif check_id in declared_checks:
                violations.append("procedure_duplicate_check_id")
            else:
                declared_checks.add(check_id)
            if not isinstance(result_fields, (tuple, list)) or not result_fields:
                violations.append("procedure_check_result_fields_required")
            elif (
                len(set(result_fields)) != len(result_fields)
                or any(not isinstance(field, str) or not _STEP_ID_PATTERN.fullmatch(field) for field in result_fields)
            ):
                violations.append("procedure_duplicate_or_invalid_check_result_field")
            if not isinstance(instruction, str) or not instruction.strip():
                violations.append("procedure_check_instruction_required")

    required_inputs = set(definition.required_inputs)
    expected_outputs = set(definition.expected_outputs)
    referenced_inputs: set[str] = set()
    referenced_outputs: set[str] = set()
    referenced_checks: set[str] = set()
    seen_step_ids: set[str] = set()
    validation_seen = False

    for step in steps:
        step_id = getattr(step, "step_id", None)
        kind = getattr(step, "kind", None)
        input_refs = getattr(step, "input_refs", None)
        output_refs = getattr(step, "output_refs", None)
        check_ids = getattr(step, "check_ids", None)
        instruction = getattr(step, "instruction", None)

        if not isinstance(step_id, str) or not _STEP_ID_PATTERN.fullmatch(step_id):
            violations.append("procedure_invalid_step_id")
        elif step_id in seen_step_ids:
            violations.append("procedure_duplicate_step_id")
        else:
            seen_step_ids.add(step_id)

        if kind not in PROCEDURE_STEP_KINDS:
            violations.append("procedure_unknown_step_kind")
        elif kind == "validation":
            validation_seen = True

        if not isinstance(input_refs, (tuple, list)) or not input_refs:
            violations.append("procedure_step_input_refs_required")
        else:
            referenced_inputs.update(ref for ref in input_refs if isinstance(ref, str))
            if any(ref not in required_inputs for ref in input_refs):
                violations.append("procedure_unknown_input_ref")

        if not isinstance(output_refs, (tuple, list)) or not output_refs:
            violations.append("procedure_step_output_refs_required")
        else:
            referenced_outputs.update(ref for ref in output_refs if isinstance(ref, str))
            if any(ref not in expected_outputs for ref in output_refs):
                violations.append("procedure_unknown_output_ref")

        if not isinstance(check_ids, (tuple, list)) or not check_ids:
            violations.append("procedure_step_check_ids_required")
        else:
            referenced_checks.update(check_id for check_id in check_ids if isinstance(check_id, str))
            if any(check_id not in declared_checks for check_id in check_ids):
                violations.append("procedure_unknown_check_id")

        if not isinstance(instruction, str) or not instruction.strip():
            violations.append("procedure_instruction_required")

    if not required_inputs.issubset(referenced_inputs):
        violations.append("procedure_missing_required_input_ref")
    if not expected_outputs.issubset(referenced_outputs):
        violations.append("procedure_missing_expected_output_ref")
    if declared_checks - referenced_checks:
        violations.append("procedure_unused_check_id")
    if not validation_seen:
        violations.append("procedure_missing_validation_step")

    question_inputs = [getattr(question, "required_input", None) for question in definition.expert_questions]
    question_input_set = {item for item in question_inputs if isinstance(item, str)}
    if required_inputs - question_input_set:
        violations.append("procedure_missing_required_input_question")
    if question_input_set - required_inputs:
        violations.append("procedure_unknown_required_input_question")
    if len(question_input_set) != len(question_inputs):
        violations.append("procedure_duplicate_or_invalid_required_input_question")

    return list(dict.fromkeys(violations))


def validate_procedure_contract(
    definition: SkillDefinition,
    label: str,
    errors: list[str],
) -> None:
    errors.extend(f"{label} {violation}" for violation in procedure_violation_ids(definition))
