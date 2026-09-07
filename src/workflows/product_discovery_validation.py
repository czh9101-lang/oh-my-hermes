"""Executable product-discovery preparation, evaluation, and durable re-entry."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import datetime
from pathlib import Path
from typing import Any, Final

from ..system.append_only_store import append_store_line
from ..system.local_store import file_lock, read_jsonl_objects
from ..system.paths import OmhPaths
from .product_discovery_artifacts import (
    ASSUMPTION_TEST_PORTFOLIO_SCHEMA_VERSION,
    CUSTOMER_DISCOVERY_PLAN_SCHEMA_VERSION,
    DISCOVERY_DECISION_FRAME_SCHEMA_VERSION,
    DISCOVERY_DECISION_RECEIPT_SCHEMA_VERSION,
    DISCOVERY_EVIDENCE_LEDGER_SCHEMA_VERSION,
    INITIAL_GTM_HYPOTHESIS_SCHEMA_VERSION,
    build_assumption_test_portfolio,
    build_customer_discovery_plan,
    build_discovery_decision_frame,
    build_discovery_decision_receipt,
    build_discovery_evidence_ledger,
    build_initial_gtm_hypothesis,
)
from .product_discovery_artifact_validation import validate_product_discovery_artifact


PRODUCT_DISCOVERY_STORE_NAME: Final = "product_discovery_artifacts.jsonl"
_EXTERNAL_EVIDENCE_CLASSES: Final = ("external_human", "behavioral_data")

__all__ = (
    "append_product_discovery_artifact",
    "build_assumption_test_portfolio",
    "build_customer_discovery_plan",
    "build_discovery_decision_frame",
    "build_discovery_evidence_ledger",
    "build_initial_gtm_hypothesis",
    "evaluate_product_discovery",
    "prepare_product_discovery",
    "product_brief_consumption",
    "read_product_discovery_artifacts",
    "validate_product_discovery_artifact",
)


def prepare_product_discovery(
    *,
    frame: Mapping[str, Any],
    ledger: Mapping[str, Any],
    plan: Mapping[str, Any],
    portfolio: Mapping[str, Any],
    gtm: Mapping[str, Any],
) -> dict[str, dict[str, Any]]:
    """Validate the five pre-decision artifacts as one discovery package."""
    artifacts = {"frame": frame, "ledger": ledger, "plan": plan, "portfolio": portfolio, "gtm": gtm}
    expected_schemas = {
        "frame": DISCOVERY_DECISION_FRAME_SCHEMA_VERSION,
        "ledger": DISCOVERY_EVIDENCE_LEDGER_SCHEMA_VERSION,
        "plan": CUSTOMER_DISCOVERY_PLAN_SCHEMA_VERSION,
        "portfolio": ASSUMPTION_TEST_PORTFOLIO_SCHEMA_VERSION,
        "gtm": INITIAL_GTM_HYPOTHESIS_SCHEMA_VERSION,
    }
    discovery_ids: set[str] = set()
    prepared: dict[str, dict[str, Any]] = {}
    for name, artifact in artifacts.items():
        errors = validate_product_discovery_artifact(artifact)
        if errors:
            raise ValueError(f"{name} is invalid: {errors[0]}")
        if artifact.get("schema_version") != expected_schemas[name]:
            raise ValueError(f"{name} has the wrong artifact type")
        discovery_ids.add(str(artifact["discovery_id"]))
        prepared[name] = dict(artifact)
    if len(discovery_ids) != 1:
        raise ValueError("all discovery artifacts must have the same discovery_id")
    frame_segment = str(prepared["frame"]["segment_ref"])
    if frame_segment != str(prepared["gtm"]["beachhead_segment_ref"]):
        raise ValueError("initial GTM beachhead must match the framed target segment")
    for assumption in prepared["portfolio"]["assumptions"]:
        if str(assumption["scope_segment_ref"]) != frame_segment:
            raise ValueError("assumption scope must match the framed target segment")
        if _time(str(assumption["deadline_at"])) > _time(str(prepared["frame"]["deadline_at"])):
            raise ValueError("assumption deadline must not exceed the decision-frame deadline")
    return prepared


def evaluate_product_discovery(package: Mapping[str, Mapping[str, Any]], *, now: str) -> dict[str, Any]:
    """Derive a conservative kill/pivot/persevere/inconclusive receipt."""
    required = {"frame", "ledger", "plan", "portfolio", "gtm"}
    if set(package) != required:
        raise ValueError("product discovery package keys are invalid")
    prepared = prepare_product_discovery(**package)
    frame = prepared["frame"]
    assumptions = prepared["portfolio"]["assumptions"]
    entries = prepared["ledger"]["entries"]
    if not isinstance(assumptions, list) or not isinstance(entries, list):
        raise ValueError("prepared discovery artifacts have invalid rows")
    _stamp(now, "now")
    evaluated_at = _time(now)
    outcomes = [_assumption_outcome(assumption, entries, evaluated_at=evaluated_at) for assumption in assumptions]
    failed = [outcome for outcome in outcomes if outcome["contradicted"]]
    rejected = [outcome["assumption_id"] for outcome in failed]
    if failed:
        decision = "kill" if any(outcome["failure_decision"] == "kill" for outcome in failed) else "pivot"
        problem_gate, route = "refuted", "product-discovery-validation"
    elif outcomes and all(outcome["validated"] for outcome in outcomes):
        decision, problem_gate, route = "persevere", "validated", "product-brief"
    else:
        decision, problem_gate, route = "inconclusive", "inconclusive", "product-discovery-validation"
    eligible_refs = [reference for outcome in outcomes for reference in outcome["eligible_refs"]]
    residual = ["risk-evidence-limits"]
    if any(outcome["timed_out"] for outcome in outcomes):
        residual.append("risk-test-deadline-expired")
    if decision == "inconclusive":
        residual.append("risk-unresolved-assumptions")
    return build_discovery_decision_receipt(
        discovery_id=str(frame["discovery_id"]),
        problem_ref=str(frame["problem_ref"]),
        segment_ref=str(frame["segment_ref"]),
        decision=decision,
        problem_gate=problem_gate,
        precommitted_test_ids=[str(assumption["test_id"]) for assumption in assumptions],
        eligible_evidence_refs=eligible_refs,
        rejected_hypothesis_ids=rejected,
        residual_risk_refs=residual,
        next_route=route,
    )


def _assumption_outcome(
    assumption: Mapping[str, Any], entries: Sequence[Mapping[str, Any]], *, evaluated_at: datetime
) -> dict[str, Any]:
    eligible: list[Mapping[str, Any]] = []
    seen_sources: set[str] = set()
    for entry in entries:
        if not _eligible(entry, assumption, evaluated_at=evaluated_at):
            continue
        source_ref = str(entry["source_ref"])
        if source_ref in seen_sources:
            continue
        seen_sources.add(source_ref)
        eligible.append(entry)
    contradiction = any(entry["direction"] == "contradicts" for entry in eligible)
    unresolved = any(entry["direction"] == "unresolved" for entry in eligible)
    support = sum(int(entry["sample_count"]) for entry in eligible if entry["direction"] == "supports")
    deadline = _time(str(assumption["deadline_at"]))
    return {
        "assumption_id": str(assumption["assumption_id"]),
        "contradicted": contradiction,
        "failure_decision": str(assumption["failure_decision"]),
        "timed_out": evaluated_at > deadline and not eligible,
        "validated": not contradiction and not unresolved and support >= int(assumption["sample_target"]),
        "eligible_refs": [str(entry["source_ref"]) for entry in eligible],
    }


def _eligible(entry: Mapping[str, Any], assumption: Mapping[str, Any], *, evaluated_at: datetime) -> bool:
    if str(entry["test_id"]) != str(assumption["test_id"]):
        return False
    if str(entry["segment_ref"]) != str(assumption["scope_segment_ref"]):
        return False
    if str(entry["source_class"]) not in _EXTERNAL_EVIDENCE_CLASSES:
        return False
    if str(entry["source_class"]) not in assumption["required_evidence_classes"]:
        return False
    if not bool(entry["representative"]) or entry["reentry"] != "reentered":
        return False
    if entry["confidence_limit"] != "bounded":
        return False
    if entry["observation_kind"] in {"source_pointer", "prototype_completion"}:
        return False
    criterion_by_direction = {
        "supports": assumption["success_criterion_ref"],
        "contradicts": assumption["failure_criterion_ref"],
        "unresolved": assumption["inconclusive_criterion_ref"],
    }
    if entry["criterion_ref"] != criterion_by_direction[entry["direction"]]:
        return False
    precommitted_at = _time(str(assumption["precommitted_at"]))
    observed_at = _time(str(entry["observed_at"]))
    deadline_at = _time(str(assumption["deadline_at"]))
    return precommitted_at <= observed_at <= deadline_at and observed_at <= evaluated_at


def product_brief_consumption(receipt: Mapping[str, Any]) -> dict[str, Any]:
    """Expose only a validated receipt's compact, transcript-free handoff."""
    if validate_product_discovery_artifact(receipt):
        return {}
    if receipt.get("schema_version") != DISCOVERY_DECISION_RECEIPT_SCHEMA_VERSION:
        return {}
    if receipt.get("decision") != "persevere" or receipt.get("problem_gate") != "validated":
        return {}
    return {
        "problem_ref": receipt["problem_ref"],
        "target_segment_ref": receipt["segment_ref"],
        "mvp_learning_boundary_ref": receipt["precommitted_test_ids"],
        "residual_risk_refs": receipt["residual_risk_refs"],
        "next_route": receipt["next_route"],
    }


def product_discovery_artifact_store_path(paths: OmhPaths) -> Path:
    """Locate the runtime-wide append-only artifact store without a new path primitive."""
    return paths.runtime_journal_dir / PRODUCT_DISCOVERY_STORE_NAME


def append_product_discovery_artifact(paths: OmhPaths, artifact: Mapping[str, Any]) -> dict[str, Any]:
    """Append one valid artifact so re-entry survives a process restart."""
    errors = validate_product_discovery_artifact(artifact)
    if errors:
        raise ValueError(errors[0])
    record = dict(artifact)
    path = product_discovery_artifact_store_path(paths)
    with file_lock(path, private=True):
        append_store_line(path, record)
    return record


def read_product_discovery_artifacts(paths: OmhPaths, *, discovery_id: str) -> list[dict[str, Any]]:
    """Read valid artifacts for one discovery identity in append order."""
    records, _ = read_jsonl_objects(product_discovery_artifact_store_path(paths))
    return [
        record
        for record in records
        if record.get("discovery_id") == discovery_id and not validate_product_discovery_artifact(record)
    ]


def _time(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _stamp(value: str, field: str) -> None:
    try:
        parsed = _time(value)
    except ValueError as exc:
        raise ValueError(f"{field} must be an ISO-8601 timestamp") from exc
    if parsed.tzinfo is None:
        raise ValueError(f"{field} must include a timezone")
