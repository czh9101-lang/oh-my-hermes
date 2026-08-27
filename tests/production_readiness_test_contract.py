from __future__ import annotations

# ruff: noqa: F405
from production_readiness_test_support import *  # noqa: F403


class OperationsArtifactContractTestsMixin:
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

    def test_resolver_map_exactly_matches_referenced_artifact_consumers(self) -> None:
            referenced = {
                item.consumer_id
                for item in OPERATIONS_ARTIFACT_CONTRACTS
                if item.enforcement_level != "guidance_only"
            }
            self.assertEqual(set(operations_contracts_workflow._CONSUMER_IMPORTS), referenced)
            with self.assertRaises(LookupError):
                resolve_artifact_contract_consumer("validate_readiness_matrix")

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
