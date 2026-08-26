from __future__ import annotations

import base64
import copy
import hashlib
import json
import logging
import pickle
import threading
import unittest
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


class OperationsArtifactContractTests(unittest.TestCase):
    def test_every_listed_operations_contract_is_classified_once(self) -> None:
        expected = {
            "agent-ops-review": ("agent_operator_productivity/v1", "executable_validated"),
            "ops-observability-card": ("ops_service_quality_board/v1", "executable_validated"),
            "reliability-review": ("omh_operation_artifact/v1", "shared_operation_validated"),
            "production-audit": ("readiness_matrix/v1", "executable_validated"),
            "build-failure-triage": ("build_failure_triage_plan/v1", "guidance_only"),
            "security-safety-review": ("security_safety_review_plan/v1", "guidance_only"),
            "failure-signal-audit": ("failure_signal_audit_plan/v1", "guidance_only"),
            "ops-review": ("ops-review", "guidance_only"),
            "deploy-and-monitor": ("deploy-and-monitor", "guidance_only"),
            "support-operations": ("support-operations", "guidance_only"),
        }
        self.assertEqual(
            {item.workflow_id: (item.contract_id, item.enforcement_level) for item in OPERATIONS_ARTIFACT_CONTRACTS},
            expected,
        )
        self.assertEqual(validate_operations_artifact_contracts(), [])

    def test_generated_catalog_payload_exposes_machine_contract_refs(self) -> None:
        payload = workflow_reference_payload()
        skills = {item["name"]: item for item in payload["skills"]}
        for item in OPERATIONS_ARTIFACT_CONTRACTS:
            self.assertEqual(
                skills[item.workflow_id]["artifact_contracts"],
                [
                    {
                        "contract_id": item.contract_id,
                        "enforcement_level": item.enforcement_level,
                        "consumer_id": item.consumer_id,
                    }
                ],
            )

    def test_validated_contracts_have_resolvable_consumers(self) -> None:
        for item in OPERATIONS_ARTIFACT_CONTRACTS:
            with self.subTest(workflow=item.workflow_id):
                if item.enforcement_level == "guidance_only":
                    self.assertEqual(item.consumer_id, "")
                else:
                    self.assertTrue(item.consumer_id)
                    self.assertTrue(callable(resolve_artifact_contract_consumer(item.consumer_id)))

    def test_enforcement_level_is_closed_and_orthogonal_to_evidence_state(self) -> None:
        with self.assertRaises(ValueError):
            ArtifactContractRef("x/v1", "observed", "consumer")
        definitions = {item.name: item for item in builtin_definitions()}
        for item in OPERATIONS_ARTIFACT_CONTRACTS:
            refs = definitions[item.workflow_id].artifact_contracts
            self.assertEqual(refs, (item.ref,))
            self.assertFalse(hasattr(refs[0], "evidence_state"))

    def test_production_audit_contract_ref_matches_registry_shared_fields(self) -> None:
        registered = artifact_contracts_for_workflow("production-audit")[0]
        canonical = readiness_workflow._canonical_contract_ref()
        self.assertEqual(
            (canonical["contract_id"], canonical["enforcement_level"], canonical["consumer_id"]),
            (registered.contract_id, registered.enforcement_level, registered.consumer_id),
        )
        validator = resolve_artifact_contract_consumer("validate_operation_artifact")
        self.assertIsInstance(validator({}), list)

    def test_reliability_catalog_consumers_use_canonical_contract(self) -> None:
        playbook = inspect_playbook("reliability-incident-review")["playbook"]
        contracts = {stage["contract"] for stage in playbook["stages"]}
        self.assertIn("omh_operation_artifact/v1", contracts)
        self.assertNotIn("operation_artifact/v1", contracts)

    def test_reliability_legacy_fixture_is_readable_without_upgrade(self) -> None:
        fixture = json.loads(Path("tests/fixtures/operations/legacy-reliability-artifact.json").read_text(encoding="utf-8"))
        self.assertEqual(fixture["schema_version"], "operation_artifact/v1")
        self.assertEqual(validate_operation_artifact(fixture), [])
        compatibility = operation_artifact_compatibility(fixture)
        self.assertTrue(compatibility["compatible"])
        self.assertTrue(compatibility["legacy"])
        self.assertEqual(compatibility["migration_action"], "none")
        self.assertEqual(compatibility["evidence_state_action"], "preserve")
        self.assertEqual(READINESS_MATRIX_ROLLBACK_CONTRACT["legacy_read_policy"], "read_without_upgrade")
        self.assertEqual(READINESS_MATRIX_ROLLBACK_CONTRACT["canonical_reliability_contract"], "omh_operation_artifact/v1")
        self.assertIn("operation_artifact/v1", READINESS_MATRIX_ROLLBACK_CONTRACT["compatible_legacy_contracts"])


class ReadinessMatrixTests(unittest.TestCase):
    def test_complete_unique_observed_rows_derive_go(self) -> None:
        matrix = build_readiness_matrix(
            scope="release-1119",
            rows=complete_rows(),
            created_at=CREATED,
            evidence_fresh_after=FRESH_AFTER,
        )
        self.assertEqual(matrix["schema_version"], "readiness_matrix/v1")
        self.assertEqual([row["category"] for row in matrix["rows"]], list(READINESS_CATEGORIES))
        self.assertEqual(len(READINESS_CATEGORY_POLICY), 9)
        self.assertEqual(dict(READINESS_CATEGORY_POLICY), {category: category == "ci" for category in READINESS_CATEGORIES})
        self.assertEqual(
            matrix["contract_ref"]["category_policy"]["schema_version"],
            READINESS_CATEGORY_POLICY_SCHEMA_VERSION,
        )
        self.assertEqual({row["evidence_state"] for row in matrix["rows"]}, {"observed"})
        self.assertEqual(matrix["verdict"], "GO")
        self.assertEqual(validate_readiness_matrix(matrix, trusted_context=TRUST_CONTEXT), [])
        self.assertFalse(any(key in json.dumps(matrix).lower() for key in ('"score"', '"rank"', '"badge"')))

    def test_builder_detaches_every_mutable_caller_container(self) -> None:
        caller_rows = complete_rows()

        matrix = build_readiness_matrix(
            scope="release-1119",
            rows=caller_rows,
            created_at=CREATED,
            evidence_fresh_after=FRESH_AFTER,
        )
        captured = json.dumps(matrix, sort_keys=True)

        def mutable_container_ids(value: object) -> set[int]:
            if type(value) is dict:
                return {id(value)} | {
                    identity
                    for item in value.values()
                    for identity in mutable_container_ids(item)
                }
            if type(value) is list:
                return {id(value)} | {
                    identity
                    for item in value
                    for identity in mutable_container_ids(item)
                }
            return set()

        self.assertFalse(mutable_container_ids(caller_rows) & mutable_container_ids(matrix))
        caller_rows[0]["evidence"][0]["result"] = "failed"
        caller_rows[2]["external_identity"]["effect_id"] = "readiness:mutated"
        caller_rows[2]["evidence"][0]["receipt"]["summary"] = "mutated caller receipt"
        caller_rows[2]["evidence"][0]["postcondition"]["result"] = "failed"

        self.assertEqual(json.dumps(matrix, sort_keys=True), captured)
        self.assertEqual(matrix["verdict"], "GO")
        self.assertEqual(matrix["rows"][0]["evidence"][0]["result"], "passed")
        self.assertEqual(matrix["rows"][2]["external_identity"]["effect_id"], "readiness:ci")
        self.assertEqual(matrix["rows"][2]["evidence"][0]["receipt"]["summary"], "")

    def test_missing_or_duplicate_rows_are_rejected(self) -> None:
        matrix = build_readiness_matrix(
            scope="release-1119", rows=complete_rows(), created_at=CREATED, evidence_fresh_after=FRESH_AFTER
        )
        missing = copy.deepcopy(matrix)
        missing["rows"].pop()
        duplicate = copy.deepcopy(matrix)
        duplicate["rows"][-1] = copy.deepcopy(duplicate["rows"][0])
        self.assertIn("missing readiness categories", "; ".join(validate_readiness_matrix(missing)))
        self.assertIn("duplicate readiness categories", "; ".join(validate_readiness_matrix(duplicate)))

    def test_prepared_only_rows_derive_hold(self) -> None:
        rows = [
            {
                "category": category,
                "requires_external_observation": category == "ci",
                "external_identity": ({"effect_id": "readiness:ci", "run_id": RUN_ID} if category == "ci" else {}),
                "evidence": [],
            }
            for category in READINESS_CATEGORIES
        ]
        matrix = build_readiness_matrix(
            scope="release-1119", rows=rows, created_at=CREATED, evidence_fresh_after=FRESH_AFTER
        )
        self.assertEqual(matrix["verdict"], "HOLD")
        self.assertEqual({row["evidence_state"] for row in matrix["rows"]}, {"missing"})
        self.assertEqual(validate_readiness_matrix(matrix), [])

    def test_failed_observation_derives_block(self) -> None:
        rows = complete_rows()
        rows[0]["evidence"] = [{**observed_check("build"), "result": "failed"}]
        matrix = build_readiness_matrix(
            scope="release-1119", rows=rows, created_at=CREATED, evidence_fresh_after=FRESH_AFTER
        )
        self.assertEqual(matrix["verdict"], "BLOCK")
        self.assertEqual(matrix["rows"][0]["evidence_state"], "failed")

    def test_external_success_requires_matching_receipt_and_postcondition(self) -> None:
        rows = complete_rows()
        rows[2] = external_row("ci", external_evidence("ci"))
        matrix = build_readiness_matrix(
            scope="release-1119", rows=rows, created_at=CREATED, evidence_fresh_after=FRESH_AFTER
        )
        self.assertEqual(matrix["verdict"], "GO")
        self.assertEqual(matrix["rows"][2]["evidence_state"], "observed")
        self.assertEqual(validate_readiness_matrix(matrix, trusted_context=TRUST_CONTEXT), [])

    def test_registry_owned_external_policy_cannot_be_coherently_downgraded(self) -> None:
        rows = complete_rows()
        rows[2] = external_row("ci", external_evidence("ci"))
        matrix = build_readiness_matrix(
            scope="release-1119", rows=rows, created_at=CREATED, evidence_fresh_after=FRESH_AFTER
        )
        self.assertEqual(matrix["verdict"], "GO")
        downgraded = copy.deepcopy(matrix)
        ci_row = downgraded["rows"][2]
        ci_row["requires_external_observation"] = False
        ci_row["external_identity"] = {}
        ci_row["evidence"] = [observed_check("ci")]
        ci_row["evidence_state"] = "observed"
        downgraded["verdict"] = "GO"
        downgraded["missing_categories"] = []

        errors = validate_readiness_matrix(downgraded, trusted_context=TRUST_CONTEXT)

        self.assertIn("category policy requires external observation", "; ".join(errors))
        self.assertIn("verdict must match derived verdict HOLD", "; ".join(errors))

        upgraded = copy.deepcopy(matrix)
        build_row = upgraded["rows"][0]
        build_row["requires_external_observation"] = True
        build_row["external_identity"] = copy.deepcopy(matrix["rows"][2]["external_identity"])
        build_row["evidence"] = copy.deepcopy(matrix["rows"][2]["evidence"])
        build_row["evidence_state"] = "observed"
        upgraded_errors = "; ".join(validate_readiness_matrix(upgraded, trusted_context=TRUST_CONTEXT))
        self.assertIn("category policy requires local observation for build", upgraded_errors)
        self.assertIn("verdict must match derived verdict HOLD", upgraded_errors)

    def test_local_observations_are_row_bound_and_globally_unique(self) -> None:
        matrix = build_readiness_matrix(
            scope="release-1119", rows=complete_rows(), created_at=CREATED, evidence_fresh_after=FRESH_AFTER
        )
        replayed = copy.deepcopy(matrix)
        replayed["rows"][1]["evidence"] = copy.deepcopy(replayed["rows"][0]["evidence"])

        errors = validate_readiness_matrix(replayed, trusted_context=TRUST_CONTEXT)
        rendered = "; ".join(errors)

        self.assertIn("observed_check_result category must match row authority", rendered)
        self.assertIn("duplicate observed_check_result check_id", rendered)
        self.assertIn("duplicate observed_check_result evidence_ref", rendered)
        self.assertIn("verdict must match derived verdict HOLD", rendered)

    def test_forged_observed_state_is_rejected(self) -> None:
        matrix = build_readiness_matrix(
            scope="release-1119", rows=complete_rows(), created_at=CREATED, evidence_fresh_after=FRESH_AFTER
        )
        matrix["rows"][2]["requires_external_observation"] = True
        self.assertIn("external observed success requires", "; ".join(validate_readiness_matrix(matrix)))
        self.assertIn("verdict must match derived verdict", "; ".join(validate_readiness_matrix(matrix)))

    def test_receipt_without_postcondition_does_not_observe(self) -> None:
        evidence = external_evidence("ci")
        evidence.pop("postcondition")
        rows = complete_rows()
        rows[2] = external_row("ci", evidence)
        with self.assertRaisesRegex(ValueError, "requires observed_postcondition/v1"):
            build_readiness_matrix(
                scope="release-1119", rows=rows, created_at=CREATED, evidence_fresh_after=FRESH_AFTER
            )

    def test_receipt_postcondition_mismatch_does_not_observe(self) -> None:
        rows = complete_rows()
        rows[2] = external_row("ci", external_evidence("ci", post_effect_id="readiness:other"))
        with self.assertRaisesRegex(ValueError, "effect_id must match"):
            build_readiness_matrix(
                scope="release-1119", rows=rows, created_at=CREATED, evidence_fresh_after=FRESH_AFTER
            )

    def test_stale_evidence_holds_and_future_evidence_is_rejected(self) -> None:
        stale_rows = complete_rows()
        stale_rows[0]["evidence"] = [observed_check("build", observed_at="2026-08-26T10:59:59Z")]
        matrix = build_readiness_matrix(
            scope="release-1119", rows=stale_rows, created_at=CREATED, evidence_fresh_after=FRESH_AFTER
        )
        self.assertEqual(matrix["rows"][0]["evidence_state"], "missing")
        self.assertEqual(matrix["verdict"], "HOLD")

        future_rows = complete_rows()
        future_rows[0]["evidence"] = [observed_check("build", observed_at="2026-08-26T12:00:01Z")]
        with self.assertRaisesRegex(ValueError, "must not be after matrix created_at"):
            build_readiness_matrix(
                scope="release-1119", rows=future_rows, created_at=CREATED, evidence_fresh_after=FRESH_AFTER
            )

    def test_stale_receipt_or_postcondition_does_not_observe(self) -> None:
        for evidence in (
            external_evidence("ci", receipt_observed_at="2026-08-26T10:59:59Z"),
            external_evidence(
                "ci",
                receipt_observed_at="2026-08-26T10:59:59Z",
                postcondition_observed_at="2026-08-26T10:59:59Z",
            ),
        ):
            rows = complete_rows()
            rows[2] = external_row("ci", evidence)
            matrix = build_readiness_matrix(
                scope="release-1119", rows=rows, created_at=CREATED, evidence_fresh_after=FRESH_AFTER
            )
            self.assertEqual(matrix["rows"][2]["evidence_state"], "missing")
            self.assertEqual(matrix["verdict"], "HOLD")

    def test_plans_exits_artifacts_and_self_reports_are_not_observations(self) -> None:
        for schema in (
            "production_audit_plan/v1",
            "dispatch_exit/v1",
            "generated_artifact/v1",
            "self_report/v1",
        ):
            with self.subTest(schema=schema):
                rows = complete_rows()
                rows[0]["evidence"] = [{"schema_version": schema, "result": "passed", "observed_at": CREATED}]
                with self.assertRaisesRegex(ValueError, "unsupported readiness evidence schema"):
                    build_readiness_matrix(
                        scope="release-1119", rows=rows, created_at=CREATED, evidence_fresh_after=FRESH_AFTER
                    )

    def test_external_binding_is_exact_across_matrix_row_receipt_and_postcondition(self) -> None:
        rows = complete_rows()
        rows[2] = external_row("ci", external_evidence("ci"))
        matrix = build_readiness_matrix(
            scope="release-1119",
            task_id=TASK_ID,
            revision=REVISION,
            rows=rows,
            created_at=CREATED,
            evidence_fresh_after=FRESH_AFTER,
        )
        self.assertEqual(matrix["verdict"], "GO")
        self.assertEqual(validate_readiness_matrix(matrix, trusted_context=TRUST_CONTEXT), [])

    def test_otherwise_valid_unrelated_receipt_cannot_produce_go(self) -> None:
        rows = complete_rows()
        rows[2] = external_row("ci", external_evidence("ci", run_id="run-unrelated"))
        with self.assertRaisesRegex(ValueError, "receipt run_id must match external_identity.run_id"):
            build_readiness_matrix(
                scope="release-1119",
                task_id=TASK_ID,
                revision=REVISION,
                rows=rows,
                created_at=CREATED,
                evidence_fresh_after=FRESH_AFTER,
            )

    def test_external_binding_rejects_absent_mismatched_and_forged_associations(self) -> None:
        cases = {
            "absent": external_row("ci", external_evidence("ci")),
            "task": external_row("ci", external_evidence("ci", post_task_id="task-other")),
            "receipt_task": external_row("ci", external_evidence("ci", task_id="task-other")),
            "revision": external_row("ci", external_evidence("ci", post_revision="revision-other")),
            "receipt_revision": external_row("ci", external_evidence("ci", revision="revision-other")),
            "run": external_row("ci", external_evidence("ci", post_run_id="run-other")),
            "receipt": external_row("ci", external_evidence("ci")),
        }
        cases["absent"].pop("external_identity")
        cases["receipt"]["evidence"][0]["postcondition"]["receipt_id"] = "receipt-forged"
        for name, row in cases.items():
            with self.subTest(name=name):
                rows = complete_rows()
                rows[2] = row
                with self.assertRaises(ValueError):
                    build_readiness_matrix(
                        scope="release-1119",
                        task_id=TASK_ID,
                        revision=REVISION,
                        rows=rows,
                        created_at=CREATED,
                        evidence_fresh_after=FRESH_AFTER,
                    )

    def test_duplicate_external_associations_are_rejected(self) -> None:
        duplicate = external_evidence("ci")
        rows = complete_rows()
        row = external_row("ci", duplicate)
        row["evidence"].append(copy.deepcopy(duplicate))
        rows[2] = row
        with self.assertRaisesRegex(ValueError, "exactly one external readiness association"):
            build_readiness_matrix(
                scope="release-1119",
                task_id=TASK_ID,
                revision=REVISION,
                rows=rows,
                created_at=CREATED,
                evidence_fresh_after=FRESH_AFTER,
            )

    def test_duplicate_receipt_association_across_rows_is_rejected(self) -> None:
        evidence = external_evidence("ci")
        rows = complete_rows()
        rows[2] = external_row("ci", evidence)
        rows[3] = external_row("ci", copy.deepcopy(evidence), effect_id="readiness:ci")
        with self.assertRaisesRegex(ValueError, "duplicate external readiness receipt associations"):
            build_readiness_matrix(
                scope="release-1119", rows=rows, created_at=CREATED, evidence_fresh_after=FRESH_AFTER
            )

    def test_duplicate_postcondition_association_across_rows_is_rejected(self) -> None:
        first = external_evidence("ci")
        second = external_evidence("ci", run_id="run-second")
        second["postcondition"]["observation_id"] = first["postcondition"]["observation_id"]
        rows = complete_rows()
        rows[2] = external_row("ci", first)
        rows[3] = external_row("ci", second, run_id="run-second")
        with self.assertRaisesRegex(ValueError, "duplicate external readiness postcondition associations"):
            build_readiness_matrix(
                scope="release-1119", rows=rows, created_at=CREATED, evidence_fresh_after=FRESH_AFTER
            )

    def test_timestamp_validation_rejects_naive_aware_orderings_without_crashing(self) -> None:
        cases = (
            ("2026-08-26T12:00:00", FRESH_AFTER, CREATED),
            (CREATED, "2026-08-26T11:00:00", CREATED),
            (CREATED, FRESH_AFTER, "2026-08-26T11:30:00"),
        )
        for observed_at, fresh_after, created_at in cases:
            with self.subTest(observed_at=observed_at, fresh_after=fresh_after, created_at=created_at):
                rows = complete_rows()
                rows[0]["evidence"] = [observed_check("build", observed_at=observed_at)]
                with self.assertRaisesRegex(ValueError, "timezone-aware ISO-8601 timestamp"):
                    build_readiness_matrix(
                        scope="release-1119",
                        task_id=TASK_ID,
                        revision=REVISION,
                        rows=rows,
                        created_at=created_at,
                        evidence_fresh_after=fresh_after,
                    )

    def test_coherently_forged_public_bundle_cannot_authenticate(self) -> None:
        rows = complete_rows()
        rows[2] = external_row("ci", external_evidence("ci"))
        matrix = build_readiness_matrix(
            scope="release-1119", rows=rows, created_at=CREATED, evidence_fresh_after=FRESH_AFTER
        )
        forged = copy.deepcopy(matrix)
        forged_task = "task-forged"
        forged_revision = "revision-forged"
        forged_run = "run-forged"
        forged_effect = "readiness:forged"
        forged["task_id"] = forged_task
        forged["revision"] = forged_revision
        forged["matrix_id"] = "readiness-" + hashlib.sha256(
            json.dumps(
                {
                    "scope": forged["scope"],
                    "task_id": forged_task,
                    "revision": forged_revision,
                    "created_at": forged["created_at"],
                },
                sort_keys=True,
                separators=(",", ":"),
            ).encode()
        ).hexdigest()[:16]
        row = forged["rows"][2]
        row["external_identity"] = {"effect_id": forged_effect, "run_id": forged_run}
        receipt = row["evidence"][0]["receipt"]
        receipt["effect_id"] = forged_effect
        receipt["run_id"] = forged_run
        receipt["evidence_refs"] = [f"task:{forged_task}", f"revision:{forged_revision}"]
        receipt["receipt_id"] = "receipt-" + hashlib.sha256(b"coherent-public-forgery").hexdigest()[:24]
        postcondition = row["evidence"][0]["postcondition"]
        postcondition.update(
            {
                "receipt_id": receipt["receipt_id"],
                "effect_id": forged_effect,
                "run_id": forged_run,
                "task_id": forged_task,
                "revision": forged_revision,
            }
        )
        row["evidence"][0]["authenticity"]["hmac_sha256"] = hashlib.sha256(
            json.dumps(row["evidence"][0], sort_keys=True, default=str).encode()
        ).hexdigest()

        errors = validate_readiness_matrix(forged, trusted_context=TRUST_CONTEXT)

        self.assertIn("external readiness authenticity tag does not match trusted context", "; ".join(errors))
        self.assertIn("verdict must match derived verdict HOLD", "; ".join(errors))

    def test_authenticated_stale_hold_cannot_widen_freshness_into_go(self) -> None:
        rows = complete_rows()
        stale = external_evidence(
            "ci",
            receipt_observed_at="2026-08-26T10:59:59Z",
            postcondition_observed_at="2026-08-26T10:59:59Z",
        )
        rows[2] = external_row("ci", stale)
        matrix = build_readiness_matrix(
            scope="release-1119", rows=rows, created_at=CREATED, evidence_fresh_after=FRESH_AFTER
        )
        self.assertEqual(matrix["verdict"], "HOLD")
        widened = copy.deepcopy(matrix)
        widened["evidence_fresh_after"] = "2026-08-26T10:00:00Z"
        widened["rows"][2]["evidence_state"] = "observed"
        widened["verdict"] = "GO"
        widened["missing_categories"] = []

        errors = validate_readiness_matrix(widened, trusted_context=TRUST_CONTEXT)

        self.assertIn("external readiness authenticity tag does not match trusted context", "; ".join(errors))
        self.assertIn("verdict must match derived verdict HOLD", "; ".join(errors))

    def test_authentication_binds_all_mutable_verdict_authority_fields(self) -> None:
        rows = complete_rows()
        rows[2] = external_row("ci", external_evidence("ci"))
        matrix = build_readiness_matrix(
            scope="release-1119", rows=rows, created_at=CREATED, evidence_fresh_after=FRESH_AFTER
        )
        mutations = []
        changed_created = copy.deepcopy(matrix)
        changed_created["created_at"] = "2026-08-26T12:30:00Z"
        changed_created["matrix_id"] = "readiness-" + hashlib.sha256(
            json.dumps(
                {
                    "scope": changed_created["scope"],
                    "task_id": changed_created["task_id"],
                    "revision": changed_created["revision"],
                    "created_at": changed_created["created_at"],
                },
                sort_keys=True,
                separators=(",", ":"),
            ).encode()
        ).hexdigest()[:16]
        mutations.append(changed_created)
        changed_schema = copy.deepcopy(matrix)
        changed_schema["schema_version"] = "readiness_matrix/v2"
        mutations.append(changed_schema)
        changed_external_mode = copy.deepcopy(matrix)
        changed_external_mode["rows"][2]["requires_external_observation"] = False
        mutations.append(changed_external_mode)
        for mutated in mutations:
            with self.subTest(mutation=mutated):
                self.assertIn(
                    "external readiness authenticity tag does not match trusted context",
                    "; ".join(validate_readiness_matrix(mutated, trusted_context=TRUST_CONTEXT)),
                )

    def test_trust_context_retains_no_addressable_raw_key_and_subclasses_fail_closed(self) -> None:
        context = ReadinessTrustContext("operator-readiness", TRUST_KEY)
        self.assertFalse(hasattr(context, "_ReadinessTrustContext__hmac_key"))
        self.assertFalse(
            any(
                isinstance(getattr(context, name), bytes)
                for name in ("context_id", "_ReadinessTrustContext__hmac_template", "_ReadinessTrustContext__usable")
            )
        )

        def bytes_leaves(value: object) -> list[bytes]:
            if isinstance(value, bytes):
                return [value]
            if isinstance(value, dict):
                return [leaf for item in value.values() for leaf in bytes_leaves(item)]
            if isinstance(value, tuple):
                return [leaf for item in value for leaf in bytes_leaves(item)]
            return []

        self.assertNotIn(TRUST_KEY, bytes_leaves(object.__getstate__(context)))

        class HostileTrustContext(ReadinessTrustContext):
            def _usable(self) -> bool:
                raise RuntimeError("raw hostile trust failure")

            def _verify(self, payload: bytes, candidate: str) -> bool:
                raise RuntimeError("raw hostile trust failure")

        rows = complete_rows()
        rows[2] = external_row("ci", external_evidence("ci"))
        matrix = build_readiness_matrix(
            scope="release-1119", rows=rows, created_at=CREATED, evidence_fresh_after=FRESH_AFTER
        )
        hostile = HostileTrustContext("operator-readiness", TRUST_KEY)

        errors = validate_readiness_matrix(matrix, trusted_context=hostile)

        self.assertIn("external readiness authenticity requires a usable trusted context", errors)
        self.assertIn("verdict must match derived verdict HOLD", errors)
        self.assertNotIn("hostile", "; ".join(errors))
        doc = ReadinessTrustContext.__doc__ or ""
        self.assertIn("same-process introspection", doc)
        self.assertNotIn("no retrieval surface", doc)

    def test_trust_context_rendering_redacts_key_material(self) -> None:
        key = b"trusted-readiness-key-material-1119"
        context = ReadinessTrustContext("operator-readiness", key)
        rendered = (
            repr(context),
            str(context),
            f"{context}",
            "%r" % context,
            "%s" % context,
            format(context),
            str(RuntimeError(context)),
            logging.Formatter("%(message)s").format(
                logging.LogRecord("test", logging.ERROR, __file__, 1, "%r", (context,), None)
            ),
        )
        forbidden = (key.decode(), key.hex(), base64.b64encode(key).decode())
        for text in rendered:
            self.assertIn("redacted", text.lower())
            for value in forbidden:
                self.assertNotIn(value, text)

    def test_supplied_matrix_id_is_authenticated_not_only_structurally_checked(self) -> None:
        rows = complete_rows()
        rows[2] = external_row("ci", external_evidence("ci"))
        matrix = build_readiness_matrix(
            scope="release-1119", rows=rows, created_at=CREATED, evidence_fresh_after=FRESH_AFTER
        )
        matrix["matrix_id"] = "readiness-forged-public-id"
        errors = validate_readiness_matrix(matrix, trusted_context=TRUST_CONTEXT)
        self.assertIn("matrix_id must match canonical", "; ".join(errors))
        self.assertIn("external readiness authenticity tag does not match trusted context", "; ".join(errors))

    def test_same_key_cannot_relabel_authenticated_evidence_to_another_context(self) -> None:
        rows = complete_rows()
        rows[2] = external_row("ci", external_evidence("ci"))
        matrix = build_readiness_matrix(
            scope="release-1119", rows=rows, created_at=CREATED, evidence_fresh_after=FRESH_AFTER
        )
        matrix["rows"][2]["evidence"][0]["authenticity"]["context_id"] = "other-context"
        other_context = ReadinessTrustContext("other-context", TRUST_KEY)
        errors = validate_readiness_matrix(matrix, trusted_context=other_context)
        self.assertIn("external readiness authenticity tag does not match trusted context", "; ".join(errors))
        self.assertIn("verdict must match derived verdict HOLD", "; ".join(errors))

    def test_every_mutable_verdict_or_trust_authority_has_a_rejection_path(self) -> None:
        rows = complete_rows()
        rows[2] = external_row("ci", external_evidence("ci"))
        original = build_readiness_matrix(
            scope="release-1119", rows=rows, created_at=CREATED, evidence_fresh_after=FRESH_AFTER
        )

        def mutate(path: tuple[object, ...], value: object) -> dict[str, object]:
            record = copy.deepcopy(original)
            target = record
            for part in path[:-1]:
                target = target[part]
            target[path[-1]] = value
            return record

        authentication_cases = (
            (("schema_version",), "readiness_matrix/v2"),
            (("matrix_id",), "readiness-public-forgery"),
            (("scope",), "scope-forged"),
            (("task_id",), "task-forged"),
            (("revision",), "revision-forged"),
            (("created_at",), "2026-08-26T12:30:00Z"),
            (("evidence_fresh_after",), "2026-08-26T10:00:00Z"),
            (("contract_ref", "consumer_id"), "forged_consumer"),
            (("rows", 2, "category"), "category-forged"),
            (("rows", 2, "requires_external_observation"), False),
            (("rows", 2, "external_identity", "effect_id"), "readiness:forged"),
            (("rows", 2, "external_identity", "run_id"), "run-forged"),
            (("rows", 2, "evidence", 0, "authenticity", "schema_version"), "external_readiness_authenticity/v2"),
            (("rows", 2, "evidence", 0, "authenticity", "algorithm"), "public-sha256"),
            (("rows", 2, "evidence", 0, "authenticity", "context_id"), "context-forged"),
            (("rows", 2, "evidence", 0, "authenticity", "hmac_sha256"), "0" * 64),
        )
        for path, value in authentication_cases:
            with self.subTest(path=path):
                errors = validate_readiness_matrix(mutate(path, value), trusted_context=TRUST_CONTEXT)
                self.assertIn("external readiness authenticity", "; ".join(errors))

        def leaf_paths(value: object, prefix: tuple[object, ...] = ()) -> list[tuple[object, ...]]:
            if isinstance(value, dict):
                return [path for key, item in value.items() for path in leaf_paths(item, (*prefix, key))]
            if isinstance(value, list):
                return [prefix]
            return [prefix]

        unsigned_evidence = original["rows"][2]["evidence"][0]
        for relative_path in leaf_paths(
            {key: value for key, value in unsigned_evidence.items() if key != "authenticity"}
        ):
            path = ("rows", 2, "evidence", 0, *relative_path)
            current = unsigned_evidence
            for part in relative_path:
                current = current[part]
            changed = [*current, "forged"] if isinstance(current, list) else f"{current}-forged"
            with self.subTest(authenticated_evidence_path=path):
                errors = validate_readiness_matrix(mutate(path, changed), trusted_context=TRUST_CONTEXT)
                self.assertIn("external readiness authenticity", "; ".join(errors))

        derived_cases = (
            (("verdict",), "HOLD"),
            (("missing_categories",), ["ci"]),
            (("rows", 2, "evidence_state"), "missing"),
            (("rollback_contract", "legacy_evidence_state_action"), "upgrade"),
        )
        for path, value in derived_cases:
            with self.subTest(path=path):
                self.assertTrue(validate_readiness_matrix(mutate(path, value), trusted_context=TRUST_CONTEXT))

        signed_paths = (
            ("schema_version",),
            ("matrix_id",),
            ("contract_ref",),
            ("scope",),
            ("task_id",),
            ("revision",),
            ("created_at",),
            ("evidence_fresh_after",),
            ("rows", 2, "category"),
            ("rows", 2, "requires_external_observation"),
            ("rows", 2, "external_identity"),
            ("rows", 2, "evidence", 0, "schema_version"),
            *(("rows", 2, "evidence", 0, "receipt", field) for field in original["rows"][2]["evidence"][0]["receipt"]),
            *(("rows", 2, "evidence", 0, "postcondition", field) for field in original["rows"][2]["evidence"][0]["postcondition"]),
        )
        adversarial_values = (
            None,
            False,
            7,
            1.5,
            [],
            {},
            {"nested": []},
            set(),
            ("tuple",),
            b"bytes",
            float("nan"),
            {"string-key": "value", 1: "mixed-key"},
        )
        for path in signed_paths:
            for value in adversarial_values:
                with self.subTest(adversarial_signed_path=path, value_type=type(value).__name__):
                    errors = validate_readiness_matrix(mutate(path, value), trusted_context=TRUST_CONTEXT)
                    rendered = "; ".join(errors)
                    self.assertTrue(errors)
                    self.assertIn("verdict must match derived verdict HOLD", rendered)
                    self.assertTrue(
                        "external readiness authenticity" in rendered
                        or "readiness_matrix signed data is not safely bounded canonical JSON" in rendered
                    )
                    self.assertNotIn(TRUST_KEY.decode(), rendered)

        stable_boundary_errors = [
            "readiness_matrix signed data is not safely bounded canonical JSON",
            "external readiness evidence is unauthenticated",
            "verdict must match derived verdict HOLD",
        ]
        cyclic_top = copy.deepcopy(original)
        cyclic_top["cycle"] = cyclic_top
        cyclic_receipt = copy.deepcopy(original)
        receipt = cyclic_receipt["rows"][2]["evidence"][0]["receipt"]
        receipt["cycle"] = receipt
        cyclic_postcondition = copy.deepcopy(original)
        postcondition = cyclic_postcondition["rows"][2]["evidence"][0]["postcondition"]
        postcondition["cycle"] = postcondition
        excessive_nesting: object = "leaf"
        for _ in range(1_500):
            excessive_nesting = [excessive_nesting]
        deeply_nested = copy.deepcopy(original)
        deeply_nested["rows"][2]["evidence"][0]["receipt"]["summary"] = excessive_nesting
        excessive_nodes = copy.deepcopy(original)
        excessive_nodes["rows"][2]["evidence"][0]["receipt"]["summary"] = [
            None
        ] * READINESS_CANONICAL_JSON_MAX_NODES
        excessive_bytes = copy.deepcopy(original)
        excessive_bytes["rows"][2]["evidence"][0]["receipt"]["summary"] = (
            "x" * READINESS_CANONICAL_JSON_MAX_BYTES
        )
        self.assertEqual(
            (
                READINESS_CANONICAL_JSON_MAX_DEPTH,
                READINESS_CANONICAL_JSON_MAX_NODES,
                READINESS_CANONICAL_JSON_MAX_BYTES,
            ),
            (16, 2_048, 65_536),
        )

        class HostileDict(dict[object, object]):
            item_calls = 0

            def items(self) -> object:
                type(self).item_calls += 1
                raise KeyError("hostile mapping hook must not escape or render")

        class HostileList(list[object]):
            iteration_calls = 0

            def __iter__(self) -> object:
                type(self).iteration_calls += 1
                raise ValueError("hostile sequence hook must not escape or render")

        hostile_top = HostileDict(original)
        hostile_receipt = copy.deepcopy(original)
        hostile_receipt["rows"][2]["evidence"][0]["receipt"] = HostileDict(
            hostile_receipt["rows"][2]["evidence"][0]["receipt"]
        )
        hostile_postcondition = copy.deepcopy(original)
        hostile_postcondition["rows"][2]["evidence"][0]["postcondition"] = HostileDict(
            hostile_postcondition["rows"][2]["evidence"][0]["postcondition"]
        )
        hostile_sequence = copy.deepcopy(original)
        hostile_sequence["rows"] = HostileList(hostile_sequence["rows"])
        boundary_cases = (
            cyclic_top,
            cyclic_receipt,
            cyclic_postcondition,
            deeply_nested,
            excessive_nodes,
            excessive_bytes,
            hostile_top,
            hostile_receipt,
            hostile_postcondition,
            hostile_sequence,
        )
        for case in boundary_cases:
            with self.subTest(public_boundary_case=type(case).__name__):
                errors = validate_readiness_matrix(case, trusted_context=TRUST_CONTEXT)
                self.assertEqual(errors, stable_boundary_errors)
                rendered = "; ".join(errors)
                self.assertNotIn(TRUST_KEY.decode(), rendered)
                self.assertNotIn("hostile", rendered)
        self.assertEqual(HostileDict.item_calls, 0)
        self.assertEqual(HostileList.iteration_calls, 0)

    def test_post_child_snapshot_mutations_cannot_escape_root_authority(self) -> None:
        stable_errors = [
            "readiness_matrix signed data is not safely bounded canonical JSON",
            "external readiness evidence is unauthenticated",
            "verdict must match derived verdict HOLD",
        ]
        cases = (
            ("receipt", ("rows", 2, "evidence", 0, "receipt"), "dict"),
            ("postcondition", ("rows", 2, "evidence", 0, "postcondition"), "dict"),
            ("row", ("rows", 2), "dict"),
            ("external_identity", ("rows", 2, "external_identity"), "dict"),
            ("evidence_list", ("rows", 2, "evidence"), "list"),
        )
        for name, path, container_kind in cases:
            with self.subTest(window=name):
                matrix = build_readiness_matrix(
                    scope="release-1119",
                    rows=complete_rows(),
                    created_at=CREATED,
                    evidence_fresh_after=FRESH_AFTER,
                )
                target: object = matrix
                for part in path:
                    target = target[part]
                mutation_observed = threading.Event()
                target_copy_calls = 0
                if container_kind == "dict":
                    original_copy = readiness_workflow._copy_plain_dict

                    def controlled_dict_copy(value: dict[object, object]) -> dict[object, object]:
                        nonlocal target_copy_calls
                        detached = original_copy(value)
                        if value is target:
                            target_copy_calls += 1
                            if target_copy_calls == 2:
                                value["post-snapshot-mutation"] = True
                                mutation_observed.set()
                        return detached

                    manager = patch.object(
                        readiness_workflow,
                        "_copy_plain_dict",
                        side_effect=controlled_dict_copy,
                    )
                else:
                    original_list_copy = readiness_workflow._copy_plain_list

                    def controlled_list_copy(value: list[object]) -> list[object]:
                        nonlocal target_copy_calls
                        detached = original_list_copy(value)
                        if value is target:
                            target_copy_calls += 1
                            if target_copy_calls == 2:
                                value.append({"post-snapshot-mutation": True})
                                mutation_observed.set()
                        return detached

                    manager = patch.object(
                        readiness_workflow,
                        "_copy_plain_list",
                        side_effect=controlled_list_copy,
                    )
                with manager:
                    errors = validate_readiness_matrix(matrix, trusted_context=TRUST_CONTEXT)
                self.assertTrue(mutation_observed.is_set())
                self.assertGreaterEqual(target_copy_calls, 2)
                self.assertEqual(errors, stable_errors)

                accepted_matrix = build_readiness_matrix(
                    scope="release-1119",
                    rows=complete_rows(),
                    created_at=CREATED,
                    evidence_fresh_after=FRESH_AFTER,
                )
                accepted_target: object = accepted_matrix
                for part in path:
                    accepted_target = accepted_target[part]
                captured_before = json.dumps(accepted_target, sort_keys=True)
                root_snapshot_calls = 0
                original_snapshot = readiness_workflow._canonical_json_snapshot

                def mutate_after_second_root_snapshot(value: object) -> object:
                    nonlocal root_snapshot_calls
                    snapshot = original_snapshot(value)
                    if value is accepted_matrix:
                        root_snapshot_calls += 1
                        if root_snapshot_calls == 2:
                            if container_kind == "dict":
                                accepted_target["post-snapshot-mutation"] = True
                            else:
                                accepted_target.append({"post-snapshot-mutation": True})
                    return snapshot

                with patch.object(
                    readiness_workflow,
                    "_canonical_json_snapshot",
                    side_effect=mutate_after_second_root_snapshot,
                ):
                    result = readiness_workflow.parse_readiness_matrix(
                        accepted_matrix, trusted_context=TRUST_CONTEXT
                    )
                self.assertGreaterEqual(root_snapshot_calls, 2)
                self.assertTrue(result.accepted)
                self.assertEqual(result.verdict, "GO")
                captured_target: object = result.require_artifact().detached_copy()
                for part in path:
                    captured_target = captured_target[part]
                self.assertEqual(json.dumps(captured_target, sort_keys=True), captured_before)
                self.assertNotEqual(json.dumps(accepted_target, sort_keys=True), captured_before)

    def test_typed_parse_result_is_the_only_accepted_artifact_authority(self) -> None:
        parse = getattr(readiness_workflow, "parse_readiness_matrix", None)
        self.assertTrue(callable(parse))
        matrix = build_readiness_matrix(
            scope="release-1119", rows=complete_rows(), created_at=CREATED, evidence_fresh_after=FRESH_AFTER
        )
        root_copy_calls = 0
        original_copy = readiness_workflow._copy_plain_dict

        def count_root_copies(value: dict[object, object]) -> dict[object, object]:
            nonlocal root_copy_calls
            if value is matrix:
                root_copy_calls += 1
            return original_copy(value)

        with patch.object(readiness_workflow, "_copy_plain_dict", side_effect=count_root_copies):
            result = parse(matrix, trusted_context=TRUST_CONTEXT)
        self.assertTrue(result.accepted)
        self.assertEqual(result.errors, ())
        self.assertGreaterEqual(root_copy_calls, 2)
        artifact = result.require_artifact()
        self.assertEqual(artifact.verdict, "GO")
        captured = artifact.detached_copy()
        matrix["verdict"] = "HOLD"
        matrix["rows"][2]["evidence_state"] = "missing"
        self.assertEqual(artifact.verdict, "GO")
        self.assertEqual(artifact.detached_copy(), captured)
        self.assertIsNot(artifact.detached_copy(), artifact.detached_copy())
        with self.assertRaises(FrozenInstanceError):
            artifact.verdict = "HOLD"
        with TemporaryDirectory() as tmp:
            persisted = Path(tmp) / "readiness.json"
            persisted.write_bytes(artifact.canonical_bytes)
            self.assertEqual(json.loads(persisted.read_bytes()), captured)
        registered = artifact_contracts_for_workflow("production-audit")[0]
        self.assertEqual(registered.consumer_id, "parse_readiness_matrix")
        self.assertIs(resolve_artifact_contract_consumer(registered.consumer_id), parse)
        shared_leaf = {"nested": ["accepted"]}
        shared_snapshot = readiness_workflow._canonical_json_snapshot(
            {"left": shared_leaf, "right": shared_leaf}
        )
        self.assertIsNotNone(shared_snapshot)
        detached_dag = shared_snapshot[0]
        self.assertEqual(detached_dag["left"], detached_dag["right"])
        self.assertIsNot(detached_dag["left"], detached_dag["right"])

    def test_concurrent_builtin_container_mutation_fails_closed_without_retry(self) -> None:
        rows = complete_rows()
        rows[2] = external_row("ci", external_evidence("ci"))
        matrix = build_readiness_matrix(
            scope="release-1119", rows=rows, created_at=CREATED, evidence_fresh_after=FRESH_AFTER
        )
        trigger = threading.Event()
        mutated = threading.Event()
        original_consume = readiness_workflow._consume_json_bytes
        original_copy = readiness_workflow._copy_plain_dict
        consume_calls = 0
        root_copy_calls = 0

        def mutate_once() -> None:
            self.assertTrue(trigger.wait(timeout=2))
            matrix["concurrent-mutation"] = True
            mutated.set()

        def controlled_consume(amount: int, bytes_used: list[int]) -> bool:
            nonlocal consume_calls
            consume_calls += 1
            accepted = original_consume(amount, bytes_used)
            if consume_calls == 2:
                trigger.set()
                self.assertTrue(mutated.wait(timeout=2))
            return accepted

        def controlled_copy(value: dict[object, object]) -> dict[object, object]:
            nonlocal root_copy_calls
            if value is matrix:
                root_copy_calls += 1
            return original_copy(value)

        worker = threading.Thread(target=mutate_once)
        worker.start()
        try:
            with (
                patch.object(readiness_workflow, "_consume_json_bytes", side_effect=controlled_consume),
                patch.object(readiness_workflow, "_copy_plain_dict", side_effect=controlled_copy),
            ):
                errors = validate_readiness_matrix(matrix, trusted_context=TRUST_CONTEXT)
        finally:
            worker.join(timeout=2)
        self.assertFalse(worker.is_alive())
        self.assertEqual(
            errors,
            [
                "readiness_matrix signed data is not safely bounded canonical JSON",
                "external readiness evidence is unauthenticated",
                "verdict must match derived verdict HOLD",
            ],
        )
        self.assertGreaterEqual(consume_calls, 2)
        self.assertGreaterEqual(root_copy_calls, 2)

        list_matrix = build_readiness_matrix(
            scope="release-1119", rows=complete_rows(), created_at=CREATED, evidence_fresh_after=FRESH_AFTER
        )
        rows_list = list_matrix["rows"]
        list_trigger = threading.Event()
        list_mutated = threading.Event()
        original_list_copy = readiness_workflow._copy_plain_list
        rows_copy_calls = 0

        def mutate_list_once() -> None:
            self.assertTrue(list_trigger.wait(timeout=2))
            rows_list.append(copy.deepcopy(rows_list[0]))
            list_mutated.set()

        def controlled_list_copy(value: list[object]) -> list[object]:
            nonlocal rows_copy_calls
            detached = original_list_copy(value)
            if value is rows_list:
                rows_copy_calls += 1
                if rows_copy_calls == 1:
                    list_trigger.set()
                    self.assertTrue(list_mutated.wait(timeout=2))
            return detached

        list_worker = threading.Thread(target=mutate_list_once)
        list_worker.start()
        try:
            with patch.object(readiness_workflow, "_copy_plain_list", side_effect=controlled_list_copy):
                list_errors = validate_readiness_matrix(list_matrix, trusted_context=TRUST_CONTEXT)
        finally:
            list_worker.join(timeout=2)
        self.assertFalse(list_worker.is_alive())
        self.assertEqual(list_errors, errors)
        self.assertGreaterEqual(rows_copy_calls, 2)

    def test_trust_context_is_opaque_and_non_serializable(self) -> None:
        context = ReadinessTrustContext("operator-readiness", TRUST_KEY)
        operations = (
            lambda: asdict(context),
            lambda: copy.copy(context),
            lambda: copy.deepcopy(context),
            lambda: pickle.dumps(context),
            lambda: context.__reduce__(),
            lambda: context.__reduce_ex__(4),
            lambda: memoryview(context),
            lambda: json.dumps(context),
        )
        for operation in operations:
            with self.subTest(operation=operation):
                with self.assertRaises((TypeError, pickle.PicklingError)):
                    operation()
        self.assertFalse(hasattr(context, "hmac_key"))
        self.assertFalse(hasattr(context, "material"))

    def test_stale_public_matrix_id_is_rejected(self) -> None:
        matrix = build_readiness_matrix(
            scope="release-1119", rows=complete_rows(), created_at=CREATED, evidence_fresh_after=FRESH_AFTER
        )
        matrix["task_id"] = "task-forged"
        self.assertIn(
            "matrix_id must match canonical scope, task, revision, and created_at identity",
            validate_readiness_matrix(matrix),
        )

    def test_absent_wrong_or_untrusted_context_cannot_advance_external_evidence(self) -> None:
        rows = complete_rows()
        rows[2] = external_row("ci", external_evidence("ci"))
        valid = build_readiness_matrix(
            scope="release-1119", rows=rows, created_at=CREATED, evidence_fresh_after=FRESH_AFTER
        )
        contexts = (
            None,
            ReadinessTrustContext("operator-readiness", b"wrong-readiness-key-material-1119"),
            ReadinessTrustContext("other-context", b"trusted-readiness-key-material-1119"),
            ReadinessTrustContext("operator-readiness", b"short"),
        )
        for context in contexts:
            with self.subTest(context=context):
                errors = validate_readiness_matrix(valid, trusted_context=context)
                self.assertIn("external readiness authenticity", "; ".join(errors))
                rebuilt = build_readiness_matrix(
                    scope="release-1119",
                    rows=rows,
                    created_at=CREATED,
                    evidence_fresh_after=FRESH_AFTER,
                    trusted_context=context,
                )
                self.assertEqual(rebuilt["verdict"], "HOLD")
                self.assertEqual(rebuilt["rows"][2]["evidence_state"], "missing")

    def test_valid_authentication_persists_no_secret(self) -> None:
        rows = complete_rows()
        rows[2] = external_row("ci", external_evidence("ci"))
        matrix = build_readiness_matrix(
            scope="release-1119", rows=rows, created_at=CREATED, evidence_fresh_after=FRESH_AFTER
        )
        rendered = json.dumps(matrix, sort_keys=True)
        self.assertEqual(matrix["verdict"], "GO")
        self.assertEqual(validate_readiness_matrix(matrix, trusted_context=TRUST_CONTEXT), [])
        self.assertNotIn(TRUST_KEY.decode(), rendered)

    def test_validator_rejects_each_invalid_top_level_state(self) -> None:
        matrix = build_readiness_matrix(
            scope="release-1119", rows=complete_rows(), created_at=CREATED, evidence_fresh_after=FRESH_AFTER
        )
        mutations = {
            "schema": ("schema_version", "wrong/v1", "schema_version must be readiness_matrix/v1"),
            "scope": ("scope", "", "scope is required"),
            "verdict": ("verdict", "PASS", "unsupported verdict"),
        }
        for name, (key, value, expected) in mutations.items():
            with self.subTest(name=name):
                invalid = copy.deepcopy(matrix)
                invalid[key] = value
                self.assertIn(expected, "; ".join(validate_readiness_matrix(invalid)))
