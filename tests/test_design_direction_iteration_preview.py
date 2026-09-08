from __future__ import annotations

import unittest
from html.parser import HTMLParser

from _local_package import load_local_package

load_local_package()

from omh.workflows.design_direction_iterations import (
    MAX_MODEL_ATTEMPTS,
    MAX_SNAPSHOTS,
    build_design_direction_iteration,
    render_design_direction_iteration_html,
    revise_design_direction_iteration,
    select_design_direction_iteration,
    terminate_design_direction_iteration,
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


def _revise(record, *, scores=(), attempts=()):
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
        criteria_revision="direction-fit-v1",
        criteria_dimensions=("clarity", "coherence"),
        scores=scores,
        model_attempts=attempts,
    )


class _VisibleDom(HTMLParser):
    """Visible text nodes and revision items only; attributes and styles are not DOM text."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.chunks: list[str] = []
        self.revision_items = 0
        self.revisions: list[list[str]] = []
        self._hidden = 0
        self._rev_depth = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in ("style", "script", "title"):
            self._hidden += 1
        if tag == "li":
            if self._rev_depth:
                self._rev_depth += 1
            elif "rev" in (dict(attrs).get("class") or "").split():
                self.revision_items += 1
                self.revisions.append([])
                self._rev_depth = 1

    def handle_endtag(self, tag: str) -> None:
        if tag in ("style", "script", "title") and self._hidden:
            self._hidden -= 1
        elif tag == "li" and self._rev_depth:
            self._rev_depth -= 1

    def handle_data(self, data: str) -> None:
        if self._hidden:
            return
        self.chunks.append(data)
        if self._rev_depth:
            self.revisions[-1].append(data)

    @property
    def text(self) -> str:
        return " ".join(self.chunks)

    def revision_text(self, index: int) -> str:
        return " ".join(self.revisions[index])


def _visible_dom(html: str) -> _VisibleDom:
    parser = _VisibleDom()
    parser.feed(html)
    return parser


class DesignDirectionIterationPreviewTests(unittest.TestCase):
    def test_Given_revised_iteration_When_rendered_Then_visible_dom_shows_every_revision_and_its_identity(self) -> None:
        revised = _revise(_root(scores=(("clarity", 88, "score-000", "evaluator-x", "rubric-v1"),)))

        dom = _visible_dom(render_design_direction_iteration_html(revised))

        self.assertEqual(dom.revision_items, len(revised["snapshots"]))
        self.assertIn(revised["iteration_id"], dom.text)
        self.assertIn(revised["root_set_digest"], dom.text)
        self.assertIn(revised["source_revision_digest"], dom.text)
        for snapshot in revised["snapshots"]:
            with self.subTest(revision=snapshot["revision_index"]):
                self.assertIn(snapshot["revision_digest"], dom.text)
                for option_ref in snapshot["option_refs"]:
                    self.assertIn(option_ref, dom.text)
        parent_digest = revised["snapshots"][0]["revision_digest"]
        self.assertEqual(revised["snapshots"][1]["parent_revision_digest"], parent_digest)
        self.assertIn(parent_digest, dom.text)

    def test_Given_revised_iteration_When_rendered_Then_feedback_ancestry_criteria_scores_and_telemetry_are_visible(self) -> None:
        revised = _revise(
            _root(),
            scores=(("clarity", 91, "score-001", "evaluator-x", "rubric-v1"),),
            attempts=(("generation", "model-a", 1200, 0.02, 3400, None),),
        )

        dom = _visible_dom(render_design_direction_iteration_html(revised))

        for expected in (
            "feedback-001", "hierarchy", "palette",
            "preserved", "revised", "combined", "introduced",
            "direction-fit-v1", "clarity", "coherence",
            "score-001", "evaluator-x", "rubric-v1",
            "generation", "model-a", "1200",
        ):
            self.assertIn(expected, dom.text)

    def test_Given_open_iteration_When_rendered_Then_budget_usage_limits_and_outcome_are_visible(self) -> None:
        revised = _revise(_root(), attempts=(("generation", "model-a", 1200, 0.02, 3400, None),))

        dom = _visible_dom(render_design_direction_iteration_html(revised))

        self.assertIn(f"2 of {MAX_SNAPSHOTS} snapshots", dom.text)
        self.assertIn(f"1 of {MAX_MODEL_ATTEMPTS} model attempts", dom.text)
        self.assertIn("OPEN", dom.text)
        self.assertIn("omh ops design-direction-iterations select", dom.text)

    def test_Given_each_terminal_state_When_rendered_Then_outcome_and_reason_are_visible_text(self) -> None:
        cases = (
            (terminate_design_direction_iteration(_root(), reason="blocked_evidence"), "BLOCK/REVISE", "blocked_evidence"),
            (terminate_design_direction_iteration(_root(), reason="cancelled"), "CANCELLED", "cancelled"),
        )
        for iteration, outcome, reason in cases:
            with self.subTest(reason=reason):
                dom = _visible_dom(render_design_direction_iteration_html(iteration))
                self.assertEqual(dom.revision_items, 1)
                self.assertIn(outcome, dom.text)
                self.assertIn(reason, dom.text)
                self.assertNotIn("omh ops design-directions", dom.text)

    def test_Given_accepted_iteration_When_rendered_Then_accepted_option_ref_is_visible_text(self) -> None:
        root = _root()
        option_ref = root["snapshots"][0]["option_refs"][1]
        selected = select_design_direction_iteration(root, option_ref=option_ref)
        original_choice = selected["snapshots"][0]["direction_set"]["chosen_option"]

        document = render_design_direction_iteration_html(selected)
        dom = _visible_dom(document)

        self.assertIn("ACCEPT", dom.text)
        self.assertIn("accepted", dom.text)
        self.assertIn(option_ref, dom.text)
        self.assertIn('class="opt chosen"', document)
        self.assertNotIn("omh ops design-directions", dom.text)
        self.assertEqual(selected["snapshots"][0]["direction_set"]["chosen_option"], original_choice)

    def test_Given_revised_iteration_When_rendered_Then_each_revision_shows_its_own_option_vocabulary(self) -> None:
        revised = _revise(_root())

        dom = _visible_dom(render_design_direction_iteration_html(revised))

        root_text = dom.revision_text(0)
        current_text = dom.revision_text(1)
        # Option B moved from contextual_accent to restrained_neutral between revisions.
        self.assertIn("contextual_accent", root_text)
        self.assertNotIn("contextual_accent", current_text)
        self.assertIn("restrained_neutral", current_text)
        for revision_text in (root_text, current_text):
            self.assertIn("hierarchy", revision_text)
            self.assertIn("palette", revision_text)
            self.assertIn("typography", revision_text)
            self.assertIn("layout", revision_text)
            self.assertIn("evidence_rail", revision_text)
            self.assertIn("placeholder_copy", revision_text)

    def test_Given_long_valid_identifiers_When_rendered_Then_they_are_visible_and_wrappable(self) -> None:
        long_model_id = "model-" + "x" * 150
        long_evaluator_id = "evaluator-" + "y" * 140
        revised = _revise(
            _root(),
            scores=(("clarity", 91, "score-001", long_evaluator_id, "rubric-v1"),),
            attempts=(("generation", long_model_id, 1200, 0.02, 3400, None),),
        )

        html = render_design_direction_iteration_html(revised)
        dom = _visible_dom(html)

        self.assertIn(long_model_id, dom.text)
        self.assertIn(long_evaluator_id, dom.text)
        self.assertIn(revised["iteration_id"], dom.text)
        # The wrap rule must cover every identifier code element, not only digests.
        self.assertIn(".iter code", html)
        self.assertIn("overflow-wrap: anywhere", html)

    def test_Given_three_snapshots_When_rendered_Then_each_revision_has_a_visible_section(self) -> None:
        first = _revise(_root())
        parent = first["snapshots"][-1]
        second = revise_design_direction_iteration(
            first,
            parent_revision_digest=parent["revision_digest"],
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

        dom = _visible_dom(render_design_direction_iteration_html(second))

        self.assertEqual(dom.revision_items, 3)
        for snapshot in second["snapshots"]:
            self.assertIn(snapshot["revision_digest"], dom.text)


if __name__ == "__main__":
    unittest.main()
