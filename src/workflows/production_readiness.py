from __future__ import annotations

import hashlib
import hmac
import json
import math
import re
from datetime import datetime
from typing import Any, Final, NoReturn, cast

from .external_effect_receipts import validate_external_effect_receipt

READINESS_MATRIX_SCHEMA_VERSION: Final = "readiness_matrix/v1"
OBSERVED_CHECK_RESULT_SCHEMA_VERSION: Final = "observed_check_result/v1"
EXTERNAL_READINESS_EVIDENCE_SCHEMA_VERSION: Final = "external_readiness_evidence/v1"
OBSERVED_POSTCONDITION_SCHEMA_VERSION: Final = "observed_postcondition/v1"
EXTERNAL_READINESS_AUTHENTICITY_SCHEMA_VERSION: Final = "external_readiness_authenticity/v1"
EXTERNAL_READINESS_AUTHENTICITY_ALGORITHM: Final = "hmac-sha256"
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
READINESS_CATEGORY_POLICY_SCHEMA_VERSION: Final = "readiness_category_policy/v1"
READINESS_CATEGORY_POLICY: Final = (
    ("build", False),
    ("tests", False),
    ("ci", True),
    ("security_privacy", False),
    ("performance", False),
    ("observability", False),
    ("rollback", False),
    ("docs_support", False),
    ("release_communication", False),
)
READINESS_EVIDENCE_STATES: Final = ("missing", "observed", "failed")
READINESS_VERDICTS: Final = ("GO", "HOLD", "BLOCK")
_REF_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,199}$")
_HMAC_RE = re.compile(r"^[0-9a-f]{64}$")
_MIN_HMAC_KEY_BYTES = 32
_MAX_HMAC_KEY_BYTES = 4_096
READINESS_CANONICAL_JSON_MAX_DEPTH: Final = 16
READINESS_CANONICAL_JSON_MAX_NODES: Final = 2_048
READINESS_CANONICAL_JSON_MAX_BYTES: Final = 65_536
_CANONICAL_JSON_REJECTED: Final = object()


class ReadinessAuthenticationError(Exception):
    """Raised when structurally unsafe data cannot be authenticated."""


class ReadinessTrustContext:
    """Caller-held HMAC state with redacted rendering and blocked standard exports.

    These supported guards do not claim resistance to arbitrary same-process introspection.
    """

    __slots__ = ("context_id", "__hmac_template", "__usable")

    def __init__(self, context_id: str, hmac_key: bytes) -> None:
        self.context_id = str(context_id)
        self.__usable = _valid_ref(self.context_id) and _MIN_HMAC_KEY_BYTES <= len(hmac_key) <= _MAX_HMAC_KEY_BYTES
        self.__hmac_template = hmac.new(hmac_key, digestmod=hashlib.sha256)

    def __repr__(self) -> str:
        return f"ReadinessTrustContext(context_id={self.context_id!r}, signer=<redacted>)"

    __str__ = __repr__

    def __format__(self, format_spec: str) -> str:
        return format(str(self), format_spec)

    def __copy__(self) -> NoReturn:
        raise TypeError("ReadinessTrustContext is opaque and non-copyable")

    def __deepcopy__(self, memo: dict[int, object]) -> NoReturn:
        raise TypeError("ReadinessTrustContext is opaque and non-copyable")

    def __reduce__(self) -> NoReturn:
        raise TypeError("ReadinessTrustContext is opaque and non-serializable")

    def __reduce_ex__(self, protocol: object) -> NoReturn:
        raise TypeError("ReadinessTrustContext is opaque and non-serializable")

    def __getstate__(self) -> NoReturn:
        raise TypeError("ReadinessTrustContext is opaque and non-serializable")

    def _usable(self) -> bool:
        return self.__usable

    def _sign(self, payload: bytes) -> str:
        signer = self.__hmac_template.copy()
        signer.update(payload)
        return signer.hexdigest()

    def _verify(self, payload: bytes, candidate: str) -> bool:
        expected = self._sign(payload)
        return hmac.compare_digest(candidate, expected)

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


OBSERVED_CHECK_RESULT_KEYS: Final = {
    "schema_version",
    "check_id",
    "result",
    "observed_at",
    "evidence_ref",
    "scope",
    "category",
    "task_id",
    "revision",
    "row_id",
}


def authenticate_external_readiness_evidence(
    evidence: dict[str, Any],
    *,
    scope: str,
    category: str,
    task_id: str,
    revision: str,
    created_at: str,
    evidence_fresh_after: str,
    external_identity: dict[str, Any],
    trusted_context: ReadinessTrustContext,
) -> dict[str, Any]:
    """Attach local keyed integrity from a caller-held trusted context."""
    if not _trusted_context_usable(trusted_context):
        raise ValueError("external readiness authentication requires a usable trusted context")
    if not _category_requires_external_observation(category):
        raise ValueError("external readiness authentication requires a registry-owned external category")
    matrix_id = _matrix_id(scope, task_id, revision, created_at)
    payload = _external_authenticity_payload(
        evidence,
        matrix_schema_version=READINESS_MATRIX_SCHEMA_VERSION,
        matrix_id=matrix_id,
        contract_ref=_canonical_contract_ref(),
        context_id=trusted_context.context_id,
        scope=scope,
        category=category,
        task_id=task_id,
        revision=revision,
        created_at=created_at,
        evidence_fresh_after=evidence_fresh_after,
        requires_external_observation=True,
        external_identity=external_identity,
    )
    if payload is None:
        raise ReadinessAuthenticationError(
            "external readiness signed data is not canonical JSON and cannot be authenticated"
        )
    tag = trusted_context._sign(payload)
    return {
        **evidence,
        "authenticity": {
            "schema_version": EXTERNAL_READINESS_AUTHENTICITY_SCHEMA_VERSION,
            "algorithm": EXTERNAL_READINESS_AUTHENTICITY_ALGORITHM,
            "context_id": trusted_context.context_id,
            "hmac_sha256": tag,
        },
    }


def build_readiness_matrix(
    *,
    scope: str,
    task_id: str,
    revision: str,
    rows: list[dict[str, Any]] | tuple[dict[str, Any], ...],
    created_at: str,
    evidence_fresh_after: str,
    trusted_context: ReadinessTrustContext | None = None,
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
    matrix_id = _matrix_id(clean_scope, clean_task_id, clean_revision, created_at)
    contract_ref = _canonical_contract_ref()
    normalized_rows: list[dict[str, Any]] = []
    receipt_ids: list[str] = []
    observation_ids: list[str] = []
    check_ids: list[str] = []
    check_evidence_refs: list[str] = []
    for supplied in rows:
        category = str(supplied.get("category", "")).strip()
        policy_requires_external = _category_requires_external_observation(category)
        supplied_requires_external = supplied.get("requires_external_observation")
        identity_errors: list[str] = []
        if not isinstance(supplied_requires_external, bool) or supplied_requires_external != policy_requires_external:
            identity_errors.append(_category_policy_error(category, policy_requires_external))
        requires_external = policy_requires_external
        supplied_identity = supplied.get("external_identity")
        external_identity = supplied_identity if isinstance(supplied_identity, dict) else {}
        identity_errors.extend(_external_identity_errors(external_identity, required=requires_external))
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
        if not requires_external and external_items:
            identity_errors.append("category policy requires local observed_check_result evidence")
        evidence_errors = [
            error
            for item in evidence
            for error in (
                ["readiness evidence must be an object"]
                if not isinstance(item, dict)
                else _evidence_errors(
                    item,
                    external_identity=external_identity,
                    scope=clean_scope,
                    category=category,
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
        check_ids.extend(_local_check_ids(accepted))
        check_evidence_refs.extend(_local_evidence_refs(accepted))
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
                    matrix_schema_version=READINESS_MATRIX_SCHEMA_VERSION,
                    matrix_id=matrix_id,
                    contract_ref=contract_ref,
                    scope=clean_scope,
                    category=category,
                    revision=clean_revision,
                    fresh_after=evidence_fresh_after,
                    observed_before=created_at,
                    trusted_context=trusted_context,
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
    duplicate_checks = _duplicates(check_ids)
    if duplicate_checks:
        raise ValueError(f"duplicate observed_check_result check_id associations: {', '.join(duplicate_checks)}")
    duplicate_check_refs = _duplicates(check_evidence_refs)
    if duplicate_check_refs:
        raise ValueError(
            f"duplicate observed_check_result evidence_ref associations: {', '.join(duplicate_check_refs)}"
        )
    verdict = _derive_verdict(normalized_rows)
    record = {
        "schema_version": READINESS_MATRIX_SCHEMA_VERSION,
        "matrix_id": matrix_id,
        "scope": clean_scope,
        "task_id": clean_task_id,
        "revision": clean_revision,
        "created_at": created_at,
        "evidence_fresh_after": evidence_fresh_after,
        "contract_ref": contract_ref,
        "rows": normalized_rows,
        "verdict": verdict,
        "missing_categories": [
            str(row.get("category", "")) for row in normalized_rows if row.get("evidence_state") != "observed"
        ],
        "rollback_contract": dict(READINESS_MATRIX_ROLLBACK_CONTRACT),
    }
    errors = validate_readiness_matrix(record, trusted_context=trusted_context)
    if errors:
        raise ValueError("; ".join(errors))
    return record


def validate_readiness_matrix(
    record: dict[str, Any], *, trusted_context: ReadinessTrustContext | None = None
) -> list[str]:
    errors: list[str] = []
    if not isinstance(record, dict):
        return ["readiness_matrix must be an object"]
    snapshot = _canonical_json_snapshot(record)
    if snapshot is None:
        return [
            "readiness_matrix signed data is not safely bounded canonical JSON",
            "external readiness evidence is unauthenticated",
            "verdict must match derived verdict HOLD",
        ]
    record = snapshot[0]
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
    matrix_id = str(record.get("matrix_id", ""))
    if not matrix_id.strip():
        errors.append("matrix_id is required")
    elif matrix_id != _matrix_id(scope, task_id, revision, created_at):
        errors.append("matrix_id must match canonical scope, task, revision, and created_at identity")
    supplied_contract_ref = record.get("contract_ref")
    contract_ref = supplied_contract_ref if isinstance(supplied_contract_ref, dict) else {}
    if not isinstance(supplied_contract_ref, dict):
        errors.append("contract_ref must be an object")
    elif contract_ref != _canonical_contract_ref():
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
    check_ids: list[str] = []
    check_evidence_refs: list[str] = []
    for index, row in enumerate(rows):
        if not isinstance(row, dict):
            errors.append(f"rows[{index}] must be an object")
            continue
        category = str(row.get("category", ""))
        policy_requires_external = _category_requires_external_observation(category)
        supplied_requires_external = row.get("requires_external_observation")
        if not isinstance(supplied_requires_external, bool):
            errors.append(f"rows[{index}].requires_external_observation must be a boolean")
        policy_matches_row = (
            isinstance(supplied_requires_external, bool)
            and supplied_requires_external == policy_requires_external
        )
        if not policy_matches_row:
            errors.append(f"rows[{index}]: {_category_policy_error(category, policy_requires_external)}")
        requires_external = policy_requires_external
        authentication_external_mode = supplied_requires_external if isinstance(supplied_requires_external, bool) else False
        supplied_identity = row.get("external_identity")
        external_identity = supplied_identity if isinstance(supplied_identity, dict) else {}
        for error in _external_identity_errors(external_identity, required=requires_external):
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
        authenticated_items = [item for item in evidence if isinstance(item, dict) and "authenticity" in item]
        if requires_external and len(external_items) > 1:
            errors.append(f"rows[{index}] external readiness rows accept exactly one external readiness association")
        if not requires_external and external_items:
            errors.append(f"rows[{index}] category policy requires local observed_check_result evidence")
        for evidence_index, item in enumerate(evidence):
            if not isinstance(item, dict):
                errors.append(f"rows[{index}].evidence[{evidence_index}] must be an object")
                continue
            for error in _evidence_errors(
                item,
                external_identity=external_identity,
                scope=scope,
                category=category,
                task_id=task_id,
                revision=revision,
                observed_before=created_at,
            ):
                errors.append(f"rows[{index}].evidence[{evidence_index}]: {error}")
        valid_evidence = [item for item in evidence if isinstance(item, dict)]
        receipt_ids.extend(_external_receipt_ids(valid_evidence))
        observation_ids.extend(_external_observation_ids(valid_evidence))
        check_ids.extend(_local_check_ids(valid_evidence))
        check_evidence_refs.extend(_local_evidence_refs(valid_evidence))
        if requires_external and any(
            item.get("schema_version") != EXTERNAL_READINESS_EVIDENCE_SCHEMA_VERSION for item in valid_evidence
        ):
            errors.append(f"rows[{index}] external observed success requires one exactly bound external association")
        derived_state = _derive_evidence_state(
            valid_evidence,
            requires_external=requires_external,
            external_identity=external_identity,
            matrix_schema_version=str(record.get("schema_version", "")),
            matrix_id=matrix_id,
            contract_ref=contract_ref,
            scope=scope,
            category=str(row.get("category", "")),
            task_id=task_id,
            revision=revision,
            fresh_after=fresh_after,
            observed_before=created_at,
            trusted_context=trusted_context,
        )
        if not policy_matches_row:
            derived_state = "missing"
        if row.get("evidence_state") not in READINESS_EVIDENCE_STATES:
            errors.append(f"rows[{index}].evidence_state is unsupported: {row.get('evidence_state')}")
        if authenticated_items and row.get("evidence_state") == "observed" and derived_state != "observed":
            authentication_errors = [
                _external_authenticity_error(
                    item,
                    matrix_schema_version=str(record.get("schema_version", "")),
                    matrix_id=matrix_id,
                    contract_ref=contract_ref,
                    scope=scope,
                    category=category,
                    task_id=task_id,
                    revision=revision,
                    created_at=created_at,
                    evidence_fresh_after=fresh_after,
                    requires_external_observation=authentication_external_mode,
                    external_identity=external_identity,
                    trusted_context=trusted_context,
                )
                for item in authenticated_items
            ]
            errors.extend(error for error in authentication_errors if error)
            errors.append(f"rows[{index}] external observed success requires one exactly bound authenticated association")
        if row.get("evidence_state") != derived_state:
            errors.append(f"rows[{index}].evidence_state must match derived evidence state {derived_state}")
        derived_rows.append({"category": row.get("category", ""), "evidence_state": derived_state})
    duplicate_receipts = sorted({receipt_id for receipt_id in receipt_ids if receipt_ids.count(receipt_id) > 1})
    if duplicate_receipts:
        errors.append(f"duplicate external readiness receipt associations: {', '.join(duplicate_receipts)}")
    duplicate_observations = _duplicates(observation_ids)
    if duplicate_observations:
        errors.append(f"duplicate external readiness postcondition associations: {', '.join(duplicate_observations)}")
    duplicate_checks = _duplicates(check_ids)
    if duplicate_checks:
        errors.append(f"duplicate observed_check_result check_id associations: {', '.join(duplicate_checks)}")
    duplicate_check_refs = _duplicates(check_evidence_refs)
    if duplicate_check_refs:
        errors.append(
            f"duplicate observed_check_result evidence_ref associations: {', '.join(duplicate_check_refs)}"
        )
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
    scope: str = "",
    category: str = "",
    task_id: str = "",
    revision: str = "",
    observed_before: str = "",
) -> list[str]:
    schema = evidence.get("schema_version")
    if schema == OBSERVED_CHECK_RESULT_SCHEMA_VERSION:
        errors = _observed_check_errors(
            evidence,
            scope=scope,
            category=category,
            task_id=task_id,
            revision=revision,
        )
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


def _observed_check_errors(
    evidence: dict[str, Any], *, scope: str, category: str, task_id: str, revision: str
) -> list[str]:
    errors: list[str] = []
    if set(evidence) != OBSERVED_CHECK_RESULT_KEYS:
        errors.append("observed_check_result keys must match observed_check_result/v1")
    result = evidence.get("result")
    if not isinstance(result, str) or result not in ("passed", "failed"):
        errors.append("observed_check_result result must be passed or failed")
    for field in ("check_id", "evidence_ref"):
        if not _valid_ref(evidence.get(field)):
            errors.append(f"observed_check_result {field} must be an opaque reference")
    if _timestamp(evidence.get("observed_at")) is None:
        errors.append("observed_check_result observed_at must be a timezone-aware ISO-8601 timestamp")
    authority = (
        ("scope", scope),
        ("category", category),
        ("task_id", task_id),
        ("revision", revision),
        ("row_id", readiness_row_id(scope, category, task_id, revision)),
    )
    for field, expected in authority:
        if evidence.get(field) != expected:
            errors.append(f"observed_check_result {field} must match row authority")
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
    postcondition_result = postcondition.get("result")
    if not isinstance(postcondition_result, str) or postcondition_result not in ("satisfied", "failed"):
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


def _external_authenticity_error(
    evidence: dict[str, Any],
    *,
    matrix_schema_version: str,
    matrix_id: str,
    contract_ref: dict[str, Any],
    scope: str,
    category: str,
    task_id: str,
    revision: str,
    created_at: str,
    evidence_fresh_after: str,
    requires_external_observation: bool,
    external_identity: dict[str, Any],
    trusted_context: ReadinessTrustContext | None,
) -> str:
    if type(trusted_context) is not ReadinessTrustContext or not _trusted_context_usable(trusted_context):
        return "external readiness authenticity requires a usable trusted context"
    authenticity = evidence.get("authenticity")
    if not isinstance(authenticity, dict) or set(authenticity) != {
        "schema_version",
        "algorithm",
        "context_id",
        "hmac_sha256",
    }:
        return "external readiness authenticity metadata is invalid"
    if (
        authenticity.get("schema_version") != EXTERNAL_READINESS_AUTHENTICITY_SCHEMA_VERSION
        or authenticity.get("algorithm") != EXTERNAL_READINESS_AUTHENTICITY_ALGORITHM
    ):
        return "external readiness authenticity metadata is invalid"
    if authenticity.get("context_id") != trusted_context.context_id:
        return "external readiness authenticity context does not match trusted context"
    candidate = authenticity.get("hmac_sha256")
    if not isinstance(candidate, str) or not _HMAC_RE.fullmatch(candidate):
        return "external readiness authenticity tag does not match trusted context"
    payload = _external_authenticity_payload(
        evidence,
        matrix_schema_version=matrix_schema_version,
        matrix_id=matrix_id,
        contract_ref=contract_ref,
        context_id=trusted_context.context_id,
        scope=scope,
        category=category,
        task_id=task_id,
        revision=revision,
        created_at=created_at,
        evidence_fresh_after=evidence_fresh_after,
        requires_external_observation=requires_external_observation,
        external_identity=external_identity,
    )
    if payload is None:
        return "external readiness signed data is not canonical JSON and is unauthenticated"
    if not trusted_context._verify(payload, candidate):
        return "external readiness authenticity tag does not match trusted context"
    return ""


def _external_authenticity_payload(
    evidence: dict[str, Any],
    *,
    matrix_schema_version: str,
    matrix_id: str,
    contract_ref: dict[str, Any],
    context_id: str,
    scope: str,
    category: str,
    task_id: str,
    revision: str,
    created_at: str,
    evidence_fresh_after: str,
    requires_external_observation: bool,
    external_identity: dict[str, Any],
) -> bytes | None:
    evidence_snapshot = _canonical_json_snapshot(evidence)
    if evidence_snapshot is None:
        return None
    unsigned_evidence = cast(dict[str, Any], evidence_snapshot[0])
    unsigned_evidence.pop("authenticity", None)
    payload = {
        "schema_version": "external_readiness_authenticity_payload/v1",
        "matrix_schema_version": matrix_schema_version,
        "matrix_id": matrix_id,
        "contract_ref": contract_ref,
        "context_id": context_id,
        "scope": scope,
        "category": category,
        "task_id": task_id,
        "revision": revision,
        "created_at": created_at,
        "evidence_fresh_after": evidence_fresh_after,
        "requires_external_observation": requires_external_observation,
        "external_identity": external_identity,
        "evidence": unsigned_evidence,
    }
    snapshot = _canonical_json_snapshot(payload)
    return None if snapshot is None else snapshot[1]


def _canonical_json_snapshot(value: object) -> tuple[Any, bytes] | None:
    nodes = [0]
    bytes_used = [0]
    active_containers: set[int] = set()
    try:
        snapshot = _snapshot_json_value(
            value,
            depth=0,
            nodes=nodes,
            bytes_used=bytes_used,
            active_containers=active_containers,
        )
        if snapshot is _CANONICAL_JSON_REJECTED:
            return None
        encoded = json.dumps(
            snapshot,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode()
    except (KeyError, RecursionError, RuntimeError, TypeError, UnicodeError, ValueError):
        return None
    if len(encoded) != bytes_used[0] or len(encoded) > READINESS_CANONICAL_JSON_MAX_BYTES:
        return None
    return snapshot, encoded


def _snapshot_json_value(
    value: object,
    *,
    depth: int,
    nodes: list[int],
    bytes_used: list[int],
    active_containers: set[int],
) -> Any:
    if depth > READINESS_CANONICAL_JSON_MAX_DEPTH:
        return _CANONICAL_JSON_REJECTED
    nodes[0] += 1
    if nodes[0] > READINESS_CANONICAL_JSON_MAX_NODES:
        return _CANONICAL_JSON_REJECTED
    if value is None or type(value) in (str, bool, int, float):
        if type(value) is float and not math.isfinite(cast(float, value)):
            return _CANONICAL_JSON_REJECTED
        if type(value) is str and len(cast(str, value)) > READINESS_CANONICAL_JSON_MAX_BYTES:
            return _CANONICAL_JSON_REJECTED
        if type(value) is int and cast(int, value).bit_length() > READINESS_CANONICAL_JSON_MAX_BYTES * 4:
            return _CANONICAL_JSON_REJECTED
        primitive_bytes = len(json.dumps(value, allow_nan=False).encode())
        return value if _consume_json_bytes(primitive_bytes, bytes_used) else _CANONICAL_JSON_REJECTED
    if type(value) not in (dict, list):
        return _CANONICAL_JSON_REJECTED
    identity = id(value)
    if identity in active_containers:
        return _CANONICAL_JSON_REJECTED
    if not _consume_json_bytes(2, bytes_used):
        return _CANONICAL_JSON_REJECTED
    active_containers.add(identity)
    try:
        if type(value) is list:
            source = cast(list[object], value)
            detached = _copy_plain_list(source)
            items: list[Any] = []
            for index, item in enumerate(detached):
                if index and not _consume_json_bytes(1, bytes_used):
                    return _CANONICAL_JSON_REJECTED
                snapshot = _snapshot_json_value(
                    item,
                    depth=depth + 1,
                    nodes=nodes,
                    bytes_used=bytes_used,
                    active_containers=active_containers,
                )
                if snapshot is _CANONICAL_JSON_REJECTED:
                    return _CANONICAL_JSON_REJECTED
                items.append(snapshot)
            return items if _same_shallow_sequence(detached, _copy_plain_list(source)) else _CANONICAL_JSON_REJECTED
        source_mapping = cast(dict[object, object], value)
        detached_mapping = _copy_plain_dict(source_mapping)
        mapping: dict[str, Any] = {}
        for index, (key, item) in enumerate(detached_mapping.items()):
            if type(key) is not str or len(key) > READINESS_CANONICAL_JSON_MAX_BYTES:
                return _CANONICAL_JSON_REJECTED
            separator_bytes = (1 if index else 0) + len(json.dumps(key).encode()) + 1
            if not _consume_json_bytes(separator_bytes, bytes_used):
                return _CANONICAL_JSON_REJECTED
            snapshot = _snapshot_json_value(
                item,
                depth=depth + 1,
                nodes=nodes,
                bytes_used=bytes_used,
                active_containers=active_containers,
            )
            if snapshot is _CANONICAL_JSON_REJECTED:
                return _CANONICAL_JSON_REJECTED
            mapping[key] = snapshot
        return (
            mapping
            if _same_shallow_mapping(detached_mapping, _copy_plain_dict(source_mapping))
            else _CANONICAL_JSON_REJECTED
        )
    finally:
        active_containers.remove(identity)


def _copy_plain_dict(value: dict[object, object]) -> dict[object, object]:
    return dict.copy(value)


def _copy_plain_list(value: list[object]) -> list[object]:
    return list.copy(value)


def _same_shallow_mapping(before: dict[object, object], after: dict[object, object]) -> bool:
    if len(before) != len(after) or set(before) != set(after):
        return False
    return all(_same_json_slot(value, after[key]) for key, value in before.items())


def _same_shallow_sequence(before: list[object], after: list[object]) -> bool:
    return len(before) == len(after) and all(
        _same_json_slot(left, right) for left, right in zip(before, after, strict=True)
    )


def _same_json_slot(left: object, right: object) -> bool:
    if type(left) in (dict, list):
        return left is right
    if type(left) in (str, bool, int, float) or left is None:
        return type(left) is type(right) and left == right
    return left is right


def _consume_json_bytes(amount: int, bytes_used: list[int]) -> bool:
    bytes_used[0] += amount
    return bytes_used[0] <= READINESS_CANONICAL_JSON_MAX_BYTES


def _trusted_context_usable(context: ReadinessTrustContext | None) -> bool:
    return type(context) is ReadinessTrustContext and context._usable()


def _derive_evidence_state(
    evidence: list[dict[str, Any]],
    *,
    requires_external: bool,
    external_identity: dict[str, Any],
    matrix_schema_version: str,
    matrix_id: str,
    contract_ref: dict[str, Any],
    scope: str,
    category: str,
    task_id: str,
    revision: str,
    fresh_after: str,
    observed_before: str,
    trusted_context: ReadinessTrustContext | None,
) -> str:
    states: list[str] = []
    for item in evidence:
        if _evidence_errors(
            item,
            external_identity=external_identity,
            scope=scope,
            category=category,
            task_id=task_id,
            revision=revision,
            observed_before=observed_before,
        ):
            continue
        schema = item.get("schema_version")
        if requires_external and schema != EXTERNAL_READINESS_EVIDENCE_SCHEMA_VERSION:
            continue
        if not requires_external and schema == EXTERNAL_READINESS_EVIDENCE_SCHEMA_VERSION:
            continue
        if schema == OBSERVED_CHECK_RESULT_SCHEMA_VERSION:
            observed_at = item.get("observed_at")
        else:
            observed_at = (item.get("postcondition") or {}).get("observed_at")
        if not _fresh(observed_at, fresh_after, observed_before):
            continue
        if schema == EXTERNAL_READINESS_EVIDENCE_SCHEMA_VERSION:
            if _external_authenticity_error(
                item,
                matrix_schema_version=matrix_schema_version,
                matrix_id=matrix_id,
                contract_ref=contract_ref,
                scope=scope,
                category=category,
                task_id=task_id,
                revision=revision,
                created_at=observed_before,
                evidence_fresh_after=fresh_after,
                requires_external_observation=requires_external,
                external_identity=external_identity,
                trusted_context=trusted_context,
            ):
                continue
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


def _local_check_ids(evidence: list[dict[str, Any]]) -> list[str]:
    return [
        str(item.get("check_id", ""))
        for item in evidence
        if item.get("schema_version") == OBSERVED_CHECK_RESULT_SCHEMA_VERSION
        and str(item.get("check_id", ""))
    ]


def _local_evidence_refs(evidence: list[dict[str, Any]]) -> list[str]:
    return [
        str(item.get("evidence_ref", ""))
        for item in evidence
        if item.get("schema_version") == OBSERVED_CHECK_RESULT_SCHEMA_VERSION
        and str(item.get("evidence_ref", ""))
    ]


def _duplicates(values: list[str]) -> list[str]:
    return sorted({value for value in values if values.count(value) > 1})


def _valid_ref(value: object) -> bool:
    return isinstance(value, str) and bool(_REF_RE.fullmatch(value))


def _category_requires_external_observation(category: str) -> bool:
    return next((required for name, required in READINESS_CATEGORY_POLICY if name == category), False)


def _category_policy_error(category: str, requires_external: bool) -> str:
    mode = "external observation" if requires_external else "local observation"
    return f"category policy requires {mode} for {category or '<unknown>'}"


def readiness_row_id(scope: str, category: str, task_id: str, revision: str) -> str:
    digest = hashlib.sha256(
        json.dumps(
            {"scope": scope, "category": category, "task_id": task_id, "revision": revision},
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
    ).hexdigest()[:16]
    return f"readiness-row-{digest}"


def _canonical_contract_ref() -> dict[str, Any]:
    return {
        "contract_id": READINESS_MATRIX_SCHEMA_VERSION,
        "enforcement_level": "executable_validated",
        "consumer_id": "validate_readiness_matrix",
        "category_policy": {
            "schema_version": READINESS_CATEGORY_POLICY_SCHEMA_VERSION,
            "categories": [
                {"category": category, "requires_external_observation": required}
                for category, required in READINESS_CATEGORY_POLICY
            ],
        },
    }


def _matrix_id(scope: str, task_id: str, revision: str, created_at: str) -> str:
    digest = hashlib.sha256(
        json.dumps(
            {"scope": scope, "task_id": task_id, "revision": revision, "created_at": created_at},
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
    ).hexdigest()[:16]
    return f"readiness-{digest}"
