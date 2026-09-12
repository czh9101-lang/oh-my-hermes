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
  live input remains a payload someone chose to supply, through
  `omh runtime session-receipt ingest --input <file>`. The local normalized
  observer engine described below is not a live-host adapter.
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

## Opt-in observer compatibility (operator and adapter reference)

The profile-local option is explicitly default-off:

```yaml
plugins:
  entries:
    omh:
      settings:
        group_chat_activity:
          enabled: false
```

OMH reads this via the host's `get_config("group_chat_activity", None)` API,
including its legacy `config` fallback. Merely enabling the option currently
reports `unavailable/member_activity_contract_unsupported` through `omh_status`.
It registers no member callback, imports no observer engine, starts no worker,
and writes no observer state. Existing tools, memory provider registration,
and session hooks continue to work. Doctor and probe report this static
compatibility boundary without failing normal chat or implying activation.

**Live group-chat collection is not implemented on the supported host surface.**
The inspected Hermes revisions `c32e0acb0ec59d53ac964007c75630e098bcd045` and
`63de8e74ef91af66386336df27399f64828d0182` expose no documented room/member
lifecycle producer. The installed group/session observation plugin does not
publish one either. Platform reactions, edits, thread creation, streaming
text, and pre-auth dispatch are not substitutes. A future adapter requires a
supported callback signature, sequence semantics, and terminal fire-site;
there is no guessed hook, event-bus publisher, transcript reader, or runtime
patch here. Keep collection disabled until that integration is available.

### Local normalized producer engine

`plugin_bundle/omh/activity_observer.py` provides explicit `ActivityObserver`
construction, `enqueue`, `flush`, `status`, and bounded `close` APIs for local
adapter development. This engine requires the OMH core package for admission;
standalone unsupported-host registration does not import it. Its closed
**internal** schema `omh_group_activity_event/v1` is not a Hermes API:

- Keys: `schema`, `profile_ref`, `session_ref`, `room_ref`, `member_ref`,
  `turn_ref`, `kind`, `event_ref`, `sequence`, `observed_at`. There is no body,
  payload, extension, approval, or transport slot. References must be
  `sha256:<64 lowercase hex>`; timestamps are UTC seconds ending in `Z`.
- Kinds: `session_start`, `member_start`, `member_complete`, `tool_call`,
  `tool_error`, `compaction`, `session_end`. Member/turn refs are null only on
  room boundaries. Profile-local keyed SHA-256 derivation happens before
  enqueue; the adapter must own its secret key and never retain raw IDs.
- Scope is `(profile, room, session)`, with nested member/turn identities.
  Only directly observed tool calls, explicit tool errors, and compactions
  become counters. Member presence never becomes subagent lifecycle evidence;
  skill, model, token, and other unsupported counters remain null/unavailable.
- Bounds are 64 active rooms, 1024 pending sanitized events, 4096 dedupe
  identities per room, and 256 member/turn entries each. Topology or identity
  overflow stops counting without evicting keys and permanently lowers coverage.
  Queue loss conservatively lowers all subsequent intervals in that engine.
- A single worker owns aggregation and existing locked receipt admission.
  Dispatch submission only parses/copies bounded metadata and enqueues. Failed
  writes/aggregation report bounded categories and counters, never raw errors
  or successful recording. `flush` is an explicit non-dispatch barrier;
  `close` drains with a bounded timeout and reports loss on timeout. A caller
  must join the worker successfully before deleting its owned home.
- `session_end` alone emits final; unload/process loss emits a useful partial
  `process_exit`. Late starts, sequence gaps, drops, stale/conflicting events, and
  checkpoint recovery fail closed for exactness. Restarted aggregates keep
  their sequence high-water mark and counters, but always report floors.
- Checkpoints contain only aggregates in one bounded profile-specific JSON
  file under OMH runtime, never an event journal. Terminal in-memory dedupe is
  capped at 64 rooms and expires after 24 hours, including idle workers. The existing receipt store
  remains replay authority after expiration/restart; there is no second
  receipt store. Engine status reports readiness, compatibility, dropped,
  gapped, rejected, write-failed counts, and last bounded outcome.

### Maintainer proof surfaces

```sh
PYTHONPATH=tests uv run python -m unittest tests/test_plugin_observer.py tests/test_plugin_observer_limits.py -v
uv run python tools/qa/seven_issues_observer.py --scenario normalized-lifecycle
uv run python tools/qa/seven_issues_observer.py --scenario installed-host-compatibility --host-source /path/to/hermes-agent
uv run python tools/qa/seven_issues_observer.py --scenario supported-host-lifecycle
```

The first QA scenario exercises the real local producer, receipt admission,
manual-ingestion replay, all six consumer projections, and CLI listing. The
second uses the actual host interpreter, PluginContext, PluginManager, tool
registry and existing session-end callback in disposable homes, then unloads
registrations. Both report cleanup and bounded machine outputs, not live
collection. A pristine fixture home's doctor may independently report missing
installation state; its exit is retained separately from the advisory observer
check. The supported-host lifecycle scenario exits **3 (unavailable)** until
an actual adapter and callback producer exist. Exit 3 must not be aggregated
as passing or used to close #1505.

## Boundaries the producer owns

Token and context accounting differ by provider; a reading stays attributed
to the producer that reported it. Text-derived error counts are heuristic
unless the host supplies structured status. A producer that restarted or
loaded mid-session sets `coverage: from_producer_load` (or `unknown`) and
reports floors.
