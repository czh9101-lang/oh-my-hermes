# Design direction iterations

Audience: wrappers, agents, and operators. This is a deterministic local control-plane contract, not a normal end-user shell workflow.

`design_direction_iteration/v1` adds bounded feedback rounds beside the closed `design_direction_set/v1`; it never changes the latter's shape. A root snapshot contains the exact closed set, a root-set digest, an externally supplied source-revision digest, a declared criteria revision/digest, and an immutable revision digest. Each later snapshot carries its parent digest, stable `<revision-digest>:<option-id>` references, structured feedback vocabulary, successor ancestry, score baseline, and nullable model telemetry.

## Operator CLI

Prepare a root, optionally writing a self-contained static vocabulary preview:

```sh
omh ops design-direction-iterations prepare \
  --surface workflow_screen --audience operator --primary-task decide --platform web --mode new \
  --context-reference design_system:design_ref_a1b2c3d4e5f60718:project_local \
  --option a:task_first:restrained_neutral:system_sans:single_column:progress_trace:placeholder_copy \
  --option b:evidence_first:contextual_accent:editorial_serif:split_panel:evidence_rail:generic_glass \
  --source-revision-digest <64-hex-source-digest> \
  --criteria-revision direction-fit-v1 --criteria-dimension clarity --score-threshold 90 \
  --html /tmp/directions.html
```

Use the returned `iteration_id`, current `revision_digest`, and stable `option_refs` for the remaining machine actions:

- `show <iteration-id> [--html path]` returns the complete trajectory or rewrites the current static preview.
- `revise <iteration-id> --parent-revision-digest <current> --feedback-reference <opaque-ref> --feedback-delta <vocabulary> ...` accepts only the rendered current parent. Successors use `kind:parent_id|parent_id:target_id` and must account for the complete parent and active child set.
- `select <iteration-id> --option-ref <current-stable-ref> [--remember-this]` accepts only a current option. `--remember-this` records a `memory-new` reviewed-memory request with the accepted revision digest; it creates no memory record and performs no automatic or global promotion.
- `stop <iteration-id> --reason blocked_evidence|blocked_capability|cancelled` records an explicit terminal outcome.

The static preview contains only the direction vocabulary and machine-readable root/current digests. It opens no browser, binds no server, makes no request, and cannot produce a visual-QA PASS.

## Hard policy

- Two to four active options come from the existing closed set validator.
- Four revision rounds and five snapshots including the root are absolute limits.
- Eight total generation, evaluation, and repair attempts are absolute; one schema repair is permitted across the entire trajectory.
- Manual/no-model rounds have zero attempts and null telemetry. Model calls whose token, cost, latency, or failure telemetry is unavailable retain null for that field; the core never fabricates zero usage.
- A revision must be materially different and provide complete preserved, revised, combined, introduced, or dropped ancestry. The same parent plus the same feedback identity is idempotent.
- A new criteria revision starts a new score baseline. Scores are comparable only when they have the same criteria digest and cover the same dimensions with the same evaluator and rubric revision.
- Acceptance, threshold, no-improvement, revision/model caps, blocked evidence/capability, and cancellation are explicit terminal reasons. Caps become `BLOCK/REVISE`, never PASS.

Direction-fit scores are advisory preference evidence. They neither prove implementation nor replace executable checks, fresh exact-lineage captures, existing visual scoring, accessibility review, or the existing visual-QA contract.

## Wrapper and contextual-feedback integration

The wrapper exposes demand-loaded prepare, show, revise, select, and reviewed-memory actions.
The public iteration core supplies the exact `machine_actions` rendered by both the CLI and wrapper;
the wrapper never reconstructs transitions, revision identities, or option identities.

Every mutating action is bound to the current `iteration_id` and `revision_digest`.
Selection additionally requires a current stable option reference. A revision requires an opaque
feedback reference, structured feedback delta, a complete successor map, and a materially different
closed direction set. Stale revision and option references are refused before a local write.

Natural-language feedback reaches the revision card only when trusted serialized wrapper-session
context names an active iteration and current revision. Generic feedback remains clarification.
The ordinary chat path does not import the action adapter or iteration core, create iteration storage,
add context, or start model, browser, provider, executor, or background work.

Only `memory_promotion.request` persisted by an explicit accepted `remember_this` selection can enter
the existing `capture_project_memory_candidate` review-first path. The candidate is thread-scoped,
forces review, retains the accepted revision digest as `source_ref`, and is idempotent by the same
accepted revision/thread identity. It never auto-approves, creates a global preference, merges recall
with current feedback, or writes Hermes-native memory.

Direction-fit remains advisory. It cannot become visual-QA PASS; visual QA still requires real
implementation and fresh exact-lineage captures.
