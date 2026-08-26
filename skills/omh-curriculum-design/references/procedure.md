# curriculum-design Specialist Procedure

Expert clarification questions:
- `learners`
  - English: Who are the learners this curriculum should serve?
  - Korean: 이 커리큘럼의 대상 학습자는 누구인가요?
- `learning goal`
  - English: Which observable learning goal should the curriculum achieve?
  - Korean: 이 커리큘럼은 어떤 관찰 가능한 학습 목표를 달성해야 하나요?
- `prerequisites`
  - English: Which learner prerequisites should the sequence assume?
  - Korean: 학습 순서는 어떤 선수 지식을 전제로 해야 하나요?
- `constraints`
  - English: Which delivery, time, accessibility, or resource constraints apply?
  - Korean: 어떤 운영, 시간, 접근성 또는 자원 제약이 적용되나요?

## Procedure

Declared checks:
- `curriculum_learner_alignment_check`
- `curriculum_outcome_assessment_alignment_check`
- `curriculum_prerequisite_constraint_check`
- `curriculum_accessibility_rights_check`

### `curriculum_frame_learners` (analysis)

Define observable outcomes against the learners, prerequisites, delivery setting, and known constraints.

- Input refs: `learners`, `learning goal`, `prerequisites`, `constraints`
- Output refs: `learner/audience, prerequisite, outcome, and constraint brief`
- Check IDs: `curriculum_learner_alignment_check`, `curriculum_prerequisite_constraint_check`

### `curriculum_sequence_learning` (production)

Order modules and activities so each stage builds the prerequisite knowledge needed for the next outcome.

- Input refs: `learners`, `learning goal`, `prerequisites`
- Output refs: `scope-and-sequence with modules/lessons and activity rationale`
- Check IDs: `curriculum_learner_alignment_check`

### `curriculum_design_assessment` (production)

Design formative and summative evidence that directly demonstrates each observable learning outcome.

- Input refs: `learning goal`, `constraints`
- Output refs: `formative/summative assessment rubric and completion evidence`
- Check IDs: `curriculum_outcome_assessment_alignment_check`

### `curriculum_validate_design` (validation)

Validate learner, outcome, assessment, accessibility, adaptation, and source-rights alignment before packaging or LMS work.

- Input refs: `learners`, `learning goal`, `prerequisites`, `constraints`
- Output refs: `accessibility, adaptation, and source/rights questions plus next route`
- Check IDs: `curriculum_learner_alignment_check`, `curriculum_outcome_assessment_alignment_check`, `curriculum_prerequisite_constraint_check`, `curriculum_accessibility_rights_check`
