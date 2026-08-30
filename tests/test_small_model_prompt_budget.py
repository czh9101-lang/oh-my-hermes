from __future__ import annotations

import unittest
from pathlib import Path

from _local_package import load_local_package

load_local_package()

from omh.coding.unit_prompt_protocol import (  # noqa: E402
    HIGH_EFFORT_CALIBRATIONS,
    MAIN_AGENT_COMPOSITION_CALIBRATIONS,
    shared_unit_preamble_lines,
)
from omh.quality.small_model_prompt_budget import (  # noqa: E402
    BLOCK_MAX_CONSTRAINTS,
    CONTRAST_EXAMPLE_MARKERS,
    SHARED_PREAMBLE_MAX_BYTES,
    SHARED_PREAMBLE_MAX_CONSTRAINTS,
    TINY_MODEL_CONSTRAINT_TARGET,
    constraint_count,
    contrast_example_markers,
    dispatched_prompt_blocks,
    format_small_model_prompt_violations,
    shared_preamble_blocks,
    small_model_prompt_payload,
    small_model_prompt_violations,
)

_MODEL_OPTI = Path(__file__).resolve().parents[1] / "MODEL_OPTI.md"


class SharedPromptBudgetTests(unittest.TestCase):
    """The executor-invariant head is written for the weakest lane in the fleet."""

    def test_the_shared_head_stays_inside_its_measured_budget(self) -> None:
        violations = small_model_prompt_violations()
        self.assertEqual(violations, [], format_small_model_prompt_violations(violations))

    def test_the_measured_ceilings_are_the_current_values(self) -> None:
        # A ratchet is only a ratchet while it sits on the measurement. If this
        # fails low, re-measure and lower the ceiling; if it fails high, say
        # which existing rule the new one displaces before raising it.
        blocks = shared_preamble_blocks()
        self.assertEqual(sum(block.chars for block in blocks), SHARED_PREAMBLE_MAX_BYTES)
        self.assertEqual(sum(block.constraints for block in blocks), SHARED_PREAMBLE_MAX_CONSTRAINTS)
        self.assertEqual(
            max(constraint_count(text) for text in dispatched_prompt_blocks().values()),
            BLOCK_MAX_CONSTRAINTS,
        )

    def test_the_goal_line_is_not_charged_to_omh_budget(self) -> None:
        # The caller's goal text length is the operator's business.
        short = shared_unit_preamble_lines("x")
        long = shared_unit_preamble_lines("x" * 4000)
        self.assertNotEqual(len(short[0]), len(long[0]))
        self.assertEqual(
            [block.label for block in shared_preamble_blocks()],
            [f"shared[{index}]" for index in range(1, len(short))],
        )

    def test_every_family_calibration_is_inside_the_per_block_ceiling(self) -> None:
        for family, text in sorted(HIGH_EFFORT_CALIBRATIONS.items()):
            with self.subTest(family=family, table="executor"):
                self.assertLessEqual(constraint_count(text), BLOCK_MAX_CONSTRAINTS)
        for family, text in sorted(MAIN_AGENT_COMPOSITION_CALIBRATIONS.items()):
            with self.subTest(family=family, table="composer"):
                self.assertLessEqual(constraint_count(text), BLOCK_MAX_CONSTRAINTS)

    def test_no_dispatched_block_carries_a_labelled_negative_sample(self) -> None:
        # A weaker model copies a labelled "Bad:" sample rather than avoiding it.
        for label, text in sorted(dispatched_prompt_blocks().items()):
            with self.subTest(block=label):
                self.assertEqual(contrast_example_markers(text), ())


class BudgetMeasurementTests(unittest.TestCase):
    def test_constraint_count_counts_directive_sentences(self) -> None:
        # A directive is marked by an RFC 2119 modal or a directive negation.
        # A bare imperative ("Run the tests.") is deliberately not counted:
        # every prose verb would qualify, and the proxy would stop meaning
        # anything.
        self.assertEqual(constraint_count("You must run it. Never skip it."), 2)
        self.assertEqual(constraint_count("Run it once, then stop. The suite is slow."), 1)
        self.assertEqual(constraint_count("The suite is slow and the fixtures are large."), 0)

    def test_contrast_markers_are_detected_case_insensitively(self) -> None:
        self.assertEqual(contrast_example_markers("BAD: rm -rf /"), ("bad:",))
        self.assertEqual(contrast_example_markers("Do not do this at home"), ("do not do this",))
        self.assertEqual(contrast_example_markers("Nothing labelled here."), ())
        for marker in CONTRAST_EXAMPLE_MARKERS:
            with self.subTest(marker=marker):
                self.assertEqual(contrast_example_markers(f"prefix {marker} sample"), (marker,))

    def test_violations_name_the_block_an_author_edits(self) -> None:
        message = format_small_model_prompt_violations(
            [
                {
                    "rule": "BLOCK_CONSTRAINTS",
                    "block": "HIGH_EFFORT_CALIBRATIONS[qwen]",
                    "measured": 9,
                    "ceiling": BLOCK_MAX_CONSTRAINTS,
                    "detail": "too many rules",
                }
            ]
        )
        self.assertIn("HIGH_EFFORT_CALIBRATIONS[qwen]", message)
        self.assertIn("src/quality/small_model_prompt_budget.py", message)
        self.assertIn("MODEL_OPTI.md", message)

    def test_payload_states_that_constraints_are_a_proxy(self) -> None:
        payload = small_model_prompt_payload()
        self.assertIn("proxy", str(payload["description"]))
        self.assertEqual(
            payload["ceilings"]["tiny_model_constraint_target"], TINY_MODEL_CONSTRAINT_TARGET
        )
        self.assertGreater(payload["dispatched_block_count"], len(HIGH_EFFORT_CALIBRATIONS))


class SmallestModelDoctrineDocTests(unittest.TestCase):
    """The two rules no regex can apply live in the doc, or nowhere."""

    def test_model_opti_documents_the_budget_and_its_unmechanized_rules(self) -> None:
        doc = _MODEL_OPTI.read_text(encoding="utf-8")
        self.assertIn("Writing for the smallest model in the fleet", doc)
        self.assertIn("SHARED_PREAMBLE_MAX_CONSTRAINTS", doc)
        self.assertIn("src/quality/small_model_prompt_budget.py", doc)
        # The two judgement rules, named so an author finds them.
        self.assertIn("positive framing", doc.casefold())
        self.assertIn("already enforces", doc.casefold())

    def test_documented_ceilings_match_the_shipped_constants(self) -> None:
        doc = _MODEL_OPTI.read_text(encoding="utf-8")
        for name, value in (
            ("SHARED_PREAMBLE_MAX_BYTES", SHARED_PREAMBLE_MAX_BYTES),
            ("SHARED_PREAMBLE_MAX_CONSTRAINTS", SHARED_PREAMBLE_MAX_CONSTRAINTS),
            ("BLOCK_MAX_CONSTRAINTS", BLOCK_MAX_CONSTRAINTS),
        ):
            with self.subTest(constant=name):
                self.assertRegex(doc, rf"`{name}`[^\n]*\b{value}\b")
        self.assertRegex(doc, rf"\b{TINY_MODEL_CONSTRAINT_TARGET}\b")


if __name__ == "__main__":
    unittest.main()
