# Workflow artifact runtime CLI

Agent and operator surface only. Normal Hermes users should ask Hermes for the outcome; this command exists for wrappers and integrations that need a deterministic, metadata-only artifact boundary.

## Command

```sh
omh runtime workflow-artifact <workflow> <operation> --input <json-file-or->
```

`--input -` reads one JSON object from stdin. Named files and stdin are capped at 262144 bytes. The CLI never accepts JSON on argv, never calls a provider, creates no subprocess, and has no scheduling, campaign-send, CRM-mutation, prototype-execution, or production-promotion operation.

The command returns `workflow_artifact_operation_result/v1`. It does not echo the input envelope. Every producer keeps its own prepared-versus-observed claim boundary.

## Closed operations

| Workflow | Operations | Explicit durable operation |
| --- | --- | --- |
| `decision-prototype` | `prepare`, `validate`, `observe`, `receipt`, `handoff`, `persist` | `persist` writes the validated existing prototype artifact store. |
| `lifecycle-growth` | `build`, `prepare`, `validate`, `evaluate`, `readout` | `build` derives the five prepared artifacts from semantic fields; returned JSON is the durable serializable artifact. |
| `product-discovery-validation` | `build`, `prepare`, `validate`, `audience-gate`, `evaluate`, `handoff`, `append` | `build` derives the five pre-decision artifacts and their hashes; `append` uses the existing append-only discovery store. |
| `sales-pipeline-review` | `prepare`, `validate`, `evaluate`, `handoff` | None; returned JSON is the durable serializable artifact. |

Unsupported workflow/operation pairs are parser errors. `validate` dispatches to the producer's schema-specific validator: lifecycle has its six artifact schemas, and sales has scope, health, forecast, outcome-learning, renewal-risk, and handoff validators.

`product-discovery-validation audience-gate` reads one `discovery_decision_frame/v1` and returns `discovery_audience_gate/v1`. An unknown, synthetic-only, or non-recruitable segment keeps evidence work open and blocks `product-brief`, `decision-prototype`, and `coding-handoff`; the result names the missing audience evidence. It is a read, never a promotion.

`decision-prototype handoff` always uses `build_decision_receipt_handoff(..., target_workflow="ralplan")`. `product-discovery-validation handoff` uses the same seam with `target_workflow="product-brief"`. A blocked or unresolved receipt remains blocked; neither handoff grants production authority.

## Readiness and authority

`build` accepts semantic field groups only. It derives schema versions, prepared statuses, claim boundaries, and discovery artifact hashes through the existing public builders; callers must not supply those generated fields. `lifecycle-growth build` returns the five artifacts that `prepare` consumes. `product-discovery-validation build` returns only the five pre-decision artifacts; a decision receipt can be created only by `evaluate`.

Validation is not readiness. For example, an `audience_trigger_policy/v1` or `lifecycle_safety_policy/v1` that structurally records `consent_state: "unknown"` can validate, but `lifecycle-growth prepare` returns `HOLD`, not a launch-ready result. Similarly, sales handoffs remain proposed with an unavailable connector, and prototype observations are accepted only as adapter-supplied bounded observations.

Preparation does not write state. Persist only when the producer already owns a store and the caller invokes `persist` or `append` explicitly. The stores receive validated artifact metadata only; this CLI never stores a raw input body, raw export, message body, transcript, command body, or execution claim by default.

## Complete JSON examples

These repository-local files are complete, synthetic-only inputs derived from the public builders and typed sales input. The focused CLI test executes each exact file through a temporary OMH and Hermes home.

| Workflow and operation | Input file | Expected machine state |
| --- | --- | --- |
| `decision-prototype prepare` | [`examples/workflow-artifacts/decision-prototype-prepare.json`](../examples/workflow-artifacts/decision-prototype-prepare.json) | `prepared_not_observed` |
| `lifecycle-growth build` | [`examples/workflow-artifacts/lifecycle-growth-build-semantic.json`](../examples/workflow-artifacts/lifecycle-growth-build-semantic.json) | derived valid artifacts; `prepare` returns `HOLD` |
| `product-discovery-validation build` | [`examples/workflow-artifacts/product-discovery-validation-build-semantic.json`](../examples/workflow-artifacts/product-discovery-validation-build-semantic.json) | derived valid package; `evaluate` returns `inconclusive` |
| `sales-pipeline-review prepare` | [`examples/workflow-artifacts/sales-pipeline-review-prepare-ready.json`](../examples/workflow-artifacts/sales-pipeline-review-prepare-ready.json) | `READY` |

Run them from the repository root:

```sh
uv run python -m omh.cli --omh-home "$OMH_HOME" --hermes-home "$HERMES_HOME" \
  runtime workflow-artifact decision-prototype prepare \
  --input examples/workflow-artifacts/decision-prototype-prepare.json

uv run python -m omh.cli --omh-home "$OMH_HOME" --hermes-home "$HERMES_HOME" \
  runtime workflow-artifact lifecycle-growth build \
  --input examples/workflow-artifacts/lifecycle-growth-build-semantic.json

uv run python -m omh.cli --omh-home "$OMH_HOME" --hermes-home "$HERMES_HOME" \
  runtime workflow-artifact product-discovery-validation build \
  --input examples/workflow-artifacts/product-discovery-validation-build-semantic.json

uv run python -m omh.cli --omh-home "$OMH_HOME" --hermes-home "$HERMES_HOME" \
  runtime workflow-artifact sales-pipeline-review prepare \
  --input examples/workflow-artifacts/sales-pipeline-review-prepare-ready.json
```

The prototype file declares a bounded planned command line and its expected metadata observation. It contains neither a script body nor a transcript, and the result does not claim that the command ran. The lifecycle and discovery files are semantic builder inputs: they contain no schema version, status, claim boundary, or discovery artifact id. The discovery input deliberately contains only `synthetic` evidence, so evaluating its built package demonstrates `inconclusive`, not customer validation.

The CLI wraps each operation payload under `result`. To use a built lifecycle result with `prepare`, pass that `result` object as the next input. To evaluate a built discovery package, pass `{"package": <build result>, "now": "<ISO-8601 timestamp>"}`; `build` cannot mint a decision receipt.

For stdin, use `--input -`; named files and stdin are both bounded. The selected OMH home is the only persistence location used by an explicit producer-owned operation.
