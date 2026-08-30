# Routing quality gate

The meta-router has a deterministic golden evaluation in
`tests/test_routing_quality.py`. It is intentionally separate from individual
skill tests so catalog or scoring changes are reviewed against user-shaped
routing outcomes.

The gate currently measures:

- known-lane dispatch accuracy across representative research, planning,
  coding, memory, paper, visual, and feedback requests;
- ambiguity guardrail behavior, including bounded candidates and a high
  confidence threshold; and
- unknown-input fallback behavior, including explicit fallback metadata.

The current baseline is 100% (7/7) on the known-lane golden set. This is a
regression baseline, not a claim about production traffic. The next telemetry
step should measure the same dimensions from privacy-safe route metadata:
dispatch accuracy from operator corrections, clarification rate, fallback
rate, and confidence-margin distributions.

Local traces can be summarized with:

```bash
omh learning metrics
```

The metrics command reads only metadata-only learning traces. Operator
missed-route corrections are counted separately and may include an expected
workflow identifier, while prompts and operator notes remain excluded.

## Adding a trigger language pack

People type in their own language. A trigger language pack is how OMH
recognises one, and adding a language is authoring a data file — not editing
the router, the catalog, or any list of supported locales.

A pack is one JSON document per language:

```json
{
  "schema_version": "trigger_language_pack/v1",
  "language": "pt-br",
  "skills": {
    "frontend": ["página inicial", "layout responsivo"],
    "build-failure-triage": ["falha de build", "erro de compilação"]
  },
  "whole_phrase_only_tokens": {
    "adversarial-consensus": ["revisão"]
  }
}
```

There are two places a pack can live, and the difference matters:

- **Shipped with the repo** — `src/routing/trigger_packs/<lang>.json`. These are
  product data: they merge into the catalog itself, so scoring, the rendered
  `skills/*/SKILL.md` trigger lists, and `docs/WORKFLOWS.md` all read one
  trigger table. Adding one means regenerating those artifacts in the same
  commit (see the Generated Artifacts Map in `CLAUDE.md`).
- **Your own** — `~/.omh/routing/trigger-packs/<lang>.json`, beside
  `routing/model-chains.json`. These merge at the scoring layer only, so your
  local phrases change what your router recognises and never rewrite the
  repo's generated artifacts. `omh doctor` lists both, and names any pack it
  refused.

Rules the validator enforces, and why:

- `language` must match the filename. The file name is the pack's identity.
- Every key under `skills` must be a real skill id. An entry naming a skill
  that does not exist is a refusal, never a silent drop — a phrase quietly
  attached to nothing, or to the wrong lane, is the failure mode a pack has
  to make impossible.
- A phrase must contain word characters, and a `whole_phrase_only_tokens`
  entry must produce at least one scored token. An entry the normalizer cannot
  see is dead weight its author believes is live.
- Every problem in a document is reported at once, so fixing a pack takes one
  round trip.

Two things to get right in the phrases themselves:

- **Short, natural, unambiguous.** Phrases match by containment, so a phrase
  generic enough to sit inside an ordinary sentence turns every such sentence
  into a dispatch. Prefer fewer, safer phrases over broad coverage.
- **Pay for them with negative controls.** Add cases to
  `ROUTING_PRECISION_CASES` in `src/quality/routing_precision.py` proving
  ordinary questions in that language still get answered rather than routed,
  and to `ROUTING_INTERVENTION_CASES` proving the lanes you claim are reached.
  Both corpora assert exact counts; update them in the same commit.

Two gates hold the mechanism together. `tests/test_trigger_language_packs.py`
proves every shipped phrase is live in its skill's scored surface and that
every language with a shipped `--language` / `OMH_LANG` **output**
localization also ships a **trigger** pack, so input and output support cannot
diverge. `tests/test_trigger_holdback_reachability.py` proves no hold-back
entry, in any language, quietly removes nothing.
