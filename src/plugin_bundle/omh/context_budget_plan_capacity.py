"""Parse capacity evidence once; derive only observed working capacity."""
from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime
from typing import assert_never

from ._governance_safety import contains_credential_like_material
from .context_budget_plan_types import (
    CAPACITY_FIELDS, Budget, BudgetPlanError, Evidence, EvidenceClass, MustKeep,
    Observations, RouteCapacity, RouteIdentity,
)


def metadata_ref(value: str, *, optional: bool = False) -> str:
    """Accept opaque route/source identifiers, not URLs, prose or credentials."""
    if optional and not value:
        return value
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:/@+-]{0,127}", value) or contains_credential_like_material(value):
        raise BudgetPlanError("invalid_metadata_ref")
    return value


def digest_json(raw: str) -> str:
    return "sha256:" + hashlib.sha256(raw.encode("utf-8")).hexdigest()


def valid_digest(value: str) -> bool:
    return bool(re.fullmatch(r"sha256:[0-9a-f]{64}", value))


def parse_route_capacity_input(raw: str) -> dict[str, Evidence]:
    """Missing evidence stays unknown; supplied evidence must be self-consistent.

    Timestamps are caller evidence clocks, not a fresh provider observation by
    OMH. No implicit TTL or documentation lookup can upgrade their authority.
    """
    data = json.loads(raw)
    if not isinstance(data, dict) or data.get("schema_version") != "route_capacity_input/v1" or set(data) - {"schema_version", *CAPACITY_FIELDS}:
        raise BudgetPlanError("invalid_capacity_schema")
    result: dict[str, Evidence] = {}
    for name in CAPACITY_FIELDS:
        item = data.get(name, {"value": None, "class": "unknown", "source": "unknown", "observed_at": ""})
        if not isinstance(item, dict) or set(item) != {"value", "class", "source", "observed_at"}:
            raise BudgetPlanError("invalid_capacity_field")
        value, classification, source, observed_at = (item[k] for k in ("value", "class", "source", "observed_at"))
        if not isinstance(source, str) or not isinstance(observed_at, str):
            raise BudgetPlanError("invalid_evidence_metadata")
        metadata_ref(source)
        if observed_at:
            try:
                timestamp = datetime.fromisoformat(observed_at.replace("Z", "+00:00"))
            except ValueError as exc:
                raise BudgetPlanError("invalid_evidence_timestamp") from exc
            if len(observed_at) > 40 or timestamp.tzinfo is None:
                raise BudgetPlanError("invalid_evidence_timestamp")
        if classification not in ("observed", "assumed", "unknown"):
            raise BudgetPlanError("invalid_evidence_class")
        if classification == "unknown":
            if value is not None:
                raise BudgetPlanError("unknown_capacity_has_value")
            kind: EvidenceClass = "unknown"
        else:
            if type(value) is not int or not 0 <= value <= 1_000_000_000:
                raise BudgetPlanError("invalid_token_allowance")
            if classification == "observed" and not observed_at:
                raise BudgetPlanError("observed_capacity_missing_clock")
            kind = "observed" if classification == "observed" else "assumed"
        result[name] = {"value": value, "class": kind, "source": source, "observed_at": observed_at}
    return result


def parse_must_keep(raw: str) -> MustKeep:
    """The pack is a content digest and local estimate, never retained text."""
    data = json.loads(raw)
    if not isinstance(data, dict) or set(data) != {"digest", "estimated_tokens_total"}:
        raise BudgetPlanError("invalid_must_keep_schema")
    digest, tokens = data["digest"], data["estimated_tokens_total"]
    if not isinstance(digest, str) or not valid_digest(digest) or type(tokens) is not int or not 0 <= tokens <= 1_000_000_000:
        raise BudgetPlanError("invalid_must_keep_metadata")
    return {"digest": digest, "estimated_tokens_total": tokens}


def derive_budget(capacity: dict[str, Evidence]) -> Budget:
    """Assumptions remain visible operands but never yield observed capacity."""
    values: list[int] = []
    classification: EvidenceClass = "observed"
    for name in CAPACITY_FIELDS:
        field = capacity[name]
        match field["class"]:
            case "unknown":
                return {"value": None, "class": "unknown"}
            case "assumed":
                classification = "assumed"
            case "observed":
                pass
            case unreachable:
                assert_never(unreachable)
        value = field["value"]
        if value is not None:
            values.append(value)
    if classification == "assumed":
        return {"value": None, "class": "assumed"}
    return {"value": max(0, values[0] - sum(values[1:])), "class": "observed"}


def build_capacity_record(identity: RouteIdentity, capacity: dict[str, Evidence]) -> RouteCapacity:
    """Identity/evidence clocks audit the source; only effective values invalidate."""
    effective = {key: {"value": value["value"], "class": value["class"]} for key, value in capacity.items()}
    return {
        "schema_version": "route_capacity_record/v1", "route_identity": identity,
        "route_identity_digest": digest_json(json.dumps(identity, sort_keys=True)),
        "capacity": capacity,
        "effective_capacity_digest": digest_json(json.dumps(effective, sort_keys=True)),
        "usable_budget_tokens": derive_budget(capacity),
        "derivation": {"operation": "window-output-reserve-retained", "operands": capacity},
    }


def observations(pack: MustKeep | None) -> Observations:
    return {
        "provider_usage_observed": "not_observed", "compaction_observed": "not_observed", "billing_observed": "not_observed",
        "local_token_estimate": {"value": pack["estimated_tokens_total"] if pack else None, "class": "assumed" if pack else "unknown"},
        "observation_clocks": {"local_estimate": None, "provider_usage": None, "compaction": None, "billing": None},
    }
