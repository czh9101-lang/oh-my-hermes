# Realtime voice trial receipts

Adoption evidence for a full-duplex voice connector, consumed by
`external-connector-readiness`. The contract lives in
`src/workflows/realtime_voice_trial_receipts.py`; this page explains what it
proves, what it refuses, and why a connector that authenticates and answers has
not yet earned a pass.

## The user goal

Someone is about to adopt or promote a realtime voice connector. They want to
know whether it keeps whole spoken turns and behaves safely in the room, on the
microphone, over the network, and on the transport they actually intend to use.
They supply a bounded trial, or run one through an authorized host, and get a
verdict per dimension with unsupported and unobserved dimensions reported as
such.

## Why generic connector readiness is not enough

A voice connector can pass every generic check and still fail conversationally.
Each of these failures is invisible to "it authenticated and returned audio":

| Failure | Where the contract catches it |
| --- | --- |
| The first syllable of every utterance is eaten | `integrity.onset: clipped` |
| One sentence becomes two turns, or two become one | `integrity.segmentation` |
| One accepted utterance is dispatched twice, or not at all | `integrity.dispatch_count` |
| An unfinished transcript is dispatched | `integrity.transcript_outcome`, `integrity.input_completeness` |
| The answer takes seconds to become audible | derived latency, only for turns that earned it |
| Playback restarts from the beginning after a streaming fallback | `streaming.replay: replayed_from_start` |
| An interruption is ignored, or queued audio plays over the user | `streaming.barge_in`, `streaming.queued_audio` |
| An ambiguous spoken phrase reaches a high-impact tool | the tool-safety section |

## Three separations

**Turn integrity is not latency.** Integrity is checked per turn from named
states. Latency is *derived*, and only for a turn with one declared timing
reference, every required milestone present, and an intact segmentation. A turn
that was split, merged, or timed against nothing reports no latency at all
rather than a plausible number.

**Requested is not observed.** `requested_stack` and `observed_stack` are
separate blocks with the same keys. `observed_stack_evidence` says how the
observed side was learned; when it is `unavailable` the observed block must be
empty. Echoing the requested model into the observed block is a validation
error, not a warning.

**Synthetic is not the room.** `test_condition` is `synthetic_fixture` or
`actual_environment`, and `environment` marks each of `room`, `microphone`,
`network`, and `transport` as `intended`, `simulated`, or `unobserved`. A
synthetic trial's verdict is capped at `hold`; so is an actual-environment
trial whose facets were not all the intended ones, with the missing facets
named.

## `realtime_voice_turn/v1`

| Field | Meaning |
| --- | --- |
| `turn_id` | Opaque per-turn reference. Repeating one inside a receipt is an error: one accepted utterance is one turn. |
| `timing_reference` | `host_monotonic`, `connector_reported`, `gateway_reported`, or `unavailable`. One per turn, never mixed. `unavailable` forbids every milestone. |
| `milestones` | Non-negative integer millisecond offsets from the turn's own zero for `speech_start`, `speech_end`, `final_transcript`, `first_output_text`, `first_output_audio`, `playback_end`; `null` where nothing was observed. The present offsets must be monotonic in that order. |
| `milestone_evidence` | `measured`, `producer_reported`, `derived`, or `unavailable` per milestone. `unavailable` and a `null` offset must agree. |
| `terminal_status` | `completed`, `interrupted`, `cancelled`, `truncated`, `failed`, or `unobserved`. |
| `integrity` | `onset`, `segmentation`, `dispatch_count`, `input_completeness`, `max_duration_behavior`, `mute_resume`, `transcript_outcome`. |
| `streaming` | `barge_in`, `queued_audio`, `replay`, `backpressure`. |

`turn_latency()` returns `eligible: false` with a basis
(`no_declared_timing_reference`, `incomplete_milestone_sequence`,
`segmentation_premature_split`, …) rather than a number whenever a turn has not
earned a reading.

## `realtime_voice_trial_receipt/v1`

| Field | Meaning |
| --- | --- |
| `trial_ref`, `connector_ref`, `connector_revision`, `profile_ref` | Opaque identities. The revision is the exact build or opaque build identity the trial observed. |
| `test_condition`, `environment` | Synthetic versus actual, and the four environment facets. A synthetic fixture may not mark any facet `intended`. |
| `requested_stack`, `observed_stack` | `provider`, `model`, `voice`, `transport`, `codec`, `sample_rate_hz`, `channels`, `endpoint_detector`, `endpoint_config_ref`, as opaque references and positive integers. |
| `observed_stack_evidence` | `measured`, `producer_reported`, or `unavailable`. |
| `trial_started_at`, `trial_ended_at` | ISO-8601 stamps; the end may not precede the start. |
| `turns` | The turn records above. |
| `fallback` | `occurred`, `requested_path_outcome`, `fallback_path_ref`, `reason`, `relative_to_audible`. |
| `tool_safety` | One record per spoken tool attempt. |
| `receipt_id` | `rvt_` plus a digest of the trial's identity. Recomputed at validation, so a stored receipt cannot be relabelled. |

### Fallback

A fallback that occurred must name the path that served the trial, why the
requested path did not, and whether it happened `before_audible` or
`after_audible`. `requested_path_outcome: succeeded` alongside
`occurred: true` is a validation error: a fallback path succeeding is never
success for the requested voice stack, provider, or model. The readiness answer
carries both paths and keeps `requested_path_ready` false.

### Tool safety

| Field | Meaning |
| --- | --- |
| `attempt_ref` | Opaque per-attempt reference. |
| `action_class` | `read_only`, `low_impact`, or `high_impact`. A high-impact attempt always requires confirmation. |
| `confidence_state` | `confident`, `ambiguous`, or `unobserved`. |
| `confirmation_required`, `confirmation_state` | `not_required`, `received`, `refused`, or `unobserved`. |
| `policy_decision` | `allowed`, `denied`, `deferred`, or `unobserved`. |
| `tool_result_ref`, `denial_reason` | An opaque result reference only for an allowed decision; a bounded reason only for a denied one. |

`policy_decision: allowed` requires `confidence_state: confident` and, when
confirmation was required, `confirmation_state: received`. An ambiguous or
unconfirmed spoken command reported as authorized execution is a validation
error. A high-impact attempt whose decision or confirmation was never observed
blocks the tool-safety dimension.

## The readiness answer

`answer_realtime_voice_readiness()` returns `realtime_voice_readiness/v1`: a
`pass`, `hold`, or `block` state with reasons for each of `turn_integrity`,
`latency`, `fallback`, `interruption`, and `tool_safety`, plus a derived
latency summary, the fallback summary, the evidence gate, and an overall
`ready`, `hold`, or `blocked` verdict. `ready` requires every dimension to pass
in the intended environment; the validator refuses a `ready` answer for a
synthetic trial or one whose requested path fell back.

The caller may pin `connector_revision`, `profile_ref`, and `now`. A receipt
from another build, another profile, or older than
`REALTIME_VOICE_TRIAL_STALE_AFTER_SECONDS` (six hours, the horizon
`external_action_readiness` already uses) blocks rather than passing quietly:
it is a real observation of something else.

### Six states chat and status keep apart

`states` reports each independently, and none of them implies another:
`connector_configured`, `synthetic_trial_passed`,
`actual_environment_trial_passed`, `voice_turn_observed`,
`tool_action_observed`, `session_completed`.

## Routing and consumers

- Realtime voice adoption phrases ("realtime voice connector", "voice connector
  trial", "voice turn integrity", "barge-in behavior", and their Korean forms)
  route to `external-connector-readiness` instead of `toolbelt-readiness`,
  which owns the missing-tool case. Negative controls in
  `src/quality/routing_precision.py` pin that a translation input, a definition
  question, a voice-memo file lookup, and a billing receipt do not fire it.
- The prepared connector card then carries `realtime_voice_trial`, naming the
  three schemas, the five dimensions, the six states as all false, and
  `network_action: none`. A prepared card never creates a receipt.
- `voice-input` may read the tool-safety verdict of a supplied receipt and may
  never create or infer one. `media-input`'s `media_result_manifest/v1`
  describes a supplied recording and is never realtime voice readiness.

## Operator entry point

```sh
omh ops realtime-voice-readiness --input trial.json
omh ops realtime-voice-readiness --input trial.json --connector-revision build-9f2c14 --profile support-desk --now 2026-09-09T12:00:00Z --json
```

The command reads a receipt somebody else produced. It refuses an invalid
receipt with the contract violations rather than a partial verdict.

## Boundaries

OMH opens no microphone, call, room, socket, or provider session; downloads no
voice model; installs no connector; executes no speech provider; and authorizes
no tool from a receipt. The trial is run by the user or an authorized host, and
OMH validates and summarizes what they report.

The contract is provider-neutral by construction: providers, models, voices,
transports, codecs, endpoint detectors, and fallback paths are opaque
references, and no validation or verdict code branches on any of them. The
demo fixtures in `demo_realtime_voice_trials()` cover a WebRTC stack and a SIP
telephony stack through the same code path.

Conversational content stays out by construction rather than by redaction. The
key set is closed, so there is no field a transcript, a raw tool argument, a
provider payload, or a recording could arrive in, and every free string is
screened for raw phone numbers, participant identities, and credential shapes
before it is accepted. Latency varies with hardware, network, language, room
noise, provider load, and endpoint policy, so the contract keeps the test
condition and sets no universal thresholds. Timing can be precise without
proving transcription accuracy or conversation quality.
