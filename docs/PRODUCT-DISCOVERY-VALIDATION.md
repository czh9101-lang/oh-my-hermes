# Product discovery validation

Operator/runtime reference: [`docs/WORKFLOW-ARTIFACTS.md`](WORKFLOW-ARTIFACTS.md). Use `omh runtime workflow-artifact product-discovery-validation <operation> --input <json-file-or->`; semantic `build` derives hashes and synthetic discovery remains `inconclusive`.

`omh.workflows.product_discovery_validation` is a local, metadata-only contract for the decision before a PRD. It does not recruit participants, contact customers, replay interviews, create a prototype, write a PRD, or execute an experiment.

## Artifact sequence

1. `discovery_decision_frame/v1` records the problem, segment, `segment_definition_state`, alternatives, owner, budget, deadline, and kill criteria as opaque references.
2. `customer_discovery_plan/v1` requires a human-owned participant handoff, consent/privacy pointer, bias controls, and re-entry. Its fixed interview focus is past behavior, current workaround, switching cost, and observed commitment.
3. `assumption_test_portfolio/v1` ranks value, usability, feasibility, viability, go-to-market, and ethics assumptions by `decision_impact * evidence_gap`. Each test precommits scope, sample, deadline, cost, owner, success/failure/inconclusive criteria, required evidence classes, and its failure decision (`kill` or `pivot`).
4. `discovery_evidence_ledger/v1` accepts only bounded references and labels its source class and limits. A pointer is not a fresh observation.
5. `initial_gtm_hypothesis/v1` keeps the beachhead, buyer/user distinction, alternative, value, pricing, channel, cohort, and learning metrics as hypotheses.
6. `discovery_decision_receipt/v1` records `kill`, `pivot`, `persevere`, or `inconclusive`, the problem gate, the audience state, `solution_work_permitted`, the missing audience evidence, eligible evidence references, rejected hypotheses, residual risks, and next route.

`evaluate_product_discovery()` treats `now` as the evaluation cutoff. It admits evidence only when it is re-entered after a matching precommit, references the matching precommitted success/failure/inconclusive criterion, is captured no later than both the test deadline and `now`, is within the matching scope, representative, externally human or behavioral, required by the test, and bounded in confidence. A completed observation captured within its window remains usable when evaluated later; an expired test with no eligible observation is `inconclusive` and carries `risk-test-deadline-expired`. Future observations or future precommits cannot validate a decision.

Each ledger has unique evidence IDs and one source reference per precommitted test, so a copied observation cannot multiply a sample. Synthetic, inferred, secondary, stakeholder-only, source-pointer, and prototype-completion material cannot satisfy the external customer gate. Any eligible `unresolved` entry tied to the precommitted inconclusive criterion prevents promotion; an eligible contradiction refutes only its own hypothesis and selects that hypothesis's precommitted `kill` or `pivot` decision. Missing, expired, out-of-scope, duplicate, future, or unreentered evidence produces `inconclusive`.

## Audience before build

`segment_definition_state` names the audience explicitly as `recruitable`, `behaviorally_observed`, `unknown`, `synthetic_only`, or `non_recruitable`. The first two are audience-defined; the other three are not.

`discovery_audience_gate(frame)` reads one prepared frame and returns `discovery_audience_gate/v1`. Evidence work always continues: an undefined audience still produces a frame, a customer discovery plan, and an evidence plan. A frame alone never permits solution work, so `solution_work_permitted` is false there; only a validated receipt can permit it. When the audience is undefined the gate lists `product-brief`, `decision-prototype`, and `coding-handoff` in `blocked_outputs`, names the outstanding work in `missing_audience_evidence_refs`, and keeps `next_route` at `product-discovery-validation`.

`build_discovery_decision_receipt()` derives `solution_work_permitted` and `missing_audience_evidence_refs` rather than accepting them, refuses `persevere` for an undefined audience, and refuses any route other than `product-discovery-validation` while solution work is blocked. A tampered receipt that flips either derived field fails validation. `evaluate_product_discovery()` therefore holds an undefined audience at `inconclusive` and adds `risk-audience-undefined`, while a contradiction still refutes its own hypothesis. Because eligible evidence must carry the assumption's `scope_segment_ref`, and that scope must equal the framed segment, evidence observed in one segment cannot validate another; a pivot needs a new decision frame, GTM beachhead, and assumption scope together.

## Product-brief handoff

`product_brief_consumption(receipt)` returns a compact handoff only for a valid `persevere` receipt with `problem_gate == "validated"` and `solution_work_permitted == true`. It contains opaque problem/segment references, the learning boundary, residual risks, and next route; it has no transcript field. Refuted, expired, malformed, or inconclusive records return `{}`.

## Durable re-entry

`append_product_discovery_artifact(paths, artifact)` writes a validated artifact to the existing runtime journal append-only primitive. `read_product_discovery_artifacts(paths, discovery_id=...)` reopens valid artifacts after a restart. The stored values are metadata-only references, not customer contacts, recordings, or raw observations.

## Integration boundary

The current executable API is intentionally not a CLI or registered skill. The serialized catalog/routing owner must register the `product-discovery-validation` workflow, English and Korean cues, sibling exclusions, generated projections, and awareness/card coverage. That owner must route any optional prototype experiment to issue #1371; a prototype completion is deliberately non-qualifying evidence here. Product-brief may consume only the compact receipt, and `idea-to-deploy` remains downstream of an accepted product brief and plan.
