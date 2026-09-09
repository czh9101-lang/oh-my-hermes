from __future__ import annotations

import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from _cli_harness import run_cli
from _local_package import load_local_package


load_local_package()
from omh.local_store import atomic_write_json
from omh.paths import resolve_paths
from omh.wrapper.contract import build_chat_interaction_payload
from omh.workflows.memory_provider_posture import (
    AUTOMATIC_BEHAVIORS,
    GENERIC_READINESS_DIMENSIONS,
    IDENTITY_SCOPES,
    LIFECYCLE_OPERATIONS,
    SYNCHRONIZATION_DIMENSIONS,
    build_memory_provider_posture,
    parse_memory_provider_posture_input,
    write_memory_provider_posture,
)


_STATUSES = {"ready", "missing", "risky", "not_observed", "unknown"}


def _input_payload() -> dict[str, object]:
    """An opaque hosted candidate: authentication looks fine, lifecycle does not."""
    return {
        "schema_version": "memory_provider_posture_input/v1",
        "provider_id": "mem9",
        "observed_version_boundary": "0.4.1",
        "storage_boundary": "hosted_api",
        "identity_scopes": {"user": {"status": "risky", "evidence_class": "declared_documentation"}},
        "automatic_behaviors": {"native_write": {"status": "risky", "evidence_class": "declared_documentation"}},
        "lifecycle_operations": {"export": {"status": "not_observed", "evidence_class": "declared_documentation"}},
        "synchronization": {"direction": {"status": "risky", "evidence_class": "declared_documentation"}},
        "generic_readiness": {"egress": {"status": "risky", "evidence_class": "declared_documentation"}},
        "observed_trials": [],
    }


def _receipt(**overrides: str) -> dict[str, str]:
    receipt = {
        "receipt_id": "trial-1",
        "provider_id": "mem9",
        "scope": "user",
        "operation": "export",
        "observed_at": "2026-09-09T00:00:00Z",
        "postcondition": "export-archive-present",
    }
    receipt.update(overrides)
    return receipt


def _posture(payload: dict[str, object] | None = None) -> dict[str, object]:
    return build_memory_provider_posture(parse_memory_provider_posture_input(payload or _input_payload()))


class MemoryProviderPostureShapeTests(unittest.TestCase):
    def test_every_lifecycle_dimension_is_present_with_a_closed_status(self) -> None:
        posture = _posture()

        blocks = {
            "identity_scopes": IDENTITY_SCOPES,
            "automatic_behaviors": AUTOMATIC_BEHAVIORS,
            "lifecycle_operations": LIFECYCLE_OPERATIONS,
            "synchronization": SYNCHRONIZATION_DIMENSIONS,
            "generic_readiness": GENERIC_READINESS_DIMENSIONS,
        }
        for block_name, dimensions in blocks.items():
            block = posture[block_name]
            self.assertIsInstance(block, dict)
            self.assertEqual(set(block), set(dimensions), block_name)
            for dimension, field in block.items():
                with self.subTest(block=block_name, dimension=dimension):
                    self.assertIn(field["status"], _STATUSES)
                    self.assertIn("evidence_class", field)
        self.assertEqual(posture["schema_version"], "memory_provider_posture/v1")
        self.assertEqual(posture["state"], "prepared_not_observed")
        self.assertEqual(posture["storage_boundary"], "hosted_api")

    def test_an_unsupplied_dimension_stays_unknown_instead_of_inheriting_a_default(self) -> None:
        posture = _posture()

        self.assertEqual(posture["identity_scopes"]["tenant"], {"status": "unknown", "evidence_class": "none"})
        self.assertEqual(posture["synchronization"]["failure_mode"], {"status": "unknown", "evidence_class": "none"})
        self.assertEqual(posture["lifecycle_operations"]["provider_side_deletion"]["status"], "unknown")
        self.assertGreater(posture["unknown_field_count"], 0)

    def test_deletion_names_four_distinct_operations_with_distinct_postconditions(self) -> None:
        posture = _posture()

        lifecycle = posture["lifecycle_operations"]
        for operation in ("disable", "local_cache_removal", "provider_side_deletion", "account_deletion", "provider_switch"):
            self.assertIn(operation, lifecycle)
        # Four different rows, so a receipt for one can never settle another.
        receipt = _receipt(operation="local_cache_removal", postcondition="local-cache-absent")
        payload = _input_payload()
        payload["observed_trials"] = [receipt]

        settled = _posture(payload)

        self.assertEqual(settled["lifecycle_operations"]["local_cache_removal"]["status"], "ready")
        self.assertEqual(settled["lifecycle_operations"]["provider_side_deletion"]["status"], "unknown")
        self.assertEqual(settled["lifecycle_operations"]["account_deletion"]["status"], "unknown")


class MemoryProviderPostureBlockerTests(unittest.TestCase):
    def test_an_opaque_hosted_candidate_is_blocked_for_automatic_and_irreversible_use(self) -> None:
        posture = _posture()

        verdicts = posture["adoption_verdicts"]
        self.assertEqual(verdicts["automatic_write"]["verdict"], "blocked")
        self.assertEqual(verdicts["destructive_operation"]["verdict"], "blocked")
        self.assertEqual(verdicts["irreversible_adoption"]["verdict"], "blocked")
        blocker_ids = {blocker["id"] for blocker in posture["blockers"]}
        self.assertEqual(
            blocker_ids,
            {
                "deletion_semantics_unknown",
                "isolation_semantics_unknown",
                "write_semantics_unknown",
                "portability_unknown",
            },
        )

    def test_a_hook_observation_never_grants_write_authority(self) -> None:
        payload = _input_payload()
        payload["automatic_behaviors"] = {
            behavior: {"status": "ready", "evidence_class": "observed_local_runtime"}
            for behavior in AUTOMATIC_BEHAVIORS
        }

        posture = _posture(payload)

        for behavior, field in posture["automatic_behaviors"].items():
            with self.subTest(behavior=behavior):
                self.assertIs(field["grants_write_authority"], False)
        # Observed hooks settle write semantics; unknown deletion and isolation
        # still block the automatic use those hooks would enable.
        self.assertEqual(posture["adoption_verdicts"]["automatic_write"]["verdict"], "blocked")
        self.assertNotIn("write_semantics_unknown", {blocker["id"] for blocker in posture["blockers"]})

    def test_a_local_provider_stays_distinguishable_from_a_hosted_one(self) -> None:
        hosted = _input_payload()
        local = _input_payload()
        local["provider_id"] = "omh"
        local["storage_boundary"] = "local_runtime"
        local["generic_readiness"] = {"egress": {"status": "missing", "evidence_class": "observed_local_runtime"}}
        local["synchronization"] = {"direction": {"status": "missing", "evidence_class": "observed_local_runtime"}}

        hosted_posture, local_posture = _posture(hosted), _posture(local)

        self.assertNotEqual(hosted_posture["storage_boundary"], local_posture["storage_boundary"])
        self.assertNotEqual(
            hosted_posture["generic_readiness"]["egress"],
            local_posture["generic_readiness"]["egress"],
        )
        self.assertNotEqual(
            hosted_posture["synchronization"]["direction"],
            local_posture["synchronization"]["direction"],
        )

    def test_the_provider_stays_optional_and_never_a_required_default(self) -> None:
        posture = _posture()

        self.assertEqual(
            posture["optionality"],
            {
                "provider_required": False,
                "omh_memory_available_without_provider": True,
                "hosted_default_permitted": False,
                "unknown_provider_effect": "omh_memory_unaffected",
            },
        )


class MemoryProviderPostureEvidenceTests(unittest.TestCase):
    def test_a_bound_receipt_settles_only_its_own_operation(self) -> None:
        payload = _input_payload()
        payload["observed_trials"] = [_receipt()]

        posture = _posture(payload)

        export = posture["lifecycle_operations"]["export"]
        self.assertEqual(export["status"], "ready")
        self.assertEqual(export["evidence_class"], "observed_trial_receipt")
        self.assertEqual(export["observed_receipt_id"], "trial-1")
        self.assertEqual(export["observed_scope"], "user")
        self.assertEqual(posture["lifecycle_operations"]["import"]["status"], "unknown")
        self.assertEqual(posture["rejected_trials"], [])

    def test_a_mismatched_receipt_is_rejected_and_keeps_the_claim_blocked(self) -> None:
        payload = _input_payload()
        payload["observed_trials"] = [
            _receipt(receipt_id="trial-2", provider_id="other", operation="provider_side_deletion", postcondition="remote-copy-absent")
        ]

        posture = _posture(payload)

        self.assertEqual(posture["observed_trials"], [])
        self.assertEqual(
            posture["rejected_trials"],
            [{"receipt_id": "trial-2", "reason": "provider_identity_mismatch"}],
        )
        self.assertEqual(posture["lifecycle_operations"]["provider_side_deletion"]["status"], "unknown")
        self.assertEqual(posture["adoption_verdicts"]["destructive_operation"]["verdict"], "blocked")

    def test_a_receipt_missing_a_binding_field_is_refused(self) -> None:
        for dropped in ("receipt_id", "provider_id", "scope", "operation", "observed_at", "postcondition"):
            with self.subTest(dropped=dropped):
                receipt = _receipt()
                del receipt[dropped]
                payload = _input_payload()
                payload["observed_trials"] = [receipt]
                with self.assertRaisesRegex(ValueError, "observed trial receipt must bind"):
                    parse_memory_provider_posture_input(payload)

    def test_documentation_alone_can_never_report_a_field_ready(self) -> None:
        for evidence_class in ("declared_documentation", "declared_package_metadata", "operator_statement", "none"):
            with self.subTest(evidence_class=evidence_class):
                payload = _input_payload()
                payload["lifecycle_operations"] = {
                    "provider_side_deletion": {"status": "ready", "evidence_class": evidence_class}
                }
                with self.assertRaisesRegex(ValueError, "ready requires observed evidence"):
                    parse_memory_provider_posture_input(payload)

    def test_open_package_code_is_a_distinct_storage_boundary_from_the_service_that_runs_it(self) -> None:
        payload = _input_payload()
        payload["storage_boundary"] = "open_package_code"

        self.assertEqual(_posture(payload)["storage_boundary"], "open_package_code")

        payload["storage_boundary"] = "managed_saas"
        with self.assertRaisesRegex(ValueError, "storage_boundary must be one of"):
            parse_memory_provider_posture_input(payload)


class MemoryProviderPostureBoundaryTests(unittest.TestCase):
    def test_the_posture_prohibits_every_provider_side_and_mutating_action(self) -> None:
        posture = _posture()

        self.assertEqual(
            posture["prohibited_actions"],
            [
                "read_secret_value",
                "call_provider",
                "install_provider_plugin",
                "enable_automatic_hook",
                "change_provider_selection",
                "synchronize_records",
                "mutate_memory_store",
                "delete_provider_memory",
                "delete_account",
                "import_provider_records_into_omh_review",
            ],
        )
        self.assertIn("not provider connectivity", posture["claim_boundary"])

    def test_memory_sync_receives_the_posture_as_not_omh_reviewed_context_only(self) -> None:
        posture = _posture()

        handoff = posture["memory_sync_handoff"]
        self.assertEqual(handoff["review_status"], "not_omh_reviewed")
        self.assertIs(handoff["imports_provider_records"], False)
        self.assertIs(handoff["authorizes_native_memory_mutation"], False)
        self.assertEqual(handoff["next_action"], "prepare_memory_sync")

    def test_the_parser_refuses_identifiers_that_could_carry_secret_material(self) -> None:
        bad_provider = _input_payload()
        bad_provider["provider_id"] = "Mem9"
        with self.assertRaisesRegex(ValueError, "provider_id"):
            parse_memory_provider_posture_input(bad_provider)

        for secret_shaped in ("sk-live-123456789", "gho_12345678901234567890", "AIzaSyDUMMYABCDEFGHIJKLMNOPQRSTUVWX123"):
            with self.subTest(value=secret_shaped):
                payload = _input_payload()
                payload["observed_version_boundary"] = secret_shaped
                with self.assertRaisesRegex(ValueError, "safe opaque metadata reference"):
                    parse_memory_provider_posture_input(payload)

        unsupported = _input_payload()
        unsupported["identity_scopes"] = {"everything": {"status": "ready", "evidence_class": "observed_local_runtime"}}
        with self.assertRaisesRegex(ValueError, "unsupported dimensions"):
            parse_memory_provider_posture_input(unsupported)

    def test_write_uses_the_operations_memory_provider_store(self) -> None:
        with TemporaryDirectory() as tmp:
            paths = resolve_paths(Path(tmp) / ".omh", Path(tmp) / ".hermes")

            artifact = write_memory_provider_posture(paths, _posture())

            self.assertTrue(artifact["written"])
            self.assertTrue(artifact["path"].startswith(str(paths.memory_provider_postures_dir)))
            self.assertTrue(Path(artifact["path"]).exists())
            self.assertTrue(artifact["posture_id"].startswith("memory_provider_"))

    def test_write_rejects_storage_resolving_outside_omh_home(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            paths = resolve_paths(root / ".omh", root / ".hermes")
            paths.omh_home.mkdir()
            outside = root / "outside"
            outside.mkdir()
            paths.operations_dir.symlink_to(outside, target_is_directory=True)

            with self.assertRaisesRegex(ValueError, "resolve under OMH home"):
                write_memory_provider_posture(paths, _posture())

    def test_ops_cli_prepares_a_posture_without_contacting_the_provider(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            input_path = root / "provider.json"
            atomic_write_json(input_path, _input_payload())

            status, stdout, stderr = run_cli(
                ["--omh-home", str(root / ".omh"), "ops", "memory-provider-posture", "--input", str(input_path), "--write"]
            )

            self.assertEqual(status, 0, stderr)
            self.assertEqual(stderr, "")
            posture = json.loads(stdout)
            self.assertEqual(posture["state"], "prepared_not_observed")
            self.assertTrue(posture["artifact"]["written"])
            self.assertEqual(posture["adoption_verdicts"]["automatic_write"]["verdict"], "blocked")
            self.assertIn("call_provider", posture["prohibited_actions"])


class MemoryProviderLifecycleRoutingTests(unittest.TestCase):
    def test_lifecycle_questions_reach_the_strengthened_readiness_path(self) -> None:
        messages = (
            "memory provider adoption for yantrikdb",
            "Is it safe to switch memory provider from mem9 to remnic?",
            "delete provider memory for this workspace",
            "export memory provider data before I leave",
            "check memory provider portability before adoption",
            "memory provider sync failure keeps queueing",
            "disable memory provider without losing anything",
            "memory provider retention rules",
        )
        for message in messages:
            with self.subTest(message=message):
                payload = build_chat_interaction_payload(message, source="discord")

                self.assertEqual(payload["route"]["selected_skill"], "external-connector-readiness")
                self.assertEqual(payload["next_action"], "prepare_external_connector_readiness")

    def test_ordinary_native_memory_review_is_not_stolen(self) -> None:
        messages = (
            "clean up my stale project memory",
            "review what you remember about me",
            "memory-sync inspect stale MEMORY.md claims",
        )
        for message in messages:
            with self.subTest(message=message):
                payload = build_chat_interaction_payload(message, source="discord")

                self.assertEqual(payload["route"]["selected_skill"], "memory-sync")
                self.assertEqual(payload["next_action"], "prepare_memory_sync")

    def test_the_readiness_workflow_names_the_posture_artifact(self) -> None:
        from omh.skill_pack import builtin_definitions

        definition = {item.name: item for item in builtin_definitions()}["external-connector-readiness"]

        self.assertIn(
            "memory_provider_posture/v1 when the candidate is an optional memory provider",
            definition.expected_outputs,
        )
        self.assertTrue(
            any("memory_provider_posture/v1" in item for item in definition.artifact_expectations),
        )


if __name__ == "__main__":
    unittest.main()
