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
| `codebase-uml` (PR #1230) | planning | https://github.com/plantuml/plantuml | CLI flags/pragmas/size limits (docs, `src/main/java/net/sourceforge/plantuml/cli/CliFlag.java`) | GPL-3.0 (external tool, invoked not vendored) | 2026-09-01 | v1.2026.7 |
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
| `llm-app-dev` harness budgets (PR #1246) | delivery | https://github.com/DenisSergeevitch/agents-best-practices | `SKILL.md` + budget/permission references | MIT | 2026-09-01 | dace8b70c563 |
| `tech-debt-audit` (issue #1235) | maintenance | https://github.com/ksimback/tech-debt-skill | none — no license published, so link-only reference; content built from OMH's own audit spec | none | 2026-09-02 | 5a15c1ca4a92 |
| `strategy-brief` decision records (issue #1236) | strategy | https://github.com/wshobson/agents | `plugins/documentation-generation/skills/architecture-decision-records/SKILL.md` | MIT | 2026-09-02 | a30778f8c4e6 |
| `accessibility-audit` rule IDs + fix partition (issue #1261) | accessibility | https://github.com/Effeilo/claude-code-frontend-skills | `front-a11y/front-a11y-rules.md` (rule-ID scheme, severity split, auto-fixable partition) | MIT (see the API false-negative rule above) | 2026-09-02 | 3c9d5a0501ff |
| `agent-evaluation` self-evaluation loops (issue #1263) | evaluation | https://github.com/github/awesome-copilot | `skills/agentic-eval/SKILL.md` (loop shapes, stop rules, judging strategies) | MIT | 2026-09-02 | 6a8fa297b0fe |
| `frontend` web-vitals budgets (issue #1262) | frontend | https://github.com/rohitg00/awesome-claude-code-toolkit | `skills/frontend-excellence/SKILL.md` (CWV threshold table, field-vs-lab note) | Apache-2.0 | 2026-09-02 | ebdf1d596d2c |

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
