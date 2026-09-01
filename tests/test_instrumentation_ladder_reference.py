"""The instrumentation-ladder reference carries the clauses that keep an audit honest.

Each locked string prevents a specific failure: a tier claimed over a gap
below it, a leak recommended into telemetry, an anti-pattern reported as a
style remark, or a scorecard without locations. A rewrite that drops one
should fail here rather than ship.
"""

from __future__ import annotations

import unittest

from omh.skills.catalog import builtin_definitions
from omh.skills.packaging import builtin_skill_reference_templates
from omh.skills.render import agent_ops_review_reference_templates


class InstrumentationLadderReferenceTests(unittest.TestCase):
    def _content(self) -> str:
        templates = agent_ops_review_reference_templates()
        self.assertEqual(
            [(t.skill_name, t.relative_path) for t in templates],
            [("agent-ops-review", "references/instrumentation-ladder.md")],
        )
        return templates[0].content

    def test_the_packaged_set_includes_it(self) -> None:
        packaged = {(t.skill_name, t.relative_path) for t in builtin_skill_reference_templates()}
        self.assertIn(("agent-ops-review", "references/instrumentation-ladder.md"), packaged)

    def test_the_six_tiers_and_the_claim_rule_survive(self) -> None:
        content = self._content()
        for tier in ("T0", "T1", "T2", "T3", "T4", "T5"):
            with self.subTest(tier=tier):
                self.assertIn(f"| {tier} |", content)
        self.assertIn("only when every row below it holds", content)

    def test_the_audit_priorities_and_scorecard_contract_survive(self) -> None:
        content = self._content()
        for marker in ("**P0**", "**P1**", "**P2**", "PASS, FAIL, or PARTIAL", "quick win"):
            with self.subTest(marker=marker):
                self.assertIn(marker, content)

    def test_the_leak_and_anti_pattern_rules_survive(self) -> None:
        content = self._content()
        self.assertIn("log message counts, lengths, and hashes", content)
        self.assertIn("key-set booleans, never values", content)
        self.assertIn("never a style remark", content)
        self.assertIn("hardcoded pricing table is itself a finding", content)
        self.assertIn("## Boundary", content)

    def test_the_extended_bodies_point_at_the_reference(self) -> None:
        by_name = {d.name: d for d in builtin_definitions()}
        review = "\n".join(by_name["agent-ops-review"].quality_bar)
        self.assertIn("omh-agent-ops-review/references/instrumentation-ladder.md", review)
        self.assertIn("PASS, FAIL, or PARTIAL", review)
        card = "\n".join(by_name["ops-observability-card"].quality_bar)
        self.assertIn("five answers", card)
        self.assertIn("Never recommend logging raw prompts", card)
        app = "\n".join(by_name["llm-app-dev"].quality_bar)
        self.assertIn("budgets bind the whole tree", app)
        self.assertIn("approval record outside the prompt", app)


if __name__ == "__main__":
    unittest.main()
