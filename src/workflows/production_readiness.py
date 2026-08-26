from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime
from typing import Any, Final

from .external_effect_receipts import validate_external_effect_receipt

READINESS_MATRIX_SCHEMA_VERSION: Final = "readiness_matrix/v1"
OBSERVED_CHECK_RESULT_SCHEMA_VERSION: Final = "observed_check_result/v1"
EXTERNAL_READINESS_EVIDENCE_SCHEMA_VERSION: Final = "external_readiness_evidence/v1"
OBSERVED_POSTCONDITION_SCHEMA_VERSION: Final = "observed_postcondition/v1"
READINESS_CATEGORIES: Final = (
    "build",
    "tests",
    "ci",
    "security_privacy",
    "performance",
    "observability",
    "rollback",
    "docs_support",
    "release_communication",
)
READINESS_EVIDENCE_STATES: Final = ("missing", "observed", "failed")
READINESS_VERDICTS: Final = ("GO", "HOLD", "BLOCK")
_REF_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,199}$")

# Machine-consumed rollback policy. Rollback removes the new consumer and
# generated annotations; it does not rewrite persisted matrices or promote old
# operation_artifact/v1 records. Readers keep legacy bytes and evidence state.
READINESS_MATRIX_ROLLBACK_CONTRACT: Final = {
    "schema_version": "readiness_matrix_rollback/v1",
    "contract_id": READINESS_MATRIX_SCHEMA_VERSION,
    "rollback_action": "remove_consumer_and_annotations",
    "persisted_record_action": "leave_unchanged",
    "legacy_read_policy": "read_without_upgrade",
    "canonical_reliability_contract": "omh_operation_artifact/v1",
    "compatible_legacy_contracts": ["operation_artifact/v1"],
    "legacy_evidence_state_action": "preserve",
}


def build_readiness_matrix(
    *,
    scope: str,
    rows: list[dict[str, Any]] | tuple[dict[str, Any], ...],
    created_at: str,
    evidence_fresh_after: str,
) -> dict[str, Any]:
    clean_scope = str(scope).strip()
    if not clean_scope:
        raise ValueError("scope is required")
    normalized_rows: list[dict[str, Any]] = []
    for supplied in rows:
        category = str(supplied.get("category", "")).strip()
        requires_external = supplied.get("requires_external_observation") is True
        supplied_evidence = supplied.get("evidence")
        evidence = supplied_evidence if isinstance(supplied_evidence, list) else []
        evidence_errors = [
            error
            for item in evidence
            for error in (["readiness evidence must be an object"] if not isinstance(item, dict) else _evidence_errors(item))
        ]
        if evidence_errors:
            raise ValueError(f"invalid evidence for {category or '<unknown>'}: {'; '.join(evidence_errors)}")
        accepted = [item for item in evidence if isinstance(item, dict)]
        normalized_rows.append(
            {
                "category": category,
                "requires_external_observation": requires_external,
                "evidence_state": _derive_evidence_state(
                    accepted,
                    requires_external=requires_external,
                    fresh_after=evidence_fresh_after,
                    observed_before=created_at,
                ),
                "evidence": accepted,
            }
        )
    verdict = _derive_verdict(normalized_rows)
    record = {
        "schema_version": READINESS_MATRIX_SCHEMA_VERSION,
        "matrix_id": _matrix_id(clean_scope, created_at),
        "scope": clean_scope,
        "created_at": created_at,
        "evidence_fresh_after": evidence_fresh_after,
        "contract_ref": {
            "contract_id": READINESS_MATRIX_SCHEMA_VERSION,
            "enforcement_level": "executable_validated",
            "consumer_id": "validate_readiness_matrix",
        },
        "rows": normalized_rows,
        "verdict": verdict,
        "missing_categories": [
            str(row.get("category", "")) for row in normalized_rows if row.get("evidence_state") != "observed"
        ],
        "rollback_contract": dict(READINESS_MATRIX_ROLLBACK_CONTRACT),
    }
    errors = validate_readiness_matrix(record)
    if errors:
        raise ValueError("; ".join(errors))
    return record


def validate_readiness_matrix(record: dict[str, Any]) -> list[str]:
    errors: list[str] = []
    if not isinstance(record, dict):
        return ["readiness_matrix must be an object"]
    if record.get("schema_version") != READINESS_MATRIX_SCHEMA_VERSION:
        errors.append("schema_version must be readiness_matrix/v1")
    if not str(record.get("matrix_id", "")).strip():
        errors.append("matrix_id is required")
    if not str(record.get("scope", "")).strip():
        errors.append("scope is required")
    for field in ("created_at", "evidence_fresh_after"):
        if _timestamp(record.get(field)) is None:
            errors.append(f"{field} must be an ISO-8601 timestamp")
    contract_ref = record.get("contract_ref")
    if not isinstance(contract_ref, dict):
        errors.append("contract_ref must be an object")
    else:
        expected_ref = {
            "contract_id": READINESS_MATRIX_SCHEMA_VERSION,
            "enforcement_level": "executable_validated",
            "consumer_id": "validate_readiness_matrix",
        }
        if contract_ref != expected_ref:
            errors.append("contract_ref must identify the executable readiness_matrix consumer")
    rollback = record.get("rollback_contract")
    if rollback != READINESS_MATRIX_ROLLBACK_CONTRACT:
        errors.append("rollback_contract must match readiness_matrix_rollback/v1")
    rows = record.get("rows")
    if not isinstance(rows, list):
        errors.append("rows must be a list")
        rows = []
    categories = [str(row.get("category", "")) for row in rows if isinstance(row, dict)]
    duplicates = sorted({category for category in categories if category and categories.count(category) > 1})
    missing = sorted(set(READINESS_CATEGORIES) - set(categories))
    extra = sorted(set(categories) - set(READINESS_CATEGORIES))
    if duplicates:
        errors.append(f"duplicate readiness categories: {', '.join(duplicates)}")
    if missing:
        errors.append(f"missing readiness categories: {', '.join(missing)}")
    if extra:
        errors.append(f"unsupported readiness categories: {', '.join(extra)}")
    fresh_after = str(record.get("evidence_fresh_after", ""))
    derived_rows: list[dict[str, Any]] = []
    for index, row in enumerate(rows):
        if not isinstance(row, dict):
            errors.append(f"rows[{index}] must be an object")
            continue
        requires_external = row.get("requires_external_observation")
        if not isinstance(requires_external, bool):
            errors.append(f"rows[{index}].requires_external_observation must be a boolean")
            requires_external = False
        evidence = row.get("evidence")
        if not isinstance(evidence, list):
            errors.append(f"rows[{index}].evidence must be a list")
            evidence = []
        for evidence_index, item in enumerate(evidence):
            if not isinstance(item, dict):
                errors.append(f"rows[{index}].evidence[{evidence_index}] must be an object")
                continue
            for error in _evidence_errors(item):
                errors.append(f"rows[{index}].evidence[{evidence_index}]: {error}")
        if requires_external and any(
            isinstance(item, dict) and item.get("schema_version") != EXTERNAL_READINESS_EVIDENCE_SCHEMA_VERSION
            for item in evidence
        ):
            errors.append(f"rows[{index}] external observed success requires a matching receipt and observed postcondition")
        derived_state = _derive_evidence_state(
            [item for item in evidence if isinstance(item, dict)],
            requires_external=bool(requires_external),
            fresh_after=fresh_after,
            observed_before=str(record.get("created_at", "")),
        )
        if row.get("evidence_state") not in READINESS_EVIDENCE_STATES:
            errors.append(f"rows[{index}].evidence_state is unsupported: {row.get('evidence_state')}")
        if requires_external and row.get("evidence_state") == "observed" and derived_state != "observed":
            errors.append(f"rows[{index}] external observed success requires a matching receipt and observed postcondition")
        if row.get("evidence_state") != derived_state:
            errors.append(f"rows[{index}].evidence_state must match derived evidence state {derived_state}")
        derived_rows.append({"category": row.get("category", ""), "evidence_state": derived_state})
    derived_verdict = _derive_verdict(derived_rows)
    verdict = record.get("verdict")
    if verdict not in READINESS_VERDICTS:
        errors.append(f"unsupported verdict: {verdict}")
    if verdict != derived_verdict:
        errors.append(f"verdict must match derived verdict {derived_verdict}")
    derived_missing = [str(row.get("category", "")) for row in derived_rows if row.get("evidence_state") != "observed"]
    if record.get("missing_categories") != derived_missing:
        errors.append("missing_categories must match non-observed rows")
    return errors


def _evidence_errors(evidence: dict[str, Any]) -> list[str]:
    schema = evidence.get("schema_version")
    if schema == OBSERVED_CHECK_RESULT_SCHEMA_VERSION:
        return _observed_check_errors(evidence)
    if schema == EXTERNAL_READINESS_EVIDENCE_SCHEMA_VERSION:
        return _external_evidence_errors(evidence)
    return [f"unsupported readiness evidence schema: {schema}"]


def _observed_check_errors(evidence: dict[str, Any]) -> list[str]:
    errors: list[str] = []
    if evidence.get("result") not in {"passed", "failed"}:
        errors.append("observed_check_result result must be passed or failed")
    for field in ("check_id", "evidence_ref"):
        if not _valid_ref(evidence.get(field)):
            errors.append(f"observed_check_result {field} must be an opaque reference")
    if _timestamp(evidence.get("observed_at")) is None:
        errors.append("observed_check_result observed_at must be an ISO-8601 timestamp")
    return errors


def _external_evidence_errors(evidence: dict[str, Any]) -> list[str]:
    errors: list[str] = []
    receipt = evidence.get("receipt")
    postcondition = evidence.get("postcondition")
    if not isinstance(receipt, dict):
        errors.append("external observed success requires external_effect_receipt/v1")
        return errors
    receipt_errors = validate_external_effect_receipt(receipt)
    errors.extend(receipt_errors)
    if receipt.get("observed_result") != "succeeded":
        errors.append("external_effect_receipt observed_result must be succeeded")
    if not isinstance(postcondition, dict):
        errors.append("external observed success requires observed_postcondition/v1")
        return errors
    if postcondition.get("schema_version") != OBSERVED_POSTCONDITION_SCHEMA_VERSION:
        errors.append("postcondition schema_version must be observed_postcondition/v1")
    if postcondition.get("result") not in {"satisfied", "failed"}:
        errors.append("postcondition result must be satisfied or failed")
    for field in ("effect_id", "external_ref", "observation_id"):
        if not _valid_ref(postcondition.get(field)):
            errors.append(f"postcondition {field} must be an opaque reference")
    if _timestamp(postcondition.get("observed_at")) is None:
        errors.append("postcondition observed_at must be an ISO-8601 timestamp")
    if postcondition.get("effect_id") != receipt.get("effect_id"):
        errors.append("receipt and postcondition effect_id must match")
    if postcondition.get("external_ref") != receipt.get("external_ref"):
        errors.append("receipt and postcondition external_ref must match")
    return errors


def _derive_evidence_state(
    evidence: list[dict[str, Any]], *, requires_external: bool, fresh_after: str, observed_before: str
) -> str:
    states: list[str] = []
    for item in evidence:
        if _evidence_errors(item):
            continue
        schema = item.get("schema_version")
        if requires_external and schema != EXTERNAL_READINESS_EVIDENCE_SCHEMA_VERSION:
            continue
        if not requires_external and schema == EXTERNAL_READINESS_EVIDENCE_SCHEMA_VERSION:
            observed_at = (item.get("postcondition") or {}).get("observed_at")
        elif schema == OBSERVED_CHECK_RESULT_SCHEMA_VERSION:
            observed_at = item.get("observed_at")
        else:
            observed_at = (item.get("postcondition") or {}).get("observed_at")
        if not _fresh(observed_at, fresh_after, observed_before):
            continue
        if schema == EXTERNAL_READINESS_EVIDENCE_SCHEMA_VERSION:
            receipt = item.get("receipt") or {}
            if not _fresh(receipt.get("observed_at"), fresh_after, observed_before):
                continue
        if schema == OBSERVED_CHECK_RESULT_SCHEMA_VERSION:
            states.append("failed" if item.get("result") == "failed" else "observed")
        else:
            postcondition = item.get("postcondition") or {}
            states.append("failed" if postcondition.get("result") == "failed" else "observed")
    if "failed" in states:
        return "failed"
    if "observed" in states:
        return "observed"
    return "missing"


def _derive_verdict(rows: list[dict[str, Any]]) -> str:
    states = [row.get("evidence_state") for row in rows]
    if "failed" in states:
        return "BLOCK"
    if len(rows) == len(READINESS_CATEGORIES) and states and all(state == "observed" for state in states):
        return "GO"
    return "HOLD"


def _timestamp(value: object) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def _fresh(observed_at: object, fresh_after: str, observed_before: str) -> bool:
    observed = _timestamp(observed_at)
    threshold = _timestamp(fresh_after)
    ceiling = _timestamp(observed_before)
    return observed is not None and threshold is not None and ceiling is not None and threshold <= observed <= ceiling


def _valid_ref(value: object) -> bool:
    return isinstance(value, str) and bool(_REF_RE.fullmatch(value))


def _matrix_id(scope: str, created_at: str) -> str:
    digest = hashlib.sha256(
        json.dumps({"scope": scope, "created_at": created_at}, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()[:16]
    return f"readiness-{digest}"
