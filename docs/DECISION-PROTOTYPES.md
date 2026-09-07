# Decision prototypes

Operator/runtime reference: [`docs/WORKFLOW-ARTIFACTS.md`](WORKFLOW-ARTIFACTS.md). Use `omh runtime workflow-artifact decision-prototype <operation> --input <json-file-or->`; preparation and validation do not execute or approve work, and `persist` is explicit.

`decision_prototype/v1` is a bounded, disposable experiment used to settle one empirical planning decision. It is not delivery, QA certification, or permission to promote code.

## Public API

- `omh.workflows.decision_prototypes.prepare_decision_prototype(proposal)` validates and prepares one experiment.
- `validate_decision_prototype(value)` returns structural errors without executing or repairing state.
- `observe_decision_prototype(prototype, adapter_observation)` accepts exactly one result from `decision_prototype_observation/v1`; it never starts a command or executor.
- `compact_decision_prototype_receipt(prototype)` exposes planning-safe evidence without commands or transcripts.
- `omh.runtime.decision_prototypes.persist_decision_prototype(paths, prototype)` writes a validated artifact under `$OMH_HOME/runtime/decision-prototypes/`.
- `read_decision_prototype(paths, decision_id)` returns a valid persisted artifact or `None`.

A proposal declares one question ending in `?`, two or three alternatives, a falsifiable hypothesis, a task or opaque user reference, an experiment kind, and a bounded time/tool/file/command budget. `budget.command_count` must cover every declared command. The prepared handoff repeats the exact commands, expected observations, workspace identity, and budget; a persisted mismatch is invalid. Valid kinds are `wireframe`, `cli_spike`, `api_probe`, `fixture`, `timing_probe`, `test_harness`, and `mocked_interaction`.

The default and only accepted write boundary is `declared_workspace_only`, with a `scratch_directory` or `temporary_worktree` identity. The artifact carries the existing `worktree_session_isolation/v1` identifier, but it neither creates nor removes a worktree. Existing production files may be read by an adapter; production writes are not authorized.

## Observation contract

An available executor produces a result only through an explicit adapter observation:

```python
from omh.workflows.decision_prototypes import observe_decision_prototype

observed = observe_decision_prototype(
    prepared,
    {
        "adapter_contract": "decision_prototype_observation/v1",
        "adapter_id": "local-timing-adapter",
        "state": "observed",  # also: timeout, inconclusive
        "measurements": [{"metric": "latency-ms", "value": "12", "evidence_ref": "measurement-1"}],
        "interpretation": "The measurement is within the declared threshold.",
        "confidence": "medium",
        "unresolved_questions": ["Production load remains unmeasured."],
        "supported_option": "sqlite",
        "rejected_options": ["redis"],
        "decision": "discard",  # also: keep
        "cleanup_status": "observed",
        "prototype_code_ref": "scratch-cache-probe",
        "actual_workspace": {
            "identity": "scratch-cache-probe",
            "write_boundary": "declared_workspace_only",
        },
        "consumed_budget": {"time_seconds": 12, "tool_count": 1, "file_count": 1, "command_count": 1},
    },
)
```

`actual_workspace` must exactly match the declared scratch/worktree identity and boundary, and `consumed_budget` must be positive for time and commands while staying within every declared ceiling. `execution.evidence_class` is `prepared_not_observed` before adapter evidence and `adapter_observed` only when an adapter has supplied the one result. An `observed` option conclusion requires at least one bounded measurement with an evidence reference. `timeout` and `inconclusive` are observed limits, not product verdicts: they cannot select or reject an option, but preserve any genuinely observed partial measurements. An unavailable executor instead leaves `execution.status` as `prepared_not_observed` and supplies the declared commands and expected observations as a prepared handoff. Tool success alone is not product validation.

A `discard` decision requires adapter-observed cleanup. `failed` or `not_observed` cleanup leaves the artifact non-discarded. The artifact accepts only bounded metadata and opaque evidence references; it rejects transcript-shaped text, sensitive metadata, and raw email or phone values.

## Planning receipt and promotion gate

`compact_decision_prototype_receipt()` carries the context decision reference, execution state, supported/rejected options, confidence, residual risk, evidence references and limits, prototype-code reference, and promotion declaration. It intentionally excludes commands, measurements values, and any transcript replay.

Prototype code remains non-promotable by default. `production_code_permitted: true` validates only with all of:

- `accepted_plan_ref`
- `accepted_plan_status: "accepted"`
- `implementation_handoff_ref`

Those references are declarations in this contract; they are not evidence that implementation, review, CI, or merge occurred.

## Integration boundary

The executable surface is ready for the later shared registration phase. That phase must add the canonical skill/catalog definition, English and Korean routing coverage, generated workflow synchronization, and `ulw-context`/`ulw-plan` handoff adapters. Do not hand-edit generated skill projections. Until that registration lands, callers may use the Python API directly; no decision-prototype CLI command is registered by this lane.
