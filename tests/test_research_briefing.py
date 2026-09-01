"""`research_briefing/v1` -- the reader-facing half of the research engine.

The engine's other outputs answer to a machine, so nothing in the repo used to
decide whether a document was written well. These tests pin the half of the
briefing format a function can decide: a title that is a sentence, a role label
outside the closed vocabulary, a repeated title, an intensifier, a measured
figure with no date, and a source with no class are all rejected at build time
rather than noticed by a reader.

The page renderer is pinned on the two properties that make it safe to hand to
someone: it fetches nothing when opened, and it carries its own print rules so
the reader's print dialog is what produces a PDF. OMH never claims it produced
one.
"""

from __future__ import annotations

import unittest

from omh.skills.catalog import builtin_definitions
from omh.skills.packaging import builtin_skill_reference_templates
from omh.workflows.research_briefing import (
    BRIEFING_CLAIM_BOUNDARY,
    BRIEFING_ROLE_LABELS,
    RESEARCH_BRIEFING_SCHEMA_VERSION,
    build_research_briefing,
    render_research_briefing_markdown,
    render_research_briefing_page,
    research_briefing_errors,
)


def _sections() -> tuple[dict[str, object], ...]:
    return (
        {
            "role": "problem",
            "title": "Load growth under rising input",
            "paragraphs": (
                "Context length rose from 8k to 128k across one year, so the per-request "
                "key-value cache now dominates serving memory.",
            ),
            "figure": "req 8k   |####\nreq 128k |################################",
        },
        {
            "role": "cost",
            "title": "Retrieval precision weakens in proportion to the KV cache removed",
            "paragraphs": (
                "Grouped-query attention shares key and value heads, so it removes cache in "
                "proportion to the group size and weakens exact-match retrieval by the same factor.",
            ),
        },
    )


def _briefing(**overrides: object) -> dict[str, object]:
    kwargs: dict[str, object] = {
        "audience": "human_briefing",
        "question": "Which attention cache layout should the serving stack adopt?",
        "as_of": "2026-09-01",
        "output_formats": ("markdown", "page"),
        "title": "KV cache layout choice for the serving stack",
        "learning_objectives": ("Choose between MHA, GQA, and MLA for a fixed memory budget.",),
        "assumed_knowledge": ("Transformer attention at the level of query, key, and value tensors.",),
        "out_of_scope": ("Training-time memory behaviour.",),
        "sections": _sections(),
        "figures": (
            {
                "label": "KV cache per request at 128k",
                "value": "40 GB",
                "basis": "derived",
                "as_of": "2026-09-01",
            },
        ),
        "glossary": (
            {"term": "GQA", "definition": "Grouped-query attention: query heads share one key/value head per group."},
        ),
        "traps": ("Treating a cache reduction as a quality-neutral change.",),
        "sources": (
            {
                "title": "GQA paper",
                "url": "https://arxiv.org/abs/2305.13245",
                "source_class": "upstream_official",
                "retrieved_on": "2026-09-01",
            },
        ),
    }
    kwargs.update(overrides)
    return build_research_briefing(**kwargs)  # type: ignore[arg-type]


class ResearchBriefingContractTests(unittest.TestCase):
    def test_build_returns_a_validated_payload_with_prepared_exports(self) -> None:
        payload = _briefing()
        self.assertEqual(payload["schema_version"], RESEARCH_BRIEFING_SCHEMA_VERSION)
        self.assertEqual(research_briefing_errors(payload), [])
        self.assertEqual(payload["claim_boundary"], BRIEFING_CLAIM_BOUNDARY)
        # Preparing a document is never rendering one.
        self.assertEqual(payload["export"], {"markdown": "prepared", "page": "prepared"})

    def test_audience_and_format_are_declared_not_inferred(self) -> None:
        with self.assertRaises(ValueError):
            _briefing(audience="whatever")
        with self.assertRaises(ValueError):
            _briefing(language="")
        # The format question belongs to the human branch alone.
        with self.assertRaises(ValueError):
            _briefing(audience="coding_agent_handoff")
        agent = _briefing(audience="coding_agent_handoff", output_formats=())
        self.assertEqual(agent["output_formats"], [])

    def test_title_rules_a_function_can_decide_are_enforced(self) -> None:
        cases = (
            ("Load rises as input grows.", "sentence"),
            ("Is the cache the problem?", "question"),
            ("3 options", "numeric scaffolding"),
        )
        for title, _why in cases:
            with self.subTest(title=title):
                with self.assertRaises(ValueError):
                    _briefing(sections=({"role": "problem", "title": title, "paragraphs": ("Body.",)},))

    def test_role_label_comes_from_the_closed_vocabulary(self) -> None:
        with self.assertRaises(ValueError):
            _briefing(sections=({"role": "summary", "title": "A real title", "paragraphs": ("Body.",)},))
        for role in BRIEFING_ROLE_LABELS:
            with self.subTest(role=role):
                payload = _briefing(
                    sections=({"role": role, "title": f"Title for {role}", "paragraphs": ("Body.",)},)
                )
                self.assertEqual(research_briefing_errors(payload), [])

    def test_repeated_titles_and_banned_prose_are_rejected(self) -> None:
        repeated = (
            {"role": "problem", "title": "Remaining problems", "paragraphs": ("One.",)},
            {"role": "limit", "title": "Remaining problems", "paragraphs": ("Two.",)},
        )
        with self.assertRaises(ValueError):
            _briefing(sections=repeated)
        with self.assertRaises(ValueError):
            _briefing(
                sections=(
                    {"role": "solution", "title": "A real title", "paragraphs": ("This is dramatically better.",)},
                )
            )
        with self.assertRaises(ValueError):
            _briefing(
                sections=({"role": "solution", "title": "A real title", "paragraphs": ("So what happens?",)},)
            )

    def test_figures_and_sources_carry_their_basis_and_class(self) -> None:
        with self.assertRaises(ValueError):
            _briefing(figures=({"label": "x", "value": "1", "basis": "guessed"},))
        # A measured figure without a date cannot be checked later.
        with self.assertRaises(ValueError):
            _briefing(figures=({"label": "x", "value": "1", "basis": "measured"},))
        with self.assertRaises(ValueError):
            _briefing(sources=({"title": "x", "source_class": "blog", "retrieved_on": "2026-09-01"},))
        with self.assertRaises(ValueError):
            _briefing(sources=({"title": "x", "source_class": "practitioner"},))


class ResearchBriefingRenderTests(unittest.TestCase):
    def test_markdown_follows_the_required_structure(self) -> None:
        markdown = render_research_briefing_markdown(_briefing())
        self.assertTrue(markdown.startswith("# KV cache layout choice for the serving stack"))
        for heading in (
            "## What you can do after reading",
            "## Assumed knowledge",
            "## Out of scope",
            "## Problem - Load growth under rising input",
            "## Cost - Retrieval precision weakens in proportion to the KV cache removed",
            "## Appendix A: glossary",
            "## Appendix B: misconceptions and traps",
            "## Appendix C: sources",
        ):
            self.assertIn(heading, markdown)
        # The figure is drawn, not described.
        self.assertIn("```", markdown)
        self.assertIn(BRIEFING_CLAIM_BOUNDARY, markdown)
        # Appendices come after the body.
        self.assertLess(markdown.index("## Problem -"), markdown.index("## Appendix A"))

    def test_page_is_self_contained_and_print_ready(self) -> None:
        page = render_research_briefing_page(_briefing())
        self.assertIn("<!DOCTYPE html>", page)
        self.assertIn("@page", page)
        self.assertIn("@media print", page)
        # Nothing may fetch when the page is opened.
        for forbidden in ("<link", "<script", "@import", "src=", "//fonts."):
            self.assertNotIn(forbidden, page, forbidden)
        self.assertIn(BRIEFING_CLAIM_BOUNDARY, page)

    def test_page_escapes_supplied_text_and_renders_deterministically(self) -> None:
        payload = _briefing(
            sections=(
                {
                    "role": "pitfall",
                    "title": "Markup in a supplied title",
                    "paragraphs": ("A body mentioning <script>alert(1)</script> and A & B.",),
                },
            )
        )
        page = render_research_briefing_page(payload)
        self.assertNotIn("<script>alert(1)</script>", page)
        self.assertIn("&lt;script&gt;", page)
        self.assertIn("A &amp; B", page)
        self.assertEqual(page, render_research_briefing_page(payload))


class ResearchBriefingLanguageTests(unittest.TestCase):
    def test_scaffolding_labels_travel_with_the_payload(self) -> None:
        payload = _briefing(
            language="ko",
            title="서빙 스택의 KV 캐시 레이아웃 선택",
            sections=(
                {
                    "role": "problem",
                    "title": "입력 증가로 인한 부하 증가",
                    "paragraphs": ("컨텍스트 길이가 8k에서 128k로 늘면서 요청당 KV 캐시가 서빙 메모리를 지배하게 됐다.",),
                },
            ),
            figures=(),
            role_labels={"problem": "문제"},
            captions={"question": "질문", "as_of": "기준 시점", "glossary": "부록 A 개념 사전"},
        )
        markdown = render_research_briefing_markdown(payload)
        self.assertIn("## 문제 - 입력 증가로 인한 부하 증가", markdown)
        self.assertIn("질문:", markdown)
        self.assertIn("기준 시점:", markdown)
        self.assertIn("## 부록 A 개념 사전", markdown)
        # Anything left unnamed falls back to English rather than failing.
        self.assertIn("## Appendix C: sources", markdown)
        page = render_research_briefing_page(payload)
        self.assertIn('lang="ko"', page)
        self.assertIn("문제 - 입력 증가로 인한 부하 증가", page)

    def test_unknown_label_keys_are_rejected(self) -> None:
        with self.assertRaises(ValueError):
            _briefing(role_labels={"summary": "요약"})
        with self.assertRaises(ValueError):
            _briefing(captions={"epilogue": "맺음말"})
        with self.assertRaises(ValueError):
            _briefing(captions={"glossary": "   "})


class ResearchSkillBranchTests(unittest.TestCase):
    def test_the_research_skill_asks_the_audience_before_retrieval(self) -> None:
        definition = next(item for item in builtin_definitions() if item.name == "research")
        inputs = " ".join(definition.required_inputs)
        self.assertIn("output audience", inputs)
        self.assertIn("output format", inputs)
        self.assertIn("output language", inputs)
        bar = " ".join(definition.quality_bar)
        self.assertIn("Ask who the output is for before retrieval", bar)
        self.assertIn("references/briefing-format.md", bar)
        self.assertIn(RESEARCH_BRIEFING_SCHEMA_VERSION, " ".join(definition.expected_outputs))
        self.assertIn(RESEARCH_BRIEFING_SCHEMA_VERSION, " ".join(definition.safety_rules))

    def test_the_briefing_format_reference_ships_with_the_skill(self) -> None:
        templates = {
            template.relative_path: template
            for template in builtin_skill_reference_templates()
            if template.skill_name == "research"
        }
        self.assertIn("references/briefing-format.md", templates)
        content = templates["references/briefing-format.md"].content
        # The closed role vocabulary must be discoverable from the reference.
        for role in BRIEFING_ROLE_LABELS:
            self.assertIn(role.capitalize(), content, role)
        self.assertIn("never inferred", content)


if __name__ == "__main__":
    unittest.main()
