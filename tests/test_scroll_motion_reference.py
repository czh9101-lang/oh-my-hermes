"""The scroll-motion-libraries reference and the routing that reaches it.

"Make the scroll feel premium" used to fall back to clarification, and when
it did reach `frontend` the skill had no material behind the word "motion" —
so a smooth-scroll handoff shipped a library with no reduced-motion branch,
no nested-scroll exemptions, and no teardown. These tests pin both halves of
the fix: the triggers that route a scroll request to `frontend`, and the
clauses in the reference that make a library choice reviewable. A rewrite
that drops one of them should fail here rather than ship.
"""

from __future__ import annotations

from pathlib import Path
import unittest

from omh.skill_pack import builtin_skill_templates
from omh.skills.catalog import builtin_definitions
from omh.skills.catalog_types import omh_skill_display_name
from omh.skills.packaging import builtin_skill_reference_templates
from omh.skills.render import scroll_motion_reference_templates

REPO_ROOT = Path(__file__).resolve().parents[1]
REFERENCE = ("frontend", "references/scroll-motion-libraries.md")

# Pinned upstream review point. The reference cites a commit so a later
# reader can diff the API it describes against the source it was read from.
LENIS_COMMIT = "eea71595f5ae595f49b21ed87520822d3624098a"


def _unwrapped(content: str) -> str:
    """Soft line wraps collapsed, for phrase assertions that span lines."""
    return " ".join(content.split())


def _body(skill: str) -> str:
    for template in builtin_skill_templates():
        if template.name == skill:
            return template.content
    raise AssertionError(f"missing skill {skill}")


def _reference() -> str:
    for template in scroll_motion_reference_templates():
        if (template.skill_name, template.relative_path) == REFERENCE:
            return template.content
    raise AssertionError(f"{REFERENCE} is not produced")


class ScrollMotionReferenceRegistryTests(unittest.TestCase):
    def test_the_packaged_set_includes_it(self) -> None:
        packaged = {(t.skill_name, t.relative_path) for t in builtin_skill_reference_templates()}
        self.assertIn(REFERENCE, packaged)

    def test_the_generated_file_matches_the_template(self) -> None:
        path = REPO_ROOT / "skills" / omh_skill_display_name(REFERENCE[0]) / REFERENCE[1]
        self.assertTrue(path.exists(), path)
        self.assertEqual(path.read_text(encoding="utf-8"), _reference())


class NativeFirstTests(unittest.TestCase):
    def test_the_native_ladder_comes_before_the_library(self) -> None:
        # A library added for an adjective is the failure this section
        # exists to block, so the native rows must be named individually.
        content = _reference()
        for native in (
            "scroll-behavior: smooth",
            "scroll-padding-top",
            "animation-timeline: scroll()",
            "IntersectionObserver",
            "scroll-snap-type",
        ):
            with self.subTest(native=native):
                self.assertIn(native, content)

    def test_the_library_needs_a_requirement_not_an_adjective(self) -> None:
        content = _unwrapped(_reference())
        self.assertIn("interpolated scroll position that more than one consumer reads", content)
        self.assertIn('"Make it feel premium" is not that requirement', content)


class LenisSourceRecordTests(unittest.TestCase):
    def test_the_source_record_pins_commit_version_and_license(self) -> None:
        content = _reference()
        self.assertIn("https://github.com/darkroomengineering/lenis", content)
        self.assertIn(LENIS_COMMIT, content)
        self.assertIn("v1.3.26", content)
        self.assertIn("MIT", content)

    def test_a_reviewed_record_is_not_permission_to_add_a_dependency(self) -> None:
        content = _unwrapped(_reference())
        self.assertIn("OMH does not install, vendor, pin, or fetch any of this at runtime", content)
        self.assertIn("a reviewed source record is not permission to add a package", content)

    def test_the_gsap_license_note_is_deferred_not_restated(self) -> None:
        # Restating it is how the "no charge" license drifts into being
        # called an OSI license; the apple-design record owns that wording.
        content = _unwrapped(_reference())
        self.assertIn("omh-apple-design/references/web-production-libraries.md", content)
        self.assertIn("is not an OSI license", content)


class IntegrationContractTests(unittest.TestCase):
    def test_every_documented_footgun_has_a_numbered_obligation(self) -> None:
        content = _unwrapped(_reference())
        for clause in (
            "One instance, one loop",
            "The recommended stylesheet ships with it",
            "Anchors are opt-in",
            "Nested scrollables are declared",
            "Teardown is owned",
            "GSAP sync is a fixed recipe",
            "CSS scroll-snap does not survive",
            "A no-build CDN tag is an origin decision",
        ):
            with self.subTest(clause=clause):
                self.assertIn(clause, content)

    def test_the_api_names_are_the_ones_upstream_documents(self) -> None:
        content = _reference()
        for api in (
            "autoRaf: true",
            "lenis.raf(time)",
            "lenis/dist/lenis.css",
            "anchors: true",
            "data-lenis-prevent",
            "allowNestedScroll: true",
            "destroy()",
            "lenis/snap",
            "gsap.ticker.lagSmoothing(0)",
        ):
            with self.subTest(api=api):
                self.assertIn(api, content)


class ReducedMotionTests(unittest.TestCase):
    def test_the_option_is_described_by_what_it_actually_does(self) -> None:
        content = _unwrapped(_reference())
        self.assertIn("`respectReducedMotion` defaults to `true`", content)
        self.assertIn("`lerp` is forced to `1`", content)
        self.assertIn("programmatic scrolls, including anchor links, jump instantly", content)

    def test_the_option_is_not_mistaken_for_covering_the_projects_animations(self) -> None:
        # This is the whole reason the section exists: the flag makes the
        # library honest and leaves every hand-written reveal untouched.
        content = _unwrapped(_reference())
        self.assertIn("What it does not do is touch the animations *you* wrote", content)
        self.assertIn("lenis.prefersReducedMotion", content)
        self.assertIn("never a default the implementation picks", content)


class LimitationsAndCostTests(unittest.TestCase):
    def test_upstream_limitations_are_quoted_not_discovered(self) -> None:
        content = _unwrapped(_reference())
        self.assertIn("Limitations to quote, not discover", content)
        for limitation in (
            "60fps on Safari",
            "30fps in low power mode",
            "iframes",
            "pre-M1 macOS Safari",
            "iOS below 16",
        ):
            with self.subTest(limitation=limitation):
                self.assertIn(limitation, content)

    def test_the_metric_is_named_and_the_budget_comes_first(self) -> None:
        content = _unwrapped(_reference())
        self.assertIn("INP is the metric it moves", content)
        self.assertIn("references/web-vitals-budgets.md", content)
        self.assertIn("before the change, not after", content)

    def test_the_behavioral_bill_names_the_states_someone_will_hit(self) -> None:
        content = _reference()
        for item in (
            "Keyboard scrolling",
            "scroll restoration",
            "Find-in-page",
            "Screen-reader",
            "Touch",
            "Print",
        ):
            with self.subTest(item=item):
                self.assertIn(item, content)

    def test_verification_is_rendered_states_and_the_boundary_holds(self) -> None:
        content = _unwrapped(_reference())
        self.assertIn("captured, not reasoned about", content)
        self.assertIn("an anchor deep link on cold load", content)
        self.assertIn("prepared_not_observed", content)
        self.assertIn(
            "not an installed dependency, a rendered frame, a motion proof, an accessibility PASS",
            content,
        )


class FrontendSkillHookTests(unittest.TestCase):
    def _definition(self):
        for definition in builtin_definitions():
            if definition.name == "frontend":
                return definition
        self.fail("frontend definition is missing")

    def test_the_body_carries_the_pointer_and_the_native_first_order(self) -> None:
        body = _body("frontend")
        self.assertIn("references/scroll-motion-libraries.md", body)
        self.assertIn("Lenis is the reviewed", body)
        self.assertIn("scroll-driven animations", body)

    def test_the_safety_rule_blocks_a_handoff_with_no_reduced_motion_branch(self) -> None:
        rules = _unwrapped(" ".join(self._definition().safety_rules))
        self.assertIn("without its reduced-motion branch", rules)
        self.assertIn("never the animations the project wrote", rules)

    def test_the_scroll_triggers_are_phrases_not_the_bare_ambiguous_word(self) -> None:
        # "parallax" alone is optics and astronomy vocabulary; the
        # negative controls in ROUTING_PRECISION_CASES fail if it is
        # promoted back to a bare token trigger.
        triggers = set(self._definition().triggers)
        for phrase in (
            "smooth scroll",
            "smooth scrolling",
            "scroll animation",
            "scroll animations",
            "parallax scroll",
            "parallax hero",
            "parallax effect",
        ):
            with self.subTest(trigger=phrase):
                self.assertIn(phrase, triggers)
        self.assertNotIn("parallax", triggers)

    def test_the_everyday_scroll_tokens_are_held_back_to_whole_phrases(self) -> None:
        # Without this hold-back "the mouse wheel scroll is broken in my
        # terminal emulator" dispatched to frontend on `scroll` plus the
        # pre-existing `broken`/`terminal` triggers; the negative control
        # `terminal-scroll-bug-stays-out-of-frontend` is the other half.
        from omh.routing.recommend import _WHOLE_PHRASE_ONLY_TRIGGER_TOKENS

        held = _WHOLE_PHRASE_ONLY_TRIGGER_TOKENS["frontend"]
        for token in ("effect", "hero", "scroll", "scrolling", "smooth"):
            with self.subTest(token=token):
                self.assertIn(token, held)
        # `parallax` stays creditable: distinctive UI vocabulary, and the two
        # optics negatives already hold it under the dispatch threshold.
        self.assertNotIn("parallax", held)

    def test_the_localized_packs_carry_the_scroll_intent(self) -> None:
        import json

        packs = {
            "ko": ("부드러운 스크롤", "스크롤 애니메이션"),
            "ja": ("スムーススクロール", "スクロールアニメーション"),
            "zh": ("平滑滚动", "滚动动画"),
        }
        for language, phrases in packs.items():
            path = REPO_ROOT / "src" / "routing" / "trigger_packs" / f"{language}.json"
            pack = json.loads(path.read_text(encoding="utf-8"))
            frontend = pack["skills"]["frontend"]
            for phrase in phrases:
                with self.subTest(language=language, trigger=phrase):
                    self.assertIn(phrase, frontend)


if __name__ == "__main__":
    unittest.main()
