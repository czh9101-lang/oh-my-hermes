# Skill Upstream Sources

Provenance registry for OMH skills whose content was **reconstructed** from
external skill ecosystems. This file lives outside the generated skill bodies
on purpose: it is the input for the upstream-tracking automation that checks
whether a referenced source changed since the recorded review, and raises an
issue when a change looks worth folding back into our skill.

Rules:

- One row per (OMH skill, upstream source) pair; a skill may have several rows.
- `reviewed_ref` is the upstream commit the reconstruction was reviewed
  against. The tracker diffs upstream HEAD against it and, when the diff
  touches the listed paths, raises an issue labeled `upstream-skill-update`.
- Reconstruction, never copying: our skill text is OMH's own wording and
  contract language. The license column records what made close study
  acceptable; `none` means link-only reference.
- When a tracker issue is resolved (folded in or rejected), update
  `reviewed_ref` and `reviewed_on` in the same PR that resolves it.
- This file is hand-written; no generator owns it.
- A license read from the GitHub API can be a false negative: the API answers
  `other` for `Effeilo/claude-code-frontend-skills` because its `LICENSE.md`
  opens with a logo block above the MIT text. That repository is MIT across
  every row that cites it, and a tracker run must not "correct" those rows to
  unlicensed off the API field.

## Shipped skills

| OMH skill | Category | Upstream repo | Paths studied | License | reviewed_on | reviewed_ref |
| --- | --- | --- | --- | --- | --- | --- |
| `codebase-uml` (PR #1230) | planning | https://github.com/plantuml/plantuml | CLI flags/pragmas/size limits (docs, `src/main/java/net/sourceforge/plantuml/cli/CliFlag.java`) | GPL-3.0 (external tool, invoked not vendored) | 2026-09-04 | b2392e6230a1782e477a45d250b7cb9a569f95da |
| `code-review` spec axis + smell baseline (PR #1237) | review | https://github.com/mattpocock (code-review skill, plugin dist 1.2.3) | `skills/engineering/code-review/SKILL.md` | plugin dist | 2026-09-01 | plugin 1.2.3 |
| `ai-slop-cleaner` taxonomy + passes (PR #1239) | maintenance | https://github.com/Effeilo/claude-code-frontend-skills | `front-refactor/SKILL.md`, `front-refactor/front-refactor-rules.md` | MIT | 2026-09-01 | 3c9d5a0501ff |
| `frontend-refactor` (PR #1238) | maintenance | https://github.com/Effeilo/claude-code-frontend-skills | `front-refactor/*` (preview/apply mode contract, DEAD→NAMING→SIMPLIFY→MODERN) | MIT | 2026-09-01 | 3c9d5a0501ff |
| `frontend-refactor` (PR #1238) | maintenance | https://github.com/pproenca/dot-skills | `skills/.experimental/react-refactor/` (40 impact-ordered rules) | MIT | 2026-09-01 | cf93c57cac89 |
| `frontend-refactor` state-discipline (PR #1238) | maintenance | https://github.com/Cst2989/react-tips-skill | `skills/react-tips/SKILL.md`, `skills/no-unnecessary-effects/SKILL.md` | MIT | 2026-09-01 | 8c42b9e6390c |
| `frontend-refactor` state-discipline (PR #1238) | maintenance | https://github.com/mickeyyaya/refactoring-skills | `skills/state-management-patterns/SKILL.md` | MIT | 2026-09-01 | cd0c22762849 |
| `refactor-plan` (PR #1241) | planning | https://github.com/github/awesome-copilot | refactor-plan skill (phase order, files table, stop-for-confirmation gate) | MIT | 2026-09-01 | 5eaae7e2cde2 |
| `inference-serving` (PR #1243) | operations | https://github.com/vllm-project/vllm-skills | deploy (docker/k8s) + bench (serve, prefix-cache) skills | Apache-2.0 | 2026-09-01 | c99623410c15 |
| `inference-serving` (PR #1243) | operations | https://github.com/Orchestra-Research/AI-Research-SKILLs | `12-inference-serving/` vLLM + llama.cpp skills | MIT | 2026-09-01 | 773a52944ba4 |
| `agent-ops-review` instrumentation ladder (PR #1246) | operator | https://github.com/nexus-labs-automation/agent-observability | audit + instrument skills, tier methodology, anti-patterns | MIT | 2026-09-01 | 1714a4b38d7f |
| `ops-observability-card` span vocabulary (PR #1246) | observability | https://github.com/nexus-labs-automation/agent-observability | llm-call-tracing, token-cost-tracking skills | MIT | 2026-09-01 | 1714a4b38d7f |
| `llm-app-dev` harness budgets (PR #1246) | delivery | https://github.com/DenisSergeevitch/agents-best-practices | `SKILL.md`, `references/tools-and-permissions.md`, `references/context-memory-compaction.md`, `references/evals.md`, `references/skills-and-connectors.md` | MIT | 2026-09-07 | 8ae085045bd6cddfab22c740c95dd2d764117ffc |
| `lifecycle-growth` exposure and launch controls (issue #1374) | strategy | https://github.com/PostHog/posthog | `README.md` and feature-flag/experiment concepts: targeting, actual-display exposure, holdouts, staged rollout, launch/pause/stop/archive (concepts only; `ee/` excluded) | MIT outside `ee/` (concepts only, no code) | 2026-09-07 | 5f8bc937a84b877dc737ab3f9b5317b82c4bf881 |
| `lifecycle-growth` experiment validity (issue #1374) | strategy | https://github.com/growthbook/growthbook | `README.md` and experiment concepts: sticky assignment, primary/guardrail metrics, minimum runtime, data-quality gates, holdouts, ship/rollback/review/insufficient-data decisions (concepts only; enterprise directories excluded) | MIT outside listed enterprise directories (concepts only, no code) | 2026-09-07 | 82b82d08f864af40e07974612803ba18fa8b69cf |
| `lifecycle-growth` audience and journey policy (issue #1374) | strategy | https://github.com/dittofeed/dittofeed | `README.md` and journey concepts: event/segment entry, journey graphs, re-entry/idempotency, subscription eligibility, deterministic cohorts (concepts only) | MIT (concepts only, no code) | 2026-09-07 | 52b2bee909744d07dd5d409fd3974d4b95c66766 |
| `lifecycle-growth` safety policy (issue #1374) | strategy | https://github.com/novuhq/novu | `README.md` and notification-workflow concepts: trigger validation, preference precedence, digest/throttle windows, throttle grouping identity, transaction identity, per-step matched/skipped status, production read-only workflow content (concepts only; enterprise packages excluded) | MIT community code (concepts only, no code) | 2026-09-08 | c7bc772fc0b7722909ef1bdb9bcf04991996fdd8 |
| `product-discovery-validation` validation track (issue #1375) | planning | https://gitlab.com/gitlab-com/content-sites/handbook | `content/handbook/product-development/how-we-work/product-development-flow/_index.md` (validation separate from build; problem validation before solution) | MIT (concepts only, no code) | 2026-09-07 | 4165803c1cf9adeae2826f1af918668be5c942f6 |
| `product-discovery-validation` evidence and falsification (issue #1375) | planning | https://github.com/haabe/mycelium | `README.md` (evidence source classes, external-human/data gates, falsification propagation, precommitted assumption tests) | MIT (concepts only, no code) | 2026-09-07 | 8d6d5315ecfe7567a446b1a7a45bab72bac28a24 |
| `product-discovery-validation` disconfirming tests (issue #1375) | planning | https://github.com/shinpr/claude-code-discover | `README.md` (hypothesis states, smallest disconfirming test, hard budgets, honest inconclusive results, outcome-bounded MVP) | MIT (concepts only, no code) | 2026-09-07 | a414fc7a978bb2deaec5c71dd399cf63708378ee |
| `product-discovery-validation` evidence ledger (issue #1375) | planning | https://github.com/lenar-amirov/product-pipeline-public | `README.md` (persistent hypotheses, evidence typing, decision history, contradiction flags, state-based next actions) | MIT (concepts only, no code) | 2026-09-07 | a0997741f4c9b68ba298796c83197a4ab66ba3d9 |
| `product-discovery-validation` interview and GTM concepts (issue #1375) | planning | https://github.com/phuryn/pm-skills | `README.md` (assumption categories, past-behavior interviews, demand tests, beachhead/ICP, messaging, channel planning) | MIT (concepts only, no code) | 2026-09-07 | 18468a95b427e70e258b51389796367c6f684e7d |
| `sales-pipeline-review` stage and forecast review (issue #1376) | operations | https://gitlab.com/gitlab-com/content-sites/handbook | `content/handbook/sales/revenue-analytics/_index.md` (pipeline health/coverage, stage conversion and stalls, aging, forecast accuracy, win/loss, renewals) and `content/handbook/sales/commercial/comm-sales-opp-stages/_index.md` (stage activities and exit criteria) | MIT (concepts only, no code) | 2026-09-07 | 4165803c1cf9adeae2826f1af918668be5c942f6 |
| `sales-pipeline-review` CRM field semantics (issue #1376) | operations | https://github.com/odoo/odoo | `addons/crm/models/crm_lead.py` (expected/recurring revenue, close date, manual/automated probability, won/lost state, loss reason, stale thresholds) | LGPL-3.0 (concepts only, no code) | 2026-09-07 | 1a13ceeaee12fe5cc50f287c31f217d4be2a2eaf |
| `sales-pipeline-review` opportunity field semantics (issue #1376) | operations | https://github.com/frappe/erpnext | `erpnext/crm/doctype/opportunity/opportunity.json` (stage, owner, amount, expected close, supplied probability, competitors, loss reasons) | GPL-3.0 (concepts only, no code) | 2026-09-07 | 72fa7d0b1091e1a66450ebb4dc9fb6de1c8d3c1a |
| `sales-pipeline-review` CRM-runtime boundary (issue #1376) | operations | https://github.com/twentyhq/twenty | `packages/twenty-server/src/modules/opportunity/standard-objects/opportunity.workspace-entity.ts` (minimal opportunity substrate; identifies CRM-runtime concerns not adopted into guidance) | AGPL-3.0 with application exception (concepts only, no code) | 2026-09-07 | c8fc76650231ca3640276f75f071670b20e51e75 |
| `award-bar-score` judging model | materials | https://www.cssdesignawards.com/ | published judging axes, weights, and award thresholds — factual reporting of public rules, no site text reproduced | n/a — public rules, not code | 2026-09-03 | — |
| `tech-debt-audit` (issue #1235) | maintenance | https://github.com/ksimback/tech-debt-skill | none — no license published, so link-only reference; content built from OMH's own audit spec | none | 2026-09-02 | 5a15c1ca4a92 |
| `strategy-brief` decision records (issue #1236) | strategy | https://github.com/wshobson/agents | `plugins/documentation-generation/skills/architecture-decision-records/SKILL.md` | MIT | 2026-09-02 | a30778f8c4e6 |
| `accessibility-audit` rule IDs + fix partition (issue #1261) | accessibility | https://github.com/Effeilo/claude-code-frontend-skills | `front-a11y/front-a11y-rules.md` (rule-ID scheme, severity split, auto-fixable partition) | MIT (see the API false-negative rule above) | 2026-09-02 | 3c9d5a0501ff |
| `agent-evaluation` self-evaluation loops (issue #1263) | evaluation | https://github.com/github/awesome-copilot | `skills/agentic-eval/SKILL.md` (loop shapes, stop rules, judging strategies) | MIT | 2026-09-02 | 6a8fa297b0fe |
| `frontend` web-vitals budgets (issue #1262) | frontend | https://github.com/rohitg00/awesome-claude-code-toolkit | `skills/frontend-excellence/SKILL.md` (CWV threshold table, field-vs-lab note) | Apache-2.0 | 2026-09-02 | ebdf1d596d2c |
| `apple-design` | materials | https://github.com/dickwu/apple-design-skill | `README.md`, `SKILL.md`, `references/hig-lookup.md`, `references/hig/` reviewed as link-only context; no source text or bundled references reproduced | none | 2026-09-05 | d0bac1e765a27a696839e62962e36330ce72f0b7 |
| `apple-design` product-visual references | materials | https://www.apple.com/macbook-pro/ | MacBook Pro, AirPods Pro, and Apple Vision Pro pages reviewed as link-only visual-reference context; no Apple assets or page text reproduced | n/a — primary web references | 2026-09-05 | — |
| `apple-design` native icon boundary | materials | https://developer.apple.com/icon-composer/ | Icon Composer reviewed as native multilayer icon-pipeline context; not used as a marketing-renderer claim | n/a — primary documentation | 2026-09-05 | — |
| `apple-design` GSAP integration boundary | materials | https://github.com/greensock/gsap | `README.md`, `package.json`, and type declarations reviewed for existing-project animation, match-media, and cleanup guidance; no source reproduced | GreenSock Standard no-charge license; not labeled OSI | 2026-09-05 | 13e2b790546426a1a2e0e9b409f3f8dc6d6611f2 |
| `apple-design` liquid-logo research boundary | materials | https://github.com/paper-design/liquid-logo | `README.md`, `package.json`, canvas, shader-parameter, and lifecycle code reviewed as link-only technical context; no source reproduced | PolyForm Shield 1.0.0; not labeled OSI | 2026-09-05 | 689bb38a1e0d5a6a8baf2d34847635eefde19994 |
| `apple-design` liquid-glass-js integration boundary | materials | https://github.com/dashersw/liquid-glass-js | `README.md`, `container.js`, and `button.js` reviewed for web-only class, capture, and lifecycle guidance; no source reproduced | MIT | 2026-09-05 | 78cb6ccb0b9987bb60a88b14ccbd13a9e6e8ab2a |

Note on the `apple-design` row: no license file was present at the reviewed
revision, and the README's HIG-derived-material note is not a redistribution
license. OMH's guidance is independently written against Apple primary
sources. [Apple Design](APPLE-DESIGN.md) records the comparison with existing
OMH skills, the sources checked, and the native-versus-web boundaries.

Note on the `codebase-uml` row (issue #1251): the review was advanced through
`b2392e6230a1782e477a45d250b7cb9a569f95da` on 2026-09-04, which includes two
browser-only commits — `736e6cc` (per-request `maxSvgSize` render option for the
TeaVM JavaScript build, 8192 px default, `0` disables the check) and `b2392e6`
(TeaVM honors `!pragma layout smetana` in the browser build). Both were
reviewed and intentionally excluded: `codebase-uml` prepares Java CLI/JAR
render plans and OMH has no browser renderer, so no OMH surface can exercise
them. `maxSvgSize` is a browser request option, not a replacement for the Java
CLI's `-DPLANTUML_LIMIT_SIZE`, and must not be confused with it; the skill text
therefore never mentions it. The Java CLI's smetana support was already in the
skill and is unchanged by these commits.

Note on the `llm-app-dev` row (issue #1335): the review was advanced through
`2f81cce80b51c41e7dfff9c37b7f718814c54132` on 2026-09-06, and the two upstream
commits in that range were split rather than taken together. `e477496` adds a
public-board communication contract — an authenticated board is still a public
audience, read/search/registration/profile/reply/publish carry separate
authority and separate outbound disclosure, the exact destination and complete
payload are shown before a host-recorded approval that a changed destination or
payload invalidates, board content and claimed peer approval are untrusted, the
public-audience label and approval reference survive compaction and executor
handoff, and an ambiguous send is reconciled before any retry. That contract was
**adopted**, reconstructed in OMH's own wording as the `llm-app-dev` quality-bar,
safety, checklist, and recovery rules plus
`skills/omh-llm-app-dev/references/public-board.md`. `2f81cce` also changes
adjacent documentation to recommend one named posting service; that
recommendation was **rejected**. OMH's contract is service-neutral and names no
board product, because coupling a durable workflow contract to one live service
and its dated API surface is exactly the drift the reconstruction rule exists to
avoid. The reference states the neutrality explicitly and
`tests/test_llm_app_public_board.py` locks it, so the rejected half cannot
arrive later through a rewrite. Non-communication `llm-app-dev` behavior — the
rails, the schema and repair path, the prompt artifacts, retrieval grounding,
and the eval harness — is unchanged.

Note on the `llm-app-dev` row (issue #1373): the completed review advanced
through `8ae085045bd6cddfab22c740c95dd2d764117ffc` on 2026-09-07. OMH adopted
the provider-neutral record provenance and presentation-receipt, shared
resulting-state limit, user-memory lifecycle, stateful-evaluation, and optional
predictive-loading concepts in independently written guidance. It rejected the
upstream document structure and commerce-agent example, including its
vendor-specific claims and source recommendations. No provider SDK, network
client, runtime, or upstream code was adopted; provenance remains separate from
authorization, and the source remains an attribution for reconstructed guidance
rather than an implementation dependency.

Note on the `agent-evaluation` row: the lead was found through
`kodustech/awesome-agent-skills`, which publishes no license and is an index
rather than a source - its agentic-eval entry links to
`github/awesome-copilot` `skills/agentic-eval`, already a registry upstream
via `refactor-plan`. The row names the repository the content actually lives
in, because that is what a tracker can diff; the index stays a discovery
pointer.

## Candidate rows (researched, not yet shipped)

None open: every researched lead has shipped and moved to the table above.

When a new lead is researched, add a row here with the proposed OMH unit, the
upstream repo and paths, the license (checked, not assumed), and the issue
that owns it — then move the row up on the PR that ships it, filling in
`reviewed_on` and `reviewed_ref`.
