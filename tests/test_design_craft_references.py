"""Design craft references: the named bar, the contract gate, the rubric.

Live design output through OMH read as generic because the design skills
stated abstract quality bars with no craft material behind them. These tests
pin the fix: the always-loaded skill bodies carry a NAMED bar and pointers,
and the on-demand references carry the material — the DESIGN.md contract
gate, the taste directions with their anti-slop checklist, the
reference-token extraction contract, and the critique rubric. The wording is
OMH's own; the concept lineage (oh-my-openagent's frontend architecture and
its permissively licensed design upstreams) is credited inside each
reference with a pinned commit and an explicit no-text-reproduced statement,
because the nearest upstream is Sustainable-Use-licensed and OMH is MIT.
"""

from __future__ import annotations

from pathlib import Path
import unittest

from omh.skill_pack import builtin_skill_reference_templates, builtin_skill_templates
from omh.skills.catalog_types import omh_skill_display_name
from omh.skills.render import DESIGN_NAMED_BAR, design_reference_templates

REPO_ROOT = Path(__file__).resolve().parents[1]

FLAT_FAILS = "technically clean but flat"


def _unwrapped(content: str) -> str:
    """Soft line wraps collapsed, for phrase assertions that span lines."""
    return " ".join(content.split())


def _reference(skill: str, relative_path: str) -> str:
    for template in design_reference_templates():
        if template.skill_name == skill and template.relative_path == relative_path:
            return template.content
    raise AssertionError(f"missing design reference {skill}/{relative_path}")


def _body(skill: str) -> str:
    for template in builtin_skill_templates():
        if template.name == skill:
            return template.content
    raise AssertionError(f"missing skill {skill}")


class DesignReferenceRegistryTests(unittest.TestCase):
    def test_all_four_references_are_registered_and_on_disk(self) -> None:
        expected = {
            ("frontend", "references/design-system-contract.md"),
            ("frontend", "references/taste-foundations.md"),
            ("frontend", "references/reference-token-extraction.md"),
            ("design-quality-gate", "references/design-critique-rubric.md"),
        }
        registered = {
            (template.skill_name, template.relative_path)
            for template in builtin_skill_reference_templates()
        }
        self.assertLessEqual(expected, registered)
        for skill, relative_path in expected:
            path = REPO_ROOT / "skills" / omh_skill_display_name(skill) / relative_path
            self.assertTrue(path.exists(), path)
            self.assertEqual(path.read_text(encoding="utf-8"), _reference(skill, relative_path))

    def test_every_reference_carries_the_honest_concept_lineage(self) -> None:
        # The nearest upstream architecture is Sustainable-Use-licensed and
        # OMH is MIT, so the attribution must (a) pin the upstream commit,
        # (b) state that no upstream text is reproduced, (c) credit the
        # permissive design upstreams the concepts actually come from, and
        # (d) carry the non-affiliation line for the named brands.
        for template in design_reference_templates():
            content = template.content
            self.assertIn("## Attribution", content, template.relative_path)
            self.assertRegex(content, r"oh-my-openagent@[0-9a-f]{7,}", template.relative_path)
            self.assertIn("Sustainable Use", content, template.relative_path)
            self.assertIn("No upstream text is reproduced", content, template.relative_path)
            for upstream in ("taste-skill", "ui-ux-pro-max-skill", "designpowers", "open-design"):
                self.assertIn(upstream, content, f"{template.relative_path}: {upstream}")
            self.assertIn("not affiliated with", content, template.relative_path)
            # The retired, incorrect claim must not resurface.
            self.assertNotIn("Apache-2.0, code-yeongyu", content, template.relative_path)


class DesignSystemContractTests(unittest.TestCase):
    def test_the_gate_is_stated_before_the_structure(self) -> None:
        content = _reference("frontend", "references/design-system-contract.md")
        self.assertIn("no component code before `DESIGN.md` exists", content)
        for heading in (
            "Research Log",
            "Atmosphere & Identity",
            "Color",
            "Typography",
            "Spacing & Layout",
            "Components",
            "Motion & Interaction",
            "Depth & Surface",
            "Accessibility Constraints & Accepted Debt",
        ):
            self.assertIn(heading, content, heading)
        # Empty sections are decisions; existing UI without a contract stops
        # and asks; a code value outside the contract is drift.
        self.assertIn("silence is not a decision", _unwrapped(content))
        self.assertIn("Never decide silently", content)
        self.assertIn("drift", content)
        self.assertIn("observed-only", content)

    def test_section_one_carries_the_taste_direction_the_rubric_judges_by(self) -> None:
        # The rubric judges inside the claimed direction, so the contract
        # must have a place to declare it — including borrowed elements.
        content = _reference("frontend", "references/design-system-contract.md")
        self.assertIn("the chosen taste direction (primary", content)
        self.assertIn("borrowed", content)

    def test_typography_covers_cjk_metrics_not_just_fallbacks(self) -> None:
        content = _reference("frontend", "references/design-system-contract.md")
        self.assertIn("CJK", content)
        self.assertIn("word-break", content)
        self.assertIn("truncation", content)


class TasteFoundationsTests(unittest.TestCase):
    def test_the_bar_is_named_and_flatness_fails(self) -> None:
        content = _reference("frontend", "references/taste-foundations.md")
        self.assertIn(DESIGN_NAMED_BAR, content)
        self.assertIn("flatness is a defect", content)

    def test_primary_direction_with_declared_borrowing(self) -> None:
        # Exactly-one would make a hybrid brief unrepresentable; the rule is
        # one PRIMARY direction plus named borrowed elements.
        content = _reference("frontend", "references/taste-foundations.md")
        self.assertIn("Name one primary direction", content)
        self.assertIn("borrowed from another direction", content)
        for direction in ("Operational", "Minimalist", "Premium", "Bold"):
            self.assertIn(direction, content, direction)

    def test_anti_slop_checklist_names_the_failure_modes(self) -> None:
        content = _reference("frontend", "references/taste-foundations.md")
        for marker in (
            "Anti-slop checklist",
            "Template gravity",
            "One-note palette",
            "Weak hierarchy",
            "Missing states",
            "prefers-reduced-motion",
            "CJK as an afterthought",
        ):
            self.assertIn(marker, content, marker)

    def test_content_ordering_beats_visual_symmetry(self) -> None:
        content = _reference("frontend", "references/taste-foundations.md")
        self.assertIn("visual symmetry never outranks that sequence", _unwrapped(content))


class ReferenceTokenExtractionTests(unittest.TestCase):
    def test_extraction_covers_static_and_live_references(self) -> None:
        content = _reference("frontend", "references/reference-token-extraction.md")
        self.assertIn("Static reference", content)
        self.assertIn("Live URL reference", content)
        self.assertIn("OMH itself never launches a browser", _unwrapped(content))
        self.assertIn("never copy logos", content)

    def test_fidelity_qa_is_stated_in_omh_vocabulary(self) -> None:
        # "reference-fidelity mode" was a dangling capability name; the QA
        # request is stated through the artifacts visual-qa actually owns.
        content = _reference("frontend", "references/reference-token-extraction.md")
        self.assertIn("visual_qa_plan/v1", content)
        self.assertIn("visual-fidelity review perspective", content)
        self.assertNotIn("reference-fidelity mode", content)


class DesignCritiqueRubricTests(unittest.TestCase):
    def test_rubric_axes_each_carry_a_fail_condition(self) -> None:
        content = _reference("design-quality-gate", "references/design-critique-rubric.md")
        for axis in (
            "Hierarchy",
            "Type discipline",
            "Spacing rhythm",
            "Color system",
            "State coverage",
            "Signature",
            "Motion restraint",
            "CJK",
        ):
            self.assertIn(axis, content, axis)
        self.assertGreaterEqual(content.count("FAIL:"), 8)

    def test_verdicts_name_the_direction_vocabulary_and_its_source(self) -> None:
        # The rubric judges inside the claimed direction, so it must carry
        # the four direction names AND the skill-rooted path to their
        # definitions — design-quality-gate covers deck/PDF/poster surfaces
        # where the frontend skill would never be consulted first.
        content = _reference("design-quality-gate", "references/design-critique-rubric.md")
        self.assertIn("omh-frontend/references/taste-foundations.md", content)
        for direction in ("operational", "minimalist/editorial", "premium/soft", "bold/expressive"):
            self.assertIn(direction, content, direction)
        self.assertIn("judge inside it", content)
        self.assertIn("smallest change", content)
        self.assertIn("never passes work", content)

    def test_the_rubric_question_is_the_named_bar(self) -> None:
        content = _reference("design-quality-gate", "references/design-critique-rubric.md")
        self.assertIn(DESIGN_NAMED_BAR, content)
        self.assertIn("Technically clean but flat fails", content)


class SkillBodyPointerTests(unittest.TestCase):
    def test_frontend_body_carries_the_named_bar_and_all_three_pointers(self) -> None:
        body = _body("frontend")
        self.assertIn("Linear/Stripe/Supabase class", body)
        self.assertIn(FLAT_FAILS, body)
        self.assertIn("references/design-system-contract.md", body)
        self.assertIn("references/taste-foundations.md", body)
        self.assertIn("references/reference-token-extraction.md", body)
        self.assertIn("no component code before the contract exists", body)

    def test_quality_gate_body_points_at_the_rubric(self) -> None:
        body = _body("design-quality-gate")
        self.assertIn("references/design-critique-rubric.md", body)
        self.assertIn(FLAT_FAILS, body)

    def test_orchestration_body_uses_a_skill_rooted_reference_path(self) -> None:
        # design-orchestration ships no references/ directory of its own, so
        # a bare relative path would resolve to nothing from its skill root.
        body = _body("design-orchestration")
        self.assertIn("omh-frontend/references/taste-foundations.md", body)
        self.assertIn(FLAT_FAILS, body)


if __name__ == "__main__":
    unittest.main()
