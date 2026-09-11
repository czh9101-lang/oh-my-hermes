# Memory Sync Fidelity

An optional memory provider can be reachable, accept a request, and still hold
only part of the turn you meant to send it. This page explains how OMH records
that difference so an operator can tell "the provider received the whole turn"
from "the provider accepted a truncated subset" or "the turn never left the
queue". It is the contract behind the `input_fidelity` block of
`memory_provider_posture/v1`.

Audience: operators deciding whether to trust a provider, wrapper and host
integrators, and agents reading a posture. Normal users keep talking to Hermes;
the commands below are agent and operator references.

## What the contract adds

`memory_provider_posture/v1` already records synchronization direction,
trigger, idempotency, failure mode, retry, and checkpoint behavior. It could
not say what went into a sync. `memory_sync_fidelity/v1` adds that, provider
neutrally, in seven policy dimensions plus per-attempt receipts:

| Dimension | Fields beyond `status` and `evidence_class` | Closed values |
| --- | --- | --- |
| `input_surface` | `roles` | per role: `eligible`, `excluded`, `unknown`; roles are `user`, `assistant`, `tool_call`, `tool_result`, `summary`, `attachment`, `metadata` |
| `selection_policy` | `mode` | `whole_turn`, `selected_messages`, `selected_fields`, `approved_facts`, `unknown` |
| `limit` | `value`, `unit`, `scope`, `origin` | unit `characters`, `tokens`, `bytes`, `messages`, `records`, `unknown`; scope `per_turn`, `per_message`, `per_session`, `unknown`; origin `configured`, `discovered`, `unknown` |
| `truncation_policy` | `mode`, `splits_structured_input` | `head`, `tail`, `boundary`, `sampled`, `rejected`, `unknown`; split is `true`, `false`, or `unknown` |
| `omission_visibility` | `mode` | `counted_without_content`, `not_counted`, `unknown` |
| `backpressure_policy` | `mode`, `max_wait_ms`, `duplicate_prevention` | mode `queue`, `coalesce`, `block`, `retry`, `skip`, `unknown`; prevention `attempt_id`, `input_digest`, `none`, `unknown` |
| `extraction` | `mode` | `local_visible`, `hosted_opaque`, `unknown` |

Every dimension carries the posture's usual `status` (`ready`, `missing`,
`risky`, `not_observed`, `unknown`) and `evidence_class`
(`declared_documentation`, `declared_package_metadata`, `operator_statement`,
`observed_local_runtime`, `observed_trial_receipt`, `none`). `ready` is refused
unless the evidence class is an observation. A known `limit.value` must name
its unit, scope, and origin; a limit you cannot name stays `unknown`.

There are no provider-specific keys. A provider that cannot state a dimension
leaves it `unknown`, and `unknown_field_count` reports how many fields are in
that state.

## Binding

The block is scoped by a `binding` that every receipt must repeat exactly:

```json
{
  "provider_mode": "local",
  "profile_ref": "qa-profile",
  "session_ref": "session-a",
  "policy_digest": "sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
  "input_digest": "sha256:bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"
}
```

A receipt whose `provider_id` or binding differs from the posture's is not
merged; it appears in `rejected_attempts` as `receipt_binding_mismatch` and
contributes no readiness. Digests are correlation metadata, not content: they
let two observations agree about which policy and which input they describe
without either one carrying the text.

## Per-attempt receipts

Each `attempts` entry is a `memory_sync_receipt/v1`. The fields are counts,
units, digests, closed categories, and a UTC timestamp; there is no field for
prompt, memory, transcript, tool-result, credential, or attachment content,
and the parser rejects unknown fields rather than storing them.

| Field | Meaning |
| --- | --- |
| `receipt_id`, `attempt_id` | Opaque references; receipt ids must be unique inside one posture |
| `outcome` | `complete`, `truncated_head`, `truncated_tail`, `truncated_boundary`, `rejected`, `skipped_queue`, `skipped_timeout`, `failed`, `unknown` |
| `selected_count`, `omitted_count`, `unit` | How much was sent and how much was left out, in the stated unit |
| `omitted_position` | `none`, `head`, `tail`, `middle`, `unknown` |
| `failure_category` | `none`, `provider_error`, `transport`, `policy_rejection`, `timeout`, `unknown` |
| `entered_provider_state` | Whether the selected input reached provider state: `yes`, `no`, `unknown` |
| `omitted_input_entered_provider_state` | Whether the omitted part reached provider state anyway: `yes`, `no`, `unknown` |
| `evidence_class` | Same vocabulary as the dimensions |
| `observed_at` | Complete UTC timestamp ending in `Z` |
| `coalesced_into` | Optional; names the receipt this skipped attempt was folded into |

Contradictions are rejected at parse time: a `complete` receipt cannot carry
omissions or a failure category; a truncated receipt cannot report
`omitted_count: 0`, and its `omitted_position` must match the outcome or be
`unknown`; a skipped or rejected receipt cannot claim
`entered_provider_state: yes` or a non-zero `selected_count`; a coalesced row
must name a different receipt and stay `skipped_queue`. Everything the
parser doesn't forbid, it accepts. A truncated receipt may carry
`omitted_count: null`, and a skipped receipt may carry `selected_count: null`
with `entered_provider_state: unknown`. `failed` and `unknown` rows carry no
consistency constraint at all.

## Readiness

`synchronization_readiness` is derived from the single most recent eligible
receipt. Input order is not a clock: when two eligible receipts share the same
`observed_at`, neither wins and readiness is `unknown`.

| Latest receipt | Readiness |
| --- | --- |
| `complete`, observed trial receipt, known count and unit, `entered_provider_state: yes` | `complete_observed` |
| `truncated_*`, observed, `entered_provider_state: yes` | `partial_observed` |
| `skipped_queue` or `skipped_timeout`, observed | `skipped_observed` |
| `rejected`, observed | `rejected_observed` |
| `failed`, observed | `failed_observed` |
| any receipt whose `evidence_class` is not `observed_trial_receipt` | `unknown` |
| no eligible receipt, or a tie on `observed_at` | `unknown` |

The asymmetry is deliberate. A provider that processed part of a turn can never
report the same readiness as one with a complete observed receipt, no matter
how available it is. A declared cap, a vendor document, or a successful HTTP
status is posture evidence; none of them is a completion receipt.

## Three evidence classes that stay apart

1. Declared documentation says what a provider promises. It sets a dimension's
   `evidence_class` to `declared_documentation` and its `status` to at most
   `not_observed`.
2. Observed local runtime says what OMH or the host saw on this machine. It can
   support `ready` for a policy dimension. It does not prove what the provider
   stored.
3. Observed provider receipts say what the provider returned for one attempt.
   Only these move readiness away from `unknown`.

The posture also records `evidence_intake: operator_supplied_not_authenticated`.
OMH parses and checks the receipts you hand it; it did not call the provider to
obtain them, and it cannot vouch that they are genuine.

## Reading the outcomes

Privacy and egress. A `complete` receipt with `entered_provider_state: yes`
reports that the selected turn material became provider state. It says
nothing about where that state lives or whether anything crossed a network.
The posture's `storage_boundary` and the receipt's `provider_mode` are the
fields to read for that, and both are declared metadata, not a traffic
observation. The worked examples below run in `local` mode against a
`local_runtime` boundary; that is what the operator declared, and the
contract neither confirms nor rules out egress from it. Either way the
receipt is operator supplied and unauthenticated. `input_surface.roles` says
which roles were eligible; a tool result marked `eligible` means tool output is in scope for
whatever boundary applies. `extraction: hosted_opaque` means you cannot see
what the provider derived from the input.

Cost. `attempts` lists the observations an operator handed in, not
submissions. Nothing in the contract says the list is exhaustive, and
nothing in it counts what the provider received, stored, or charged for. A
`skipped_queue` or `skipped_timeout` row only says that this attempt cannot
claim the selected input entered provider state; its `selected_count` may be
null and its `entered_provider_state` may be `unknown`. Grouping rows by
`outcome` or `entered_provider_state` tells you how the supplied receipts
were classified. It doesn't bound provider traffic in either direction, and
it is not a billing figure. A `coalesce` policy with
`duplicate_prevention: input_digest` is a declared intent to avoid repeats;
the posture cannot prove that intent held (see the gaps below).

Failure and skips. `skipped_queue`, `skipped_timeout`, and `rejected`
receipts cannot claim that the selected input entered provider state; the
parser refuses those outcomes with `entered_provider_state: yes`. That is
not proof of absence. The same row may carry `entered_provider_state:
unknown`, and its `omitted_input_entered_provider_state` may be `yes`, which
reports that the omitted material entered provider state anyway. Readiness is
`skipped_observed` or `rejected_observed` in every one of those cases, so
read the two entered-state fields on the row rather than the readiness
label. `failed` is looser still. The schema accepts `failed` next to
`entered_provider_state: yes`, and readiness stays `failed_observed`,
because a provider error can arrive after part of the input was stored.
Read the entered-state fields on a failed row before assuming rollback;
`unknown` there means exactly that. All of these turns are visible in the
posture instead of silently missing from recall later. A skipped turn with
`coalesced_into` did not inherit its target's completion; it stays
`skipped_queue`.

Sync completeness. `partial_observed` means the latest observed receipt is a
truncated outcome with `entered_provider_state: yes`. It does not promise a
measured omission: `omitted_count` may be null and `omitted_position` may be
`unknown`, and readiness is still `partial_observed`. Only
`complete_observed` requires a known `selected_count` and a named `unit`.
When the omission is measured, the row tells you how much and where; when
it isn't, all you know is that something was cut. Ask what was cut before
you trust recall from that provider for that turn.

Deletion and portability. `deletion_and_portability_effect` reports whether
omitted input ever entered provider state (from the latest observed receipt)
and leaves `export` and `provider_side_deletion` as `unknown`. Whether a
provider can delete or export what it holds is a lifecycle question owned by
`external-connector-readiness`, and this contract does not infer the answer
from repository code or marketing copy.

## Worked examples

Both examples below were produced by running the real command against a
temporary OMH home. The command reads a `memory_provider_posture_input/v1`
file and prints the posture as JSON; `--write` stores it under the OMH
operations store.

```sh
omh ops memory-provider-posture --input posture.json
```

### Complete observed sync

Input `input_fidelity` block (the surrounding posture input carries
`schema_version`, `provider_id: provider-local`, `observed_version_boundary`,
and `storage_boundary`):

```json
{
  "schema_version": "memory_sync_fidelity/v1",
  "binding": {
    "provider_mode": "local",
    "profile_ref": "qa-profile",
    "session_ref": "session-a",
    "policy_digest": "sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
    "input_digest": "sha256:bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"
  },
  "input_surface": {
    "status": "not_observed", "evidence_class": "declared_documentation",
    "roles": {"user": "eligible", "assistant": "eligible", "tool_result": "eligible", "metadata": "eligible"}
  },
  "selection_policy": {"status": "not_observed", "evidence_class": "declared_documentation", "mode": "whole_turn"},
  "limit": {"status": "not_observed", "evidence_class": "declared_documentation", "value": 100, "unit": "bytes", "scope": "per_turn", "origin": "configured"},
  "truncation_policy": {"status": "not_observed", "evidence_class": "declared_documentation", "mode": "boundary", "splits_structured_input": false},
  "omission_visibility": {"status": "not_observed", "evidence_class": "declared_documentation", "mode": "counted_without_content"},
  "backpressure_policy": {"status": "not_observed", "evidence_class": "declared_documentation", "mode": "queue", "max_wait_ms": 1000, "duplicate_prevention": "input_digest"},
  "extraction": {"status": "not_observed", "evidence_class": "declared_documentation", "mode": "local_visible"},
  "attempts": [
    {
      "schema_version": "memory_sync_receipt/v1",
      "receipt_id": "receipt-1",
      "provider_id": "provider-local",
      "provider_mode": "local",
      "profile_ref": "qa-profile",
      "session_ref": "session-a",
      "policy_digest": "sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
      "input_digest": "sha256:bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
      "attempt_id": "attempt-1",
      "outcome": "complete",
      "selected_count": 80,
      "omitted_count": 0,
      "unit": "bytes",
      "omitted_position": "none",
      "failure_category": "none",
      "entered_provider_state": "yes",
      "omitted_input_entered_provider_state": "no",
      "evidence_class": "observed_trial_receipt",
      "observed_at": "2026-09-10T00:00:00Z"
    }
  ]
}
```

Observed output, trimmed to the derived fields:

```json
{
  "synchronization_readiness": "complete_observed",
  "readiness_receipt_id": "receipt-1",
  "evidence_intake": "operator_supplied_not_authenticated",
  "unknown_field_count": 3,
  "rejected_attempts": [],
  "deletion_and_portability_effect": {
    "omitted_input_entered_provider_state": "no",
    "export": "unknown",
    "provider_side_deletion": "unknown"
  }
}
```

The three unknown fields are the roles the input never mentioned
(`tool_call`, `summary`, `attachment`); the output lists every role and fills
the missing ones with `unknown`. The posture's `memory_sync_handoff` block
carries the same readiness as `input_fidelity_summary` next to
`review_status: not_omh_reviewed`, `imports_provider_records: false`, and
`authorizes_native_memory_mutation: false`.

### Partial sync with a skipped turn

Same policy, three receipts: the complete one above, a queued turn that was
folded into a later attempt, and that later attempt, which the provider cut at
the tail.

```json
[
  {"receipt_id": "receipt-1", "attempt_id": "attempt-1", "outcome": "complete",
   "selected_count": 80, "omitted_count": 0, "unit": "bytes", "omitted_position": "none",
   "failure_category": "none", "entered_provider_state": "yes",
   "omitted_input_entered_provider_state": "no", "evidence_class": "observed_trial_receipt",
   "observed_at": "2026-09-10T00:00:00Z"},
  {"receipt_id": "receipt-3", "attempt_id": "attempt-3", "outcome": "skipped_queue",
   "selected_count": 0, "omitted_count": 100, "unit": "bytes", "omitted_position": "unknown",
   "failure_category": "none", "entered_provider_state": "no",
   "omitted_input_entered_provider_state": "no", "evidence_class": "observed_trial_receipt",
   "observed_at": "2026-09-10T00:00:30Z", "coalesced_into": "receipt-2"},
  {"receipt_id": "receipt-2", "attempt_id": "attempt-2", "outcome": "truncated_tail",
   "selected_count": 80, "omitted_count": 20, "unit": "bytes", "omitted_position": "tail",
   "failure_category": "none", "entered_provider_state": "yes",
   "omitted_input_entered_provider_state": "no", "evidence_class": "observed_trial_receipt",
   "observed_at": "2026-09-10T00:01:00Z"}
]
```

Each row also repeats `schema_version: memory_sync_receipt/v1`,
`provider_id: provider-local`, and the five binding fields, omitted here for
length. Observed output:

```json
{
  "synchronization_readiness": "partial_observed",
  "readiness_receipt_id": "receipt-2",
  "unknown_field_count": 3,
  "deletion_and_portability_effect": {
    "omitted_input_entered_provider_state": "no",
    "export": "unknown",
    "provider_side_deletion": "unknown"
  }
}
```

Readiness follows the latest receipt, so the earlier complete sync does not
mask the later truncation. The skipped turn stays in `attempts` as
`skipped_queue` with `coalesced_into: receipt-2`; it is visible, and it did not
become complete because its target was later submitted.

## How memory-sync uses it

The posture reaches `memory-sync` as `not_omh_reviewed` context and a
`prepare_memory_sync` next action. The workflow explains risk from these fields
and proposes nothing that bypasses the existing review and native-write
approval boundary: it imports no provider record into OMH review and
authorizes no `MEMORY.md` or `USER.md` write. Lifecycle questions about
enabling, pausing, deleting, or exporting provider memory belong to
`external-connector-readiness`.

## What this contract does not prove

- It does not install a provider, inspect credentials, call a hosted API, or
  upload memory. Readiness inspection adds no egress.
- It does not count provider writes. Receipt ids are unique inside one posture
  and `duplicate_prevention` is a declared policy; neither proves that a retry
  or a coalesced submission avoided a duplicate write. That proof needs an
  eligible submission adapter with an independent write-count oracle, which
  this tree does not have. The related backpressure criterion (F7 in the
  delivery plan) is recorded as blocked, not passed.
- It does not authenticate receipts. `evidence_intake` says so on every posture.
- It does not answer export or provider-side deletion; both stay `unknown`
  here and are owned by the lifecycle posture.

Related: [Project Memory](MEMORY.md), [Memory Context Review](MEMORY_CONTEXT.md),
[Memory Recall Incident](MEMORY-RECALL-INCIDENT.md).
