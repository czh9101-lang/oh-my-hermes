from __future__ import annotations

from .catalog_types import SkillDefinition
from .expert_question_rendering import expert_questions_markdown


def specialist_procedure_reference_markdown(definition: SkillDefinition) -> str:
    questions = expert_questions_markdown(definition)
    return f"# {definition.name} Specialist Procedure\n\n{questions}\n\n{procedure_markdown(definition)}\n"


def procedure_markdown(definition: SkillDefinition) -> str:
    if not definition.procedure_steps:
        return ""
    lines = ["## Procedure", "", "Declared checks:", *[f"- `{check_id}`" for check_id in definition.procedure_checks], ""]
    for step in definition.procedure_steps:
        lines.extend(
            [
                f"### `{step.step_id}` ({step.kind})",
                "",
                step.instruction,
                "",
                f"- Input refs: {', '.join(f'`{ref}`' for ref in step.input_refs)}",
                f"- Output refs: {', '.join(f'`{ref}`' for ref in step.output_refs)}",
                f"- Check IDs: {', '.join(f'`{check_id}`' for check_id in step.check_ids)}",
                "",
            ]
        )
    return "\n".join(lines).rstrip()


def procedure_reference_lines(definition: SkillDefinition) -> list[str]:
    if not definition.procedure_steps:
        return []
    lines = ["- Procedure checks:", *[f"  - `{check_id}`" for check_id in definition.procedure_checks], "- Procedure steps:"]
    for step in definition.procedure_steps:
        lines.extend(
            [
                f"  - `{step.step_id}` (`{step.kind}`)",
                f"    - Input refs: {', '.join(f'`{ref}`' for ref in step.input_refs)}",
                f"    - Output refs: {', '.join(f'`{ref}`' for ref in step.output_refs)}",
                f"    - Check IDs: {', '.join(f'`{check_id}`' for check_id in step.check_ids)}",
                f"    - Instruction: {step.instruction}",
            ]
        )
    return lines


def procedure_step_payloads(definition: SkillDefinition) -> list[dict[str, object]]:
    return [
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


def copy_procedure_check_payloads(payloads: object) -> list[str]:
    if not isinstance(payloads, list) or not all(isinstance(item, str) for item in payloads):
        raise TypeError("procedure check payloads must be a list of strings")
    return list(payloads)


def copy_procedure_step_payloads(payloads: object) -> list[dict[str, object]]:
    if not isinstance(payloads, list):
        raise TypeError("procedure step payloads must be a list")
    copied: list[dict[str, object]] = []
    for item in payloads:
        if not isinstance(item, dict):
            raise TypeError("procedure step payload must be an object")
        copied.append(
            {
                "step_id": item["step_id"],
                "kind": item["kind"],
                "input_refs": list(item["input_refs"]),
                "output_refs": list(item["output_refs"]),
                "check_ids": list(item["check_ids"]),
                "instruction": item["instruction"],
            }
        )
    return copied
