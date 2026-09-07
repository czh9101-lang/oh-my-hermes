# Sales pipeline review executable contract

Operator/runtime reference: [`docs/WORKFLOW-ARTIFACTS.md`](WORKFLOW-ARTIFACTS.md). Use `omh runtime workflow-artifact sales-pipeline-review <operation> --input <json-file-or->`; preparation is proposed metadata only and never CRM mutation or approval.

The executable contract for issue #1376 lives in
`omh.workflows.sales_pipeline_review`. It accepts typed, in-memory metadata for
a caller-supplied CRM snapshot. It does not read, store, or synchronize a CRM
export.

## Public API

- `prepare_sales_pipeline_review(request)` validates every input boundary and
  returns `PreparedSalesPipelineReview`. Its status is `READY` or `HOLD`.
- `evaluate_sales_pipeline_review(request)` calls the preparation gate first.
  Health, forecast, and optional annex calculations remain absent on `HOLD`.
- `prepare_sales_pipeline_handoff(request)` accepts only a `READY` evaluation
  and emits proposed follow-ups and CRM corrections. The connector is always
  reported as unavailable by this core contract.
- `validate_sales_pipeline_artifact(record)` dispatches by schema version to
  all six public artifact validators. Individual validators are also exported.

Inputs are frozen dataclasses. Identifiers and evidence references are bounded,
opaque metadata tokens. Human names, email addresses, URLs, paths, transcripts,
message content, and raw exports do not belong in these contracts.

## Artifact surface

| Schema | Availability | Contract |
| --- | --- | --- |
| `sales_pipeline_scope/v1` | Always | Source/as-of/horizon, cohort, owner/motion scope, currency and amount semantics, supplied stage/forecast/scenario definitions, observed conversion bases, freshness, and HOLD gaps. |
| `sales_pipeline_health/v1` | READY only | Movement, aging, stalls, slips, exit gaps, next-step quality, concentration, and deal exceptions. |
| `sales_forecast_assessment/v1` | READY only | Seller category/probability, rule-derived scenarios, observed buyer commitments, supplied-probability weighting, matched-cohort calibration, confidence, and evidence limits. |
| `sales_outcome_learning_annex/v1` | Conditional | Bounded observed won/lost/unqualified counts and reasons, contradictions, gaps, and research follow-ups. |
| `sales_renewal_risk_annex/v1` | Conditional | Renewal-horizon health/utilization/support/budget/staffing signals, risks, gaps, contradictions, hypotheses, and owners. |
| `sales_pipeline_handoff/v1` | Explicit handoff | Proposed account follow-ups and complete object/field/value/evidence/owner/approval CRM corrections, with no connector effect claim. |

## Gate behavior

Preparation returns `HOLD` before calculation when the snapshot has invalid or
stale as-of metadata, an invalid review horizon, undefined amount/stage/forecast
or scenario semantics, duplicate opportunity IDs, missing or unknown owners,
unsupported probabilities, unsafe metadata, or mixed currencies without a
bounded observed conversion basis.

A stage never creates seller probability, forecast category, or buyer
commitment. Missing seller probability remains unavailable and is excluded from
the weighted seller scenario. Calibration is available only where a prior
forecast and observed outcome share the declared cohort, horizon, and an
unambiguous opportunity ID.

## Integration boundary

The later shared registration phase should import these APIs from
`omh.workflows.sales_pipeline_review` and call `evaluate_sales_pipeline_review`
before any workflow handoff. It should register the canonical
`sales-pipeline-review` catalog definition, English and Korean routing surfaces,
reciprocal sibling exclusions, harness/awareness metadata, source attribution,
and generated projections. Those shared files are intentionally not modified
in this issue lane.

Route account discovery or qualification to `sales-development`, qualitative
customer material to `feedback-triage`, generic calculations to
`data-analysis`, and authoritative finance reporting to `finance-analysis`.
The handoff artifact carries these routing boundaries as machine-consumed
values.

Core has no network client, persistence adapter, CRM mutation, sync, alert,
outreach, dashboard, revenue-booking, or authoritative finance capability.
Such an external action requires a separately approved connector and observed
result; this contract always reports it unavailable.
