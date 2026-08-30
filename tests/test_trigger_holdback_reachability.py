"""Reachability gate for whole-phrase-only trigger hold-backs, in every language.

A hold-back keeps specific words out of a skill's scored trigger-token pool
because, split from their trigger phrase, they are too generic to credit on
their own (see the comments beside the dict in `src/routing/recommend.py`). The
hold-back is a set difference applied at definition-prepare time:
`_tokens(triggers) - holdback_entries`.

`routing_tokens` (imported as `_tokens` in that module) folds every token
through NFKD normalization, which decomposes precomposed Hangul syllables into
their component jamo. A holdback entry written in ordinary composed Korean
(e.g. `"검토"`, codepoint U+AC80) is therefore a different string from the
decomposed token `_tokens` actually produces for that same word, so the set
subtraction silently removes nothing -- the entry still gets scored even though
the comment says it is held back. `adversarial-consensus` shipped five such
dead rows (검토, 계획, 관점에서, 여러, 찾아).

The failure was never Korean-specific: it is what happens whenever an entry is
written in one representation and compared against another. So this test does
not check the English table and the Korean rows separately. It resolves the
hold-back the way scoring resolves it -- source table merged with every trigger
language pack's `whole_phrase_only_tokens` -- and proves that no word an entry
names still surfaces in the skill's actual scored `trigger_tokens`. A dead
hold-back in any language, from any source, fails here.
"""

from __future__ import annotations

import unittest

from omh.routing.recommend import (
    _WHOLE_PHRASE_ONLY_TRIGGER_TOKENS,
    _prepared_routable_definitions,
    _tokens,
    _trigger_pack_packs,
)


def _holdback_entries_by_skill() -> dict[str, set[str]]:
    """Every hold-back entry scoring actually applies, from every source."""
    entries: dict[str, set[str]] = {
        skill: set(tokens) for skill, tokens in _WHOLE_PHRASE_ONLY_TRIGGER_TOKENS.items()
    }
    for pack in _trigger_pack_packs():
        for skill, tokens in pack.whole_phrase_only_tokens:
            entries.setdefault(skill, set()).update(tokens)
    return entries


class WholePhraseOnlyTriggerHoldbackReachabilityTests(unittest.TestCase):
    def test_every_holdback_entry_is_actually_absent_from_scored_trigger_tokens(self) -> None:
        prepared_by_name = {prepared.definition.name: prepared for prepared in _prepared_routable_definitions()}

        leaked: list[str] = []
        for skill, entries in sorted(_holdback_entries_by_skill().items()):
            prepared = prepared_by_name[skill]
            for entry in sorted(entries):
                still_scored = _tokens(entry) & prepared.trigger_tokens
                if still_scored:
                    leaked.append(f"{skill}: {entry!r} still scored as {sorted(still_scored)}")

        self.assertEqual(
            leaked,
            [],
            "Dead whole-phrase-only hold-back entries -- these words are documented as held "
            "back but are still scored on their own because the hold-back set subtraction "
            "never removed them (composed vs. NFKD-decomposed text is the usual cause). "
            "Source table: src/routing/recommend.py. Pack tables: the "
            "`whole_phrase_only_tokens` block of the language's trigger pack. Leaked: "
            + "; ".join(leaked),
        )

    def test_the_korean_holdback_moved_to_the_pack_and_still_applies(self) -> None:
        # The regression this file was written for, restated against the pack:
        # the five `adversarial-consensus` words now live in `ko.json`, and the
        # hold-back has to survive the move, not just the fix.
        entries = _holdback_entries_by_skill()["adversarial-consensus"]

        for word in ("검토", "계획", "관점에서", "여러", "찾아"):
            with self.subTest(word=word):
                self.assertIn(word, entries)


if __name__ == "__main__":
    unittest.main()
