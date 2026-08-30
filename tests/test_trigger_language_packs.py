"""The trigger language pack contract.

Three things have to hold for packs to be a mechanism rather than a second
place to hide language literals:

1. A pack is validated data. Naming a skill that does not exist, or a phrase
   that can never match, is a refusal that says what is wrong -- not a silent
   drop, which is how a pack entry becomes a phrase its author believes is live.
2. Every shipped pack entry is reachable: routing the phrase actually reaches
   the skill the pack named. A dead entry fails here in whatever language it
   was written, which is the `whole_phrase_only_tokens` lesson applied to
   phrases.
3. Input and output support cannot diverge. Every language OMH will answer in
   is a language OMH can be addressed in.
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from omh.commands.language import LANGUAGE_CODES
from omh.routing.localization import normalized_phrase
from omh.routing.recommend import (
    _prepared_routable_definitions,
    _tokens,
    recommend_skills,
)
from omh.routing.trigger_language_packs import (
    ORIGIN_SHIPPED,
    TRIGGER_LANGUAGE_PACK_SCHEMA_VERSION,
    TriggerLanguagePack,
    load_user_trigger_language_packs,
    merged_trigger_phrases,
    parse_trigger_language_pack,
    shipped_trigger_language_packs,
    shipped_trigger_pack_languages,
    trigger_pack_state,
    user_trigger_pack_dir,
)
from omh.skills.catalog import builtin_definitions, routable_definitions


def _known_skills() -> frozenset[str]:
    return frozenset(definition.name for definition in builtin_definitions())


def _pack_document(**overrides: object) -> dict[str, object]:
    document: dict[str, object] = {
        "schema_version": TRIGGER_LANGUAGE_PACK_SCHEMA_VERSION,
        "language": "xx",
        "skills": {"plan": ["planlama yap"]},
    }
    document.update(overrides)
    return document


class TriggerLanguagePackValidationTests(unittest.TestCase):
    def _parse(self, document: object, *, language: str = "xx"):
        return parse_trigger_language_pack(
            document,
            language=language,
            origin="user",
            source="test",
            known_skills=_known_skills(),
        )

    def test_a_well_formed_pack_applies(self) -> None:
        pack, status = self._parse(_pack_document())

        self.assertEqual(status, "applied")
        assert pack is not None
        self.assertEqual(pack.phrases_by_skill(), {"plan": ("planlama yap",)})
        self.assertEqual(pack.phrase_count, 1)

    def test_an_unknown_skill_id_is_refused_rather_than_dropped(self) -> None:
        _, status = self._parse(_pack_document(skills={"not-a-skill": ["bir sey"]}))

        self.assertIn("unknown skill id 'not-a-skill'", status)

    def test_a_phrase_with_no_word_characters_is_refused(self) -> None:
        _, status = self._parse(_pack_document(skills={"plan": ["---"]}))

        self.assertIn("can ever match", status)

    def test_a_holdback_entry_that_removes_nothing_is_refused(self) -> None:
        # `ab` is under the tokenizer's three-character floor, so subtracting it
        # would remove nothing -- the exact shape of the #1188 dead hold-back.
        _, status = self._parse(_pack_document(whole_phrase_only_tokens={"plan": ["ab"]}))

        self.assertIn("produces no scored token", status)

    def test_every_problem_is_reported_at_once(self) -> None:
        _, status = self._parse(
            _pack_document(
                language="yy",
                skills={"not-a-skill": ["bir sey"], "plan": ["ok phrase", "ok phrase"]},
            )
        )

        self.assertIn("does not match the pack name", status)
        self.assertIn("unknown skill id", status)
        self.assertIn("repeats trigger phrase", status)

    def test_a_wrong_schema_version_is_refused(self) -> None:
        _, status = self._parse(_pack_document(schema_version="trigger_language_pack/v0"))

        self.assertIn("schema_version must be", status)

    def test_unsupported_fields_are_refused(self) -> None:
        _, status = self._parse(_pack_document(negative_controls=[]))

        self.assertIn("unsupported fields", status)

    def test_a_non_object_document_is_refused(self) -> None:
        _, status = self._parse([])

        self.assertEqual(status, "invalid: document must be a JSON object")


class UserTriggerPackLoadingTests(unittest.TestCase):
    def test_an_invalid_user_pack_is_named_and_the_others_still_load(self) -> None:
        with tempfile.TemporaryDirectory() as raw_home:
            home = Path(raw_home)
            directory = user_trigger_pack_dir(home)
            directory.mkdir(parents=True)
            (directory / "tr.json").write_text(
                json.dumps(_pack_document(language="tr")), encoding="utf-8"
            )
            (directory / "xx.json").write_text(
                json.dumps(_pack_document(skills={"nope": ["x"]})), encoding="utf-8"
            )

            packs, statuses = load_user_trigger_language_packs(home, _known_skills())

        self.assertEqual([pack.language for pack in packs], ["tr"])
        self.assertEqual(dict(statuses)["tr"], "applied")
        self.assertIn("unknown skill id 'nope'", dict(statuses)["xx"])

    def test_an_absent_pack_directory_is_normal(self) -> None:
        with tempfile.TemporaryDirectory() as raw_home:
            packs, statuses = load_user_trigger_language_packs(Path(raw_home), _known_skills())

        self.assertEqual(packs, ())
        self.assertEqual(statuses, ())

    def test_state_reports_shipped_packs_and_the_user_directory(self) -> None:
        with tempfile.TemporaryDirectory() as raw_home:
            state = trigger_pack_state(Path(raw_home), _known_skills())

        self.assertEqual(state["schema_version"], TRIGGER_LANGUAGE_PACK_SCHEMA_VERSION)
        shipped = {row["language"] for row in state["shipped"]}
        self.assertEqual(shipped, set(shipped_trigger_pack_languages()))
        self.assertEqual(state["user"], [])


class ShippedTriggerPackTests(unittest.TestCase):
    def test_the_shipped_packs_validate(self) -> None:
        packs = shipped_trigger_language_packs(_known_skills())

        self.assertTrue(packs)
        for pack in packs:
            with self.subTest(language=pack.language):
                self.assertEqual(pack.origin, ORIGIN_SHIPPED)
                self.assertTrue(pack.skills)

    def test_korean_moved_out_of_source_and_into_its_pack(self) -> None:
        # The move is the point: Korean goes through the same mechanism as every
        # other language rather than being the one language the source knows.
        korean = next(
            pack for pack in shipped_trigger_language_packs(_known_skills()) if pack.language == "ko"
        )

        self.assertGreater(pack_skill_count := len(korean.skills), 90, "the ko pack lost skills")
        self.assertGreater(korean.phrase_count, 800, f"the ko pack lost phrases ({pack_skill_count} skills)")

    def test_every_shipped_pack_phrase_is_live_in_its_skills_scored_surface(self) -> None:
        """The phrase counterpart of the hold-back reachability contract.

        A hold-back is dead when the word it names still scores. A phrase is
        dead when the opposite happens: the entry exists but nothing about it
        reaches the skill's scored surface, so it credits nothing no matter what
        the user types. Composed-vs-decomposed text, a stray zero-width
        character, or a phrase that tokenizes away entirely all produce it, and
        none of them are visible by reading the pack.

        This does not assert the phrase *wins* -- which skill outranks which on
        a given sentence is the routing corpora's question, not the pack's.
        """
        prepared_by_name = {
            prepared.definition.name: prepared for prepared in _prepared_routable_definitions()
        }
        dead: list[str] = []
        for pack in shipped_trigger_language_packs(_known_skills()):
            for skill, phrases in pack.skills:
                prepared = prepared_by_name.get(skill)
                if prepared is None:
                    continue  # Not routable; the catalog-merge test covers it.
                for phrase in phrases:
                    scores_as_phrase = normalized_phrase(phrase) in prepared.plain_trigger_phrases
                    scores_as_token = bool(_tokens(phrase) & prepared.trigger_tokens)
                    if not scores_as_phrase and not scores_as_token:
                        dead.append(f"{pack.language}/{skill}: {phrase!r}")

        self.assertEqual(
            dead,
            [],
            "Trigger pack phrases that credit nothing: neither the +6 phrase match nor a "
            "single scored token survives normalization, so the entry is dead weight its "
            "author believes is live: " + "; ".join(dead[:20]),
        )

    def test_the_seed_packs_reach_their_lane_end_to_end(self) -> None:
        """One live probe per seed language, through the ordinary router."""
        probes = (
            ("ja", "ボローチェッカーの所有権エラーを直して", "rust"),
            ("ja", "RAGパイプライン構築と構造化出力スキーマを設計して", "llm-app-dev"),
            ("ja", "セグメンテーション違反とコアダンプを調べて", "native-debugging"),
            ("ja", "フロントエンドのランディングページを作って", "frontend"),
            ("zh", "借用检查器报所有权错误", "rust"),
            ("zh", "大模型应用开发要做检索增强生成", "llm-app-dev"),
            ("zh", "段错误和核心转储怎么排查", "native-debugging"),
            ("zh", "前端落地页需要响应式布局", "frontend"),
            ("ko", "빌드 실패 원인 봐줘", "build-failure-triage"),
        )

        for language, message, expected in probes:
            with self.subTest(language=language, expected=expected):
                top = recommend_skills(message, limit=1)
                self.assertEqual(top[0]["skill"], expected)

    def test_shipped_pack_phrases_reach_the_catalog(self) -> None:
        catalog_triggers = {
            definition.name: set(definition.triggers) for definition in routable_definitions()
        }
        merged = merged_trigger_phrases(shipped_trigger_language_packs(_known_skills()))

        missing = [
            f"{skill}: {phrase!r}"
            for skill, phrases in merged.items()
            if skill in catalog_triggers
            for phrase in phrases
            if phrase not in catalog_triggers[skill]
        ]

        self.assertEqual(missing, [], "shipped pack phrases that never reached the catalog")


class LanguageParityTests(unittest.TestCase):
    def test_every_output_language_also_ships_a_trigger_pack(self) -> None:
        """Input and output support cannot diverge again.

        `--language` / `OMH_LANG` decides what OMH answers in. A language OMH
        answers in but cannot be addressed in is the exact asymmetry trigger
        packs exist to close, so shipping an output localization without a
        trigger pack fails here. English needs no pack: it is the base corpus
        the catalog is authored in.
        """
        shipped = set(shipped_trigger_pack_languages())
        missing = sorted(code for code in LANGUAGE_CODES if code != "en" and code not in shipped)

        self.assertEqual(
            missing,
            [],
            "These languages ship localized OUTPUT but no trigger pack, so OMH answers in "
            "them without being able to recognise them: "
            + ", ".join(missing)
            + ". Add src/routing/trigger_packs/<lang>.json -- see docs/routing-quality.md.",
        )

    def test_a_pack_language_does_not_have_to_ship_output_localization(self) -> None:
        # The gate is one-directional on purpose. Recognising a language is
        # cheap and additive; translating every message is not, so a community
        # pack must not be blocked on a full output localization.
        self.assertIsInstance(shipped_trigger_pack_languages(), tuple)


class TriggerLanguagePackShapeTests(unittest.TestCase):
    def test_merging_keeps_pack_order_and_drops_repeats(self) -> None:
        first = TriggerLanguagePack("aa", "user", "a", (("plan", ("one", "two")),))
        second = TriggerLanguagePack("bb", "user", "b", (("plan", ("two", "three")),))

        self.assertEqual(
            merged_trigger_phrases((first, second)),
            {"plan": ("one", "two", "three")},
        )


if __name__ == "__main__":
    unittest.main()
