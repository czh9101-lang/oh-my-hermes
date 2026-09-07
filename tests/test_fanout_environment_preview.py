from __future__ import annotations

import unittest

from _local_package import load_local_package

load_local_package()


class ChildEnvironmentPreviewTests(unittest.TestCase):
    def test_nonportable_parent_name_is_omitted_from_the_journalable_preview(self) -> None:
        from omh.coding.fanout_environment import resolve_child_environment
        from omh.coding.fanout_journal import build_fanout_run_journal

        invalid_name = "X" * 129
        decision = resolve_child_environment(
            {invalid_name: "synthetic", "PATH": "/usr/bin"},
            owner="codex",
            purpose="owner",
        )
        baseline = resolve_child_environment(
            {"PATH": "/usr/bin"}, owner="codex", purpose="owner"
        )
        policy = decision.receipt
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

        self.assertEqual(decision.environment, {"PATH": "/usr/bin"})
        self.assertEqual(policy["removed"], [])
        self.assertEqual(policy["counts"]["removed"], 1)
        self.assertTrue(policy["truncated"]["removed"])
        self.assertTrue(policy["classifications_truncated"])
        self.assertNotIn(invalid_name, str(policy))
        self.assertNotIn("synthetic", str(policy))
        self.assertNotEqual(policy["policy_digest"], baseline.receipt["policy_digest"])
        receipt = journal["units"][0]["child_environment_policy"]
        self.assertEqual(receipt["counts"]["removed"], 1)
        self.assertNotIn(invalid_name, str(journal))
        self.assertNotIn("synthetic", str(journal))


if __name__ == "__main__":
    unittest.main()
