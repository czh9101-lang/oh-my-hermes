"""The web-vitals-budgets reference carries the clauses that keep a number honest.

Each locked string prevents a specific failure: a threshold softened to fit a
result, a lab run reported as a field percentile, a budget written after the
measurement, a comparison across device profiles, or an optimization list
with nothing attributed to it. A rewrite that drops one should fail here
rather than ship.
"""

from __future__ import annotations

import unittest

from omh.skills.catalog import builtin_definitions
from omh.skills.packaging import builtin_skill_reference_templates
from omh.skills.render import frontend_performance_reference_templates

REFERENCE = ("frontend", "references/web-vitals-budgets.md")


class WebVitalsBudgetsReferenceTests(unittest.TestCase):
    def _content(self) -> str:
        for template in frontend_performance_reference_templates():
            if (template.skill_name, template.relative_path) == REFERENCE:
                return template.content
        self.fail(f"{REFERENCE} is not produced")

    def test_the_packaged_set_includes_it(self) -> None:
        packaged = {(t.skill_name, t.relative_path) for t in builtin_skill_reference_templates()}
        self.assertIn(REFERENCE, packaged)

    def test_the_three_metrics_keep_their_published_bars(self) -> None:
        content = self._content()
        for metric, good, poor in (
            ("LCP", "under 2.5s", "over 4.0s"),
            ("INP", "under 200ms", "over 500ms"),
            ("CLS", "under 0.1", "over 0.25"),
        ):
            with self.subTest(metric=metric):
                # The attribution table also opens rows with the metric
                # name; the threshold row is the one that spells it out.
                row = [line for line in content.splitlines() if line.startswith(f"| {metric} - ")]
                self.assertEqual(len(row), 1, f"{metric} needs exactly one threshold row")
                self.assertIn(good, row[0])
                self.assertIn(poor, row[0])
        # The bars are the platform's, not ours: softening them to pass is the
        # failure this line exists to block.
        self.assertIn("quote them, do not\nsoften them to fit a result", content)

    def test_field_and_lab_stay_apart(self) -> None:
        content = self._content()
        self.assertIn("A p75 claim needs field data", content)
        self.assertIn("never that the percentile moved", content)
        self.assertIn("75th percentile of real sessions", content)

    def test_the_budget_is_chosen_before_the_change(self) -> None:
        content = self._content()
        self.assertIn("A budget chosen after seeing the result is not a budget", content)
        for element in ("The metric and its bar", "The device and network class", "The baseline"):
            with self.subTest(element=element):
                self.assertIn(element, content)

    def test_attribution_precedes_optimization(self) -> None:
        content = self._content()
        self.assertIn("Never optimize a metric - optimize the thing the metric measured", content)
        for question in ("Which element is the LCP element", "Which interaction produced the worst paint", "Which node shifted"):
            with self.subTest(question=question):
                self.assertIn(question, content)
        self.assertIn("is folklore", content)
        self.assertIn("improves a different element than the one attributed did not fix the metric", content)

    def test_the_reference_stays_vendor_neutral(self) -> None:
        # The upstream this was reconstructed from is Next.js-specific; a
        # framework API or analytics product named here would make the
        # contract someone else's stack.
        content = self._content().lower()
        for vendor in ("next/image", "next/font", "vercel", "lighthouse", "react"):
            with self.subTest(vendor=vendor):
                self.assertNotIn(vendor, content)

    def test_the_evidence_boundary_survives(self) -> None:
        content = self._content()
        self.assertIn("prepared_not_observed", content)
        self.assertIn("a lab pass is never a claim\nabout real users", content)

    def test_the_skill_body_points_at_the_reference(self) -> None:
        definitions = {definition.name: definition for definition in builtin_definitions()}
        frontend = definitions["frontend"]
        quality_bar = "\n".join(frontend.quality_bar)
        self.assertIn("references/web-vitals-budgets.md", quality_bar)
        self.assertIn("State performance as a budget, not an adjective", quality_bar)
        self.assertIn("Attribute before optimizing", quality_bar)
        safety = "\n".join(frontend.safety_rules)
        self.assertIn("without the device class, route, and load shape", safety)


if __name__ == "__main__":
    unittest.main()
