"""Closed, metadata-only artifacts for product-discovery validation."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from datetime import datetime
from typing import Any, Final

from ..system.append_only_store import opaque_ref


DISCOVERY_DECISION_FRAME_SCHEMA_VERSION: Final = "discovery_decision_frame/v1"
DISCOVERY_EVIDENCE_LEDGER_SCHEMA_VERSION: Final = "discovery_evidence_ledger/v1"
CUSTOMER_DISCOVERY_PLAN_SCHEMA_VERSION: Final = "customer_discovery_plan/v1"
ASSUMPTION_TEST_PORTFOLIO_SCHEMA_VERSION: Final = "assumption_test_portfolio/v1"
DISCOVERY_DECISION_RECEIPT_SCHEMA_VERSION: Final = "discovery_decision_receipt/v1"
INITIAL_GTM_HYPOTHESIS_SCHEMA_VERSION: Final = "initial_gtm_hypothesis/v1"

EVIDENCE_SOURCE_CLASSES: Final = (
    "external_human",
    "behavioral_data",
    "internal_stakeholder",
    "secondary_research",
    "synthetic",
    "inferred",
)
ASSUMPTION_CATEGORIES: Final = ("value", "usability", "feasibility", "viability", "go_to_market", "ethics")
DECISIONS: Final = ("kill", "pivot", "persevere", "inconclusive")
PROBLEM_GATES: Final = ("validated", "refuted", "inconclusive")

_CLAIM_BOUNDARY: Final = (
    "This metadata-only artifact preserves bounded references and declared or derived discovery state. "
    "It does not recruit, contact, inspect customers, replay transcripts, build a prototype or PRD, execute a test, "
    "or establish product-market fit."
)


def _ref(value: str, field: str) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{field} must be a string")
    return opaque_ref(value, field=field, error=ValueError)


def _refs(values: Sequence[str], field: str, *, minimum: int = 1) -> list[str]:
    if not isinstance(values, Sequence) or isinstance(values, str):
        raise ValueError(f"{field} must be a list")
    if len(values) < minimum:
        raise ValueError(f"{field} must have at least {minimum} item")
    return [_ref(value, f"{field}[{index}]") for index, value in enumerate(values)]


def _stamp(value: str, field: str) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{field} must be a string")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"{field} must be an ISO-8601 timestamp") from exc
    if parsed.tzinfo is None:
        raise ValueError(f"{field} must include a timezone")
    return value


def _artifact(schema_version: str, discovery_id: str, fields: Mapping[str, Any], *, status: str) -> dict[str, Any]:
    safe_discovery_id = _ref(discovery_id, "discovery_id")
    digest = hashlib.sha256(
        json.dumps({"schema_version": schema_version, "discovery_id": safe_discovery_id, **fields}, sort_keys=True).encode()
    ).hexdigest()[:16]
    return {
        "schema_version": schema_version,
        "artifact_id": f"discovery-{digest}",
        "discovery_id": safe_discovery_id,
        "status": status,
        **fields,
        "claim_boundary": _CLAIM_BOUNDARY,
    }


def build_discovery_decision_frame(*, discovery_id: str, problem_ref: str, segment_ref: str, alternative_refs: Sequence[str], decision_owner_ref: str, learning_budget_ref: str, deadline_at: str, kill_criteria_refs: Sequence[str]) -> dict[str, Any]:
    """Prepare the bounded problem decision before evidence is accepted."""
    return _artifact(
        DISCOVERY_DECISION_FRAME_SCHEMA_VERSION,
        discovery_id,
        {
            "problem_ref": _ref(problem_ref, "problem_ref"),
            "segment_ref": _ref(segment_ref, "segment_ref"),
            "alternative_refs": _refs(alternative_refs, "alternative_refs"),
            "decision_owner_ref": _ref(decision_owner_ref, "decision_owner_ref"),
            "learning_budget_ref": _ref(learning_budget_ref, "learning_budget_ref"),
            "deadline_at": _stamp(deadline_at, "deadline_at"),
            "kill_criteria_refs": _refs(kill_criteria_refs, "kill_criteria_refs"),
        },
        status="prepared_not_observed",
    )


def build_discovery_evidence_ledger(*, discovery_id: str, entries: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """Prepare durable evidence re-entry rows without raw customer material."""
    if not isinstance(entries, Sequence) or isinstance(entries, str):
        raise ValueError("entries must be a list")
    normalized = [_evidence_entry(entry) for entry in entries]
    if not normalized:
        raise ValueError("entries must have at least 1 item")
    if len({entry["evidence_id"] for entry in normalized}) != len(normalized):
        raise ValueError("evidence_id values must be unique")
    if len({(entry["test_id"], entry["source_ref"]) for entry in normalized}) != len(normalized):
        raise ValueError("duplicate evidence source for one precommitted test")
    return _artifact(DISCOVERY_EVIDENCE_LEDGER_SCHEMA_VERSION, discovery_id, {"entries": normalized}, status="reentered")


def _evidence_entry(entry: Mapping[str, Any]) -> dict[str, Any]:
    if not isinstance(entry, Mapping):
        raise ValueError("evidence entry must be an object")
    source_class = entry.get("source_class")
    direction = entry.get("direction")
    observation_kind = entry.get("observation_kind")
    reentry = entry.get("reentry")
    confidence_limit = entry.get("confidence_limit")
    sample_count = entry.get("sample_count")
    representative = entry.get("representative")
    if source_class not in EVIDENCE_SOURCE_CLASSES:
        raise ValueError("evidence source_class is unsupported")
    if direction not in ("supports", "contradicts", "unresolved"):
        raise ValueError("evidence direction is unsupported")
    if observation_kind not in ("past_behavior", "current_workaround", "switching_cost", "observed_commitment", "source_pointer", "prototype_completion"):
        raise ValueError("evidence observation_kind is unsupported")
    if reentry not in ("reentered", "not_reentered") or confidence_limit not in ("bounded", "limited", "unknown"):
        raise ValueError("evidence reentry or confidence_limit is unsupported")
    if not isinstance(sample_count, int) or isinstance(sample_count, bool) or sample_count < 1:
        raise ValueError("evidence sample_count must be a positive integer")
    if not isinstance(representative, bool):
        raise ValueError("evidence representative must be a boolean")
    return {
        "evidence_id": _ref(entry.get("evidence_id", ""), "evidence_id"),
        "test_id": _ref(entry.get("test_id", ""), "test_id"),
        "source_class": source_class,
        "source_ref": _ref(entry.get("source_ref", ""), "source_ref"),
        "criterion_ref": _ref(entry.get("criterion_ref", ""), "criterion_ref"),
        "observed_at": _stamp(entry.get("observed_at", ""), "observed_at"),
        "segment_ref": _ref(entry.get("segment_ref", ""), "segment_ref"),
        "direction": direction,
        "observation_kind": observation_kind,
        "sample_count": sample_count,
        "representative": representative,
        "reentry": reentry,
        "confidence_limit": confidence_limit,
    }


def build_customer_discovery_plan(*, discovery_id: str, participant_criteria_ref: str, consent_privacy_ref: str, bias_control_refs: Sequence[str], human_task_ref: str) -> dict[str, Any]:
    """Prepare human-owned interview re-entry without simulating participants."""
    return _artifact(
        CUSTOMER_DISCOVERY_PLAN_SCHEMA_VERSION,
        discovery_id,
        {
            "participant_criteria_ref": _ref(participant_criteria_ref, "participant_criteria_ref"),
            "interview_focuses": ["past_behavior", "current_workaround", "switching_cost", "observed_commitment"],
            "consent_privacy_ref": _ref(consent_privacy_ref, "consent_privacy_ref"),
            "bias_control_refs": _refs(bias_control_refs, "bias_control_refs"),
            "human_task_ref": _ref(human_task_ref, "human_task_ref"),
            "evidence_reentry_required": True,
        },
        status="prepared_not_observed",
    )


def build_assumption_test_portfolio(*, discovery_id: str, assumptions: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """Precommit the smallest disconfirming test before accepting observations."""
    if not isinstance(assumptions, Sequence) or isinstance(assumptions, str):
        raise ValueError("assumptions must be a list")
    normalized = [_assumption(assumption) for assumption in assumptions]
    if not normalized:
        raise ValueError("assumptions must have at least 1 item")
    if len({row["test_id"] for row in normalized}) != len(normalized):
        raise ValueError("assumption test_id values must be unique")
    return _artifact(ASSUMPTION_TEST_PORTFOLIO_SCHEMA_VERSION, discovery_id, {"assumptions": normalized}, status="precommitted")


def _assumption(value: Mapping[str, Any]) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise ValueError("assumption must be an object")
    category = value.get("category")
    impact = value.get("decision_impact")
    gap = value.get("evidence_gap")
    sample_target = value.get("sample_target")
    failure_decision = value.get("failure_decision")
    classes = value.get("required_evidence_classes")
    if category not in ASSUMPTION_CATEGORIES:
        raise ValueError("assumption category is unsupported")
    if not isinstance(impact, int) or isinstance(impact, bool) or impact not in range(1, 6):
        raise ValueError("assumption decision_impact must be from 1 to 5")
    if not isinstance(gap, int) or isinstance(gap, bool) or gap not in range(1, 6):
        raise ValueError("assumption evidence_gap must be from 1 to 5")
    if not isinstance(sample_target, int) or isinstance(sample_target, bool) or sample_target < 1:
        raise ValueError("assumption sample_target must be positive")
    if failure_decision not in ("kill", "pivot"):
        raise ValueError("assumption failure_decision must be kill or pivot")
    if not isinstance(classes, Sequence) or isinstance(classes, str):
        raise ValueError("assumption required_evidence_classes must be a list")
    required_classes = list(classes)
    if not required_classes or any(item not in EVIDENCE_SOURCE_CLASSES for item in required_classes):
        raise ValueError("assumption required_evidence_classes are unsupported")
    return {
        "assumption_id": _ref(value.get("assumption_id", ""), "assumption_id"),
        "category": category,
        "decision_impact": impact,
        "evidence_gap": gap,
        "priority": impact * gap,
        "test_id": _ref(value.get("test_id", ""), "test_id"),
        "smallest_test_ref": _ref(value.get("smallest_test_ref", ""), "smallest_test_ref"),
        "success_criterion_ref": _ref(value.get("success_criterion_ref", ""), "success_criterion_ref"),
        "failure_criterion_ref": _ref(value.get("failure_criterion_ref", ""), "failure_criterion_ref"),
        "inconclusive_criterion_ref": _ref(value.get("inconclusive_criterion_ref", ""), "inconclusive_criterion_ref"),
        "failure_decision": failure_decision,
        "scope_segment_ref": _ref(value.get("scope_segment_ref", ""), "scope_segment_ref"),
        "sample_target": sample_target,
        "deadline_at": _stamp(value.get("deadline_at", ""), "deadline_at"),
        "cost_cap_ref": _ref(value.get("cost_cap_ref", ""), "cost_cap_ref"),
        "owner_ref": _ref(value.get("owner_ref", ""), "owner_ref"),
        "required_evidence_classes": required_classes,
        "precommitted_at": _stamp(value.get("precommitted_at", ""), "precommitted_at"),
    }


def build_initial_gtm_hypothesis(*, discovery_id: str, beachhead_segment_ref: str, buyer_ref: str, user_ref: str, current_alternative_ref: str, value_proposition_ref: str, pricing_hypothesis_ref: str, initial_channel_ref: str, first_cohort_ref: str, learning_metric_refs: Sequence[str]) -> dict[str, Any]:
    """Prepare the initial go-to-market hypothesis without inventing market proof."""
    safe_buyer = _ref(buyer_ref, "buyer_ref")
    safe_user = _ref(user_ref, "user_ref")
    if safe_buyer == safe_user:
        raise ValueError("buyer_ref and user_ref must be distinct")
    return _artifact(
        INITIAL_GTM_HYPOTHESIS_SCHEMA_VERSION,
        discovery_id,
        {
            "beachhead_segment_ref": _ref(beachhead_segment_ref, "beachhead_segment_ref"),
            "buyer_ref": safe_buyer,
            "user_ref": safe_user,
            "current_alternative_ref": _ref(current_alternative_ref, "current_alternative_ref"),
            "value_proposition_ref": _ref(value_proposition_ref, "value_proposition_ref"),
            "pricing_hypothesis_ref": _ref(pricing_hypothesis_ref, "pricing_hypothesis_ref"),
            "initial_channel_ref": _ref(initial_channel_ref, "initial_channel_ref"),
            "first_cohort_ref": _ref(first_cohort_ref, "first_cohort_ref"),
            "learning_metric_refs": _refs(learning_metric_refs, "learning_metric_refs"),
        },
        status="prepared_not_observed",
    )


def build_discovery_decision_receipt(*, discovery_id: str, problem_ref: str, segment_ref: str, decision: str, problem_gate: str, precommitted_test_ids: Sequence[str], eligible_evidence_refs: Sequence[str], rejected_hypothesis_ids: Sequence[str], residual_risk_refs: Sequence[str], next_route: str) -> dict[str, Any]:
    """Record a bounded decision that can be consumed without transcript replay."""
    if decision not in DECISIONS or problem_gate not in PROBLEM_GATES:
        raise ValueError("decision or problem_gate is unsupported")
    if (problem_gate == "validated") != (decision == "persevere"):
        raise ValueError("only a validated problem may persevere")
    return _artifact(
        DISCOVERY_DECISION_RECEIPT_SCHEMA_VERSION,
        discovery_id,
        {
            "problem_ref": _ref(problem_ref, "problem_ref"),
            "segment_ref": _ref(segment_ref, "segment_ref"),
            "decision": decision,
            "problem_gate": problem_gate,
            "precommitted_test_ids": _refs(precommitted_test_ids, "precommitted_test_ids"),
            "eligible_evidence_refs": _refs(eligible_evidence_refs, "eligible_evidence_refs", minimum=0),
            "rejected_hypothesis_ids": _refs(rejected_hypothesis_ids, "rejected_hypothesis_ids", minimum=0),
            "residual_risk_refs": _refs(residual_risk_refs, "residual_risk_refs", minimum=0),
            "next_route": _ref(next_route, "next_route"),
        },
        status="derived_from_supplied_metadata",
    )
