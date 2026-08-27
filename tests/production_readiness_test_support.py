from __future__ import annotations

__all__ = (
    "ArtifactContractRef",
    "CREATED",
    "FRESH_AFTER",
    "FrozenInstanceError",
    "OPERATIONS_ARTIFACT_CONTRACTS",
    "Path",
    "READINESS_CANONICAL_JSON_MAX_BYTES",
    "READINESS_CANONICAL_JSON_MAX_DEPTH",
    "READINESS_CANONICAL_JSON_MAX_NODES",
    "READINESS_CATEGORIES",
    "READINESS_CATEGORY_POLICY",
    "READINESS_CATEGORY_POLICY_SCHEMA_VERSION",
    "READINESS_MATRIX_ROLLBACK_CONTRACT",
    "REVISION",
    "RUN_ID",
    "ReadinessTrustContext",
    "TASK_ID",
    "TRUST_CONTEXT",
    "TRUST_KEY",
    "TemporaryDirectory",
    "artifact_contracts_for_workflow",
    "asdict",
    "base64",
    "build_readiness_matrix",
    "builtin_definitions",
    "complete_rows",
    "copy",
    "external_evidence",
    "external_row",
    "hashlib",
    "inspect_playbook",
    "json",
    "logging",
    "observed_check",
    "operation_artifact_compatibility",
    "operations_contracts_workflow",
    "patch",
    "pickle",
    "readiness_json",
    "readiness_workflow",
    "resolve_artifact_contract_consumer",
    "threading",
    "validate_operation_artifact",
    "validate_operations_artifact_contracts",
    "validate_readiness_matrix",
    "workflow_reference_payload",
)

import base64
import copy
import hashlib
import json
import logging
import pickle
import threading
from dataclasses import FrozenInstanceError, asdict
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from _local_package import load_local_package

load_local_package()
from omh.catalogs.playbooks import inspect_playbook
from omh.external_effect_receipts import build_external_effect_receipt
from omh.operations_contracts import (
    OPERATIONS_ARTIFACT_CONTRACTS,
    ArtifactContractRef,
    artifact_contracts_for_workflow,
    resolve_artifact_contract_consumer,
    validate_operations_artifact_contracts,
)
from omh.operations import operation_artifact_compatibility, validate_operation_artifact
from omh.production_readiness import (
    READINESS_CANONICAL_JSON_MAX_BYTES,
    READINESS_CANONICAL_JSON_MAX_DEPTH,
    READINESS_CANONICAL_JSON_MAX_NODES,
    READINESS_CATEGORIES,
    READINESS_CATEGORY_POLICY,
    READINESS_CATEGORY_POLICY_SCHEMA_VERSION,
    READINESS_MATRIX_ROLLBACK_CONTRACT,
    ReadinessTrustContext,
    authenticate_external_readiness_evidence,
    build_readiness_matrix as _build_readiness_matrix,
    readiness_row_id,
    validate_readiness_matrix,
)
from omh.skills.catalog import builtin_definitions
from omh.skills.render import workflow_reference_payload
import omh.workflows.operations_contracts as operations_contracts_workflow
import omh.workflows.production_readiness as readiness_workflow


CREATED = "2026-08-26T12:00:00Z"
FRESH_AFTER = "2026-08-26T11:00:00Z"
TASK_ID = "task-1119"
REVISION = "abc123def456"
RUN_ID = "run-1119"
TRUST_KEY = b"trusted-readiness-key-material-1119"
TRUST_CONTEXT = ReadinessTrustContext("operator-readiness", TRUST_KEY)


def build_readiness_matrix(
    *,
    task_id: str = TASK_ID,
    revision: str = REVISION,
    trusted_context: ReadinessTrustContext | None = TRUST_CONTEXT,
    **kwargs: object,
) -> dict[str, object]:
    return _build_readiness_matrix(
        task_id=task_id,
        revision=revision,
        trusted_context=trusted_context,
        **kwargs,
    )


def observed_check(
    category: str,
    *,
    observed_at: str = CREATED,
    scope: str = "release-1119",
    task_id: str = TASK_ID,
    revision: str = REVISION,
) -> dict[str, object]:
    return {
        "schema_version": "observed_check_result/v1",
        "check_id": f"{category}-check",
        "result": "passed",
        "observed_at": observed_at,
        "evidence_ref": f"evidence-{category}",
        "scope": scope,
        "category": category,
        "task_id": task_id,
        "revision": revision,
        "row_id": readiness_row_id(scope, category, task_id, revision),
    }


def external_evidence(
    category: str,
    *,
    effect_id: str | None = None,
    post_effect_id: str | None = None,
    run_id: str = RUN_ID,
    post_run_id: str | None = None,
    task_id: str = TASK_ID,
    post_task_id: str | None = None,
    revision: str = REVISION,
    post_revision: str | None = None,
    receipt_observed_at: str = CREATED,
    postcondition_observed_at: str = CREATED,
) -> dict[str, object]:
    effect = effect_id or f"readiness:{category}"
    receipt = build_external_effect_receipt(
        effect_id=effect,
        action="ci_run",
        acting_surface="runtime_ci_record",
        observed_result="succeeded",
        run_id=run_id,
        external_ref=f"external-{category}",
        evidence_refs=(f"task:{task_id}", f"revision:{revision}"),
        observed_at=receipt_observed_at,
    )
    evidence = {
        "schema_version": "external_readiness_evidence/v1",
        "receipt": receipt,
        "postcondition": {
            "schema_version": "observed_postcondition/v1",
            "receipt_id": receipt["receipt_id"],
            "effect_id": post_effect_id or effect,
            "run_id": post_run_id or run_id,
            "task_id": post_task_id or task_id,
            "revision": post_revision or revision,
            "external_ref": f"external-{category}",
            "result": "satisfied",
            "observed_at": postcondition_observed_at,
            "observation_id": f"postcondition-{category}",
        },
    }
    return authenticate_external_readiness_evidence(
        evidence,
        scope="release-1119",
        category=category,
        task_id=task_id,
        revision=revision,
        created_at=CREATED,
        evidence_fresh_after=FRESH_AFTER,
        external_identity={"effect_id": effect, "run_id": run_id},
        trusted_context=TRUST_CONTEXT,
    )


def external_row(category: str, evidence: dict[str, object], *, effect_id: str | None = None, run_id: str = RUN_ID) -> dict[str, object]:
    return {
        "category": category,
        "requires_external_observation": True,
        "external_identity": {"effect_id": effect_id or f"readiness:{category}", "run_id": run_id},
        "evidence": [evidence],
    }


def complete_rows() -> list[dict[str, object]]:
    rows = [
        {
            "category": category,
            "requires_external_observation": False,
            "evidence": [observed_check(category)],
        }
        for category in READINESS_CATEGORIES
        if category != "ci"
    ]
    rows.insert(2, external_row("ci", external_evidence("ci")))
    return rows

import omh.workflows.production_readiness_json as readiness_json
