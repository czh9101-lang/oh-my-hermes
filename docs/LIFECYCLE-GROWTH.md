# Lifecycle-growth contract

Operator/runtime reference: [`docs/WORKFLOW-ARTIFACTS.md`](WORKFLOW-ARTIFACTS.md). Use `omh runtime workflow-artifact lifecycle-growth <operation> --input <json-file-or->`; `build` derives metadata, `validate` is structural, and readiness may remain `HOLD`.

`omh.workflows.lifecycle_growth_contracts` prepares bounded metadata-only
lifecycle-growth plans and evaluates caller-supplied observed readouts. It has
no provider client, audience resolver, message sender, scheduler, or flag
mutation path.

## Six bound artifacts

Every builder requires the same opaque `lifecycle_growth_id`; readiness rejects
wrong artifact slots or unequal IDs.

- `build_lifecycle_growth_brief` creates `lifecycle_growth_brief/v1` with the
  lifecycle stage, value-bearing behavior, target segment, baseline, available
  product surfaces, experiment budget, evidence handles, non-goals, and owner.
- `build_audience_trigger_policy` creates `audience_trigger_policy/v1` with
  stable identity and event-schema states, entry/exit/exclusion references,
  idempotency key, re-entry policy, and collision policy.
- `build_lifecycle_safety_policy` creates `lifecycle_safety_policy/v1` with
  independent consent, suppression, and eligibility states, explicit
  suppression precedence, preference policy, global/campaign frequency
  budget references, a nested throttle grouping record, and the workflow
  content mutation boundary (content state, mutation route, promotion
  decision, promotion result).
- `build_growth_experiment_plan` creates `growth_experiment_plan/v1` with
  treatment and control, sticky assignment, distinct assignment and actual
  exposure *events*, metric/guardrail/holdout policy, minimum runtime, pause
  and rollback conditions, data health, and approval state. Assignment and
  exposure units may be the same population unit.
- `build_growth_measurement_readout` creates `growth_measurement_readout/v1`
  with distinct funnel counts, provider delivery, actual exposure, data,
  runtime, and causal evidence references, a bounded list of per-step
  outcome records, a step trace state, and a nested analysis status record.
- `build_growth_handoff_disposition` creates `growth_handoff_disposition/v1`
  with typed connector/content/analytics/product/implementation actions,
  owner, approver, connector evidence state, timing, stop conditions, and a
  nested analysis cancellation handoff.

`validate_lifecycle_growth_artifact(record) -> list[str]` never raises for
malformed artifacts. It rejects raw-payload-shaped keys and returns structural,
count, evidence, and closed-state errors.

## Safety records

`omh.workflows.lifecycle_growth_safety` holds the provider-neutral records
adopted from the issue #1400 source review. None of them names a provider
storage key, feature flag, dashboard state, or enum.

- `build_throttle_grouping` keeps the configured key or expression
  (`key_kind` is `static_path` or `dynamic_expression`) apart from the value
  it resolved to, names a `recipient` or `tenant` scope, and derives
  `group_identity` from both. Two resolved dynamic values are two groups; a
  resolved value is never re-read as a second key, so a value that looks like
  a path still groups under its own expression. A static path with a
  `missing` value stays `ungrouped`; a dynamic expression with an `empty`
  value falls back to `default_window`. `window_reset_consequence` records
  whether adopting the grouping resets in-flight windows once. Validation
  recomputes the identity and rejects a forged one.
- `build_step_outcome` records one conditional step as `matched`
  (`step_proceeded`) or `skipped` (`step_skipped`) with a reason code and a
  fixed `evaluated_values_state` of `redacted`: the record has no slot for an
  evaluated value, and a secret-shaped or whole-context reason is folded into
  a digest handle. A readout carries at most eight of them plus
  `step_trace_state` (`recorded`, `write_failed`, `not_attempted`). The trace
  never feeds `delivery_count` or the disposition: a failed trace write does
  not fail a send, and a recorded trace is not delivery evidence.
- `route_lifecycle_workflow_mutation` reads the safety policy's
  `workflow_content_state`, `mutation_route`, `promotion_decision_state`, and
  `promotion_result_state`. Production content is `view_only`; the mutation
  target is always `development_draft`; the route is `READY` only for a draft
  with an `approved` promotion decision, and `promotion_result_state` stays
  `not_observed` until a provider result is observed. Readiness reuses the
  same reasons, so a production edit or an unpromoted draft holds a launch.

## Analysis-run state

`omh.workflows.lifecycle_growth_analysis` holds the provider-neutral records
adopted from the issue #1429 source review. A missing result used to be
indistinguishable from a queued, running, failed, canceled, or never-started
analysis; these records keep those apart. None of them names a provider model,
controller, endpoint, query state, or timeout.

- `build_analysis_status` records one run as exactly one of `not_started`,
  `queued`, `running`, `completed`, `failed`, `canceled`, or `unknown`, with a
  safe `run_ref`, the `observed_at` time that state was observed, the run's
  `elapsed_minutes`, and evidence references. A `not_started` record may carry
  `observed_at` — looking and finding nothing is an observation — but no run
  reference, elapsed time, or evidence. Every state except `not_started` and
  `unknown` requires an evidence handle.
- `delay_state` is derived, never told, and is a second axis rather than a
  substitute for the first. It is `not_applicable` for a settled or unstarted
  run, `unknown` for in-flight work with no supplied
  `service_expectation_minutes`, and otherwise `within_expectation` or
  `delayed`. OMH hard-codes no staleness cutoff, so a queued run older than
  any particular window stays queued and is reported as delayed, never as
  failed, canceled, or absent.
- `derive_readout_disposition` gates on the run after the rollback branch: an
  observed guardrail breach still outranks everything, an in-flight run makes
  the readout `review` however good the numbers look, and any state other than
  `completed` leaves them `insufficient_data`. Analysis-job runtime,
  experiment minimum runtime, and source-data freshness stay three separate
  questions: an experiment can satisfy its exposure window while its analysis
  is still queued, and a completed run can still read stale source data.
- `select_latest_analysis_run` prefers the newest in-flight run over a newer
  settled one, ordering by observed time with the supplied position as the
  tiebreak, and reports `in_flight`, `settled`, or `none` with the selected
  run's reference, state, observation time, and evidence.
  `route_lifecycle_analysis_request` holds a further analysis while that
  selection is in flight unless `reconciliation_state` is `reconciled`; a
  `READY` verdict means nothing observed blocks preparing another run, not
  that one may be started.
- `build_analysis_cancellation` keeps three states apart. `handoff_status` is
  `prepared_not_observed` on every record, including one carrying observations,
  because OMH only ever prepared the request. `acknowledgement_state` records
  an observed provider acceptance or rejection, and `result_state` records an
  observed terminal outcome; an observed result requires an acknowledgement,
  and `observed_canceled` requires an accepted request plus evidence.
  Readiness cross-checks the two artifacts: a `canceled` run state needs an
  observed cancellation result, an observed cancellation contradicts an
  in-flight state, and a `single_run` request naming another run holds.

## Launch, entry, and readout

`prepare_lifecycle_growth` consumes the five launch artifacts (`brief`,
`audience`, `safety`, `experiment`, and `handoff`). A first, approved experiment
can be locally `READY` without a fabricated readout. It returns `HOLD` for
unknown/ineligible consent, suppression, frequency, event semantics, identity,
or owner; absent holdout/approval/data health; unavailable connector evidence;
production read-only content or an unapproved promotion decision; wrong slot;
or mismatched lifecycle ID.

A supplied `readout` is optional at launch but represents an existing observed
run. Its stale data, unknown denominator, broken instrumentation, sample-ratio
mismatch, cross-exposure, or overlap pauses readiness, and so does a queued or
running analysis: preparing another one while work is in flight duplicates it.

`evaluate_lifecycle_growth_entry` is a pure metadata decision. It returns
`HOLD` for a duplicate event ID, disallowed or unexited re-entry, or active
overlap; it neither adds a person to an audience nor starts a journey.

`readout_lifecycle_growth` derives `ship`, `rollback`, `review`, or
`insufficient_data`. It requires non-zero eligible/displayed/outcome stages,
valid actual-exposure/runtime/causal references, and a valid causal method
before `ship`. `evaluate_lifecycle_growth` also compares observed runtime days
with the experiment's minimum runtime, so an early readout cannot ship. Both
derived results report `analysis_run_state`, `analysis_observed_at`, and
`analysis_delay_state` beside the counts.

All records have `prepared_not_observed` status. Evidence references identify
what a later caller asserts was observed; the records and derived local results
are not proof that a provider delivered, displayed, measured, canceled, or
caused an outcome. A prepared cancellation handoff in particular is not an
external mutation, a provider request, or a stopped analysis.

## Launch review (issue #1399)

`omh.workflows.lifecycle_growth_launch` adds three pure launch-review builders
that the CLI exposes as `audience`, `promote`, and `graduate`. Each returns its
own versioned `prepared_not_observed` record; none of them touches the six
artifact schemas above, so stored artifacts and `validate` behave exactly as
before. Exact input keys, bounds, and CLI exit codes live in
[`docs/WORKFLOW-ARTIFACTS.md`](WORKFLOW-ARTIFACTS.md).

- `build_launch_audience_review` (`launch_audience_review/v1`) reads ordered
  rules under `first_match` semantics. An unconditional rule at a 100 percent
  share shadows every later rule that shares the same evaluation domain,
  bucketing domain, and subject (`person`, `group`, or `device`); those rules
  are reported `reachable: false` and listed in `unreachable_rule_refs`. A
  partial share, a conditional rule, a different domain, or a different subject
  never shadows. Unknown semantics or null domains produce `reachable: null`
  and a `HOLD` verdict. Reachable means "not provably shadowed"; it is not
  membership, targeting, or exposure, and the record carries no exposure count.
  Variants, splits, partial shares, and the holdout exclusion share stay
  configuration.
- `build_launch_promotion_preflight` (`launch_promotion_preflight/v1`) compares
  a source and a target environment without copying anything. The target is
  always `disabled`. Dependencies already satisfied and dependencies a promotion
  would create are kept in disjoint lists; carried dependencies and schedules are
  empty unless the caller explicitly approves each carry. The existing
  development-draft promotion gate still applies through the optional `safety`
  block, and an approval is never an observed promotion result.
- `build_launch_graduation_check` (`launch_graduation_check/v1`) proposes a
  separate gate cleanup only when the caller supplies `complete` rollout, at
  least one evidence reference, and `satisfied` rollback conditions. Anything
  else is `not_proposed` with the naming reason. OMH never infers that a gate
  was deleted.
- `evaluate_lifecycle_growth(..., evaluation_context=...)` accepts an optional
  context with `experiment_reference_state` and `baseline_exposure_state`. A
  deleted reference is blocked and `insufficient_data`; an absent baseline and
  zero displayed exposure carry their own reason codes; neither is reported as
  a runtime outage. Omitting the context preserves the original output byte for
  byte, and a resolved or observed context can never upgrade an existing hold.

## Upstream review (issue #1399)

The five community commits cited by the issue were reviewed by read against
the PostHog default branch through `ae880d309f33eaf236cb4e46991f249a88e1c16e`.
Only the PostHog row in [`docs/SKILL-SOURCES.md`](SKILL-SOURCES.md) advanced;
the GrowthBook, Dittofeed, and Novu rows keep their earlier pins. No upstream
code, tool name, parameter name, endpoint, UI, or enterprise (`ee/`) file was
adopted, and no product module imports the upstream package.

Adopted as provider-neutral concepts:

- Ordered first-match rules with a same-scope catch-all making later rules
  unreachable, person/group/device bucketing, and a holdout carried as an
  exclusion share (audience review).
- A read-only promotion preflight that copies nothing, lands disabled, keeps
  satisfied and to-create dependencies distinct, and carries schedules or
  dependencies only on explicit approval (promotion preflight).
- Removing a gate after general availability as its own cleanup change rather
  than a permanent check (graduation check). The rollout-evidence and
  rollback-condition prerequisites are OMH's contract, not upstream evidence.
- A stale or deleted experiment reference reported as a validation error, and a
  zero-exposure baseline reported as no data rather than a server error
  (evaluation context).

Rejected or left out: card and result UI shapes, links to person or cohort
records, copy tool and parameter names, target-count limits, write-scope rules,
any device-level fallback assumption beyond the subject label, and the `ee/`
diffs, which were not read. Upstream reachability is a display warning, so OMH
labels it configuration analysis and never observed targeting.

## Integration boundary

The catalog workflow, routing, generated `skills/omh-lifecycle-growth/*`, and
source attribution exist. Wrapper and integration callers should call these
APIs, not duplicate their validation, and must not treat a local `READY`
response or a `prepared_not_observed` launch-review record as external
execution evidence.

Copy remains with `content-operator`, supplied-data calculations with
`data-analysis`, recurring scheduling with `automation-blueprint`, and external
sends or flag changes with `connector-operator`. A validated product change can
move to `product-brief`; a lifecycle hypothesis is not coding work by itself.
