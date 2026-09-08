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
  outcome records, and a step trace state.
- `build_growth_handoff_disposition` creates `growth_handoff_disposition/v1`
  with typed connector/content/analytics/product/implementation actions,
  owner, approver, connector evidence state, timing, and stop conditions.

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
mismatch, cross-exposure, or overlap pauses readiness.

`evaluate_lifecycle_growth_entry` is a pure metadata decision. It returns
`HOLD` for a duplicate event ID, disallowed or unexited re-entry, or active
overlap; it neither adds a person to an audience nor starts a journey.

`readout_lifecycle_growth` derives `ship`, `rollback`, `review`, or
`insufficient_data`. It requires non-zero eligible/displayed/outcome stages,
valid actual-exposure/runtime/causal references, and a valid causal method
before `ship`. `evaluate_lifecycle_growth` also compares observed runtime days
with the experiment's minimum runtime, so an early readout cannot ship.

All records have `prepared_not_observed` status. Evidence references identify
what a later caller asserts was observed; the records and derived local results
are not proof that a provider delivered, displayed, measured, or caused an
outcome.

## Integration boundary

A serialized shared-registration owner must still add the catalog workflow,
English/Korean routing and exclusions, harness/projection coverage, generated
skill output, source attribution, and count updates. It should call these APIs,
not duplicate their validation. It must not treat a local `READY` response as
external execution evidence.

Copy remains with `content-operator`, supplied-data calculations with
`data-analysis`, recurring scheduling with `automation-blueprint`, and external
sends or flag changes with `connector-operator`. A validated product change can
move to `product-brief`; a lifecycle hypothesis is not coding work by itself.
