# Session Activity Receipts

`session_activity_receipt/v1` is one bounded, host-supplied summary of one
observed interval of one Hermes session. It exists so that workflow learning,
context-budget review, run-efficiency analysis, skill health, achievements,
and agent-ops review can reuse one observed record instead of each scraping a
transcript, and so that "the skill was not used" can be told apart from "nobody
saw whether the skill was offered".

## What a receipt is, and is not

A receipt is **supplied observed evidence**. The Hermes host, an observer
plugin, a wrapper, or an operator observed a session and handed OMH a summary.
OMH validates that summary, records it once, and projects it for consumers.

A receipt is not:

- automatic host execution or collection. OMH does not crawl Hermes
  databases, patch Hermes hooks, or start any telemetry of its own. The only
  input is a payload someone chose to supply, through
  `omh runtime session-receipt ingest --input <file>`.
- proof of an unobserved interval. A producer loaded mid-session reports a
  floor for the part it watched, never the whole session.
- execution, verification, review, CI, merge-readiness, or merge evidence.
  Every record carries the claim boundary that says so.

## Contract

Every string on a receipt is either a closed-vocabulary value or an opaque
reference; the key set is closed; arrays are capped. Raw prompts, tool
arguments or results, transcripts, secrets, and filesystem paths cannot be
carried, by key name (`prompt`, `transcript`, `tool_args`, `path`, ...) and by
shape.

| Field | Meaning |
| --- | --- |
| `receipt_id` | Stable identity for idempotent ingestion. The same id with the same observation records once; the same id with a different observation is quarantined. |
| `producer` | `kind` in `hermes_host`, `observer_plugin`, `wrapper`, `operator`; an opaque `ref` and optional `version`. |
| `profile_ref`, `session_ref` | Opaque identities or privacy-preserving handles. Ingestion bound to an expected profile quarantines any other. |
| `boundary` | `kind` in `turn`, `compaction`, `snapshot`, `process_exit`, `session_end`; `final` may be true only on `session_end`; `sequence` orders boundaries within a session. |
| `observed_interval` | `started_at`, `ended_at` (ISO-8601 UTC, `Z`), and `coverage` in `full_session`, `from_producer_load`, `unknown`. |
| `metrics` | Per-counter readings, each `{value, availability, measurement}`. Counters: `skills_exposed`, `skills_activated`, `tool_calls`, `tool_errors`, `subagent_spawns`, `subagent_completions`, `compaction_boundaries`, `model_calls`, `model_errors`, `tokens_input`, `tokens_output`, `tokens_total`, `context_peak_tokens`, `wall_clock_ms`, `model_latency_ms`. |
| `skills` | Bounded `exposed_names` and `activated_names` (skill labels or digest handles), truncation flags, and `name_form` (`plain` or `digest`). |
| `model_refs`, `evidence_refs` | Bounded opaque handles. Links, secrets, and control characters fold to digests. |

### Availability and measurement

- `availability` is `observed`, `heuristic`, or `unavailable` per counter. A
  counter the producer did not observe is omitted or `unavailable` with a
  `null` value. It is never zero and never inferred from prose. A count the
  producer derived from text, such as tool errors read off output when the
  host supplied no structured status, is `heuristic`, and consumers list it
  apart from observed counters.
- `measurement` is `exact` or `floor`. When `coverage` is anything but
  `full_session`, every observed counter must be a `floor`: a producer that
  did not watch the whole session cannot report an exact total. The
  validator refuses an `exact` reading under partial coverage.

### Exposure versus activation

`skills_exposed` and `skills_activated` are separate readings. Exposed-but-
unused is derived only when both were observed: from the two name lists when
both are complete, from the two counters otherwise, and `unavailable` with a
named reason when either side is missing or the counters disagree.

### Partial versus final

Only a `session_end` boundary may carry `final: true`. Turn, compaction,
snapshot, and process-exit receipts are partial; every consumer projection
reports them as `session_outcome: partial` with `terminal: false`, so a turn
boundary is never mistaken for the session's terminal result.

## Ingestion

```sh
omh runtime session-receipt ingest --input receipt.json \
  [--expected-profile <ref>] [--max-age-seconds <n>] [--redact-skill-names] \
  [--consumer workflow-learning --consumer skill-health ...] [--dry-run]
omh runtime session-receipt list [--session <ref>] [--limit <n>]
```

This is an agent, wrapper, and operator surface, not a normal user step.

Outcomes:

| Outcome | Meaning |
| --- | --- |
| `recorded` | The receipt validated and was appended to `$OMH_HOME/runtime/session_activity_receipts.jsonl`. |
| `already_recorded` | The same receipt identity with the same observation is already on record; nothing was written. |
| `rejected` | The payload is not a receipt. `errors` names every fault. Nothing reaches disk. |
| `quarantined` | The payload is a receipt this store may not take: `cross_profile`, `stale_after_final` (a final receipt for the session exists), `stale_sequence` (a later boundary exists), `stale_age` (older than `--max-age-seconds`), or `identity_conflict`. Bounded handles and the reason go to `session_activity_quarantine.jsonl`; the payload does not. |

`--redact-skill-names` stores every skill name as a stable digest handle for
installs where the names themselves are sensitive. Exposed-but-unused still
derives across digests.

## Consumers

The six named workflows read the same record through
`session_activity_evidence(receipt, consumer)`, each asking about its own
counters:

| Consumer | Counters |
| --- | --- |
| `workflow-learning` | skills exposed/activated, tool calls, tool errors, compaction boundaries |
| `context-budget-review` | context peak, tokens in/out/total, compaction boundaries |
| `run-efficiency` | wall clock, model latency, tool calls, model calls, tokens total |
| `skill-health` | skills exposed/activated |
| `achievements` | skills activated, tool calls, subagent spawns/completions |
| `agent-ops-review` | tool calls, tool errors, subagent spawns/completions, model calls, model errors |

Every projection carries `observed_metrics`, `heuristic_metrics`, and
`unavailable_metrics` as separate lists, `observation_floor` when coverage was
partial, `terminal` only for a final receipt, and the claim boundary.
Consumers that read skill counters also carry the exposure summary.

Coding-unit telemetry (`omh_unit_telemetry/v1`) remains the specialized
surface for executor units; a session receipt describes a Hermes session and
does not replace it.

## Boundaries the producer owns

Token and context accounting differ by provider; a reading stays attributed
to the producer that reported it. Text-derived error counts are heuristic
unless the host supplies structured status. A producer that restarted or
loaded mid-session sets `coverage: from_producer_load` (or `unknown`) and
reports floors.
