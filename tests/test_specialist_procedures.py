from __future__ import annotations

from dataclasses import replace
from pathlib import Path
import unittest

from _local_package import load_local_package

load_local_package()
from omh.skills.catalog import builtin_definitions
from omh.skills.catalog_types import ExpertQuestion, ProcedureStep
from omh.skills.packaging import builtin_skill_reference_templates
from omh.skills.procedure_validation import procedure_violation_ids
from omh.skills.render import builtin_skill_templates, workflow_reference_payload


TARGETS = frozenset(
    {
        "finance-analysis",
        "legal-compliance-review",
        "sales-development",
        "curriculum-design",
    }
)

DOMAIN_REVIEW_CONTRACTS = {
    "finance-analysis": {
        "outputs": (
            "finance_scope_source_record/v1",
            "finance_reconciliation_analysis_schedule/v1",
            "finance_risk_register/v1",
            "finance_decision_brief/v1",
        ),
        "steps": (
            "finance_scope_sources",
            "finance_reconcile_sources",
            "finance_analyze_variances",
            "finance_interpret_conditionally",
            "finance_validate_brief",
        ),
        "checks": {
            "finance_scope_comparability_check": {"entity_perimeter", "period_cutoff", "currency_units", "accounting_basis", "comparator_version", "close_status", "source_provenance"},
            "finance_source_reconciliation_check": {"totals_status", "account_mapping_status", "basis_units_status", "cutoff_status", "duplicate_missing_status", "tie_out_status", "unreconciled_gaps"},
            "finance_policy_assumption_check": {"formula_provenance", "policy_provenance", "materiality_status", "fx_allocation_treatment", "assumption_approval_status"},
            "finance_conditional_interpretation_check": {"analysis_applicability", "revenue_bridge_status", "receivables_dso_status", "working_capital_status", "unavailable_evidence"},
            "finance_validation_escalation_check": {"recalculation_status", "reconciliation_status", "source_conflicts", "control_exceptions", "high_impact_assumptions", "disposition", "escalation_owner"},
        },
    },
    "legal-compliance-review": {
        "outputs": (
            "legal_scope_authority_record/v1",
            "legal_issue_traceability_matrix/v1",
            "legal_risk_counsel_hold_register/v1",
            "legal_review_disposition/v1",
        ),
        "steps": (
            "legal_scope_facts_instruments",
            "legal_trace_authority",
            "legal_map_issues_exceptions",
            "legal_apply_counsel_holds",
            "legal_validate_disposition",
        ),
        "checks": {
            "legal_scope_facts_instruments_check": {"actors_roles", "operative_facts", "instrument_set", "order_of_precedence", "governing_law_forum", "regulatory_jurisdictions", "execution_effective_as_of_dates", "assumptions_blockers"},
            "legal_authority_citation_check": {"source_type", "source_identifier", "source_version", "effective_status", "pinpoint", "operative_text_summary", "verification_status"},
            "legal_issue_matrix_check": {"applicability_facts", "obligation_position", "definitions_dependencies", "exceptions_carveouts_conflicts", "evidence_status", "risk_uncertainty", "action_owner", "recommended_disposition", "counsel_question", "issue_family_applicability"},
            "legal_counsel_hold_check": {"trigger_ids", "impact", "likelihood_applicability", "urgency", "evidence_confidence", "reversibility", "hold_status", "counsel_owner"},
            "legal_final_determination_guard": {"invented_authority_status", "stale_authority_status", "unresolved_triggers", "disposition"},
        },
    },
    "sales-development": {
        "outputs": (
            "sales_opportunity_evidence_record/v1",
            "sales_qualification_state/v1",
            "sales_draft_sequence/v1",
            "sales_handoff_disposition/v1",
        ),
        "steps": (
            "sales_scope_account_evidence",
            "sales_build_qualification_state",
            "sales_check_sequence_eligibility",
            "sales_prepare_draft_sequence",
            "sales_validate_handoff",
        ),
        "checks": {
            "sales_account_evidence_check": {"fit_disqualifiers", "offer_use_case", "account_stage_owner", "stakeholder_states", "problem_current_approach_impact", "source_locator_date_reliability_permission", "contradictions", "unknowns", "claim_evidence_state"},
            "sales_qualification_state_check": {"stakeholder_authority_state", "problem_current_state", "measurable_impact", "decision_criteria_process", "alternatives", "timing_urgency", "risks_blockers", "champion_economic_buyer_hypotheses", "prioritized_questions", "buyer_confirmation_evidence", "disposition"},
            "sales_sequence_eligibility_check": {"consent_basis", "privacy_constraints", "suppression_status", "channel_eligibility", "policy_constraints", "audience_persona", "timing_cadence", "evidence_backed_personalization", "approved_proof", "purpose_value_cta", "objection_hypothesis", "validation_question", "owner_approver", "stop_opt_out_reply_conditions", "draft_status"},
            "sales_handoff_check": {"proposed_confirmed_status", "action", "owner", "approver", "target_timing", "success_exit_criterion", "dependencies", "evidence_refs", "crm_object_field_value_proposals", "unresolved_gaps", "disposition"},
        },
    },
    "curriculum-design": {
        "outputs": (
            "curriculum_learner_outcome_brief/v1",
            "curriculum_alignment_map/v1",
            "curriculum_sequence_design/v1",
            "curriculum_validation_disposition/v1",
        ),
        "steps": (
            "curriculum_frame_learners_outcomes",
            "curriculum_define_evidence_criteria",
            "curriculum_design_sequence_scaffolds",
            "curriculum_validate_alignment",
            "curriculum_revise_revalidate",
        ),
        "checks": {
            "curriculum_intake_readiness_check": {"learner_setting", "baseline_evidence", "motivation_goals", "language_culture", "access_variability", "outcome_performance_conditions_criteria_transfer", "prerequisite_misconception_diagnostic_remediation", "delivery_policy_constraints"},
            "curriculum_outcome_evidence_alignment_check": {"outcome_id", "performance_condition_criterion", "assessment_evidence", "rubric_criteria", "formative_checks", "coverage_status", "orphan_mismatch_insufficient_evidence"},
            "curriculum_scaffolding_inclusion_check": {"activation_diagnosis", "modeling_examples", "guided_practice", "feedback", "independent_transfer", "scaffold_removal", "accessible_formats_interactions", "language_cultural_support", "technology_barriers", "accommodations_flexible_paths", "equivalent_demonstration", "barrier_addressed"},
            "curriculum_validation_revision_check": {"criterion_id", "status", "exact_gaps", "learner_impact", "required_revision", "owner_decision", "unresolved_evidence", "revalidation_checks", "review_pilot_plan", "evidence_state"},
        },
    },
}


class SpecialistProcedureContractTests(unittest.TestCase):
    def setUp(self) -> None:
        self.definitions = {definition.name: definition for definition in builtin_definitions()}
        self.finance = self.definitions["finance-analysis"]

    def test_locked_targets_have_complete_machine_contracts(self) -> None:
        procedure_targets = {
            name for name, definition in self.definitions.items() if definition.procedure_steps
        }
        self.assertEqual(procedure_targets, TARGETS)

        for name in sorted(TARGETS):
            definition = self.definitions[name]
            with self.subTest(name=name):
                self.assertEqual(procedure_violation_ids(definition), [])
                self.assertTrue(all(isinstance(step, ProcedureStep) for step in definition.procedure_steps))
                self.assertEqual(
                    {question.required_input for question in definition.expert_questions},
                    set(definition.required_inputs),
                )
                self.assertEqual(
                    {ref for step in definition.procedure_steps for ref in step.input_refs},
                    set(definition.required_inputs),
                )
                self.assertEqual(
                    {ref for step in definition.procedure_steps for ref in step.output_refs},
                    set(definition.expected_outputs),
                )
                self.assertTrue(any(step.kind == "validation" for step in definition.procedure_steps))

    def test_missing_required_input_reference_fails_independently(self) -> None:
        omitted = self.finance.required_inputs[-1]
        steps = tuple(
            replace(step, input_refs=tuple(ref for ref in step.input_refs if ref != omitted))
            for step in self.finance.procedure_steps
        )
        self.assertEqual(
            procedure_violation_ids(replace(self.finance, procedure_steps=steps)),
            ["procedure_missing_required_input_ref"],
        )

    def test_missing_expected_output_reference_fails_independently(self) -> None:
        omitted = self.finance.expected_outputs[-1]
        fallback = self.finance.expected_outputs[0]
        steps = tuple(
            replace(
                step,
                output_refs=tuple(ref for ref in step.output_refs if ref != omitted) or (fallback,),
            )
            for step in self.finance.procedure_steps
        )
        self.assertEqual(
            procedure_violation_ids(replace(self.finance, procedure_steps=steps)),
            ["procedure_missing_expected_output_ref"],
        )

    def test_duplicate_step_id_fails_independently(self) -> None:
        steps = (
            self.finance.procedure_steps[0],
            replace(
                self.finance.procedure_steps[1],
                step_id=self.finance.procedure_steps[0].step_id,
            ),
            *self.finance.procedure_steps[2:],
        )
        self.assertEqual(
            procedure_violation_ids(replace(self.finance, procedure_steps=steps)),
            ["procedure_duplicate_step_id"],
        )

    def test_unknown_input_output_and_check_refs_fail_independently(self) -> None:
        first = self.finance.procedure_steps[0]
        mutations = (
            (
                replace(first, input_refs=(*first.input_refs, "unknown-input")),
                "procedure_unknown_input_ref",
            ),
            (
                replace(first, output_refs=(*first.output_refs, "unknown-output")),
                "procedure_unknown_output_ref",
            ),
            (
                replace(first, check_ids=(*first.check_ids, "unknown-check")),
                "procedure_unknown_check_id",
            ),
        )
        for mutated, expected in mutations:
            with self.subTest(expected=expected):
                steps = (mutated, *self.finance.procedure_steps[1:])
                self.assertEqual(
                    procedure_violation_ids(replace(self.finance, procedure_steps=steps)),
                    [expected],
                )

    def test_missing_required_input_question_coverage_fails_independently(self) -> None:
        self.assertEqual(
            procedure_violation_ids(
                replace(self.finance, expert_questions=self.finance.expert_questions[:-1])
            ),
            ["procedure_missing_required_input_question"],
        )

    def test_unknown_required_input_question_fails_independently(self) -> None:
        questions = (
            *self.finance.expert_questions,
            ExpertQuestion("unknown-input", "Unknown?", "알 수 없나요?"),
        )
        self.assertEqual(
            procedure_violation_ids(replace(self.finance, expert_questions=questions)),
            ["procedure_unknown_required_input_question"],
        )

    def test_no_validation_step_fails_independently(self) -> None:
        steps = tuple(replace(step, kind="analysis") for step in self.finance.procedure_steps)
        self.assertEqual(
            procedure_violation_ids(replace(self.finance, procedure_steps=steps)),
            ["procedure_missing_validation_step"],
        )

    def test_domain_review_contracts_define_outputs_order_and_check_results(self) -> None:
        self.assertEqual(set(DOMAIN_REVIEW_CONTRACTS), TARGETS)
        for name, expected in DOMAIN_REVIEW_CONTRACTS.items():
            definition = self.definitions[name]
            checks = {check.check_id: check for check in definition.procedure_checks}
            with self.subTest(name=name):
                self.assertEqual(definition.expected_outputs, expected["outputs"])
                self.assertEqual(tuple(step.step_id for step in definition.procedure_steps), expected["steps"])
                self.assertEqual(set(checks), set(expected["checks"]))
                for check_id, required_fields in expected["checks"].items():
                    self.assertEqual(set(checks[check_id].required_result_fields), required_fields)

    def test_check_result_field_mutations_fail_independently(self) -> None:
        first = self.finance.procedure_checks[0]
        fixtures = (
            (
                replace(first, required_result_fields=()),
                "procedure_check_result_fields_required",
            ),
            (
                replace(
                    first,
                    required_result_fields=(first.required_result_fields[0], first.required_result_fields[0]),
                ),
                "procedure_duplicate_or_invalid_check_result_field",
            ),
        )
        for mutated, expected in fixtures:
            with self.subTest(expected=expected):
                checks = (mutated, *self.finance.procedure_checks[1:])
                self.assertEqual(
                    procedure_violation_ids(replace(self.finance, procedure_checks=checks)),
                    [expected],
                )

    def test_machine_payload_and_shipped_target_bytes_match_catalog(self) -> None:
        payloads = {item["name"]: item for item in workflow_reference_payload()["skills"]}
        templates = {template.name: template.content for template in builtin_skill_templates()}
        references = {
            template.skill_name: template.content
            for template in builtin_skill_reference_templates()
            if template.relative_path == "references/procedure.md"
        }
        self.assertEqual(set(references), TARGETS)

        for name in sorted(TARGETS):
            definition = self.definitions[name]
            expected_steps = [
                {
                    "step_id": step.step_id,
                    "kind": step.kind,
                    "input_refs": list(step.input_refs),
                    "output_refs": list(step.output_refs),
                    "check_ids": list(step.check_ids),
                    "instruction": step.instruction,
                }
                for step in definition.procedure_steps
            ]
            with self.subTest(name=name):
                expected_checks = [
                    {
                        "check_id": check.check_id,
                        "required_result_fields": list(check.required_result_fields),
                        "instruction": check.instruction,
                    }
                    for check in definition.procedure_checks
                ]
                self.assertEqual(payloads[name]["procedure_checks"], expected_checks)
                self.assertEqual(payloads[name]["procedure_steps"], expected_steps)
                shipped = Path(f"skills/omh-{name}/SKILL.md").read_text(encoding="utf-8")
                shipped_reference = Path(f"skills/omh-{name}/references/procedure.md").read_text(encoding="utf-8")
                self.assertEqual(shipped, templates[name])
                self.assertEqual(shipped_reference, references[name])


if __name__ == "__main__":
    unittest.main()
