# Project Memory

OMH project memory is a local, reviewed control plane for one project. Normal
users use natural-language Hermes chat: ask Hermes to remember a bounded fact,
review existing memory, or clean up old project context. The memory commands
below are an agent, wrapper, and operator control-plane reference, not steps for
normal chat users.

OMH does not read, patch, or mutate opaque Hermes internal memory.

## Optional Project Terms Source

A repository may have one optional `PROJECT_TERMS.md` at its root. It is a
portable, repository-reviewed human source for the words that project uses. If
it is absent, nothing happens: OMH does not create it, import it, scan for an
alternative, or treat its absence as a setup or health problem. OMH never
rewrites, commits, reverse-generates, or keeps the file synchronized.

The file and the machine store have different authority:

- `PROJECT_TERMS.md` is human-readable source material. Editing or committing it
  does not activate a mapping or change OMH behavior.
- An active, reviewed, project-scoped `domain_intelligence_profile/v1` is the
  machine-readable terminology source. It is reached only through preview,
  pending staging, separate review, and explicit approval.
- Reviewed mappings remain advisory clarification context with the claim
  boundary `routing_prior_not_override`. The ordinary router remains
  authoritative.

### Strict `omh-project-terms/v1` grammar

The parser accepts UTF-8 without a byte-order mark, no more than 65,536 exact
source bytes, and either LF throughout or CRLF throughout. The preamble,
including blank lines, is exact:

```markdown
# Project Terms

Project terminology only. This file is not agent instructions, routing rules, approval, execution, or evidence. Changes affect OMH only after explicit review.

<!-- omh-project-terms/v1 -->

## domain: delivery

- term: `dispatch packet` = `handoff`
  definition: A prepared package of coding work for one selected owner.
  say-instead: handoff
  localized[ko]: 핸드오프
  distinct-from: `dispatch` - Dispatch is observed execution, not preparation.
- workflow-hint: `ralplan`
```

After the preamble, the only accepted constructs are:

- `## domain: <domain_id>` for a unique normalized domain. Every domain must
  contain at least one term mapping.
- ``- term: `phrase` = `canonical_term` `` for at most 40 mappings per domain.
- Two-space-indented human metadata immediately below a term:
  `definition:` (at most 240 code points), repeatable `say-instead:` values (at
  most 80 each), `localized[<locale>]:` labels (at most 80 each and one per
  locale), and one ``distinct-from: `canonical_term` - note`` (note at most 240).
- ``- workflow-hint: `existing-installable-id` `` for at most 20 unique existing
  catalog workflow ids per domain.

Values are normalized and checked with the domain-vocabulary safety rules.
Unknown lines, keys, locales, or workflow ids; duplicate or conflicting domains,
mappings, or metadata; mixed line endings; unsafe values; and size/count
violations fail the whole parse before any write. Definitions, `say-instead`
guidance, localized labels, and `distinct-from` notes are retained only in the
read-only human parse view. File capture projects only phrase-to-canonical
mappings and workflow hints into the unchanged profile-v1 candidate shape. The
SHA-256 covers the exact source bytes, so accepted LF and CRLF files can have
different source digests without their bytes being rewritten.

### Reviewed lifecycle

Normal users can ask Hermes to inspect or align project terminology without
learning commands. The commands below are an agent/operator control-plane
reference. Run them from inside the repository; file mode is bound to the
canonical project root and accepts exactly the regular, non-symlink root file
named `PROJECT_TERMS.md`. Absolute paths, traversal, alternate names, and mixed
file/direct-capture arguments fail closed.

```sh
# Agent/operator only: parse and preview; writes neither the source nor the store.
omh --scope project memory domain-capture \
  --from-file PROJECT_TERMS.md --json

# Agent/operator only: atomically stage one pending candidate per file domain.
omh --scope project memory domain-capture \
  --from-file PROJECT_TERMS.md --stage --json

# Agent/operator only: inspect a pending card and derive current source freshness.
omh --scope project memory domain-review \
  --candidate <candidate-id> --source-freshness

# Agent/operator only: make the separately reviewed candidate active.
omh --scope project memory domain-approve <candidate-id>

# Agent/operator only: list active project profiles and derive source freshness.
omh --scope project memory domain-list \
  --scope-kind project --scope-ref <project-ref> --source-freshness

# Agent/operator only: retire one active profile while preserving review/history.
omh --scope project memory domain-retire \
  --scope-kind project --scope-ref <project-ref> --domain <domain-id> \
  --reason superseded
```

Preview returns `project_terms_capture/v1` in `prepared_not_observed` state with
stable profile ids, exact source SHA-256, current base revisions, capacity, and
an empty `mutation_set`. It does not invent future candidate ids. `--stage`
preflights the complete file and capacity, rechecks revisions under the store
lock, and then writes all pending candidates or none. It does not write an
active profile. Review and approval remain separate actions; approval writes
the existing active profile and immutable review record. A competing approval
or retirement makes an older candidate fail closed as `stale_candidate`.
Listing observes active profiles. Retirement records a retired revision and
preserves review and history; later activation requires another reviewed
candidate.

File-derived candidates store only bounded provenance:
`source_class="omh_local"` and `source_ref="pt_sha256:<exact-byte-sha256>"`.
With explicit `--source-freshness`, project-scoped review and list derive one of
four states against the current root file:

- `unchanged`: current exact bytes match the recorded digest.
- `changed`: current exact bytes have a different digest.
- `missing`: the tracked root file is absent.
- `untracked`: the artifact is not valid provenance for this repository's
  project-terms source.

Freshness is computed for that response only. It is not persisted and does not
stage, approve, reject, replace, retire, rewrite, or delete anything. `changed`
and `missing` are review-needed information, not synchronization and not a
claim that the active profile changed. The file remains project-only: this path
cannot import user or organization vocabulary into the repository source.

### Behavior and evidence boundaries

Neither the source prose nor a reviewed mapping routes, reranks, or dispatches a
request. Only an eligible, genuinely unresolved router-owned interaction may
receive one bounded advisory clarification from an active reviewed profile.
Explicit workflows, dispatch, status, help, file lookup, maintenance, direct
answers, task cards, and already selected routes remain protected. Workflow
hints are advisory identifiers, not instructions or dispatch authority.

Project terms do not execute work, perform code review, run tests, invoke CI,
approve a plan, establish merge readiness, or merge code. Preview, staging,
review cards, approval records, profile matches, freshness, and prepared context
packs are not evidence that Hermes, a model, provider, executor, or coding
owner read or used the terms. OMH persists neither the human definitions nor
raw prompts, transcripts, hidden reasoning, logs, or task progress through this
capture path.

The generated `ulw-context` skill is the Hermes workflow surface for direct
terminology lookup, explicitly confirmed pending-candidate capture, and
confirmed dependency-frontier interviewing before a separate planning or
handoff transition. Its catalog id is `context`; it does not make glossary prose
a routing input or change the lifecycle above. Generated name and description
metadata make it eligible for host semantic nomination, but live Hermes
semantic nomination is `not_tested`: the installed host profile used for QA did
not contain `ulw-context`, and no profile mutation was made. Explicit invocation
and deterministic lexical-proxy results are the only current selection
evidence. The lexical proxy measures metadata token competition; it is not
semantic nomination evidence.

Reviewed active project-domain profiles are now projected read-only into the
existing handoff context and role packs. Only validated, approved summaries for
the current project are included; pending, rejected, retired, malformed,
digest- or review-mismatched, and scope-mismatched artifacts are excluded. The
projection has identical semantics for Codex, Claude Code, Hermes, and generic
executor targets. It stores no exact source-file bytes. A projected summary is
prepared context only, not evidence that an executor or model received, read,
or used it, and not dispatch, execution, review, CI, merge-readiness, or merge
evidence.

The lifecycle still does not automatically place `PROJECT_TERMS.md` in
sessions, runtime records, or coding handoffs. Exact source bytes can be
included only through explicit workspace-relative file selection in
`handoff_input_manifest/v1`, subject to content hashing, safety classification,
item limits, and byte-budget checks. Manifest preparation or inclusion is
prepared context only; it is not proof of dispatch, receipt, reading, model
use, execution, review, CI, merge-readiness, or merge.

The workflow adapts dependency-frontier interviewing and the
separation of terminology from implementation decisions from Matt Pocock's
`grilling` and `domain-modeling` skills at revision
`84fdeffd12f2ee307994d1eb6feb48173b6e0502`, under the MIT License (Copyright
2026 Matt Pocock). The strict file grammar and reviewed OMH lifecycle above are
OMH-specific adaptations rather than copied upstream prose or runtime behavior.

## Reviewed Domain Intelligence

Domain intelligence is a separate reviewed vocabulary store for agents,
wrappers, and operators. It lets an operator curate bounded user,
organization, or project vocabulary without changing routing in the same
release. The store lives under `.omh/memory/domain-intelligence/` with
`candidates/`, `profiles/`, `reviews/`, `history/`, and `operations/`
subdirectories.

Every scope reference is an explicit opaque key supplied by an operator or a
wrapper identity boundary:

- `user`
- `organization`
- `project`

The stored ref is labeled `operator_or_wrapper_supplied`. It is a routing
scope key for future use, not authenticated identity evidence, and OMH v1 never
infers it from chat text, summaries, or vocabulary.

The operator lifecycle is:

```sh
# Agent/operator only: stage bounded vocabulary for manual review.
omh memory domain-capture \
  --scope-kind organization \
  --scope-ref org-acme \
  --domain sales \
  --mapping "QBR=quarterly_business_review" \
  --mapping "deal desk=deal_desk"

# Agent/operator only: inspect pending review cards.
omh memory domain-review

# Agent/operator only: approve, reject, list, or retire.
omh memory domain-approve <candidate-id>
omh memory domain-reject <candidate-id> --reason insufficient_evidence
omh memory domain-list --scope-kind organization --scope-ref org-acme
omh memory domain-retire --scope-kind organization --scope-ref org-acme --domain sales --reason superseded
```

Captured candidates are `domain_intelligence_candidate/v1` and always remain
`pending_review` until an explicit approval or rejection. Approval writes one
current `domain_intelligence_profile/v1` for the exact `(scope.kind,
scope.ref, domain_id)` key and one immutable
`domain_intelligence_review_record/v1`. The profile id is deterministic for
that key, so a public approval path cannot create duplicate current profiles.
Approvals are complete replacements: the next approved candidate increments the
revision and archives the prior profile under `history/`. Retirement writes a
reviewed retired revision and keeps review/history files; later reactivation
requires another reviewed candidate.

Candidates record `base_profile_revision`. Approval fails closed with
`stale_candidate` if another approval or retirement changed the current
revision after capture.

Profile eligibility is fail-closed. Readers exclude pending, rejected, retired,
malformed, digest-mismatched, or review-mismatched artifacts and report bounded
diagnostics instead of exposing raw content. The canonical profile digest covers
only behavior-bearing fields: schema version, profile id, revision, status,
scope, domain id, sorted vocabulary mappings, sorted workflow hints,
confidence metadata, bounded provenance metadata, and base profile revision.
It excludes candidate/review identifiers, reviewer claims, timestamps,
claim-boundary prose, and filesystem paths.

Local filesystem authorization is the trust boundary for this store. Payload
and operation digests detect accidental corruption, partial or internally
inconsistent mutation, and mismatched artifact linkage. They do not
authenticate content against a process authorized to write the store: such a
process can coordinate replacements across candidates, profiles, reviews,
history entries, and operations and recompute the ordinary SHA-256 digests.
The design has no secret key or immutable external anchor. Symlink rejection,
path-containment checks, bounded reads, and untrusted-artifact validation still
protect storage access and fail closed on unsafe or malformed input; they do
not authenticate an authorized local writer.

Domain-intelligence artifacts persist only explicit bounded vocabulary,
identifier-like workflow hints, confidence metadata, and bounded provenance.
They do not store raw prompts, transcripts, hidden reasoning, chat logs, or
Hermes internal memory. The profile claim boundary remains
`routing_prior_not_override`. Only an eligible, genuinely unresolved wrapper
interaction may consume an active reviewed profile from the current
repository's own project-local store, and only to select one catalog-owned
clarification question. The route, candidate handoff, and plan artifact's
`deep_interview_contract/v1` remain unchanged. This is clarification context,
not routing authority, plan approval, execution, review, CI, merge,
authentication, or Hermes internal-memory evidence.

For each eligible interaction, OMH derives the canonical current repository and
its unnamed project-scoped `.omh` store internally rather than accepting a
caller-supplied domain identity. User and organization profiles are not
consumed until an authenticated principal binding exists. The bounded
`domain_routing_context/v1` response context is ephemeral: it is not copied
into wrapper-session continuity, runtime records, coding handoffs, status
records, or other persisted interaction artifacts.

Replacement or retirement takes effect on the next eligible interaction. Any
unhealthy, incomplete, malformed, or conflicting profile store fails closed to
the existing generic question. Direct answers, file lookup, help, maintenance,
task cards, explicit workflows, static specialist routes, operator actions,
workflow learning, status, and every dispatch remain protected. Profiles do
not automatically learn from chat, select a route, rerank candidates, or
trigger dispatch.

This clarification-only milestone leaves broader work explicit: reviewed
activation before any routing influence; multi-round research and planning
cognition; passive missed-route review; domain-pack expansion; and optional
offline evaluation. Each requires its own reviewed authority, privacy, and
evidence contract before it can change public behavior.

If a future routing design includes malicious local writers in its threat model,
it must revisit authenticated provenance; key management or authenticated
append-only infrastructure is outside this local metadata-only foundation.

Reviewer claims and source references are safe opaque identifiers only:
ASCII letters, digits, `_`, `.`, `:`, and `-`, up to 120 characters. Review
reasons are metadata-only reason codes, not free-form text. Accepted codes are
`duplicate`, `incorrect_scope`, `insufficient_evidence`, `operator_request`,
`scope_error`, and `superseded`.

## V2 Model

New OMH-owned artifacts use versioned, replay-gated records:

- `project_memory_candidate/v2` is a bounded candidate with source class,
  canonical scope, retention choice, and safety result.
- `project_memory_record/v2`, `omh_memory_scope/v2`, and
  `omh_memory_block/v2` carry an opaque identity, positive revision, source
  class, admission, retention, and revalidation data.
- `project_memory_review_record/v2` is immutable admission evidence. Its
  states are `pending_review`, `approved_manual`, `approved_auto_safe`,
  `blocked`, and `rejected`. `approved_auto_safe` is a local policy result,
  not a human-review claim.
- `omh_memory_replay_evaluation/v1` records why an immutable revision was or
  was not eligible for a particular replay boundary. Preparation is not proof
  that a model, provider, or executor used it.

A current v2 record must pass the same deterministic evaluator before project
recall, handoff context, provider prefetch/pre-compression, block rendering, or
an explicit block read. Unsupported schema, missing review linkage, safety
failure, expiry, stale review, scope mismatch, conflict, supersession, or a
legacy v1 artifact fails closed with a reason code.

## Admission: Remember, Refuse, or Defer

For a new fact, Hermes asks for source class, target store, canonical scope,
retention class, and an explicit decision:

- **Remember** creates only one bounded **durable** candidate. It remains
  pending review until OMH-local approval and a target write are separately
  observed.
- **Refuse** covers secrets, raw logs, transcripts, prompt-injection-shaped
  instructions, and temporary task progress.
- **Defer** sends uncertain source, scope, target, retention, and external
  provider/vector material to review rather than retaining it.

Hermes-native and external provider/vector context is `not_omh_reviewed`. It
may nominate a candidate but never inherits OMH admission. OMH-local processing
does not promise no egress: a configured Hermes runtime may transmit rendered
OMH prefetch content in its model request.

## Retention and Replay

Retention is additive to record type:

| Class | Rule |
| --- | --- |
| `volatile` | Explicit only; admission starts its 1-7 day TTL, defaulting to seven days. It is ineligible at the exact UTC expiry boundary. |
| `standard` | Preserves current type behavior. An `episode` defaults to 30 days; other records keep explicit TTL/staleness behavior. |
| `durable` | Has no TTL. It receives a revalidation deadline only when explicitly configured. |

Expiry removes influence only; it does not move an artifact or prove any
absence. A stale revalidation deadline requires fresh review or a bounded,
identity-specific confirmation.

A reviewer can re-class a record at approval:
`omh memory approve <candidate-id> --retention-class durable` promotes a
settled decision so it does not inherit the default review clock nobody chose
for it. The override re-derives retention and a *defaulted* review deadline
with the new class's own rules — but a cadence the captor explicitly chose
(`--stale-after-days` at capture, recorded as `cadence_source: explicit`)
survives the re-class: durable makes the deadline optional, not forbidden,
and a flag about retention never silently removes a review date the reviewer
saw on the card. Supplying the class the candidate already has is a
validated no-op. Lifecycle (correction/restore) candidates keep their
reviewed class; the flag applies to plain approvals only.

### Cadence Tunables

The three memory clocks are policy tunables, not baked-in constants. A stored
setup profile's `memory_policy` block may carry, additively and optionally:

| Field | Default | Meaning |
| --- | --- | --- |
| `stale_after_days_default` | 90 | Review cadence minted for fact/decision/lesson/procedure captures without an explicit `--stale-after-days`. |
| `episode_ttl_days` | 30 | TTL minted for episode captures without an explicit `--ttl-days`. |
| `due_soon_days` | 14 | Advance-notice window recall packs warn inside, shared by review-due and expiry notices; accepted range 1–365. |

An absent or invalid value falls back to the named default, and the effective
values are always disclosed on the `project_memory_policy/v1` payload, so
`omh memory status` shows what is actually in force. A capture's cadence
(explicit or default) is stored on the record and honoured by later flagless
`omh memory confirm` runs.

### Durability Receipts

"Written" is not one event. A memory operation reports exactly which of four
receipt states it reached, and it names that state rather than implying a
later one:

| State | Meaning |
| --- | --- |
| `candidate_persisted` | The bounded candidate is on local disk under the candidate path. It is pending review and carries no replay eligibility. |
| `approved_record_persisted` | The approved record and its review linkage are on local disk under the approved path, after an OMH-local approval decision. |
| `indexes_refreshed` | The local index, link journal, and counter references that address the record agree with the persisted record. |
| `replay_ready` | The record satisfies scope, perspective, retention, and freshness gates, so a recall pack may include it. |

Each state names disk or index facts the operation itself observed, never
work it handed to something else. A queued, buffered, staged, or in-flight
write is not `candidate_persisted`, an approval decision without its record
write is not `approved_record_persisted`, and a persisted record whose index
references were not refreshed is neither `indexes_refreshed` nor
`replay_ready`. Reaching one state never asserts the states above it: a
candidate that never clears review stops at `candidate_persisted`, and an
approved record that is expired, stale, or out of scope stops short of
`replay_ready`.

States are receipts, not record states. They describe what one operation
observed about persistence, so they carry no lifecycle authority: they cannot
approve, expire, retire, or revive anything, and they are not execution,
review, CI, merge, or Hermes internal-memory evidence. An operation that
cannot confirm a state says so and names the state it did reach, which is what
keeps a partial write readable as partial instead of silently reported as
durable.

## Freshness: Review-Due Dates and Source Evidence

A record's freshness is one verdict derived from three stored inputs plus the
caller's clock. Nothing is inferred from conversation, and nothing is fetched.

| Input | Field | Effect |
| --- | --- | --- |
| Retention deadline | `ttl.expires_at` | Past it, the record is `expired`. |
| Review-due date | `staleness.review_due_at` | Past it, the record is `stale`. |
| Source digest | `source_evidence.sha256` | A changed source is `stale`; an unreadable one is `unknown`. |

`review_due_at` is the readable name for the date `staleness.stale_after`
always held; both are written, and when they disagree the earlier one wins, so
editing one spelling can never restore freshness.

Source evidence is opt-in and local. When `--source-ref` names an absolute
path to a readable local file, capture records the file's SHA-256 alongside it.
Every later freshness check re-reads that file and compares. A ref that is not
an absolute path, a file that is gone or unreadable, and a file past the
digest budget all read as `unknown` — never as `fresh`. OMH makes no network
call to check anything, and never rewrites or deletes a record because its
source moved.

Both `stale` and `unknown` are ineligible for default recall, exactly like
`expired`. `omh memory recall --include-stale` surfaces them for inspection
carrying their ineligible replay evidence, which keeps the pack unattachable
as approved context.

### Freshness Warnings

A recall pack used to drop an ineligible record silently. It now carries a
bounded `freshness_warnings` list naming every record whose freshness is not
confirmed, and a handoff pack is emitted even when the warning is all it has
to say:

```json
{
  "record_id": "mem_...",
  "state": "stale",
  "reason_code": "stale_review_required",
  "review_due_at": "2026-01-01T00:00:00Z",
  "detail": "Its revalidation deadline passed, so nobody has confirmed the record since then.",
  "delivered": false,
  "next_action": "Confirm, replace, or retire this record before it steers the plan."
}
```

`delivered` says whether the record reached the pack (`--include-stale`) or
was held back. Records excluded for reasons that are not about freshness —
`no_query_overlap`, `over_budget` on a fresh record — never warn. Wrapper
briefs and runtime artifact summaries carry the warnings through by name
rather than as a count, so a summarized handoff still says which record needs
a decision. A warning is prepared context, never execution, review, CI, or
merge evidence.

The warning also fires *before* the cliff: for the last 14 days (the
`due_soon_days` tunable) before a record's review deadline, packs still
deliver it normally but carry a `review_due_soon` warning naming the deadline;
inside the same window before a retention TTL ends, the warning is
`expires_soon` — the retention twin, pointing out that confirmation cannot
extend a TTL and the honest moves are a re-capture, a correction, or letting
it expire. The expiry window scales down to half the record's own TTL (never
below one day), so a short-lived volatile record warns near its end instead
of carrying a permanent banner from the day it was approved. Expiry outranks
review-due when both windows overlap: a passed TTL is terminal, a passed
review date is not. Only delivered records earn advance
notice — it is a statement about this pack's own content, not a store-wide
page — advisory notices never displace blocking warnings inside the bounded
list, and once a deadline actually passes, the ordinary blocking warning
covers held-back records as before.

### Confirming, Correcting, or Retiring

Answering the warning uses three verbs. `omh memory confirm <record-id>` is
the lightweight one: it states the record is still true and resets its review
deadline (default 90 days ahead, or `--stale-after-days N`), rewriting only
revalidation metadata — the payload digest deliberately excludes it, so the
record's identity, admission, and immutable review record are untouched.
`omh memory confirm --all-due` confirms every record whose sole problem is a
passed deadline; each record still passes the single-record gates, so the
batch reports superseded or source-changed records as skipped with their
refusal reason and detail rather than silently re-blessing them. Expired
records never enter the batch at all — their verdict is `retention_expired`,
not `review_due`, and the fix is `omh memory retire`. Confirmation never
resurrects an expired record and never overrides the source-evidence gate —
a record whose cited source changed needs a correction, because a new
deadline would not restore its eligibility anyway.

The heavier verbs are unchanged: `omh memory correct` supersedes the record
with a reviewable replacement, `omh memory retire` archives it once expired.
Both preserve the prior revision — a correction writes
`history/<record-id>.r<revision>.json` carrying the original payload, its
admission provenance, its source evidence, and a `superseded_by` link to the
successor revision.

## Recall Ranking and Delivery Usage

Recall packs order eligible records relevance-first: keyword relevance rank
(term and tag overlap) is the primary sort key, and deterministic reciprocal
rank fusion over relevance, recency (`approved_at`), and delivery usage
orders records that share a relevance rank. A weaker keyword match can never
displace a stronger one, including across the budget cut; without a query all
relevance ties and recency plus usage decide the order. Each included record
carries a `ranking` block with its per-signal ranks and an integer
`rrf_score_micro`, so the order is always explainable from the pack itself.
The usage signal ranks on saturating buckets (0, 1-2, 3-9, 10+ deliveries) so
delivery counts cannot compound into a permanent head start.

Delivery usage counts only recall packs that were actually attached to a
prepared handoff payload — building a pack is speculative, so a delegation
that ends without a handoff, or rejects the pack, counts nothing. A CLI
`omh memory recall` is an inspection and does not count. Counters live in
`.omh/memory/usage.json` (`omh_memory_recall_usage/v1`); a missing or corrupt
usage store reads as empty and never blocks recall, and usage is a ranking
hint plus a retirement-report annotation, never an eligibility input.
Retirement reports annotate each expired or expiring row with `recall_usage`
so an operator can see whether a record was ever delivered before archiving
it. Lifecycle receipts list `recall_usage_counters` under their exclusions: a
prune deletes the manifest targets, not the delivery counters.

This ranking design is a deterministic reinterpretation of the hybrid-search
rank fusion and per-observation usage tracking popularized by memory servers
such as Honcho; OMH keeps the bookkeeping and drops the model calls.

## Recall Anchors, Age Tiers, and Duplicate Detection

Three bookkeeping ideas ported from Mnemosyne, deterministically:

**Pins** — mark an approved record as a recall anchor (at most 12 pins;
a pin never overrides expiry, scope, perspective, or review eligibility):

```sh
# Agent/operator only: anchor one reviewed record in every recall pack.
omh memory pin <record-id>

# Agent/operator only: remove the anchor marker.
omh memory unpin <record-id>
```

Pinned eligible records lead the pack and skip only the `no_query_overlap`
cut. Pins take priority within the recall budget rather than expanding it,
and at most `limit - 1` pinned slots (minimum one) lead a pack — further
pins compete as normal records, so a fully-used pin budget can never blank
query-driven recall. Pin markers live in `.omh/memory/pins.json`
(`omh_memory_pins/v1`), outside record payloads, so digests and review
linkage are untouched; retirement reports annotate rows with `pinned`,
lifecycle receipts list `pin_markers` under exclusions, and each ranking
block carries `pinned`.

**Age tiers** — the ranking's `decayed_score_micro` degrades with record
age (0-30 days full weight, 30-180 days half, older a quarter), reported as
`age_tier`; `rrf_score_micro` stays pure rank fusion so fusion quality
remains comparable across records. Relevance still leads the sort, so a
stale strong keyword match beats a fresh weak one; tiers only reorder
records of equal relevance.

**Duplicate detection** — capture compares the normalized summary (NFC,
lowercase, collapsed whitespace) against active records and stamps an exact
match as `duplicate_of` on the candidate and its review card. Review-first:
nothing is silently merged, the reviewer decides — but a duplicate never
auto-approves, even under the auto-safe policy.

**Episode rollup** — additive consolidation without a model:

```sh
# Agent/operator only: report which records one episode would roll up.
omh memory rollup --tag deploy

# Agent/operator only: stage the reviewable episode candidate.
omh memory rollup --tag deploy --apply
```

The rollup selects up to 8 non-expired, non-episode records matching a tag
and/or scope (oldest first), and proposes one `episode` candidate whose
summary is a per-member-budgeted mechanical join — every member is
represented — and whose `derived_from` names every member; synthesis stays
Hermes' job. The episode inherits its members' confinement, strictest
wins: mixed scopes or mixed perspectives refuse (`mixed_scope` /
`mixed_perspective`), a shared perspective or scope carries onto the
episode, and any volatile member makes the episode volatile with the
smallest member TTL. Originals are never modified or retired by a rollup;
the candidate always lands in review (a derived aggregate never
auto-approves, even under the auto-safe policy), a re-run while an
identical candidate is pending reports `already_staged` instead of staging
twins, and `omh memory lineage` on the approved episode walks back to all
members.

**Recall signal refinements** — the ranking block reports
`veracity_weight_pct` (an `approved_manual` record weighs 100, an
`approved_auto_safe` record 90 in the decayed score — both classes stay
fully eligible), and packs echo `query_intent`: a query carrying an
unambiguous English time cue — the tokens `yesterday`, `today`, `recent`,
`recently`, `ago`, or a phrase such as `most recent` / `last week` — is
classified `temporal` and doubles the recency weight inside rank fusion.
Ambiguous engineering adjectives (`current`, `latest`, `now`, `newest`)
deliberately do not fire as bare tokens. Relevance stays the primary sort
key in both cases, so neither signal can change which keyword matches win —
only how peers of equal relevance order.

## Attention Tiers: Active, Reference, Archive

Approved memory grows, and an old but still-true record can crowd out the few
facts that should steer the current conversation. Every approved record
therefore carries an explicit attention tier saying how much of the working
context it may occupy. The tier says nothing about whether the record is true,
approved, or fresh — expiry, scope, perspective, and review eligibility are
unchanged by it.

| Tier | Effect on recall |
| --- | --- |
| `active` | Leads the working context. The default for every approved record. |
| `reference` | Stays recallable, but yields to active peers inside the same budget. |
| `archive` | Leaves default recall. The record stays in the store and answers an explicit archived query. |

The tier enters the one existing ranking ladder, which now reads: pinned
anchors, then attention tier, then relevance rank, then the decayed fused
score, then record id. A reference record therefore ranks below every active
record of the same pin status, and there is no second ordering mechanism to
keep in sync. Each included record reports its `attention_tier`, and its
`ranking` block reports the integer `attention_rank` used for the sort. The
fused score itself is untouched, so a tier change reorders a pack without
rewriting the relevance evidence that explains it.

Every pack carries an `attention` block disclosing what it is made of:

```json
{
  "active_included": 2,
  "reference_included": 1,
  "archived_included": 0,
  "archived_excluded": 1,
  "include_archived": false,
  "detail": "2 active record(s) lead this working context. 1 reference-tier record(s) are included behind them. 1 archived record(s) stayed out of the working context; they remain in the store and are listed as archived_tier exclusions."
}
```

A tier change is previewed before it is applied, and the preview writes
nothing:

```sh
# Agent/operator only: preview what the tier change does to the working context.
omh memory attention <record-id> --tier reference

# Agent/operator only: apply the previewed change and journal the prior tier.
omh memory attention <record-id> --tier archive --reason "superseded by the canary policy" --apply

# Agent/operator only: query the archive explicitly.
omh memory recall "deploys" --include-archived
```

The preview projects the working context twice — as it stands, and with the
requested tier substituted — through the same recall builder, so
`working_context_after` is the pack the operator actually gets rather than a
description of one. It also names `leaving_working_context` and
`entering_working_context` by record id. Pass `--query` to preview against the
task text the change is meant to affect. A change that would be a no-op is
refused as `tier_unchanged`, so every journal line is a real move, and a
missing record is refused as `record_not_found` with a readable reason.

Applied changes write `attention` on the record (tier, bounded reason, prior
tier, change timestamp) and append one `omh_memory_attention_journal/v1` line
to `.omh/memory/attention.jsonl`. The prior tier is recorded in both places, so
an archive is always reversible from local evidence alone. The tier lives
outside the reviewed payload digest, so changing it never breaks a record's
immutable review linkage — and corrections and restores carry the tier onto the
successor revision instead of silently returning an archived record to the
active set.

**Archive the tier is not retirement the lifecycle verb.** Archiving moves no
file: the record stays in `records/`, stays readable, stays valid, and is
listed in every pack as an `archived_tier` exclusion rather than vanishing from
it. Retirement moves an expired revision into `.omh/memory/archive/` and writes
a tombstone. The tier is also not an exemption: an expired archive-tier record
still retires on the normal schedule. Neither operation is a deletion.

This is Letta's context hierarchy read deterministically: OMH keeps the
explicit tiers and the disclosure, and drops the model-side judgement about
what deserves attention.

## Provenance and Lineage

A capture may declare which approved records a new fact was derived from:

```sh
# Agent/operator only: capture a conclusion with explicit provenance links.
omh memory capture "Always purge cache before deploy" --derived-from <record-id>

# Agent/operator only: trace where a record came from and what built on it.
omh memory lineage <record-id>
omh memory lineage <record-id> --depth 5
```

`--derived-from` is repeatable (at most 8 refs) and every ref must name an
existing approved record at capture time. The lineage report
(`omh_memory_lineage/v1`) walks ancestors and descendants breadth-first up to
`--depth` hops (clamped to 1-10), cuts cycles, and marks deeper unexplored
links as `truncated`. A parent that was later retired or pruned appears under
`unresolved_refs` rather than failing the report. Recall items expose each
record's `derived_from` list so an executor can ask for lineage when a
summary alone is not enough. Lineage is prepared-context traversal only; it
never claims the derivation itself was re-verified.

## Perspectives (Observer / Observed)

A record may optionally carry a perspective: which actor's view it is
(`observer`, defaulting to `hermes`) and which actor it is about
(`observed`). This is Honcho's peer paradigm reinterpreted deterministically:
no theory-of-mind inference, just bookkeeping that keeps one actor's lessons
out of another actor's context. An `observed` label that should reach
handoffs must be an executor target (`codex`, `claude-code`, `generic`,
`hermes`, `omx-runtime`, `omo-runtime`, `omc-runtime`); other labels such as
`operator` are inspection-only lenses that no handoff ever selects.

```sh
# Agent/operator only: capture a fact about one executor.
omh memory capture "codex needs the full test command spelled out" --observed codex

# Agent/operator only: recall through a lens; unscoped records always pass.
omh memory recall "test command" --observed codex

# Agent/operator only: list observer/observed pairs with record counts.
omh memory perspectives
```

Lens semantics: an unscoped record (no perspective) behaves exactly as
before and passes every lens. A scoped record surfaces only through a
matching lens; lens labels normalize like capture labels (lowercased,
trimmed). Handoff recall packs and handoff context packs automatically use
the selected executor target as their `observed` lens, so a record captured
about `codex` reaches codex handoffs — and never a `claude-code` or
`generic` handoff. While the executor target is still unresolved
(`choose`), handoffs carry unscoped records only. A plain recall with no
lens is an inspection surface and hides nothing. Recall packs echo the
active lens under `perspective`, each included record carries its own
`perspective` block, and context packs exclude mismatched records with
reason `perspective_mismatch`.

## Legacy Migration and Reactivation

Legacy v1 files remain readable in status and review surfaces as
`review_required_legacy`; they do not replay. The first operator step is always
a report:

```sh
# Agent/operator only: dry-run, source-by-source counts and review-required notice.
omh memory inventory

# Agent/operator only: persist the bounded inventory ledger when requested.
omh memory inventory --write-ledger

# Agent/operator only: re-scan and reactivate exactly one reviewed artifact.
omh memory reactivate <record-id> --revision <n> --apply
```

Inventory reports deterministic counts for active records, scope items, blocks,
archive/history, candidates/reviews, index references, declared-link journals,
corrupt or unknown artifacts, and external exclusions. It does not emit raw
values or content hashes. Reactivation is per artifact/revision, report-first,
under the store lock, and creates v2 review evidence. It never mass-promotes
legacy memory or silently grants replay eligibility.

## Exact Lifecycle Vocabulary

- **expire** removes influence only.
- **retire** archives a readable local revision recoverably.
- **restore** creates a new pending revision linked to the archived revision
  while preserving that archive. A newer live conflict remains review-blocked.
- **prune** hard-deletes only the manifest-declared OMH-local target set for an
  expired volatile revision, after report and explicit confirmation.

The `archive` attention tier is not one of these verbs. It changes only how
much of the working context a live record may occupy; it moves no file and
writes no tombstone. See Attention Tiers above.

Restore and prune are report-first operator actions:

```sh
# Agent/operator only: inspect first, then apply a recoverable archive move.
omh memory retire
omh memory retire --apply

# Agent/operator only: inspect archive targets; a restore remains pending review.
omh memory restore <record-id> --revision <n>
omh memory restore <record-id> --revision <n> --apply

# Agent/operator only: inspect the local manifest, then explicitly hard-delete it.
omh memory prune <record-id> --revision <n>
omh memory prune <record-id> --revision <n> --apply --confirm-hard-delete-local
```

A prune receipt names attempted, observed, absent, unresolved, and excluded
local targets. It does not cover backups, filesystem snapshots, trash, synced
copies, Hermes-native memory, providers, vector stores, executors, or unlinked
artifacts. A tombstone blocks restore/retry of that exact opaque identity; it
does not block a newly captured fact.

## Batch Context Updates

Direct batch mutation is not a trust path. Agents and operators use a staged
sequence:

```sh
# Agent/operator only: validate into review-only candidates.
omh memory batch-stage --batch memory-update-batch.json

# Agent/operator only: create immutable review evidence for the staged set.
omh memory batch-review <batch-id>

# Agent/operator only: apply only the linked approved set under the store lock.
omh memory batch-apply <batch-id> --apply
```

Each stage is separate from replay eligibility. The compatibility
`omh memory apply --batch` path reports `review_required` rather than directly
writing unreviewed updates.

## Dreaming

Dreaming has only `off` and `reminder` modes. The reminder scheduler runs
automatically at five points:

| hook | trigger | purpose |
| --- | --- | --- |
| `on_turn_start` | `turn` | Evaluate when the interval is due (five turns by default). |
| `on_pre_compress` | `compaction` | Preserve a reminder before compression discards messages. |
| `on_session_end` | `session_end` | Review what a productive session may have made worth keeping. |
| `shutdown` | `shutdown` | Take the final opportunity before the process exits. |
| `initialize` | `session_start_recovery` | Recover when the prior session ended without consolidation. |

The brief nominates duplicate clusters, records at or near their deadline,
and working-context headroom below the configured floor. Standing reasons also
include `stale_review_required` and `expired_volatile_records`. An unchanged
standing condition is suppressed until its value changes, rather than being
restated every time the scheduler runs. A record OMH cannot source is never an
eviction candidate: absence of provenance is not evidence that the record is
wrong.

The ranking inputs do not expand eligibility or establish truth:

- Pins guarantee inclusion among eligible records; they never override
  expiry, scope, perspective, or review eligibility.
- Attention tiers control how much working context a record may occupy, not
  whether it is true.
- `approved_manual` carries 100% veracity weight and `approved_auto_safe`
  carries 90%; an unknown approval mode fails closed to the lower weight.
- Age tiers only degrade old records within an equal relevance rank.
- Delivery usage uses saturating buckets, so repeated use cannot compound into
  a permanent head start.

Dreaming prepares a reminder and metadata-only evidence. It never invokes a
model or performs consolidation, retirement, restore, or prune.

## Hermes Memory Tiering: Demotion (L1 → L2)

Hermes-native memory files (`MEMORY.md`, `USER.md`) are L1: small,
character-capped, and paid for on every turn. The OMH record store is L2:
reviewed, governed, and not bound by that cap. When L1 fills, the usual move
is deleting an entry — which loses the fact. Demotion is the other move: the
content goes down into L2 and a short reference line stays in L1.

```sh
# Agent/operator only: plan which entries to move, biggest savings first.
omh memory demote [--file MEMORY.md] [--max 5]

# Capture the planned entries as review-first OMH candidates.
omh memory demote --stage
```

The plan selects entries no approved OMH record already explains, ranked by
size; an entry an approved record covers is reported separately as deletable
rather than copied down twice, and each planned row carries its
`staging_status` (`unstaged`, `already_staged`, `previously_rejected`) so the
plan advertises exactly the work `--stage` would do. Each row carries a
prepared reference line — `[omh#<sha12>] <first 60 chars…>` — keyed by the
sha256 of the entry's exact bytes, the one identifier both stores can
compute, so the line survives the candidate → record lifecycle it points
into.

Demotion moves content intact or it refuses; it never quietly damages it.
An entry the 240-char summary bound would truncate is refused as
`summary_bound_exceeded` (split it to demote it), and one the
sensitive-content redactor would collapse is refused as
`redacted_cannot_demote` — both stay in L1, because the next documented step
deletes the L1 original and that must never happen to a copy that is not
intact. Staging is idempotent (`already_staged` / `previously_rejected`,
never a duplicate candidate), cleanly staged candidates go through the
ordinary review gates, and the payload counts `captured_count` vs
`refused_count`. Note the plan output prints entry text verbatim — it is the
operator's own local Hermes memory content, but treat the plan like that
content when capturing terminal transcripts.

The L1 half stays with Hermes: OMH reads Hermes memory and cannot change it.
After approving the staged candidates, ask Hermes to replace each original
entry with its reference line through Hermes's own memory tool. Everything on
this surface is `prepared_not_observed` until that happens. The reverse move
— promoting an approved OMH record up into L1 — remains the memory bridge's
`promotable` surface (`omh memory status`).

## Prepared Context Boundary

A prepared recall or handoff pack is OMH-local context, not proof that an
executor ran, a provider received content, a model used it, or review/CI/merge
happened. Keep source class, admission mode, retention class, revision, and
replay reason with the bounded preparation evidence.
