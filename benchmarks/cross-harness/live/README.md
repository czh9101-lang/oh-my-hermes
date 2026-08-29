# Cross-harness live lane

AUDIENCE: agent/maintainer. This is not the normal human OMH workflow.

The live lane is a controller that **executes** `cross_harness_benchmark/v1` work
and then submits only what it observed. It emits two artifacts:

1. an ordinary `cross_harness_benchmark_cli_input/v1` envelope, scored by the
   same production parser, evaluator, and scorer as any mailed-in file; and
2. a `cross_harness_live_receipt/v1` receipt binding that envelope's digest to
   the observations behind it, an authenticity tier, and the run's efficiency
   facts.

`cross_harness_benchmark/v1` is untouched. The corpus, the submission schema,
the scoring semantics, and both production trust anchors are unchanged; this
directory adds a producer, not a contract. The v1 scorer still returns
`evidence_authenticity: "unverified_submission"` and `execution_verified: false`
for every envelope the controller emits, because those fields describe what the
offline evaluator can prove. Controller provenance lives only in the receipt.

## Modes

| Mode | Executes | Requires | Tier it can reach |
| --- | --- | --- | --- |
| `fake` (default) | nothing | — | `fake_adapter` |
| `probe` | the corpus command binding (`python3 -m omh.cli harness validate`), locally and free | — | `controller_observed` |
| `dispatch` | the command binding plus one isolated Hermes child | `--allow-paid-live` **and** `--max-paid-calls N` | `controller_observed` |

`fake` mode starts no process. Its fixture results are simulated, and they are
submitted with `evidence_class: "prepared"`, which is below every fixture's
required class. A fake run therefore cannot produce a passing fixture, a level
above 1, or a certification — that property is a test, not a promise.

`dispatch` mode runs exactly one child through the approved explicit boundary,
`omh coding hermes-child dispatch --confirm-dispatch`, with the prompt on stdin
only. It refuses before any subprocess when the paid flags are absent, when the
budget is below the scheduled call count, or when model/provider metadata is
missing. Passing `--allow-paid-live` outside `dispatch` mode is also refused, so
the flag can never be left set by habit.

## Observable fixtures

The controller submits a fixture result only when it observed that fixture's
predicate value itself. Four of the fifteen v1 fixtures are observable:

| Fixture | Predicate | Observed from |
| --- | --- | --- |
| `evidence-command-binding` | `actual_machine.semantic_result` | the executed command binding's exit and machine result |
| `ultrawork-child-propagation` | `actual_machine.parent_exit` | the dispatch process exit |
| `ultrawork-observed-runtime` | `facts.dispatch_state` | the `routing_observation/v1` status |
| `evidence-runtime-observation` | `facts.observation_state` | whether a valid routing observation was produced |

The other eleven are offline metadata facts this controller does not execute.
They are simply absent from the submission, so the v1 evaluator reports them
`unsupported` — a visible coverage gap, never a pass. `--base ENVELOPE` fills
them from an existing submission; the receipt then records each carried fixture
as `carried_from_base` with no observation ids and downgrades the tier to
`mixed_controller_and_submitted`.

## Authenticity tiers

Ordered weakest to strongest:

`fake_adapter` → `unverified_submission` → `mixed_controller_and_submitted` → `controller_observed`

`unverified_submission` is exactly the claim v1 always makes. The controller may
report a stronger tier only for results it executed, and the receipt validator
rejects a tier that outruns its own coverage lists.

## Efficiency is not quality

The receipt's `efficiency` block reports `duration_ms`, `tokens`, and `cost_usd`
for the run. It never earns points, never changes a level, and never turns a
`partial` into a `pass`; it is not part of the envelope at all. Telemetry the
child did not report stays `null` and is never estimated, and
`observations_reporting_tokens` / `observations_reporting_cost_usd` say how many
observations actually supplied each figure.

## Commands

Run from the repository root.

```sh
PYTHONPATH=. python3 benchmarks/cross-harness/live/bench.py doctor

PYTHONPATH=. python3 benchmarks/cross-harness/live/bench.py run --mode fake

PYTHONPATH=. python3 benchmarks/cross-harness/live/bench.py run \
  --mode probe \
  --envelope-output artifacts/live-envelope.json \
  --receipt-output artifacts/live-receipt.json

python3 -m omh.cli benchmark report --input artifacts/live-envelope.json
```

Live dispatch, explicit on every axis:

```sh
PYTHONPATH=. python3 benchmarks/cross-harness/live/bench.py run \
  --mode dispatch --allow-paid-live --max-paid-calls 1 \
  --model <model-alias> --provider <provider-alias>
```

Exit status: `0` when every observation completed, `1` when an observation
failed, `2` for a refusal or contract error (the payload carries the reason
code). Scoring exit status comes from `omh.cli benchmark score/report` as usual.

## Privacy

Artifacts are metadata-only: stable ids, digests, reason codes, and bounded
machine facts. The dispatch task text is passed on stdin and never persisted;
command stdout is read for the machine result and then discarded; workspace and
home paths never enter an artifact. Runtime homes are redirected into a
temporary root for the duration of the run.

## Claim boundary

Controller observation covers only the fixtures listed in
`controller_observed_fixture_ids`. It is not general live executor quality proof,
and a `mixed_controller_and_submitted` receipt does not make its carried results
observed. Report the level and coverage with the receipt's tier beside the
score's `evidence_authenticity` and `execution_verified`; never merge them.
