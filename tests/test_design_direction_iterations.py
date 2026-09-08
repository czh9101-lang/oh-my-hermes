from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from omh.system.paths import OmhPaths
from omh.workflows.design_direction_iterations import (
    MAX_MODEL_ATTEMPTS,
    MAX_REVISION_ROUNDS,
    build_design_direction_iteration,
    revise_design_direction_iteration,
    select_design_direction_iteration,
    terminate_design_direction_iteration,
    validate_design_direction_iteration,
    write_design_direction_iteration,
)
from omh.workflows.design_directions import build_design_direction_set

_REFERENCE = ("design_system", "design_ref_a1b2c3d4e5f60718", "project_local")
_A = ("a", "task_first", "restrained_neutral", "system_sans", "single_column", "progress_trace", ("placeholder_copy",))
_B = ("b", "evidence_first", "contextual_accent", "editorial_serif", "split_panel", "evidence_rail", ("generic_glass",))
_C = ("c", "content_first", "high_contrast", "utilitarian_mono", "editorial_grid", "decision_map", ("decorative_gradient",))
_D = ("d", "task_first", "high_contrast", "system_sans", "split_panel", "none", ("card_wall",))
_B_REVISED = ("b", "evidence_first", "restrained_neutral", "editorial_serif", "split_panel", "evidence_rail", ("generic_glass",))


def _set(*options):
    return build_design_direction_set(
        surface="workflow_screen",
        audience="operator",
        primary_task="decide",
        platform="web",
        mode="new",
        context_references=(_REFERENCE,),
        options=options or (_A, _B, _C, _D),
    )


def _root(*, scores=()):
    return build_design_direction_iteration(
        _set(),
        source_revision_digest="a" * 64,
        criteria_revision="direction-fit-v1",
        criteria_dimensions=("clarity", "coherence"),
        score_threshold=90,
        scores=scores,
    )


def _revise(record, *, criteria_revision="direction-fit-v1", criteria_dimensions=("clarity", "coherence"), scores=(), attempts=()):
    parent = record["snapshots"][-1]
    return revise_design_direction_iteration(
        record,
        parent_revision_digest=parent["revision_digest"],
        feedback_reference="feedback-001",
        feedback_delta=("hierarchy", "palette"),
        direction_set=_set(_A, _B_REVISED, _C, _D),
        successors=(
            ("preserved", ("a",), "a"),
            ("revised", ("b",), "b"),
            ("combined", ("c", "d"), "c"),
            ("introduced", (), "d"),
        ),
        criteria_revision=criteria_revision,
        criteria_dimensions=criteria_dimensions,
        scores=scores,
        model_attempts=attempts,
    )


class DesignDirectionIterationContractTests(unittest.TestCase):
    def test_Given_direction_set_When_prepared_Then_root_snapshot_has_immutable_lineage(self) -> None:
        iteration = _root()
        snapshot = iteration["snapshots"][0]

        self.assertEqual(validate_design_direction_iteration(iteration), [])
        self.assertEqual(snapshot["revision_index"], 0)
        self.assertEqual(snapshot["root_set_digest"], iteration["root_set_digest"])
        self.assertEqual(snapshot["source_revision_digest"], "a" * 64)
        self.assertEqual(snapshot["parent_revision_digest"], "")
        self.assertEqual(snapshot["criteria"]["revision"], "direction-fit-v1")
        self.assertEqual(snapshot["usage"]["model_attempts"], 0)
        self.assertIsNone(snapshot["usage"]["observed_tokens"])

    def test_Given_different_root_score_evidence_When_prepared_Then_they_cannot_overwrite_each_other(self) -> None:
        first = _root(scores=(("clarity", 90, "score-001", "evaluator-x", "rubric-v1"),))
        second = _root(scores=(("clarity", 89, "score-002", "evaluator-x", "rubric-v1"),))

        self.assertNotEqual(first["iteration_id"], second["iteration_id"])

    def test_Given_revision_When_revised_Then_all_successor_kinds_keep_stable_ancestry(self) -> None:
        first = _revise(_root())
        second_parent = first["snapshots"][-1]
        second = revise_design_direction_iteration(
            first,
            parent_revision_digest=second_parent["revision_digest"],
            feedback_reference="feedback-002",
            feedback_delta=("layout",),
            direction_set=_set(_A, _B_REVISED, _C),
            successors=(
                ("preserved", ("a",), "a"),
                ("revised", ("b",), "b"),
                ("combined", ("c", "d"), "c"),
            ),
            criteria_revision="direction-fit-v1",
            criteria_dimensions=("clarity", "coherence"),
        )

        third_parent = second["snapshots"][-1]
        third = revise_design_direction_iteration(
            second,
            parent_revision_digest=third_parent["revision_digest"],
            feedback_reference="feedback-003",
            feedback_delta=("signature_element",),
            direction_set=_set(_A, _B_REVISED),
            successors=(
                ("preserved", ("a",), "a"),
                ("revised", ("b",), "b"),
                ("dropped", ("c",), ""),
            ),
            criteria_revision="direction-fit-v1",
            criteria_dimensions=("clarity", "coherence"),
        )

        self.assertEqual(validate_design_direction_iteration(third), [])
        transition_kinds = {
            successor["kind"]
            for snapshot in third["snapshots"][1:]
            for successor in snapshot["successors"]
        }
        self.assertEqual(transition_kinds, {"preserved", "revised", "combined", "introduced", "dropped"})
        self.assertNotEqual(third["snapshots"][-1]["revision_digest"], third_parent["revision_digest"])

    def test_Given_same_feedback_and_parent_When_retried_Then_revision_is_idempotent(self) -> None:
        root = _root()
        first = _revise(root)
        replay = _revise(root)

        self.assertEqual(replay, first)
        self.assertEqual(len(replay["snapshots"]), 2)

    def test_Given_stale_parent_When_revised_Then_it_is_refused_without_a_branch(self) -> None:
        revised = _revise(_root())

        with self.assertRaisesRegex(ValueError, "stale parent"):
            revise_design_direction_iteration(
                revised,
                parent_revision_digest=revised["snapshots"][0]["revision_digest"],
                feedback_reference="feedback-other",
                feedback_delta=("layout",),
                direction_set=_set(_A, _B_REVISED, _C, _D),
                successors=(
                    ("preserved", ("a",), "a"),
                    ("revised", ("b",), "b"),
                    ("combined", ("c", "d"), "c"),
                    ("introduced", (), "d"),
                ),
                criteria_revision="direction-fit-v1",
                criteria_dimensions=("clarity", "coherence"),
            )

    def test_Given_stale_option_reference_When_selected_Then_it_is_refused(self) -> None:
        revised = _revise(_root())

        with self.assertRaisesRegex(ValueError, "stale option"):
            select_design_direction_iteration(revised, option_ref=revised["snapshots"][0]["option_refs"][0])

    def test_Given_criteria_change_When_revised_Then_scores_start_a_new_baseline(self) -> None:
        revised = _revise(
            _root(),
            criteria_revision="direction-fit-v2",
            criteria_dimensions=("clarity", "accessibility"),
            scores=(("clarity", 91, "score-001", "evaluator-x", "rubric-v2"),),
        )
        snapshot = revised["snapshots"][-1]

        self.assertFalse(snapshot["score_comparison"]["comparable_with_parent"])
        self.assertEqual(snapshot["score_comparison"]["baseline_revision_digest"], "")
        self.assertEqual(snapshot["criteria"]["revision"], "direction-fit-v2")

    def test_Given_different_evaluator_or_rubric_When_criteria_is_unchanged_Then_scores_are_not_compared(self) -> None:
        root = _root(scores=(("clarity", 90, "score-001", "evaluator-x", "rubric-v1"),))
        revised = _revise(root, scores=(("clarity", 89, "score-002", "evaluator-y", "rubric-v2"),))
        comparison = revised["snapshots"][-1]["score_comparison"]

        self.assertFalse(comparison["comparable_with_parent"])
        self.assertEqual(comparison["baseline_revision_digest"], "")
        self.assertEqual(revised["terminal"]["outcome"], "OPEN")

    def test_Given_non_improving_comparable_scores_When_revised_Then_it_stops_without_visual_pass(self) -> None:
        root = _root(scores=(("clarity", 90, "score-001", "evaluator-x", "rubric-v1"),))
        revised = _revise(root, scores=(("clarity", 89, "score-002", "evaluator-x", "rubric-v1"),))

        self.assertEqual(revised["terminal"]["outcome"], "BLOCK/REVISE")
        self.assertEqual(revised["terminal"]["reason"], "no_improvement")
        self.assertNotIn("visual_qa_pass", revised["terminal"])

    def test_Given_threshold_score_When_revised_Then_it_stops_without_claiming_selection(self) -> None:
        revised = _revise(_root(), scores=(("clarity", 90, "score-001", "evaluator-x", "rubric-v1"),))

        self.assertEqual(revised["terminal"]["outcome"], "STOPPED")
        self.assertEqual(revised["terminal"]["reason"], "threshold_reached")
        self.assertIsNone(revised["terminal"]["accepted_option_ref"])

    def test_Given_model_call_cap_When_another_revision_is_requested_Then_it_blocks_at_the_cap(self) -> None:
        attempts = tuple(("generation", "unknown", None, None, None, None) for _ in range(MAX_MODEL_ATTEMPTS))
        capped = _revise(_root(), attempts=attempts)
        result = _revise(capped)

        self.assertEqual(result["terminal"]["outcome"], "BLOCK/REVISE")
        self.assertEqual(result["terminal"]["reason"], "model_call_cap_exhausted")
        self.assertEqual(len(result["snapshots"]), 2)

    def test_Given_revision_cap_When_a_fifth_round_is_requested_Then_it_blocks_without_a_sixth_snapshot(self) -> None:
        record = _root()
        for index in range(MAX_REVISION_ROUNDS):
            parent = record["snapshots"][-1]
            use_revised = index % 2 == 0
            record = revise_design_direction_iteration(
                record,
                parent_revision_digest=parent["revision_digest"],
                feedback_reference=f"feedback-cap-{index}",
                feedback_delta=("palette",),
                direction_set=_set(_A, _B_REVISED if use_revised else _B, _C, _D),
                successors=(
                    ("preserved", ("a",), "a"),
                    ("revised", ("b",), "b"),
                    ("preserved", ("c",), "c"),
                    ("preserved", ("d",), "d"),
                ),
                criteria_revision="direction-fit-v1",
                criteria_dimensions=("clarity", "coherence"),
            )
        parent = record["snapshots"][-1]
        capped = revise_design_direction_iteration(
            record,
            parent_revision_digest=parent["revision_digest"],
            feedback_reference="feedback-cap-final",
            feedback_delta=("palette",),
            direction_set=_set(_A, _B, _C, _D),
            successors=(
                ("preserved", ("a",), "a"),
                ("revised", ("b",), "b"),
                ("preserved", ("c",), "c"),
                ("preserved", ("d",), "d"),
            ),
            criteria_revision="direction-fit-v1",
            criteria_dimensions=("clarity", "coherence"),
        )

        self.assertEqual(len(capped["snapshots"]), MAX_REVISION_ROUNDS + 1)
        self.assertEqual(capped["terminal"]["reason"], "revision_cap_exhausted")

    def test_Given_two_repairs_When_revision_is_built_Then_the_shared_attempt_budget_refuses_it(self) -> None:
        with self.assertRaisesRegex(ValueError, "schema repair"):
            _revise(
                _root(),
                attempts=(
                    ("schema_repair", "unknown", None, None, None, None),
                    ("schema_repair", "unknown", None, None, None, None),
                ),
            )

    def test_Given_each_terminal_reason_When_recorded_Then_it_has_an_explicit_outcome(self) -> None:
        expected = {
            "blocked_evidence": "BLOCK/REVISE",
            "blocked_capability": "BLOCK/REVISE",
            "cancelled": "CANCELLED",
        }
        for reason, outcome in expected.items():
            with self.subTest(reason=reason):
                terminated = terminate_design_direction_iteration(_root(), reason=reason)
                self.assertEqual(terminated["terminal"]["outcome"], outcome)
                self.assertEqual(terminated["terminal"]["reason"], reason)

    def test_Given_explicit_remember_on_selection_When_accepted_Then_it_requests_existing_reviewed_memory(self) -> None:
        selected = select_design_direction_iteration(_root(), option_ref=_root()["snapshots"][0]["option_refs"][0], remember_this=True)

        request = selected["memory_promotion"]["request"]
        self.assertEqual(selected["terminal"]["reason"], "accepted")
        self.assertEqual(request["action"], "memory-new")
        self.assertTrue(request["review_required"])
        self.assertEqual(request["accepted_revision_digest"], selected["terminal"]["accepted_revision_digest"])
        self.assertFalse(request["automatic_write"])

    def test_Given_same_root_When_persisted_twice_Then_the_second_write_is_a_noop(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            paths = OmhPaths(omh_home=Path(tmp) / "omh", hermes_home=Path(tmp) / "hermes")
            first = write_design_direction_iteration(paths, _root())
            second = write_design_direction_iteration(paths, _root())

        self.assertEqual(first, second)


if __name__ == "__main__":
    unittest.main()
