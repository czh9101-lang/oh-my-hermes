from __future__ import annotations

from functools import lru_cache

from .catalog import installable_skill_definitions
from .render import (
    SkillReferenceTemplate,
    SkillTemplate,
    buzz_reference_templates,
    buzz_skill,
    code_review_reference_templates,
    context_reference_templates,
    context_skill,
    deep_interview_skill,
    design_reference_templates,
    jit_learn_skill,
    loop_reference_templates,
    loop_skill,
    memory_new_skill,
    memory_sync_skill,
    router_reference_templates,
    router_skill,
    structural_search_skill,
    wiki_reference_templates,
    wiki_skill,
    workflow_skill,
)


def builtin_skill_templates() -> list[SkillTemplate]:
    return list(_builtin_skill_templates_cached())


def builtin_skill_reference_templates() -> list[SkillReferenceTemplate]:
    return [
        *router_reference_templates(),
        *wiki_reference_templates(),
        *code_review_reference_templates(),
        *context_reference_templates(),
        *buzz_reference_templates(),
        *loop_reference_templates(),
        *design_reference_templates(),
    ]


def _skill_template_for(name: str) -> SkillTemplate:
    if name == "context":
        return context_skill()
    if name == "deep-interview":
        return deep_interview_skill()
    if name == "jit-learn":
        return jit_learn_skill()
    if name == "loop":
        return loop_skill()
    if name == "memory-new":
        return memory_new_skill()
    if name == "memory-sync":
        return memory_sync_skill()
    if name == "wiki":
        return wiki_skill()
    if name == "buzz":
        return buzz_skill()
    if name in ("codebase-onboarding", "codegraph-refresh"):
        return structural_search_skill(name)
    return workflow_skill(name)


@lru_cache(maxsize=1)
def _builtin_skill_templates_cached() -> tuple[SkillTemplate, ...]:
    names = [definition.name for definition in installable_skill_definitions()]
    return (router_skill(), *[_skill_template_for(name) for name in names if name != "oh-my-hermes"])
