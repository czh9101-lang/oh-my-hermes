# finance-analysis Specialist Procedure

Expert clarification questions:
- `period`
  - English: Which reporting period should this finance analysis cover?
  - Korean: 이 재무 분석은 어느 기간을 대상으로 해야 하나요?
- `supplied finance source`
  - English: Which supplied finance source should anchor the analysis?
  - Korean: 어떤 제공된 재무 자료를 분석의 근거로 삼아야 하나요?
- `decision question`
  - English: Which decision should this finance analysis support?
  - Korean: 이 재무 분석은 어떤 의사결정을 지원해야 하나요?
- `calculation assumptions`
  - English: Which calculation assumptions should be applied or challenged?
  - Korean: 어떤 계산 가정을 적용하거나 검토해야 하나요?

## Procedure

Declared checks:
- `finance_source_boundary_check`
- `finance_calculation_reconciliation_check`
- `finance_assumption_traceability_check`
- `finance_decision_risk_check`

### `finance_scope_sources` (analysis)

Fix the period and source boundary before interpreting any amount, and label unavailable records explicitly.

- Input refs: `period`, `supplied finance source`
- Output refs: `period and source-boundary statement`
- Check IDs: `finance_source_boundary_check`

### `finance_analyze_variances` (analysis)

Reconcile comparable figures, calculate material variances, and keep supplied values separate from assumptions.

- Input refs: `supplied finance source`, `calculation assumptions`
- Output refs: `actual-versus-plan and variance narrative with calculation/assumption gaps`
- Check IDs: `finance_calculation_reconciliation_check`, `finance_assumption_traceability_check`

### `finance_register_risks` (production)

Record the cash, close, control, and decision risks supported by the bounded evidence.

- Input refs: `supplied finance source`, `decision question`
- Output refs: `cash, close, control, or decision-risk register`
- Check IDs: `finance_decision_risk_check`

### `finance_validate_brief` (validation)

Validate source and assumption traceability, then name unresolved decision questions and the appropriate review route.

- Input refs: `period`, `supplied finance source`, `decision question`, `calculation assumptions`
- Output refs: `decision questions and next route such as strategy-brief, data-analysis, or human finance review`
- Check IDs: `finance_source_boundary_check`, `finance_calculation_reconciliation_check`, `finance_assumption_traceability_check`, `finance_decision_risk_check`
