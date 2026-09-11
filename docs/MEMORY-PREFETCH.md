# Canonical Memory Prefetch

People ask Hermes to remember a preference or explain why it wasn't followed.
They don't need to run a recall command. This page is the agent, wrapper, and
operator reference for the OMH memory provider and its local evidence.

## One selector, different callers

With `memory.provider: omh`, `OmhMemoryProvider.render_pack` calls
`prepare_prefetch_records`, which delegates selection to
`select_memory_recall`. Handoff preparation reaches that same selector through
`build_project_memory_recall_pack`. The installed plugin uses only its bundled
modules and the standard library, not the control-plane package or a second
ranker.

The shared contract applies scope, perspective, immutable review linkage,
supersession, lifecycle, source freshness, and archive eligibility. Eligible
records compete in this order: privileged pins, attention tier, relevance,
decayed rank-fusion score, then record ID. Recency, saturating handoff-usage
buckets, age, admission weight, and temporal query intent feed that score.
Pins bypass no-query-overlap exclusion, not review or scope checks. Once a
record or summary-character budget is crossed, the remaining ranked suffix is
excluded as `over_budget`.

Shared selection doesn't mean every caller returns identical results. Inputs
must agree: stores, reviews, operations, policy, usage, pins, clock, scope,
perspective, query intent, and budgets. The provider uses the Hermes perspective
and a six-record limit; handoff recall defaults to five records and the selected
executor's perspective. The provider currently uses selector-default policy,
not the control plane's stored setup-profile memory policy.

## Scope is not a store path

The provider reads the nearest repository's `.omh` record store first, then
its user home. A duplicate record ID uses the project copy. Reviews and usage
also prefer the first home; pins are combined. Scope labels decide eligibility
independently of which store held the record.

The normal prefetch and handoff allowlist contains:

| Record label | Meaning |
| --- | --- |
| `user-global/default` | Explicitly reviewed cross-project context within the stores the caller reads |
| `project/<identity>` | Context for the current project label |
| `thread/<session-id>` | Context for the current session, included only when a session ID is available |

For prefetch, project identity is the nearest repository directory name, or
`default` outside a repository. Handoff derives it from the project containing
the supplied OMH home's parent. Same-named checkouts share a label; this isn't
an authenticated repository or user identity. The handoff facade reads its
supplied store only, so a global label doesn't make it discover other stores.

Delivery requires an explicit nonempty allowlist with a valid project entry.
A missing required kind, invalid scope, or blank ref yields
`scope_status: unresolved`, an empty disabled record pack, and
`scope_unresolved`. It never falls back to all records or global-only recall.
This is a record-selection boundary; separately governed blocks and a
consolidation reminder aren't selected by this record allowlist.

Scope and perspective mismatches disclose only aggregate exclusion counts,
not foreign IDs or summaries. Records without a perspective pass each actor
lens. The live provider selects `observed: hermes`; handoffs select their
executor, including Codex, Claude Code, Hermes, and generic targets. An
unresolved executor doesn't inherit another actor's perspective.

Operator `omh memory recall` without scope flags retains wildcard inspection
within the selected store. Supplying only half a scope fails closed.
`--include-stale` is inspection-only and carries ineligible replay evidence;
it can't turn stale records into approved handoff context. Inspection isn't
what the live provider delivers.

## Store rollout

The store layout and `project_memory_record/v2` and
`project_memory_recall_pack/v1` schemas stay unchanged. Existing `project`,
`target`, `thread`, and `run` labels remain readable. `user-global` is additive,
not a reinterpretation of old data. Neither upgrade nor recall rewrites records,
review evidence, or scope labels.

A record in the user home is not automatically global. In particular,
`project/default`, the capture default, doesn't match prefetch inside a
repository named `release-tools`. That record stays stored and inspectable;
it simply isn't in the `project/release-tools` delivery lens. `target` and
`run` records likewise aren't implicitly added to the normal allowlist.

For a rollout, inspect the existing store and intended audience first. Keep
correct project/thread labels. If an old fact really belongs across projects,
stage an explicitly scoped candidate and complete normal review and approval;
don't relabel JSON in place or bulk-promote old records. Select the user store
when the preference should be available to prefetch across repositories.
Legacy v1 records remain `review_required_legacy` in inspection and require
explicit reviewed reactivation before replay.

Agent/operator examples, not a user quick start:

```sh
# Stage a cross-project preference in the user store. Approval is separate.
omh --scope user memory capture "Prefer concise responses" \
  --scope-kind user-global --scope-ref default

# Inspect just that lens in the user store.
omh --scope user memory recall "responses" \
  --scope-kind user-global --scope-ref default

# Stage a project fact from inside the release-tools repository.
omh --scope project memory capture "Release checks include the CLI suite" \
  --scope-kind project --scope-ref release-tools
```

`--scope` selects storage; `--scope-kind` and `--scope-ref` label the record.
The capture, recall, and recall-incident CLI paths accept `user-global`; this
isn't a claim that every other memory subcommand supports that label.

## Selection, rendering, and served status

The provider initializes with an empty query. `queue_prefetch` rerenders for
the next turn using the queued query, which the host supplies after a completed
turn. `prefetch` returns the already prepared pack; its query argument doesn't
rerank that pack on the hot path.

The pack contains system-tier blocks, a reference-block label index, the record
section, and any current consolidation reminder. Record rendering preserves a
prefix of the canonical selection. Its separate 2,400-character section budget
includes XML tags, escaping, separators, and aggregate omission reports. Each
summary projection is capped at 500 characters. A record that doesn't fit
ends the prefix; a smaller later record can't jump ahead. Record IDs and types
aren't shortened to fit. A budget too small for the omission report returns
no section.

Selection cuts are `over_budget`; selected records lost to section rendering
are `render_budget_exhausted`. These are different events. The receipt names
selected IDs and rendered IDs separately, including selected-but-not-rendered
records. Its content digest covers the full stored summary, not the potentially
shorter escaped projection.

`recall_status()` reports the last pack actually returned by `prefetch`, not a
queued replacement. Its count is rendered full blocks plus rendered record
elements, never selected records or consolidation reminders. A reference-only
index is memory content without a discrete count. Initialization and shutdown
clear in-memory served status and receipts. A newly served empty pack replaces
the old state. The status line alone doesn't name records or prove host delivery
or model use.

## Receipt privacy and identity

`build_prefetch_receipt` creates `omh_memory_prefetch_receipt/v1` in `prepared`
state. `prefetch` marks it `returned_to_host`, exposes it through
`latest_prefetch_receipt()`, and attempts to persist it at
`<provider-user-home>/memory/prefetch_receipt.json`. A write `OSError` doesn't
fail the turn or produce a separate failure receipt, so disk may still hold an
older receipt. The file holds the last persisted receipt, not a per-turn
history, and survives shutdown for session-bound diagnosis.

The producer emits record IDs, summary digests, selected/rendered counts,
exclusion counts, truncation, scope and perspective lenses, query digest and
intent, session ID, clock, store-path digests, selector configuration identity,
and render-budget identity. It includes no summaries, raw query, prompts,
transcripts, block values, rendered text, or consolidation text. IDs, digests,
and session/scope labels still allow correlation, so protect the receipt like
other local memory metadata. Redaction isn't anonymity.

The reader caps input at 64 KiB, contains decoder-recursion failures, and rejects
malformed, oversized, foreign-schema, internally inconsistent, or digest-mismatched receipts.
Validation checks that rendered IDs form the selected prefix and that
`delivery_observed` and `model_use_observed` remain `null`.

`receipt_id` is an ordinary SHA-256 over the preparation body, excluding
`receipt_id`, `state`, and `served_at`. It detects inconsistent edits; it isn't
a signature or sender authentication. An authorized local writer can replace
the body and recompute the digest. Lifecycle state and served time aren't
covered by that digest. Configuration and store digests bind local inputs,
not an authenticated provider, user, or host.

A receipt supports a claim about local provider preparation and return. It
cannot prove that Hermes put the section in a model request or that the model
followed it. Missing evidence is unknown, never proof of nondelivery. The
[recall incident workflow](MEMORY-RECALL-INCIDENT.md) checks the receipt's
session, store, configuration, scope, and rendered anchor digest before using
it, without copying unrelated record metadata into the incident.

Local preparation makes no model or network call. A configured Hermes host
may later send rendered content to its model provider; receipt privacy isn't
a no-egress promise for that content. Host execution, billing, delivery, and
model use need their own observations.

Related: [Project Memory](MEMORY.md), especially the retained retrieval suite
and its canonical-versus-provider parity checks.
