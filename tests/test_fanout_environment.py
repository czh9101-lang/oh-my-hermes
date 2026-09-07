from __future__ import annotations

import unittest

from _local_package import load_local_package

load_local_package()


class ChildEnvironmentPolicyTests(unittest.TestCase):
    def test_owner_environment_uses_the_portable_base_and_declared_grants_only(self) -> None:
        from omh.coding.fanout_environment import resolve_child_environment

        decision = resolve_child_environment(
            {
                "PATH": "/usr/bin",
                "HOME": "/tmp/home",
                "LANG": "C",
                "CODEX_TOKEN": "owner-sentinel",
                "PROJECT_FLAG": "project-sentinel",
                "UNRELATED_PROVIDER_TOKEN": "must-not-leave-parent",
            },
            owner="codex",
            purpose="owner",
            declaration={
                "owner_capabilities": {"codex": ["CODEX_TOKEN"]},
                "project_variables": ["PROJECT_FLAG"],
            },
        )

        self.assertTrue(decision.ready)
        self.assertEqual(
            decision.environment,
            {
                "CODEX_TOKEN": "owner-sentinel",
                "HOME": "/tmp/home",
                "LANG": "C",
                "PATH": "/usr/bin",
                "PROJECT_FLAG": "project-sentinel",
            },
        )
        self.assertEqual(
            decision.receipt["passed"],
            ["CODEX_TOKEN", "HOME", "LANG", "PATH", "PROJECT_FLAG"],
        )
        self.assertEqual(decision.receipt["removed"], ["UNRELATED_PROVIDER_TOKEN"])
        self.assertNotIn("owner-sentinel", str(decision.receipt))
        self.assertNotIn("must-not-leave-parent", str(decision.receipt))

    def test_owner_state_flow_is_ready_without_an_environment_credential(self) -> None:
        from omh.coding.fanout_environment import resolve_child_environment

        decision = resolve_child_environment(
            {"PATH": "/usr/bin", "CODEX_HOME": "/tmp/codex"},
            owner="codex",
            purpose="owner",
        )

        self.assertTrue(decision.ready)
        self.assertEqual(decision.environment["CODEX_HOME"], "/tmp/codex")
        self.assertNotIn("CODEX_TOKEN", decision.receipt["passed"])

    def test_verification_requires_a_separate_grant_and_refuses_a_secret_override(self) -> None:
        from omh.coding.fanout_environment import resolve_child_environment

        declaration = {
            "owner_capabilities": {"codex": ["CODEX_TOKEN"]},
            "verification_capabilities": ["VERIFY_TOKEN"],
        }
        missing = resolve_child_environment(
            {"PATH": "/usr/bin", "CODEX_TOKEN": "owner-sentinel"},
            owner="codex",
            purpose="verification",
            declaration=declaration,
        )
        denied = resolve_child_environment(
            {"PATH": "/usr/bin", "VERIFY_TOKEN": "verification-sentinel"},
            owner="codex",
            purpose="verification",
            declaration=declaration,
            overrides={"API_TOKEN": "forbidden-sentinel"},
        )

        self.assertFalse(missing.ready)
        self.assertEqual(missing.receipt["missing"], ["VERIFY_TOKEN"])
        self.assertNotIn("CODEX_TOKEN", missing.environment)
        self.assertFalse(denied.ready)
        self.assertEqual(denied.receipt["denied"], ["API_TOKEN"])
        self.assertNotIn("API_TOKEN", denied.environment)
        self.assertNotIn("forbidden-sentinel", str(denied.receipt))

    def test_journal_keeps_the_bounded_name_only_policy_receipt(self) -> None:
        from omh.coding.fanout_environment import resolve_child_environment
        from omh.coding.fanout_journal import build_fanout_run_journal

        policy = resolve_child_environment(
            {"PATH": "/usr/bin", "OWNER_TOKEN": "journal-sentinel"},
            owner="codex",
            purpose="owner",
            declaration={"owner_capabilities": {"codex": ["OWNER_TOKEN"]}},
        ).receipt
        journal = build_fanout_run_journal(
            {
                "fanout_id": "fanout-test",
                "merge_order": ["core"],
                "units": [
                    {
                        "unit_id": "core",
                        "run_ref": "run",
                        "owner": "codex",
                        "status": "completed",
                        "process_succeeded": True,
                        "child_environment_policy": policy,
                    }
                ],
            }
        )

        receipt = journal["units"][0]["child_environment_policy"]
        self.assertEqual(receipt["schema_version"], "child_environment_policy/v1")
        self.assertNotIn("journal-sentinel", str(journal))

    def test_cli_policy_parser_keeps_name_only_declarations(self) -> None:
        import argparse

        from omh.commands.fanout_environment_parser import (
            add_fanout_environment_arguments,
            child_environment_policy_from_args,
        )

        parser = argparse.ArgumentParser()
        add_fanout_environment_arguments(parser)
        args = parser.parse_args(
            [
                "--owner-env",
                "codex:CODEX_TOKEN",
                "--project-env",
                "PROJECT_FLAG",
                "--verification-env",
                "VERIFY_TOKEN",
                "--deny-env",
                "UNRELATED_SECRET",
                "--allow-broad-environment",
            ]
        )

        self.assertEqual(
            child_environment_policy_from_args(args),
            {
                "allow_broad_inheritance": True,
                "owner_capabilities": {"codex": ["CODEX_TOKEN"]},
                "project_variables": ["PROJECT_FLAG"],
                "verification_capabilities": ["VERIFY_TOKEN"],
                "denied_names": ["UNRELATED_SECRET"],
            },
        )

    def test_explicit_compatibility_is_visible_without_serializing_values(self) -> None:
        from omh.coding.fanout_environment import resolve_child_environment

        decision = resolve_child_environment(
            {"PATH": "/usr/bin", "LEGACY_TOOL_ENV": "legacy-sentinel"},
            owner="codex",
            purpose="owner",
            declaration={"allow_broad_inheritance": True},
        )

        self.assertTrue(decision.ready)
        self.assertEqual(decision.environment["LEGACY_TOOL_ENV"], "legacy-sentinel")
        self.assertEqual(decision.receipt["compatibility"], "compatibility_explicit")
        self.assertNotIn("legacy-sentinel", str(decision.receipt))

    def test_policy_digest_covers_names_after_preview_lists_are_bounded(self) -> None:
        from omh.coding.fanout_environment import resolve_child_environment

        base_names = [f"PROJECT_{index:03d}" for index in range(80)]
        parent = {name: "sentinel" for name in [*base_names, "PROJECT_999"]}
        first = resolve_child_environment(
            parent,
            owner="codex",
            purpose="owner",
            declaration={"project_variables": base_names},
        )
        second = resolve_child_environment(
            parent,
            owner="codex",
            purpose="owner",
            declaration={"project_variables": [*base_names, "PROJECT_999"]},
        )

        self.assertNotEqual(first.receipt["policy_digest"], second.receipt["policy_digest"])
        self.assertEqual(first.receipt["counts"]["passed"], 80)
        self.assertEqual(second.receipt["counts"]["passed"], 81)
        self.assertTrue(first.receipt["truncated"]["passed"])

    def test_unrelated_denied_parent_name_is_removed_without_blocking_the_child(self) -> None:
        from omh.coding.fanout_environment import resolve_child_environment

        decision = resolve_child_environment(
            {"PATH": "/usr/bin", "UNRELATED_SECRET": "must-not-leave"},
            owner="codex",
            purpose="owner",
            declaration={"denied_names": ["UNRELATED_SECRET"]},
        )

        self.assertTrue(decision.ready)
        self.assertNotIn("UNRELATED_SECRET", decision.environment)
        self.assertEqual(decision.receipt["denied"], ["UNRELATED_SECRET"])

    def test_receipt_classifies_names_without_values_and_rejects_a_denied_required_grant(self) -> None:
        from omh.coding.fanout_environment import resolve_child_environment

        decision = resolve_child_environment(
            {"PATH": "/usr/bin", "OWNER_TOKEN": "must-not-persist"},
            owner="codex",
            purpose="owner",
            declaration={
                "owner_capabilities": {"codex": ["OWNER_TOKEN"]},
                "denied_names": ["OWNER_TOKEN"],
            },
        )

        self.assertFalse(decision.ready)
        classified = {entry["name"]: entry for entry in decision.receipt["classifications"]}
        self.assertEqual(classified["OWNER_TOKEN"]["classification"], "denied")
        self.assertEqual(classified["OWNER_TOKEN"]["reason"], "owner_capability")
        self.assertEqual(classified["OWNER_TOKEN"]["policy_source"], "declaration")
        self.assertNotIn("must-not-persist", str(decision.receipt))

    def test_lineage_receipt_matches_the_effective_child_environment(self) -> None:
        from omh.coding.fanout_dispatch import fanout_child_environment

        decision = fanout_child_environment(
            {"PATH": "/usr/bin"},
            depth=0,
            fanout_id="fanout-test",
            unit_id="core",
            owner="codex",
        )

        self.assertIn("OMH_FANOUT_DEPTH", decision.receipt["passed"])
        self.assertIn("OMH_FANOUT_LINEAGE", decision.receipt["passed"])
        self.assertEqual(decision.receipt["counts"]["passed"], len(decision.environment))

    def test_journal_rejects_unvalidated_policy_receipt_scalars(self) -> None:
        from omh.coding.fanout_journal import build_fanout_run_journal

        journal = build_fanout_run_journal(
            {
                "fanout_id": "fanout-test",
                "merge_order": ["core"],
                "units": [
                    {
                        "unit_id": "core",
                        "run_ref": "run",
                        "owner": "codex",
                        "status": "completed",
                        "process_succeeded": True,
                        "child_environment_policy": {
                            "schema_version": "child_environment_policy/v1",
                            "status": "ready",
                            "owner": "arbitrary owner text",
                            "purpose": "owner",
                            "compatibility": "least_privilege",
                            "approved": [],
                            "denied": [],
                            "missing": [],
                            "passed": [],
                            "removed": [],
                            "policy_digest": "not-a-digest",
                            "claim_boundary": "arbitrary boundary",
                        },
                    }
                ],
            }
        )

        self.assertNotIn("child_environment_policy", journal["units"][0])


if __name__ == "__main__":
    unittest.main()
