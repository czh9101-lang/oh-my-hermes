from __future__ import annotations

from dataclasses import asdict
import re
from typing import Any

from ..harness_quality import HARNESS_QUALITY_KEYS, HARNESS_QUALITY_SCHEMA_VERSION
from .catalog import (
    HarnessDefinition,
    REASONING_DEMAND_VALUES,
    SkillDefinition,
    builtin_definitions,
    builtin_harnesses,
    harness_quality_contract,
    primary_harness_for_skill,
)
from .expert_question_validation import (
    validate_expert_questions as _validate_expert_questions,
)


CATALOG_VALIDATION_SCHEMA_VERSION = "catalog_validation/v1"
SKILL_STRUCTURE_LINT_SCHEMA_VERSION = "omh_skill_structure_lint/v1"

# Stable rule IDs. A rule ID is a contract with whoever reads a failure report,
# so renaming one is a breaking change even when the check behind it is the
# same. Each ID owns exactly one structural question.
STRUCTURE_LINT_RULE_IDS = (
    "SKILL_CATALOG_CONTRACT",
    "SKILL_CONTEXT_BUDGET",
    "SKILL_EXECUTABLE_CONSUMER",
    "SKILL_FRONTMATTER_FIELDS",
    "SKILL_GENERATED_PARITY",
    "SKILL_HARNESS_RESOLVES",
    "SKILL_TRIGGER_FORMAT",
)

# A host picker reads frontmatter `name` + `description` as unquoted YAML
# scalars, so a trigger only reaches selection when it survives that encoding.
# This mirrors the renderer's own safe-trigger test rather than re-deriving it.
_PICKER_SAFE_TRIGGER = re.compile(r"^[0-9A-Za-z\uac00-\ud7a3][0-9A-Za-z\uac00-\ud7a3 _.-]*$")

# The always-loaded body ceiling. Bodies are paid for on every turn the skill is
# in context, so this is a hard structural limit, not a quality score.
STRUCTURE_LINT_SKILL_BODY_BYTE_CEILING = 24_000


def validate_catalog_contract() -> dict[str, object]:
    definitions = builtin_definitions()
    harnesses = builtin_harnesses()
    errors: list[str] = []
    warnings: list[str] = []

    skill_names = [definition.name for definition in definitions]
    harness_names = [harness.name for harness in harnesses]
    _require_unique(skill_names, "skill", errors)
    _require_unique(harness_names, "harness", errors)
    harness_name_set = set(harness_names)

    for definition in definitions:
        errors.extend(_validate_skill_definition(definition, harness_name_set))
    for harness in harnesses:
        quality = harness_quality_contract(harness.name)
        errors.extend(_validate_harness_definition(harness))
        errors.extend(_validate_harness_quality_payload(quality, f"harness {harness.name} harness_quality"))
        errors.extend(_validate_harness_quality_matches_definition(quality, harness))
    errors.extend(_validate_named_harness_gates({harness.name: harness for harness in harnesses}))

    return {
        "schema_version": CATALOG_VALIDATION_SCHEMA_VERSION,
        "ok": not errors,
        "counts": {"skills": len(definitions), "harnesses": len(harnesses)},
        "errors": errors,
        "warnings": warnings,
    }


def validate_skill_definition_contract(definition: SkillDefinition) -> list[str]:
    """Validate one definition against the same field contract the catalog gate uses.

    `validate_catalog_contract()` answers "is the shipped catalog renderable".
    A caller holding a definition that is deliberately NOT in the catalog - a
    reviewable skill draft, for instance - needs the same per-definition answer
    without registering anything, so the loop body is exposed rather than copied.
    """
    return _validate_skill_definition(definition, {harness.name for harness in builtin_harnesses()})


def harness_summary_payload() -> dict[str, object]:
    definitions = builtin_definitions()
    skills_by_harness = _skills_by_harness(definitions)
    skill_profiles_by_harness = _skill_profiles_by_harness(definitions)
    return {
        "schema_version": "harness_list/v1",
        "validation": validate_catalog_contract(),
        "harnesses": [
            {
                "name": harness.name,
                "purpose": harness.purpose,
                "quality_tier": harness.quality_tier,
                "evidence_ladder": list(harness.evidence_ladder),
                "wrapper_actions": list(harness.wrapper_actions),
                "primary_skills": skills_by_harness.get(harness.name, []),
                "primary_skill_profiles": skill_profiles_by_harness.get(harness.name, []),
            }
            for harness in builtin_harnesses()
        ],
    }


def harness_inspection_payload(name: str) -> dict[str, object]:
    definitions = builtin_definitions()
    skills_by_harness = _skills_by_harness(definitions)
    skill_profiles_by_harness = _skill_profiles_by_harness(definitions)
    for harness in builtin_harnesses():
        if harness.name != name:
            continue
        quality = harness_quality_contract(name)
        validation_errors = _validate_harness_definition(harness) + _validate_harness_quality_payload(
            quality,
            f"harness {name} harness_quality",
        )
        validation_errors.extend(_validate_harness_quality_matches_definition(quality, harness))
        return {
            "schema_version": "harness_inspect/v1",
            "harness": _dataclass_payload(harness),
            "harness_quality": quality,
            "primary_skills": skills_by_harness.get(name, []),
            "primary_skill_profiles": skill_profiles_by_harness.get(name, []),
            "validation": {
                "ok": not validation_errors,
                "errors": validation_errors,
            },
        }
    raise KeyError(name)


def _validate_skill_definition(definition: SkillDefinition, harness_names: set[str]) -> list[str]:
    errors: list[str] = []
    label = f"skill {definition.name}"
    _require_text(definition.name, f"{label} name", errors)
    _require_text(definition.description, f"{label} description", errors)
    _require_text(definition.use_when, f"{label} use_when", errors)
    _require_text(definition.why_this_exists, f"{label} why_this_exists", errors)
    if definition.reasoning_demand not in REASONING_DEMAND_VALUES:
        errors.append(
            f"{label} reasoning_demand must be one of {list(REASONING_DEMAND_VALUES)}"
        )
    for field in ("triggers", "required_inputs", "expected_outputs", "artifact_expectations", "safety_rules", "quality_bar"):
        _require_text_sequence(getattr(definition, field), f"{label} {field}", errors)
    _validate_expert_questions(definition, label, errors)
    _require_text_sequence(definition.do_not_use_when, f"{label} do_not_use_when", errors)
    _validate_skill_example(definition.good_example, f"{label} good_example", errors)
    _validate_skill_example(definition.bad_example, f"{label} bad_example", errors)
    primary_harness = primary_harness_for_skill(definition.name)
    if primary_harness not in harness_names:
        errors.append(f"{label} primary_harness is unknown: {primary_harness}")
    return errors


def _validate_skill_example(example: object, label: str, errors: list[str]) -> None:
    if example is None:
        errors.append(f"{label} is required")
        return
    for field in ("prompt", "expected", "why"):
        _require_text(getattr(example, field, None), f"{label} {field}", errors)


def _validate_harness_definition(harness: HarnessDefinition) -> list[str]:
    errors: list[str] = []
    label = f"harness {harness.name}"
    for field in ("name", "purpose", "use_when", "fallback", "delegation_expectation", "privacy_default", "quality_tier"):
        _require_text(getattr(harness, field), f"{label} {field}", errors)
    for field in (
        "required_inputs",
        "expected_outputs",
        "stop_conditions",
        "verification",
        "artifact_events",
        "quality_bar",
        "evidence_ladder",
        "wrapper_actions",
        "overclaim_guards",
    ):
        _require_text_sequence(getattr(harness, field), f"{label} {field}", errors)
    if harness.privacy_default != "metadata_only":
        errors.append(f"{label} privacy_default must be metadata_only")
    if len(set(harness.evidence_ladder)) != len(harness.evidence_ladder):
        errors.append(f"{label} evidence_ladder must not contain duplicate steps")
    return errors


def _validate_harness_quality_payload(value: dict[str, object], label: str) -> list[str]:
    errors: list[str] = []
    extra = sorted(set(value) - set(HARNESS_QUALITY_KEYS))
    missing = [key for key in HARNESS_QUALITY_KEYS if key not in value]
    if extra:
        errors.append(f"{label} has unsupported keys: {extra}")
    if missing:
        errors.append(f"{label} is missing keys: {missing}")
    if value.get("schema_version") != HARNESS_QUALITY_SCHEMA_VERSION:
        errors.append(f"{label} schema_version must be {HARNESS_QUALITY_SCHEMA_VERSION}")
    for key in ("harness", "quality_tier"):
        _require_text(value.get(key), f"{label} {key}", errors)
    for key in ("quality_bar", "evidence_ladder", "wrapper_actions", "overclaim_guards"):
        _require_text_sequence(value.get(key), f"{label} {key}", errors)
    return errors


def _validate_harness_quality_matches_definition(value: dict[str, object], harness: HarnessDefinition) -> list[str]:
    errors: list[str] = []
    expected = {
        "harness": harness.name,
        "quality_tier": harness.quality_tier,
        "quality_bar": list(harness.quality_bar),
        "evidence_ladder": list(harness.evidence_ladder),
        "wrapper_actions": list(harness.wrapper_actions),
        "overclaim_guards": list(harness.overclaim_guards),
    }
    for key, expected_value in expected.items():
        if value.get(key) != expected_value:
            errors.append(f"harness {harness.name} harness_quality {key} must match catalog definition")
    return errors


def _validate_named_harness_gates(harnesses: dict[str, HarnessDefinition]) -> list[str]:
    errors: list[str] = []
    required_steps = {
        "deep-interview": ("ambiguity_identified", "blocking_question_asked", "answer_recorded", "clarified_brief_ready"),
        "planning": (
            "request_clarified",
            "plan_drafted",
            "option_tradeoffs_recorded",
            "test_strategy_recorded",
            "acceptance_recorded",
            "handoff_ready",
        ),
        "research": (
            "research_question_scoped",
            "primary_sources_checked",
            "conflicts_checked",
            "evidence_synthesized",
            "uncertainty_recorded",
        ),
    }
    for harness_name, steps in required_steps.items():
        harness = harnesses.get(harness_name)
        if not harness:
            errors.append(f"harness {harness_name} is required for Hermes-native quality gates")
            continue
        missing = [step for step in steps if step not in harness.evidence_ladder]
        if missing:
            errors.append(f"harness {harness_name} evidence_ladder is missing gate steps: {missing}")
    return errors


def _skills_by_harness(definitions: list[SkillDefinition]) -> dict[str, list[str]]:
    grouped: dict[str, list[str]] = {}
    for definition in definitions:
        grouped.setdefault(primary_harness_for_skill(definition.name), []).append(definition.name)
    return {key: sorted(value) for key, value in grouped.items()}


def _skill_profiles_by_harness(definitions: list[SkillDefinition]) -> dict[str, list[dict[str, str]]]:
    grouped: dict[str, list[dict[str, str]]] = {}
    for definition in definitions:
        grouped.setdefault(primary_harness_for_skill(definition.name), []).append(
            {
                "name": definition.name,
                "reasoning_demand": definition.reasoning_demand,
            }
        )
    return {
        harness: sorted(profiles, key=lambda profile: profile["name"])
        for harness, profiles in grouped.items()
    }


def _dataclass_payload(value: SkillDefinition | HarnessDefinition) -> dict[str, object]:
    data = asdict(value)
    return {key: list(item) if isinstance(item, tuple) else item for key, item in data.items()}


def _require_unique(values: list[str], label: str, errors: list[str]) -> None:
    seen: set[str] = set()
    for value in values:
        if value in seen:
            errors.append(f"duplicate {label} name: {value}")
        seen.add(value)


def _require_text(value: Any, label: str, errors: list[str]) -> None:
    if not isinstance(value, str) or not value.strip():
        errors.append(f"{label} must be a non-empty string")


def _require_text_sequence(value: Any, label: str, errors: list[str]) -> None:
    if not isinstance(value, (tuple, list)) or not value:
        errors.append(f"{label} must be a non-empty list")
        return
    for index, item in enumerate(value):
        if not isinstance(item, str) or not item.strip():
            errors.append(f"{label}[{index}] must be a non-empty string")


def skill_structure_lint_payload(
    *, definitions: list[SkillDefinition] | None = None
) -> dict[str, object]:
    """Answer one question for every tracked skill: is it structurally valid?

    The checks that answer that question already existed, scattered across the
    catalog gate, the renderer, the harness map, the wrapper card contract, and
    the context-cost accounting. A maintainer holding a defective skill could
    not tell which of those surfaces should have caught it. This aggregates
    them under stable rule IDs and returns a pass/fail verdict.

    Deliberately absent: scores, grades, rankings, third-party evaluation,
    natural-language review, and any file or network read. Every rule reads
    in-process catalog data, so the lint is offline by construction rather than
    by policy. Structural success is not proof that a host loaded the skill.
    """
    resolved = list(builtin_definitions() if definitions is None else definitions)
    harness_names = {harness.name for harness in builtin_harnesses()}
    violations: list[dict[str, str]] = []
    for definition in sorted(resolved, key=lambda item: item.name):
        violations.extend(_structure_lint_violations(definition, harness_names))
    return {
        "schema_version": SKILL_STRUCTURE_LINT_SCHEMA_VERSION,
        "ok": not violations,
        "checks": "structure_only",
        "proves_host_loading": False,
        "skill_count": len(resolved),
        "rules": list(STRUCTURE_LINT_RULE_IDS),
        "violations": violations,
    }


def _structure_lint_violations(
    definition: SkillDefinition, harness_names: set[str]
) -> list[dict[str, str]]:
    """Run every rule against one skill.

    Rules are evaluated independently and in a fixed order so that one defect
    reports one rule. Ordering the output by rule ID keeps two runs on the same
    input byte-identical.
    """
    found: list[dict[str, str]] = []
    for rule, detail in (
        ("SKILL_CATALOG_CONTRACT", _lint_catalog_contract(definition)),
        ("SKILL_CONTEXT_BUDGET", _lint_context_budget(definition)),
        ("SKILL_EXECUTABLE_CONSUMER", _lint_executable_consumer(definition)),
        ("SKILL_FRONTMATTER_FIELDS", _lint_frontmatter_fields(definition)),
        ("SKILL_GENERATED_PARITY", _lint_generated_parity(definition)),
        ("SKILL_HARNESS_RESOLVES", _lint_harness_resolves(definition, harness_names)),
        ("SKILL_TRIGGER_FORMAT", _lint_trigger_format(definition)),
    ):
        if detail:
            found.append({"rule": rule, "skill": definition.name, "detail": detail})
    return found


def _lint_catalog_contract(definition: SkillDefinition) -> str:
    """Every machine-consumed catalog field is present and well-typed.

    The per-definition contract is reused rather than copied so this rule
    cannot drift from the gate that guards the shipped catalog.
    """
    errors = validate_skill_definition_contract(definition)
    # A harness that does not resolve is SKILL_HARNESS_RESOLVES' verdict, not
    # this one; leaving it here would make one defect report two rules.
    errors = [error for error in errors if "primary_harness is unknown" not in error]
    return "; ".join(sorted(errors))


def _lint_frontmatter_fields(definition: SkillDefinition) -> str:
    """The two fields a host picker actually reads must render.

    `name` and `description` are the entire selection surface. A skill whose
    description cannot be built is invisible at selection time no matter how
    complete its body is.
    """
    from .render import frontmatter_description

    if not definition.name.strip():
        return "frontmatter name must be a non-empty string"
    try:
        description = frontmatter_description(definition)
    except ValueError as exc:
        return f"frontmatter description cannot be rendered: {exc}"
    if not description.strip():
        return "frontmatter description must be a non-empty string"
    if "\n" in description:
        return "frontmatter description must be a single YAML scalar line"
    return ""


def _lint_harness_resolves(definition: SkillDefinition, harness_names: set[str]) -> str:
    harness = primary_harness_for_skill(definition.name)
    if harness not in harness_names:
        return f"primary harness does not resolve: {harness}"
    return ""


def _lint_generated_parity(definition: SkillDefinition) -> str:
    """Catalog metadata must survive into the generated frontmatter.

    The renderer copies `category`, `phase`, `role`, and `quality_tier` into
    frontmatter by looking the skill up in the catalog *by name*. When a
    definition is not the one registered under its own name, that lookup misses
    and every field silently degrades to a generic fallback -- the skill still
    renders and still installs, but it ships metadata it was never built from.

    Parity is therefore checked against the rendered bytes rather than against
    the catalog entry: comparing the catalog to itself would be tautological and
    would pass no matter how badly the projection had drifted.
    """
    rendered = _rendered_frontmatter(definition)
    if rendered is None:
        # An unrenderable skill is SKILL_FRONTMATTER_FIELDS' verdict.
        return ""
    mismatched = sorted(
        key
        for key, expected in (
            ("category", definition.category),
            ("phase", definition.phase),
            ("role", definition.hermes_role),
            ("quality_tier", definition.quality_tier),
        )
        if rendered.get(key) != expected
    )
    if mismatched:
        return f"generated frontmatter does not match the catalog definition: {mismatched}"
    return ""


def _rendered_frontmatter(definition: SkillDefinition) -> dict[str, str] | None:
    """Parse the `metadata.hermes` scalars out of a freshly rendered skill body.

    Only the flat `key: value` lines are read. That is deliberately the same
    shallow view a host picker takes, and it keeps the lint free of a YAML
    dependency.
    """
    from .render import workflow_skill_from_definition

    try:
        content = workflow_skill_from_definition(definition, definition.name).content
    except (ValueError, KeyError):
        return None
    match = re.match(r"^---\n(.*?)\n---\n", content, re.DOTALL)
    if match is None:
        return None
    return {
        key: value.strip()
        for key, value in re.findall(r"^\s+([a-z_]+): (.+)$", match.group(1), re.MULTILINE)
    }


def _lint_trigger_format(definition: SkillDefinition) -> str:
    """Triggers are machine-matched strings, so their shape is structural.

    Reachability is checked only for skills that render a picker surface, and
    routers are exempt: `oh-my-hermes` and `meta-router` are addressed by a
    sigil command rather than by trigger phrases, which the renderer already
    encodes by refusing to emit a trigger tail for them.
    """
    malformed = [
        trigger
        for trigger in definition.triggers
        if not isinstance(trigger, str) or not trigger.strip() or trigger != trigger.strip() or "\n" in trigger
    ]
    if malformed:
        return f"triggers must be single-line, stripped, non-empty strings: {sorted(malformed)}"
    if definition.category == "router":
        return ""
    reachable = any(
        _PICKER_SAFE_TRIGGER.fullmatch(value)
        for value in (*definition.triggers, *definition.aliases)
    )
    if not reachable:
        return "no trigger or alias survives frontmatter encoding, so the picker cannot select this skill"
    return ""


def _lint_executable_consumer(definition: SkillDefinition) -> str:
    """A declared skill identity must be the one executable surfaces consume.

    The wrapper chat-card contract and the routing recommendation policy both key
    off the catalog name. A skill legitimately reaches users through only one of
    them, so their presence is not the check; their agreement is. When both name
    a skill, they must dispatch the same `next_action`, otherwise the routed
    action and the rendered card send the user down two different paths.

    Many skills reach users through neither surface -- the router, the harness
    map, and the installed skill pack are dispatch paths too -- so requiring a
    consumer to exist would condemn valid skills. What is never valid is two
    consumers naming the same skill and disagreeing about what it does.

    Renaming a catalog entry out from under a paired consumer is the defect this
    catches: the skill still renders, but the surfaces that dispatch it no longer
    describe the same action.
    """
    from ..routing.recommend import _SKILL_POLICIES
    from ..wrapper.contract import _WORKFLOW_OPERATIONS_CHAT_CARDS

    card = _WORKFLOW_OPERATIONS_CHAT_CARDS.get(definition.name)
    policy = _SKILL_POLICIES.get(definition.name)
    if card is None or policy is None:
        return ""
    card_action = str(card.get("next_action", ""))
    if card_action and card_action != policy.next_action:
        return (
            "wrapper card and routing policy disagree on next_action: "
            f"{card_action} != {policy.next_action}"
        )
    return ""


def _lint_context_budget(definition: SkillDefinition) -> str:
    """A hard always-loaded byte ceiling, checked as a limit and not a score.

    The rendered body is what an install pays for on every turn the skill is in
    context. This reports only whether the ceiling is exceeded.
    """
    from .render import workflow_skill_from_definition

    try:
        template = workflow_skill_from_definition(definition, definition.name)
    except (ValueError, KeyError):
        # An unrenderable body is another rule's verdict.
        return ""
    size = len(template.content)
    if size > STRUCTURE_LINT_SKILL_BODY_BYTE_CEILING:
        return (
            f"always-loaded skill body is {size} bytes, over the "
            f"{STRUCTURE_LINT_SKILL_BODY_BYTE_CEILING} byte ceiling"
        )
    return ""
