"""The cleanup-passes reference carries the clauses that prevent a wrong cleanup.

Each locked string is the sentence that stops a specific failure: a bundled
multi-smell diff nobody can review for behavior preservation, a silently
widened scope, a detector output treated as a verdict, or a red gate carried
into the next pass. A rewrite that drops one should fail here, not ship.
"""

from __future__ import annotations

import unittest

from omh.skills.catalog import builtin_definitions
from omh.skills.packaging import builtin_skill_reference_templates
from omh.skills.render import ai_slop_cleaner_reference_templates


class AiSlopCleanerReferenceTests(unittest.TestCase):
    def _content(self) -> str:
        templates = ai_slop_cleaner_reference_templates()
        self.assertEqual(
            [(t.skill_name, t.relative_path) for t in templates],
            [("ai-slop-cleaner", "references/cleanup-passes.md")],
        )
        return templates[0].content

    def test_the_packaged_set_includes_it(self) -> None:
        packaged = {(t.skill_name, t.relative_path) for t in builtin_skill_reference_templates()}
        self.assertIn(("ai-slop-cleaner", "references/cleanup-passes.md"), packaged)

    def test_the_six_taxonomy_categories_are_named(self) -> None:
        content = self._content()
        for category in (
            "Duplication",
            "Dead code",
            "Needless abstraction",
            "Boundary violation",
            "Missing tests",
            "Templated defaults",
        ):
            with self.subTest(category=category):
                self.assertIn(category, content)

    def test_the_pass_order_is_fixed_and_gated(self) -> None:
        content = self._content()
        self.assertLess(content.index("**Dead code**"), content.index("**Duplicates**"))
        self.assertLess(content.index("**Duplicates**"), content.index("**Naming and error handling**"))
        self.assertLess(
            content.index("**Naming and error handling**"), content.index("**Test reinforcement**")
        )
        self.assertIn("Never carry a red gate forward", content)

    def test_scope_detection_and_report_contracts_survive(self) -> None:
        content = self._content()
        self.assertIn("never edited, and never\nused to justify widening the diff", content)
        self.assertIn("candidate list, not a verdict", content)
        self.assertIn("prepared_not_observed", content)
        for part in ("changed files", "simplifications", "behavior lock", "remaining risks"):
            with self.subTest(part=part):
                self.assertIn(part, content)
        self.assertIn("## Boundary", content)

    def test_the_body_points_at_the_reference_and_orders_the_passes(self) -> None:
        definition = next(d for d in builtin_definitions() if d.name == "ai-slop-cleaner")
        joined = "\n".join(definition.quality_bar)
        self.assertIn("omh-ai-slop-cleaner/references/cleanup-passes.md", joined)
        self.assertIn("never bundling categories", joined)
        self.assertIn("templated defaults", joined)
        safety = "\n".join(definition.safety_rules)
        self.assertIn("never widen it silently", safety)


if __name__ == "__main__":
    unittest.main()
