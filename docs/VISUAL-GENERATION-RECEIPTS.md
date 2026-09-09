# Visual Generation Receipts

`visual_generation_receipt/v1` binds one image result to the route a producer
actually attested, separately from the route the request asked for.

OMH defines and validates this contract. It does not call an image API, read a
credential, choose a paid model, or implement a backend. A host or connector
executes the attempt and attests only the fields it can observe.

## Why the contract exists

`visual_observation/v1` records that a referenced image artifact was reported.
That is a real observation, and it stays what it is. What it cannot answer is
the question a user actually asks about a generated image: which backend and
model produced these bytes, from which prompt-card revision and reference
inputs, for which accepted attempt.

A backend can accept a requested model value without attesting that it honoured
the selection. A returned file therefore proves a file, never a route. Without a
receipt, OMH can show a generated image while lacking the evidence to say the
requested provider, model, quality, or edit inputs produced it.

## The one separation everything follows from

| Block | Meaning |
| --- | --- |
| `requested_route` | What the request asked for. Intent, never evidence. |
| `observed_route` | What the producer attested. |
| `attested_route_fields` | The only bridge between the two. |

Every field on `observed_route` that is not named in `attested_route_fields` is
exactly `unknown`, and a field that *is* named may not be `unknown` — attesting
nothing is not an attestation. Nothing copies across that line. A successful
artifact cannot promote a requested provider, model, quality, operation,
dimension, or credential class into an observed one.

`observed_route_field()` answers from the attested set rather than from the
stored value, so a hand-edited store that filled a field in without attesting it
still reads as `unknown`.

Route fields are `provider`, `model`, `quality`, `operation`, `dimensions`, and
`credential_class`. The first four are the claim fields: a mismatch or an
unknown on one of them raises a warning. Dimensions and credential class are
reported but do not warn on their own, because a host legitimately normalises a
requested size and a credential path is not a claim about what produced the
image.

## Identity

A receipt is minted per attempt and binds four identities:

- `card_id` and `card_digest` — the prompt card *revision*. The digest is
  published on the card itself (`omh img-summary prompt-card` prints it), and it
  moves when the copy, language, archetype, or aspect ratio changes. That is
  what makes a stale-card warning possible.
- `action_id` — the accepted generation action.
- `attempt_id` — this attempt.
- `effect_id` — `visual_generation:<card_id>:<action_id>:<attempt_id>`, the
  store's selection key. The action is part of it because a producer that
  numbers attempts per action would otherwise file two actions' first attempts
  under one identity, and the second would supersede the first.

`external_effect_ref` carries an identity a wrapper already minted for the
action, so a receipt reuses it rather than introducing a second claim that the
action occurred.

Re-reporting one attempt identically appends nothing and returns the receipt
already on record. A report that differs — a later outcome, an attested field
the producer learned, replaced bytes at the same reference — appends a new
receipt linked through `supersedes_receipt_ref`. Nothing on disk is rewritten.

A superseded receipt is a retracted report. `latest_receipt_for_effect()` is the
one rule for which receipt speaks for an attempt, and binding goes through it:
naming a superseded receipt id is refused, so a later `failed` report cannot sit
in the store while an observation still claims the earlier success. `omh
img-summary status` marks superseded attempts rather than showing two and
letting a reader pick the flattering one.

## Outcome and failure stage

`outcome` is `succeeded`, `partial`, `failed`, or `unknown`. `failure_stage` is
`none`, `setup`, `authorization`, `request`, `provider`, `download`,
`validation`, or `unknown`.

`none` belongs to `succeeded` alone, so every other outcome has to say where it
stopped. Only a succeeded receipt carries an artifact, and only a succeeded
receipt can back a generated-image observation. A failed or partial attempt
keeps a bounded stage and mints no image evidence.

## Artifact

A succeeded receipt binds `content_sha256`, `mime_type`, `byte_size`, an opaque
`artifact_ref`, and an optional `provider_response_ref`.

The binding is to the digest, not to the path. A reference reused for different
bytes is a different result and therefore a different receipt identity, so an
earlier result cannot survive under a replaced file. Two receipts naming one
reference with different digests raise `artifact_digest_drift`.

Binding checks correspondence rather than asserting it: a receipt whose MIME
type differs from the observed artifact's, or whose digest differs from one the
observation already carries, is refused instead of overwriting what the
observation reported.

Every stored handle — `artifact_ref`, `provider_response_ref`, `evidence_refs` —
is an opaque identifier and may not be a filesystem path.

## Input lineage

`input_lineage` carries `source_image_count`, `source_image_digests`, and
`edit_constraints` (`preserve`, `remove`, `replace`). A `generate` request has
none of the three. An `edit` request names at least one source image, and its
digests are either empty or one per source image.

Raw prompts, source images, private paths, credentials, and provider
request/response bodies are never stored. The prompt card is bound by digest.

## Usage and cost

Each of `input_tokens`, `output_tokens`, `image_units`, and `cost_usd` is a
producer-attributed reading of `{value, measurement}`, where measurement is
`measured` or `estimated`.

An absent metric reads as `{value: None, measurement: "unavailable"}`. Absence
never becomes zero, an estimate is never measured spend, and a reported zero
survives as the measured or estimated zero it is. Storing an explicit
`unavailable` reading is refused: omission already says it.

## Warnings

| Warning | Raised by |
| --- | --- |
| `route_mismatch:<field>` | attested observed field disagrees with a requested one |
| `unknown_observed_route:<field>` | claim field the producer did not attest |
| `stale_card_identity:<receipt_id>` | receipt filed against an earlier card revision |
| `artifact_digest_drift:<artifact_ref>` | one reference seen carrying different bytes |
| `provider_response_reuse:<ref>` | one provider response credited to two attempts |

The first two come from `route_evidence()` on a single receipt. The last three
are cross-receipt and come from `route_status_warnings()`.

A warning is reported, never resolved. A disagreement is not settled in favour
of either side, and an unattested field is not filled in.

## Evidence states stay separate

Generation, visual QA, and delivery are three states, and a receipt is evidence
for the first only. `does_not_prove` on every receipt reads
`visual_qa_passed`, `delivered`, `image_content_correct`.

An unbound generated-image observation additionally says
`generation_route_attested` in its own `does_not_prove`: it recorded that a file
was reported and cannot name the provider, model, quality, operation, or attempt
behind those bytes. Binding a succeeded receipt drops that entry and nothing
else.

## Legacy records

A `visual_observation/v1` record written before receipts existed stays readable
and still validates. `project_legacy_visual_observation()` reads it through the
receipt contract with every route, attempt, lineage, digest, and usage field
`unknown`. Those unknowns are not defaults: they are fields that were never
observed, and nothing infers a provider from configuration or a digest from the
file being on disk.

## Commands

```sh
# The card prints the digest a receipt binds.
omh img-summary prompt-card --kind pr --section "summary:What changed:..."

# Record what a producer attested. Omit an --observed-* flag to leave that
# field unknown; passing one is the attestation.
omh img-summary receipt \
  --card-id <card> --card-digest <sha256> \
  --action-id <action> --attempt-id <attempt> --producer <host> \
  --outcome succeeded --operation generate \
  --requested-model gpt-image-1 --observed-model gpt-image-1-mini \
  --artifact-ref <ref> --content-sha256 <sha256> \
  --mime-type image/png --byte-size 4096 \
  --usage 'image_units=1:measured' --usage 'cost_usd=0.04:estimated' \
  --summary "connector reported one image"

# Bind a succeeded receipt to a generated-image observation.
omh img-summary observe --card-id <card> --type generated-image \
  --path /abs/path.png --summary "..." --receipt-id <receipt>

# Requested against observed, with every warning.
omh img-summary status --card-id <card> --card-digest <sha256>
```

The store is `~/.omh/visual/generation_receipts.jsonl`, append-only under a real
OS lock on both POSIX and Windows.

## Boundaries

A receipt is one producer's report of one image attempt. It is not visual QA,
attachment, posting, sharing, or delivery evidence, and it does not prove that
the image content is correct or factual.

Provider model names, quality tiers, pricing, and response metadata change.
Receipts stay adapter-neutral: values are attributed to the producer, no
provider catalog lives in OMH core, and a provider response id is never treated
as proof of image quality. Stored identifiers are bounded opaque references and
content digests, scoped to the active OMH profile.
