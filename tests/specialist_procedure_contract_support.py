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
