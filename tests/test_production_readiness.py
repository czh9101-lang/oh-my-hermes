from __future__ import annotations

import copy
import json
import unittest
from pathlib import Path

from _local_package import load_local_package

load_local_package()
from omh.catalogs.playbooks import inspect_playbook
from omh.external_effect_receipts import build_external_effect_receipt
from omh.operations_contracts import (
    OPERATIONS_ARTIFACT_CONTRACTS,
    ArtifactContractRef,
    resolve_artifact_contract_consumer,
    validate_operations_artifact_contracts,
)
from omh.operations import operation_artifact_compatibility, validate_operation_artifact
from omh.production_readiness import (
    READINESS_CATEGORIES,
    READINESS_MATRIX_ROLLBACK_CONTRACT,
    build_readiness_matrix,
    validate_readiness_matrix,
)
from omh.skills.catalog import builtin_definitions
from omh.skills.render import workflow_reference_payload


CREATED = "2026-08-26T12:00:00Z"
FRESH_AFTER = "2026-08-26T11:00:00Z"


def observed_check(category: str, *, observed_at: str = CREATED) -> dict[str, object]:
    return {
        "schema_version": "observed_check_result/v1",
        "check_id": f"{category}-check",
        "result": "passed",
        "observed_at": observed_at,
        "evidence_ref": f"evidence-{category}",
    }


def external_evidence(
    category: str,
    *,
    effect_id: str | None = None,
    post_effect_id: str | None = None,
    receipt_observed_at: str = CREATED,
    postcondition_observed_at: str = CREATED,
) -> dict[str, object]:
    effect = effect_id or f"readiness:{category}"
    receipt = build_external_effect_receipt(
        effect_id=effect,
        action="ci_run",
        acting_surface="runtime_ci_record",
        observed_result="succeeded",
        run_id="run-1119",
        external_ref=f"external-{category}",
        observed_at=receipt_observed_at,
    )
    return {
        "schema_version": "external_readiness_evidence/v1",
        "receipt": receipt,
        "postcondition": {
            "schema_version": "observed_postcondition/v1",
            "effect_id": post_effect_id or effect,
            "external_ref": f"external-{category}",
            "result": "satisfied",
            "observed_at": postcondition_observed_at,
            "observation_id": f"postcondition-{category}",
        },
    }


def complete_rows() -> list[dict[str, object]]:
    return [
        {
            "category": category,
            "requires_external_observation": False,
            "evidence": [observed_check(category)],
        }
        for category in READINESS_CATEGORIES
    ]


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
        self.assertEqual({row["evidence_state"] for row in matrix["rows"]}, {"observed"})
        self.assertEqual(matrix["verdict"], "GO")
        self.assertEqual(validate_readiness_matrix(matrix), [])
        self.assertFalse(any(key in json.dumps(matrix).lower() for key in ('"score"', '"rank"', '"badge"')))

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
            {"category": category, "requires_external_observation": False, "evidence": []}
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
        rows[2] = {
            "category": "ci",
            "requires_external_observation": True,
            "evidence": [external_evidence("ci")],
        }
        matrix = build_readiness_matrix(
            scope="release-1119", rows=rows, created_at=CREATED, evidence_fresh_after=FRESH_AFTER
        )
        self.assertEqual(matrix["verdict"], "GO")
        self.assertEqual(matrix["rows"][2]["evidence_state"], "observed")
        self.assertEqual(validate_readiness_matrix(matrix), [])

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
        rows[2] = {"category": "ci", "requires_external_observation": True, "evidence": [evidence]}
        with self.assertRaisesRegex(ValueError, "requires observed_postcondition/v1"):
            build_readiness_matrix(
                scope="release-1119", rows=rows, created_at=CREATED, evidence_fresh_after=FRESH_AFTER
            )

    def test_receipt_postcondition_mismatch_does_not_observe(self) -> None:
        rows = complete_rows()
        rows[2] = {
            "category": "ci",
            "requires_external_observation": True,
            "evidence": [external_evidence("ci", post_effect_id="readiness:other")],
        }
        with self.assertRaisesRegex(ValueError, "effect_id must match"):
            build_readiness_matrix(
                scope="release-1119", rows=rows, created_at=CREATED, evidence_fresh_after=FRESH_AFTER
            )

    def test_stale_or_future_evidence_does_not_observe(self) -> None:
        for observed_at in ("2026-08-26T10:59:59Z", "2026-08-26T12:00:01Z"):
            with self.subTest(observed_at=observed_at):
                rows = complete_rows()
                rows[0]["evidence"] = [observed_check("build", observed_at=observed_at)]
                matrix = build_readiness_matrix(
                    scope="release-1119", rows=rows, created_at=CREATED, evidence_fresh_after=FRESH_AFTER
                )
                self.assertEqual(matrix["rows"][0]["evidence_state"], "missing")
                self.assertEqual(matrix["verdict"], "HOLD")

    def test_stale_receipt_or_postcondition_does_not_observe(self) -> None:
        for evidence in (
            external_evidence("ci", receipt_observed_at="2026-08-26T10:59:59Z"),
            external_evidence("ci", postcondition_observed_at="2026-08-26T10:59:59Z"),
        ):
            rows = complete_rows()
            rows[2] = {"category": "ci", "requires_external_observation": True, "evidence": [evidence]}
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
