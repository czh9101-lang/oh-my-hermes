"""Existing memory policy characterization tests.

Tests existing v1 policy behavior including naive expiry at UTC boundaries,
episode default TTLs, and manual vs auto-safe admission paths.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from _local_package import load_local_package

load_local_package()
from project_identity_fixture import memory_paths as resolve_paths
from omh.plugin_bundle.omh.hermes_memory import classify_record_expiry
from omh.profiles.setup import write_setup_profile
from omh.workflows import memory


class ExistingMemoryPolicyCharacterizationTests(unittest.TestCase):
    def test_naive_expiry_is_utc_at_the_exact_boundary(self) -> None:
        record = {"ttl": {"expires_at": "2026-07-30T12:00:00"}}
        now = datetime(2026, 7, 30, 12, 0, 0, tzinfo=timezone.utc)

        self.assertEqual(classify_record_expiry(record, now=now), "expired")

    def test_episode_defaults_to_thirty_days(self) -> None:
        retention = memory._ttl_metadata(None, record_type="episode", created_at="2026-07-30T12:00:00Z")

        self.assertEqual(retention, {"ttl_days": 30, "expires_at": "2026-08-29T12:00:00Z"})

    def test_manual_and_auto_safe_paths_share_safe_classification(self) -> None:
        with TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            manual_paths = resolve_paths(root / "manual" / ".omh", root / "manual" / ".hermes")
            auto_paths = resolve_paths(root / "auto" / ".omh", root / "auto" / ".hermes")
            write_setup_profile(auto_paths, memory_mode="auto-safe")

            manual = memory.capture_project_memory_candidate(
                manual_paths,
                "Run deterministic checks before release.",
                record_type="procedure",
            )
            automatic = memory.capture_project_memory_candidate(
                auto_paths,
                "Run deterministic checks before release.",
                record_type="procedure",
            )

            self.assertEqual(manual["candidate"]["safety"]["status"], "safe")
            self.assertFalse(manual["auto_approved"])
            self.assertTrue(automatic["auto_approved"])
            self.assertEqual(automatic["record"]["safety"]["status"], "safe")


if __name__ == "__main__":
    unittest.main()
