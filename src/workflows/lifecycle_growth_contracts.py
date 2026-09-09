from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any, Final

from .lifecycle_growth_analysis import (
    IN_FLIGHT_ANALYSIS_STATES,
    analysis_cancellation_hold_reasons,
    analysis_run_state,
    analysis_status_field,
    build_analysis_cancellation,
    build_analysis_status,
    route_lifecycle_analysis_request,
    select_latest_analysis_run,
)
from .lifecycle_growth_artifacts import (
    build_audience_trigger_policy,
    build_growth_experiment_plan,
    build_growth_handoff_disposition,
    build_lifecycle_growth_brief,
    build_lifecycle_safety_policy,
    validate_audience,
    validate_brief,
    validate_experiment,
    validate_handoff,
    validate_safety,
)
from .lifecycle_growth_launch import lifecycle_growth_evaluation_context
from .lifecycle_growth_readout import (
    build_growth_measurement_readout,
    derive_readout_disposition,
    validate_readout,
)
from .lifecycle_growth_safety import (
    build_step_outcome,
    build_throttle_grouping,
    derive_throttle_group_identity,
    route_lifecycle_workflow_mutation,
    workflow_mutation_hold_reasons,
)
from .lifecycle_growth_values import artifact_shape_errors, metadata_ref, metadata_refs


LIFECYCLE_GROWTH_READINESS_SCHEMA_VERSION: Final = "lifecycle_growth_readiness/v1"
LIFECYCLE_GROWTH_READOUT_SCHEMA_VERSION: Final = "lifecycle_growth_readout/v1"
LIFECYCLE_GROWTH_ENTRY_SCHEMA_VERSION: Final = "lifecycle_growth_entry/v1"
_LAUNCH_SCHEMAS: Final = {
    "brief": "lifecycle_growth_brief/v1",
    "audience": "audience_trigger_policy/v1",
    "safety": "lifecycle_safety_policy/v1",
    "experiment": "growth_experiment_plan/v1",
    "handoff": "growth_handoff_disposition/v1",
}


def validate_lifecycle_growth_artifact(record: Any) -> list[str]:
    """Return structural errors for one versioned lifecycle-growth artifact."""
    schema, errors = artifact_shape_errors(record)
    if not schema or not isinstance(record, Mapping):
        return errors
    validators = {
        "lifecycle_growth_brief/v1": validate_brief,
        "audience_trigger_policy/v1": validate_audience,
        "lifecycle_safety_policy/v1": validate_safety,
        "growth_experiment_plan/v1": validate_experiment,
        "growth_measurement_readout/v1": validate_readout,
        "growth_handoff_disposition/v1": validate_handoff,
    }
    validator = validators.get(schema)
    if validator is None:
        return errors + ["lifecycle growth artifact schema_version is unsupported"]
    return errors + validator(record)


def prepare_lifecycle_growth(artifacts: Mapping[str, Mapping[str, Any]]) -> dict[str, object]:
    """Prepare an approved first launch; an optional observed readout gates an existing run."""
    records, errors = _launch_records(artifacts)
    readout = artifacts.get("readout")
    if isinstance(readout, Mapping):
        if readout.get("schema_version") != "growth_measurement_readout/v1":
            errors.append("readout artifact has the wrong schema")
        elif validate_lifecycle_growth_artifact(readout):
            errors.append("readout artifact is invalid")
        else:
            records["readout"] = readout
    elif readout is not None:
        errors.append("readout artifact is invalid")
    if errors:
        return _readiness(errors)
    _require_same_lifecycle_growth_id(records, errors)
    _hold_for_safety(records["safety"], errors)
    _hold_for_audience(records["audience"], errors)
    _hold_for_experiment(records["experiment"], errors)
    _hold_for_handoff(records["handoff"], errors)
    _hold_for_analysis(records, errors)
    return _readiness(errors)


def evaluate_lifecycle_growth(
    experiment: Mapping[str, Any], readout: Mapping[str, Any], *, evaluation_context: object = None,
) -> dict[str, object]:
    """Evaluate supplied evidence; absent context preserves the original result.

    Optional reference/baseline context can only hold interpretation, never turn
    configuration, assignment, delivery or a launch proposal into exposure.
    """
    errors = _expected_errors(experiment, "growth_experiment_plan/v1", "experiment")
    errors.extend(_expected_errors(readout, "growth_measurement_readout/v1", "readout"))
    if not errors and experiment.get("lifecycle_growth_id") != readout.get("lifecycle_growth_id"):
        errors.append("experiment and readout lifecycle_growth_id differ")
    result = readout_lifecycle_growth(readout)
    observed_days = readout.get("runtime_days_observed")
    minimum_days = experiment.get("minimum_runtime_days")
    if isinstance(observed_days, int) and isinstance(minimum_days, int) and observed_days < minimum_days:
        result["disposition"] = "insufficient_data"
        result["interpretation_state"] = "HOLD"
        errors.append("minimum runtime has not elapsed")
    if errors:
        result["disposition"] = "insufficient_data"
        result["interpretation_state"] = "HOLD"
    return {
        "schema_version": LIFECYCLE_GROWTH_READOUT_SCHEMA_VERSION,
        "interpretation_state": result["interpretation_state"],
        "disposition": result["disposition"],
        "assignment_unit": experiment.get("assignment_unit", ""),
        "exposure_unit": experiment.get("exposure_unit", ""),
        "actual_exposure_count": result["actual_exposure_count"],
        "delivery_count": result["delivery_count"],
        "runtime_days_observed": result["runtime_days_observed"],
        "analysis_run_state": result["analysis_run_state"],
        "analysis_observed_at": result["analysis_observed_at"],
        "analysis_delay_state": result["analysis_delay_state"],
        "artifact_errors": _errors(result.get("artifact_errors")) + errors,
        "claim_boundary": "Assignment is not exposure; evaluation is derived from bounded caller-supplied metadata only.",
        **lifecycle_growth_evaluation_context(
            evaluation_context, displayed_count=max(0, _count(readout, "displayed_count")),
        ),
    }


def readout_lifecycle_growth(readout: Mapping[str, Any]) -> dict[str, object]:
    """Derive a readout disposition without claiming provider observation occurred."""
    errors = _expected_errors(readout, "growth_measurement_readout/v1", "readout")
    disposition = derive_readout_disposition(readout)
    return {
        "schema_version": LIFECYCLE_GROWTH_READOUT_SCHEMA_VERSION,
        "interpretation_state": "READY" if not errors and disposition == "ship" else "HOLD",
        "disposition": disposition if not errors else "insufficient_data",
        "actual_exposure_count": _count(readout, "displayed_count"),
        "delivery_count": _count(readout, "delivered_count"),
        "action_count": _count(readout, "acted_count"),
        "outcome_count": _count(readout, "outcome_count"),
        "runtime_days_observed": _count(readout, "runtime_days_observed"),
        "analysis_run_state": analysis_run_state(readout),
        "analysis_observed_at": analysis_status_field(readout, "observed_at"),
        "analysis_delay_state": analysis_status_field(readout, "delay_state"),
        "artifact_errors": errors,
        "claim_boundary": "This derived readout is not provider, delivery, display, outcome, or causal evidence. Analysis-run state and its observation time are reported as the provider observed them, and elapsed time never restates them.",
    }


def evaluate_lifecycle_growth_entry(audience: Mapping[str, Any], *, event_id_ref: str, prior_event_id_refs: Sequence[str], prior_exit_observed: bool, active_intervention_refs: Sequence[str]) -> dict[str, object]:
    """Apply idempotency, re-entry, and overlap policy to caller-supplied metadata only."""
    event_id = metadata_ref(event_id_ref, field="event_id_ref")
    prior_ids = metadata_refs(prior_event_id_refs, field="prior_event_id_refs", required=False)
    active = metadata_refs(active_intervention_refs, field="active_intervention_refs", required=False)
    errors = _expected_errors(audience, "audience_trigger_policy/v1", "audience")
    reasons: list[str] = errors.copy()
    if event_id in prior_ids:
        reasons.append("duplicate event id")
    if prior_ids and audience.get("reentry_policy") == "never":
        reasons.append("re-entry is disallowed")
    if prior_ids and audience.get("reentry_policy") == "after_exit_only" and not prior_exit_observed:
        reasons.append("re-entry requires observed exit")
    if active:
        reasons.append("overlapping intervention is active")
    return {
        "schema_version": LIFECYCLE_GROWTH_ENTRY_SCHEMA_VERSION,
        "verdict": "HOLD" if reasons else "ACCEPT",
        "event_id_ref": event_id,
        "idempotency_key_ref": audience.get("idempotency_key_ref", ""),
        "reason_codes": reasons,
        "claim_boundary": "This entry decision is metadata validation only; it does not enter an audience or execute a journey.",
    }


def _launch_records(artifacts: Mapping[str, Mapping[str, Any]]) -> tuple[dict[str, Mapping[str, Any]], list[str]]:
    records: dict[str, Mapping[str, Any]] = {}
    errors: list[str] = []
    for name, schema in _LAUNCH_SCHEMAS.items():
        record = artifacts.get(name)
        if not isinstance(record, Mapping):
            errors.append(f"{name} artifact is missing")
            continue
        errors.extend(_expected_errors(record, schema, name))
        records[name] = record
    return records, errors


def _expected_errors(record: Mapping[str, Any], schema: str, label: str) -> list[str]:
    if record.get("schema_version") != schema:
        return [f"{label} artifact has the wrong schema"]
    errors = validate_lifecycle_growth_artifact(record)
    return [f"{label} artifact is invalid"] if errors else []


def _require_same_lifecycle_growth_id(records: Mapping[str, Mapping[str, Any]], errors: list[str]) -> None:
    ids = {record.get("lifecycle_growth_id") for record in records.values()}
    if len(ids) != 1:
        errors.append("artifacts must share lifecycle_growth_id")


def _hold_for_safety(safety: Mapping[str, Any], errors: list[str]) -> None:
    for field in ("consent_state", "suppression_state", "frequency_state", "channel_eligibility_state", "quiet_hours_state", "locale_state", "legal_tenant_state"):
        if safety.get(field) != "eligible":
            errors.append(f"{field} is not eligible")
    errors.extend(workflow_mutation_hold_reasons(safety))


def _hold_for_audience(audience: Mapping[str, Any], errors: list[str]) -> None:
    if audience.get("audience_identity_state") != "known":
        errors.append("audience identity is unknown")
    if audience.get("canonical_event_state") != "known":
        errors.append("canonical event semantics are unknown")


def _hold_for_experiment(experiment: Mapping[str, Any], errors: list[str]) -> None:
    if experiment.get("approval_state") != "approved":
        errors.append("human approval is absent")
    if experiment.get("holdout_state") != "preserved":
        errors.append("holdout is not preserved")
    if experiment.get("data_health_state") != "healthy":
        errors.append("experiment data health is not healthy")


def _hold_for_analysis(records: Mapping[str, Mapping[str, Any]], errors: list[str]) -> None:
    """Hold on an unfinished analysis, and on a cancellation the run does not support.

    A supplied readout stands for an existing run. While its analysis is queued
    or running, preparing another one duplicates work that is already in
    flight, so readiness holds until the caller reconciles it. The cancellation
    cross-check is here rather than in either validator because it is the only
    place that sees the observed run state and the prepared handoff together.
    """
    readout = records.get("readout")
    if readout is None:
        return
    if readout_lifecycle_growth(readout)["interpretation_state"] == "HOLD":
        errors.append("observed readout interpretation is on hold")
    if analysis_run_state(readout) in IN_FLIGHT_ANALYSIS_STATES:
        errors.append("the latest analysis run is still in flight; reconcile it before preparing another")
    errors.extend(analysis_cancellation_hold_reasons(readout.get("analysis_status"), records["handoff"].get("analysis_cancellation")))


def _hold_for_handoff(handoff: Mapping[str, Any], errors: list[str]) -> None:
    if handoff.get("connector_evidence_state") != "observed_available":
        errors.append("connector is not observed available")


def _readiness(hold_reasons: list[str]) -> dict[str, object]:
    return {
        "schema_version": LIFECYCLE_GROWTH_READINESS_SCHEMA_VERSION,
        "verdict": "HOLD" if hold_reasons else "READY",
        "launch_ready": not hold_reasons,
        "hold_reasons": hold_reasons,
        "claim_boundary": "Readiness is a local validation result, not approval or external-effect evidence.",
    }


def _errors(value: object) -> list[str]:
    return value if isinstance(value, list) and all(isinstance(error, str) for error in value) else ["readout errors are invalid"]


def _count(record: Mapping[str, Any], field: str) -> int:
    value = record.get(field)
    return value if isinstance(value, int) and not isinstance(value, bool) else 0


__all__ = [
    "analysis_cancellation_hold_reasons",
    "build_analysis_cancellation",
    "build_analysis_status",
    "build_audience_trigger_policy",
    "build_growth_experiment_plan",
    "build_growth_handoff_disposition",
    "build_growth_measurement_readout",
    "build_lifecycle_growth_brief",
    "build_lifecycle_safety_policy",
    "build_step_outcome",
    "build_throttle_grouping",
    "derive_throttle_group_identity",
    "evaluate_lifecycle_growth",
    "evaluate_lifecycle_growth_entry",
    "prepare_lifecycle_growth",
    "readout_lifecycle_growth",
    "route_lifecycle_analysis_request",
    "route_lifecycle_workflow_mutation",
    "select_latest_analysis_run",
    "validate_lifecycle_growth_artifact",
]
