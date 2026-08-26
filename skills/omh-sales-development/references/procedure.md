# sales-development Specialist Procedure

Expert clarification questions:
- `account or segment`
  - English: Which account or customer segment should this sales work focus on?
  - Korean: 이 영업 작업은 어떤 계정 또는 고객 세그먼트에 집중해야 하나요?
- `available evidence`
  - English: Which available account or market evidence should anchor this sales work?
  - Korean: 어떤 계정 또는 시장 근거 자료를 이 영업 작업의 기반으로 삼아야 하나요?
- `buyer hypothesis`
  - English: Which buyer hypothesis should discovery test?
  - Korean: 발견 과정에서 어떤 구매자 가설을 검증해야 하나요?
- `sales objective`
  - English: Which sales objective should the next-step plan support?
  - Korean: 다음 단계 계획은 어떤 영업 목표를 지원해야 하나요?

## Procedure

Declared checks:
- `sales_evidence_hypothesis_separation_check`
- `sales_qualification_coverage_check`
- `sales_outreach_non_execution_check`
- `sales_next_step_ownership_check`

### `sales_scope_opportunity` (analysis)

Separate observed account evidence from buyer and problem hypotheses, and name the gaps that discovery must test.

- Input refs: `account or segment`, `available evidence`, `buyer hypothesis`, `sales objective`
- Output refs: `account/segment, buyer, problem, and evidence-gap brief`
- Check IDs: `sales_evidence_hypothesis_separation_check`

### `sales_plan_discovery` (production)

Build discovery and qualification questions that test the buyer hypothesis against the stated objective.

- Input refs: `account or segment`, `buyer hypothesis`, `sales objective`
- Output refs: `discovery-question and qualification framework`
- Check IDs: `sales_qualification_coverage_check`

### `sales_shape_narrative` (production)

Draft an evidence-bounded value narrative and objection hypotheses without presenting outreach as sent.

- Input refs: `available evidence`, `buyer hypothesis`, `sales objective`
- Output refs: `value narrative, objection hypotheses, and outreach-draft outline`
- Check IDs: `sales_evidence_hypothesis_separation_check`, `sales_outreach_non_execution_check`

### `sales_validate_next_steps` (validation)

Validate evidence and qualification coverage, then assign an owner and approval boundary to each non-executing next step.

- Input refs: `account or segment`, `available evidence`, `buyer hypothesis`, `sales objective`
- Output refs: `next-step/owner plan with CRM, approval, and source gaps explicit`
- Check IDs: `sales_evidence_hypothesis_separation_check`, `sales_qualification_coverage_check`, `sales_outreach_non_execution_check`, `sales_next_step_ownership_check`
