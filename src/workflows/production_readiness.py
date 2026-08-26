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
    task_id: str,
    revision: str,
    rows: list[dict[str, Any]] | tuple[dict[str, Any], ...],
    created_at: str,
    evidence_fresh_after: str,
) -> dict[str, Any]:
    clean_scope = str(scope).strip()
    clean_task_id = str(task_id).strip()
    clean_revision = str(revision).strip()
    top_errors = _matrix_identity_and_time_errors(
        scope=clean_scope,
        task_id=clean_task_id,
        revision=clean_revision,
        created_at=created_at,
        evidence_fresh_after=evidence_fresh_after,
    )
    if top_errors:
        raise ValueError("; ".join(top_errors))
    normalized_rows: list[dict[str, Any]] = []
    receipt_ids: list[str] = []
    observation_ids: list[str] = []
    for supplied in rows:
        category = str(supplied.get("category", "")).strip()
        requires_external = supplied.get("requires_external_observation") is True
        supplied_identity = supplied.get("external_identity")
        external_identity = supplied_identity if isinstance(supplied_identity, dict) else {}
        identity_errors = _external_identity_errors(external_identity, required=requires_external)
        if not requires_external and external_identity:
            identity_errors.append("non-external readiness rows must not carry external_identity")
        supplied_evidence = supplied.get("evidence")
        evidence = supplied_evidence if isinstance(supplied_evidence, list) else []
        external_items = [
            item
            for item in evidence
            if isinstance(item, dict) and item.get("schema_version") == EXTERNAL_READINESS_EVIDENCE_SCHEMA_VERSION
        ]
        if requires_external and len(external_items) > 1:
            identity_errors.append("external readiness rows accept exactly one external readiness association")
        evidence_errors = [
            error
            for item in evidence
            for error in (
                ["readiness evidence must be an object"]
                if not isinstance(item, dict)
                else _evidence_errors(
                    item,
                    external_identity=external_identity,
                    task_id=clean_task_id,
                    revision=clean_revision,
                    observed_before=created_at,
                )
            )
        ]
        if identity_errors or evidence_errors:
            raise ValueError(
                f"invalid evidence for {category or '<unknown>'}: {'; '.join([*identity_errors, *evidence_errors])}"
            )
        accepted = [item for item in evidence if isinstance(item, dict)]
        receipt_ids.extend(_external_receipt_ids(accepted))
        observation_ids.extend(_external_observation_ids(accepted))
        normalized_rows.append(
            {
                "category": category,
                "requires_external_observation": requires_external,
                "external_identity": dict(external_identity),
                "evidence_state": _derive_evidence_state(
                    accepted,
                    requires_external=requires_external,
                    external_identity=external_identity,
                    task_id=clean_task_id,
                    revision=clean_revision,
                    fresh_after=evidence_fresh_after,
                    observed_before=created_at,
                ),
                "evidence": accepted,
            }
        )
    duplicate_receipts = sorted({receipt_id for receipt_id in receipt_ids if receipt_ids.count(receipt_id) > 1})
    if duplicate_receipts:
        raise ValueError(f"duplicate external readiness receipt associations: {', '.join(duplicate_receipts)}")
    duplicate_observations = _duplicates(observation_ids)
    if duplicate_observations:
        raise ValueError(f"duplicate external readiness postcondition associations: {', '.join(duplicate_observations)}")
    verdict = _derive_verdict(normalized_rows)
    record = {
        "schema_version": READINESS_MATRIX_SCHEMA_VERSION,
        "matrix_id": _matrix_id(clean_scope, clean_task_id, clean_revision, created_at),
        "scope": clean_scope,
        "task_id": clean_task_id,
        "revision": clean_revision,
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
    scope = str(record.get("scope", ""))
    task_id = str(record.get("task_id", ""))
    revision = str(record.get("revision", ""))
    created_at = str(record.get("created_at", ""))
    fresh_after = str(record.get("evidence_fresh_after", ""))
    errors.extend(
        _matrix_identity_and_time_errors(
            scope=scope,
            task_id=task_id,
            revision=revision,
            created_at=created_at,
            evidence_fresh_after=fresh_after,
        )
    )
    if not str(record.get("matrix_id", "")).strip():
        errors.append("matrix_id is required")
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
    if record.get("rollback_contract") != READINESS_MATRIX_ROLLBACK_CONTRACT:
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
    derived_rows: list[dict[str, Any]] = []
    receipt_ids: list[str] = []
    observation_ids: list[str] = []
    for index, row in enumerate(rows):
        if not isinstance(row, dict):
            errors.append(f"rows[{index}] must be an object")
            continue
        requires_external = row.get("requires_external_observation")
        if not isinstance(requires_external, bool):
            errors.append(f"rows[{index}].requires_external_observation must be a boolean")
            requires_external = False
        supplied_identity = row.get("external_identity")
        external_identity = supplied_identity if isinstance(supplied_identity, dict) else {}
        for error in _external_identity_errors(external_identity, required=bool(requires_external)):
            errors.append(f"rows[{index}]: {error}")
        if not requires_external and external_identity:
            errors.append(f"rows[{index}]: non-external readiness rows must not carry external_identity")
        evidence = row.get("evidence")
        if not isinstance(evidence, list):
            errors.append(f"rows[{index}].evidence must be a list")
            evidence = []
        external_items = [
            item
            for item in evidence
            if isinstance(item, dict) and item.get("schema_version") == EXTERNAL_READINESS_EVIDENCE_SCHEMA_VERSION
        ]
        if requires_external and len(external_items) > 1:
            errors.append(f"rows[{index}] external readiness rows accept exactly one external readiness association")
        for evidence_index, item in enumerate(evidence):
            if not isinstance(item, dict):
                errors.append(f"rows[{index}].evidence[{evidence_index}] must be an object")
                continue
            for error in _evidence_errors(
                item,
                external_identity=external_identity,
                task_id=task_id,
                revision=revision,
                observed_before=created_at,
            ):
                errors.append(f"rows[{index}].evidence[{evidence_index}]: {error}")
        valid_evidence = [item for item in evidence if isinstance(item, dict)]
        receipt_ids.extend(_external_receipt_ids(valid_evidence))
        observation_ids.extend(_external_observation_ids(valid_evidence))
        if requires_external and any(
            item.get("schema_version") != EXTERNAL_READINESS_EVIDENCE_SCHEMA_VERSION for item in valid_evidence
        ):
            errors.append(f"rows[{index}] external observed success requires one exactly bound external association")
        derived_state = _derive_evidence_state(
            valid_evidence,
            requires_external=bool(requires_external),
            external_identity=external_identity,
            task_id=task_id,
            revision=revision,
            fresh_after=fresh_after,
            observed_before=created_at,
        )
        if row.get("evidence_state") not in READINESS_EVIDENCE_STATES:
            errors.append(f"rows[{index}].evidence_state is unsupported: {row.get('evidence_state')}")
        if requires_external and row.get("evidence_state") == "observed" and derived_state != "observed":
            errors.append(f"rows[{index}] external observed success requires one exactly bound external association")
        if row.get("evidence_state") != derived_state:
            errors.append(f"rows[{index}].evidence_state must match derived evidence state {derived_state}")
        derived_rows.append({"category": row.get("category", ""), "evidence_state": derived_state})
    duplicate_receipts = sorted({receipt_id for receipt_id in receipt_ids if receipt_ids.count(receipt_id) > 1})
    if duplicate_receipts:
        errors.append(f"duplicate external readiness receipt associations: {', '.join(duplicate_receipts)}")
    duplicate_observations = _duplicates(observation_ids)
    if duplicate_observations:
        errors.append(f"duplicate external readiness postcondition associations: {', '.join(duplicate_observations)}")
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


def _matrix_identity_and_time_errors(
    *, scope: str, task_id: str, revision: str, created_at: str, evidence_fresh_after: str
) -> list[str]:
    errors: list[str] = []
    if not scope.strip():
        errors.append("scope is required")
    for field, value in (("task_id", task_id), ("revision", revision)):
        if not _valid_ref(value):
            errors.append(f"{field} must be an opaque reference")
    created = _timestamp(created_at)
    fresh = _timestamp(evidence_fresh_after)
    if created is None:
        errors.append("created_at must be a timezone-aware ISO-8601 timestamp")
    if fresh is None:
        errors.append("evidence_fresh_after must be a timezone-aware ISO-8601 timestamp")
    if created is not None and fresh is not None and fresh > created:
        errors.append("evidence_fresh_after must not be after created_at")
    return errors


def _external_identity_errors(identity: dict[str, Any], *, required: bool) -> list[str]:
    if not identity:
        return ["external observed readiness requires external_identity"] if required else []
    errors: list[str] = []
    if set(identity) != {"effect_id", "run_id"}:
        errors.append("external_identity must contain exactly effect_id and run_id")
    for field in ("effect_id", "run_id"):
        if not _valid_ref(identity.get(field)):
            errors.append(f"external_identity.{field} must be an opaque reference")
    return errors


def _evidence_errors(
    evidence: dict[str, Any],
    *,
    external_identity: dict[str, Any] | None = None,
    task_id: str = "",
    revision: str = "",
    observed_before: str = "",
) -> list[str]:
    schema = evidence.get("schema_version")
    if schema == OBSERVED_CHECK_RESULT_SCHEMA_VERSION:
        errors = _observed_check_errors(evidence)
        errors.extend(_future_timestamp_errors(evidence.get("observed_at"), observed_before, "observed_check_result observed_at"))
        return errors
    if schema == EXTERNAL_READINESS_EVIDENCE_SCHEMA_VERSION:
        return _external_evidence_errors(
            evidence,
            external_identity=external_identity or {},
            task_id=task_id,
            revision=revision,
            observed_before=observed_before,
        )
    return [f"unsupported readiness evidence schema: {schema}"]


def _observed_check_errors(evidence: dict[str, Any]) -> list[str]:
    errors: list[str] = []
    if evidence.get("result") not in {"passed", "failed"}:
        errors.append("observed_check_result result must be passed or failed")
    for field in ("check_id", "evidence_ref"):
        if not _valid_ref(evidence.get(field)):
            errors.append(f"observed_check_result {field} must be an opaque reference")
    if _timestamp(evidence.get("observed_at")) is None:
        errors.append("observed_check_result observed_at must be a timezone-aware ISO-8601 timestamp")
    return errors


def _external_evidence_errors(
    evidence: dict[str, Any],
    *,
    external_identity: dict[str, Any],
    task_id: str,
    revision: str,
    observed_before: str,
) -> list[str]:
    errors: list[str] = []
    receipt = evidence.get("receipt")
    postcondition = evidence.get("postcondition")
    if not isinstance(receipt, dict):
        return ["external observed success requires external_effect_receipt/v1"]
    errors.extend(validate_external_effect_receipt(receipt))
    if receipt.get("observed_result") != "succeeded":
        errors.append("external_effect_receipt observed_result must be succeeded")
    if _timestamp(receipt.get("observed_at")) is None:
        errors.append("external_effect_receipt observed_at must be a timezone-aware ISO-8601 timestamp")
    errors.extend(_future_timestamp_errors(receipt.get("observed_at"), observed_before, "external_effect_receipt observed_at"))
    if receipt.get("effect_id") != external_identity.get("effect_id"):
        errors.append("receipt effect_id must match external_identity.effect_id")
    if receipt.get("run_id") != external_identity.get("run_id"):
        errors.append("receipt run_id must match external_identity.run_id")
    supplied_refs = receipt.get("evidence_refs")
    refs = supplied_refs if isinstance(supplied_refs, list) else []
    for label, value in (("task_id", task_id), ("revision", revision)):
        prefix = f"{label.removesuffix('_id')}:"
        marker = f"{prefix}{value}"
        bound_markers = [ref for ref in refs if isinstance(ref, str) and ref.startswith(prefix)]
        if bound_markers != [marker]:
            errors.append(f"receipt evidence_refs must bind exactly one matching {label} marker")
    if not isinstance(postcondition, dict):
        return [*errors, "external observed success requires observed_postcondition/v1"]
    if postcondition.get("schema_version") != OBSERVED_POSTCONDITION_SCHEMA_VERSION:
        errors.append("postcondition schema_version must be observed_postcondition/v1")
    if postcondition.get("result") not in {"satisfied", "failed"}:
        errors.append("postcondition result must be satisfied or failed")
    for field in ("receipt_id", "effect_id", "run_id", "task_id", "revision", "external_ref", "observation_id"):
        if not _valid_ref(postcondition.get(field)):
            errors.append(f"postcondition {field} must be an opaque reference")
    if _timestamp(postcondition.get("observed_at")) is None:
        errors.append("postcondition observed_at must be a timezone-aware ISO-8601 timestamp")
    errors.extend(_future_timestamp_errors(postcondition.get("observed_at"), observed_before, "postcondition observed_at"))
    bindings = (
        ("receipt_id", receipt.get("receipt_id")),
        ("effect_id", external_identity.get("effect_id")),
        ("run_id", external_identity.get("run_id")),
        ("task_id", task_id),
        ("revision", revision),
        ("external_ref", receipt.get("external_ref")),
    )
    for field, expected in bindings:
        if postcondition.get(field) != expected:
            errors.append(f"postcondition {field} must match its bound readiness identity")
    receipt_time = _timestamp(receipt.get("observed_at"))
    postcondition_time = _timestamp(postcondition.get("observed_at"))
    if receipt_time is not None and postcondition_time is not None and postcondition_time < receipt_time:
        errors.append("postcondition observed_at must not precede receipt observed_at")
    return errors


def _future_timestamp_errors(value: object, observed_before: str, label: str) -> list[str]:
    observed = _timestamp(value)
    ceiling = _timestamp(observed_before)
    if observed is not None and ceiling is not None and observed > ceiling:
        return [f"{label} must not be after matrix created_at"]
    return []


def _derive_evidence_state(
    evidence: list[dict[str, Any]],
    *,
    requires_external: bool,
    external_identity: dict[str, Any],
    task_id: str,
    revision: str,
    fresh_after: str,
    observed_before: str,
) -> str:
    states: list[str] = []
    for item in evidence:
        if _evidence_errors(
            item,
            external_identity=external_identity,
            task_id=task_id,
            revision=revision,
            observed_before=observed_before,
        ):
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
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        return None
    return parsed


def _fresh(observed_at: object, fresh_after: str, observed_before: str) -> bool:
    observed = _timestamp(observed_at)
    threshold = _timestamp(fresh_after)
    ceiling = _timestamp(observed_before)
    return observed is not None and threshold is not None and ceiling is not None and threshold <= observed <= ceiling


def _external_receipt_ids(evidence: list[dict[str, Any]]) -> list[str]:
    return [
        str(receipt.get("receipt_id", ""))
        for item in evidence
        if item.get("schema_version") == EXTERNAL_READINESS_EVIDENCE_SCHEMA_VERSION
        for receipt in [item.get("receipt")]
        if isinstance(receipt, dict) and str(receipt.get("receipt_id", ""))
    ]


def _external_observation_ids(evidence: list[dict[str, Any]]) -> list[str]:
    return [
        str(postcondition.get("observation_id", ""))
        for item in evidence
        if item.get("schema_version") == EXTERNAL_READINESS_EVIDENCE_SCHEMA_VERSION
        for postcondition in [item.get("postcondition")]
        if isinstance(postcondition, dict) and str(postcondition.get("observation_id", ""))
    ]


def _duplicates(values: list[str]) -> list[str]:
    return sorted({value for value in values if values.count(value) > 1})


def _valid_ref(value: object) -> bool:
    return isinstance(value, str) and bool(_REF_RE.fullmatch(value))


def _matrix_id(scope: str, task_id: str, revision: str, created_at: str) -> str:
    digest = hashlib.sha256(
        json.dumps(
            {"scope": scope, "task_id": task_id, "revision": revision, "created_at": created_at},
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
    ).hexdigest()[:16]
    return f"readiness-{digest}"
