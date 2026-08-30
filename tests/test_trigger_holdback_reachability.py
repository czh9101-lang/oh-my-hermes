"""Reachability gate for `_WHOLE_PHRASE_ONLY_TRIGGER_TOKENS`.

`_WHOLE_PHRASE_ONLY_TRIGGER_TOKENS` holds back specific words from a skill's
scored trigger-token pool because, split from their trigger phrase, they are
too generic to credit on their own (see the comments beside the dict in
`src/routing/recommend.py`). The hold-back is a set difference applied at
definition-prepare time: `_tokens(triggers) - holdback_entries`.

`routing_tokens` (imported as `_tokens` in that module) folds every token
through NFKD normalization, which decomposes precomposed Hangul syllables into
their component jamo. A holdback entry written in ordinary composed Korean
(e.g. `"검토"`, codepoint U+AC80) is therefore a different string from the
decomposed token `_tokens` actually produces for that same word, so the set
subtraction silently removes nothing — the entry still gets scored even though
the comment says it is held back. `adversarial-consensus` shipped five such
dead rows (검토, 계획, 관점에서, 여러, 찾아).

This test proves the hold-back is real: none of the words an entry names may
still surface in the skill's actual scored `trigger_tokens`.
"""

from __future__ import annotations

import unittest

from omh.routing.recommend import (
    _WHOLE_PHRASE_ONLY_TRIGGER_TOKENS,
    _prepared_routable_definitions,
    _tokens,
)


class WholePhraseOnlyTriggerHoldbackReachabilityTests(unittest.TestCase):
    def test_every_holdback_entry_is_actually_absent_from_scored_trigger_tokens(self) -> None:
        prepared_by_name = {prepared.definition.name: prepared for prepared in _prepared_routable_definitions()}

        leaked: list[str] = []
        for skill, entries in _WHOLE_PHRASE_ONLY_TRIGGER_TOKENS.items():
            prepared = prepared_by_name[skill]
            for entry in entries:
                still_scored = _tokens(entry) & prepared.trigger_tokens
                if still_scored:
                    leaked.append(f"{skill}: {entry!r} still scored as {sorted(still_scored)}")

        self.assertEqual(
            leaked,
            [],
            "Dead _WHOLE_PHRASE_ONLY_TRIGGER_TOKENS entries in src/routing/recommend.py -- "
            "these words are documented as held back but are still scored on their own "
            "because the hold-back set subtraction never removed them (composed vs. "
            "NFKD-decomposed Hangul is the usual cause): " + "; ".join(leaked),
        )


if __name__ == "__main__":
    unittest.main()
