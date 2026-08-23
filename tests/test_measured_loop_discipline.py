from __future__ import annotations

import unittest

from _local_package import load_local_package

load_local_package()
from omh.skills.catalog import builtin_definitions
from omh.skills.packaging import builtin_skill_reference_templates, builtin_skill_templates


class MeasuredLoopDisciplineDoctrineTests(unittest.TestCase):
    def _reference(self):
        return next(
            template
            for template in builtin_skill_reference_templates()
            if template.relative_path == "references/measured-loop-discipline.md"
        )

    def _skills(self):
        return {skill.name: skill for skill in builtin_skill_templates()}

    def _definitions(self):
        return {definition.name: definition for definition in builtin_definitions()}

    def test_reference_exists_and_stays_compact(self) -> None:
        reference = self._reference()
        self.assertEqual(reference.skill_name, "loop")
        # Progressive-disclosure budget: the doctrine must stay a compact,
        # on-demand load, well under the generic 24,500 per-reference ceiling.
        self.assertLess(len(reference.content.encode("utf-8")), 8_000)

    def test_reference_carries_no_url(self) -> None:
        reference = self._reference()
        skills = self._skills()
        self.assertNotIn("http", reference.content)
        # The always-loaded body needs its own gate: it is where a
        # well-meaning later edit is most likely to add a "see also" link.
        self.assertNotIn("http", skills["loop"].content)

    def test_reference_carries_omh_vocabulary(self) -> None:
        reference = self._reference()
        for anchor in (
            "goal_ledger/v1",
            "loop_cycle/v1",
            "permission profile",
            "prepared",
        ):
            self.assertIn(anchor, reference.content)

    def test_reference_attributes_the_source(self) -> None:
        reference = self._reference()
        self.assertIn("karpathy/autoresearch", reference.content)
        self.assertIn("No upstream text is reproduced", reference.content)

    def test_body_declares_the_evaluation_contract(self) -> None:
        body = self._skills()["loop"].content
        self.assertIn("one command produces one number and a direction", body)
        self.assertIn("never edits the scoring harness", body)

    def test_section_names_the_rules_it_frames(self) -> None:
        body = self._skills()["loop"].content
        self.assertIn(
            "The measured-loop rules in the quality bar above apply when a loop has a score.",
            body,
        )

    def test_body_states_the_constraint_metric_precedence(self) -> None:
        body = self._skills()["loop"].content
        self.assertIn(
            "the binding constraint chooses which attempt to make, and the metric chooses whether that attempt is kept",
            body,
        )

    def test_body_keeps_the_metric_out_of_the_completion_gate(self) -> None:
        body = self._skills()["loop"].content
        start = body.index("## Measured Loops")
        end = body.index("## Runtime Evidence")
        section = body[start:end]
        self.assertIn("The metric never decides completion", section)
        self.assertIn("goal_ledger/v1", section)

    def test_ledger_reads_as_loop_guidance_not_an_omh_promise(self) -> None:
        loop = self._definitions()["loop"]
        rule = next(item for item in loop.quality_bar if "human-scannable ledger" in item)
        self.assertIn("the loop itself appends", rule)
        for entry in (*loop.artifact_expectations, *loop.expected_outputs):
            self.assertNotIn("tab-separated", entry)
            self.assertNotIn(".tsv", entry)
        # The only producer contract allowed to say "ledger" is the goal ledger.
        # Equality, not absence: a NEW ledger entry must fail this.
        self.assertEqual(
            [entry for entry in loop.artifact_expectations if "ledger" in entry],
            ["linked goal_ledger/v1 only when completion evidence is required"],
        )
        self.assertEqual([entry for entry in loop.expected_outputs if "ledger" in entry], [])

    def test_recovery_notes_carry_the_idea_exhaustion_ladder(self) -> None:
        loop = self._definitions()["loop"]
        self.assertTrue(
            any(
                "recombine" in note and "before declaring the loop blocked" in note
                for note in loop.recovery_notes
            )
        )

    def test_pointers_are_targeted(self) -> None:
        skills = self._skills()
        self.assertIn("## Measured Loops", skills["loop"].content)
        self.assertIn("measured-loop-discipline.md", skills["loop"].content)
        # A named pair of other skills proves the splice is targeted, not universal.
        self.assertNotIn("## Measured Loops", skills["ralplan"].content)
        self.assertNotIn("measured-loop-discipline.md", skills["ralplan"].content)
        self.assertNotIn("## Measured Loops", skills["ultrawork"].content)
        self.assertNotIn("measured-loop-discipline.md", skills["ultrawork"].content)

    def test_measured_loop_section_is_not_a_workflow_pattern(self) -> None:
        # Guards the deliberate decision that the attempt-commit cycle is
        # cycle discipline, not a member of the workflow-pattern enum.
        body = self._skills()["loop"].content
        start = body.index("## Measured Loops")
        end = body.index("## Runtime Evidence")
        section = body[start:end]
        self.assertNotIn("workflow pattern", section)


if __name__ == "__main__":
    unittest.main()
