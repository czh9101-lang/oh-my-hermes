# legal-compliance-review Specialist Procedure

Expert clarification questions:
- `jurisdiction`
  - English: Which jurisdiction should this legal or compliance review apply to?
  - Korean: 이 법률 또는 컴플라이언스 검토는 어느 관할권을 기준으로 해야 하나요?
- `document or process version`
  - English: Which document or process version is in scope for review?
  - Korean: 어떤 문서 또는 프로세스 버전을 검토 범위로 삼아야 하나요?
- `supplied authority`
  - English: Which supplied authority should inform this review?
  - Korean: 어떤 제공된 근거 자료를 이 검토에 반영해야 하나요?
- `review objective`
  - English: Which review objective or decision should the issue matrix support?
  - Korean: 이슈 매트릭스는 어떤 검토 목표 또는 의사결정을 지원해야 하나요?

## Procedure

Declared checks:
- `legal_jurisdiction_authority_check`
- `legal_version_traceability_check`
- `legal_issue_matrix_completeness_check`
- `legal_counsel_escalation_check`

### `legal_scope_authority` (analysis)

Fix the jurisdiction, version, supplied authority, and evidence boundary before identifying issues.

- Input refs: `jurisdiction`, `document or process version`, `supplied authority`
- Output refs: `jurisdiction, document/version, authority, and evidence-boundary statement`
- Check IDs: `legal_jurisdiction_authority_check`, `legal_version_traceability_check`

### `legal_map_requirements` (analysis)

Map each relevant clause, control, or requirement to its supported rationale, owner, and unresolved question.

- Input refs: `document or process version`, `supplied authority`, `review objective`
- Output refs: `clause/control/requirement matrix with issue, rationale, owner, and open question`
- Check IDs: `legal_issue_matrix_completeness_check`

### `legal_rank_escalations` (production)

Rank supported issues by decision impact and separate remediation options from questions requiring counsel.

- Input refs: `jurisdiction`, `review objective`
- Output refs: `risk-ranked negotiation, remediation, or counsel-escalation brief`
- Check IDs: `legal_counsel_escalation_check`

### `legal_validate_review` (validation)

Validate version and authority traceability, matrix completeness, and the boundary between supplied evidence and interpretation.

- Input refs: `jurisdiction`, `document or process version`, `supplied authority`, `review objective`
- Output refs: `review checklist that distinguishes supplied evidence from legal interpretation`
- Check IDs: `legal_jurisdiction_authority_check`, `legal_version_traceability_check`, `legal_issue_matrix_completeness_check`, `legal_counsel_escalation_check`
