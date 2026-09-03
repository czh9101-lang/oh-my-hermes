"""One observed capability snapshot feeds handoff, briefing, and owner fit."""

from __future__ import annotations

from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest import mock

from _local_package import load_local_package

load_local_package()

from omh.coding.coding_delegation import build_coding_delegation_payload  # noqa: E402
from omh.coding.executor_capability_snapshots import (  # noqa: E402
    DESCRIPTIVE_CAPABILITY_NAMES,
    build_executor_capability_snapshot,
    executor_capability_snapshot_path,
    prepared_executor_capability_snapshot,
    resolved_executor_capability_snapshot,
    validate_executor_capability_snapshot,
    write_executor_capability_snapshot,
)
from omh.coding.executor_capabilities import capability_for_profile  # noqa: E402
from omh.coding.fanout import build_fanout_contract  # noqa: E402
from omh.wrapper.briefing import build_coding_briefing  # noqa: E402


class UnifiedExecutorCapabilityProjectionTests(unittest.TestCase):
    def test_prepared_snapshot_identity_is_deterministic_without_observation_time(self) -> None:
        with mock.patch(
            "omh.coding.executor_capability_snapshots.utc_now",
            side_effect=["2026-08-13T12:00:00Z", "2026-08-13T13:00:00Z"],
        ):
            first = prepared_executor_capability_snapshot("codex")
            second = prepared_executor_capability_snapshot("codex")

        self.assertEqual(first, second)

    def test_prepared_snapshot_owns_the_complete_capability_vocabulary(self) -> None:
        snapshot = prepared_executor_capability_snapshot("codex")

        self.assertEqual(snapshot["schema_version"], "executor_capability_snapshot/v2")
        self.assertEqual(snapshot["executor"], "codex")
        self.assertEqual(snapshot["capabilities"]["worktree_isolation"]["status"], "prepared")
        for name in DESCRIPTIVE_CAPABILITY_NAMES:
            with self.subTest(name=name):
                self.assertEqual(snapshot["capabilities"][name], {"status": "unknown"})

    def test_snapshot_validation_bounds_unsupported_field_names(self) -> None:
        snapshot = prepared_executor_capability_snapshot("codex")
        snapshot["SENTINEL-" + ("x" * 100_000)] = "value"
        snapshot["capabilities"]["SENTINEL-" + ("y" * 100_000)] = {
            "status": "unknown"
        }

        rendered = "; ".join(validate_executor_capability_snapshot(snapshot))

        self.assertLessEqual(len(rendered), 500)
        self.assertNotIn("x" * 1000, rendered)
        self.assertNotIn("y" * 1000, rendered)

    def test_resolved_snapshot_projects_recorded_evidence_and_unknown_defaults(self) -> None:
        with TemporaryDirectory() as tmp:
            directory = Path(tmp)
            recorded = build_executor_capability_snapshot(
                executor="codex",
                capabilities={
                    "edit_format_patch": {
                        "status": "host_observed",
                        "scope": {"surface": "local_cli"},
                        "evidence_ref": "probe:patch-edit",
                        "observed_at": "2026-08-13T12:00:00Z",
                    }
                },
                recorded_at="2026-08-13T12:01:00Z",
            )
            write_executor_capability_snapshot(
                executor_capability_snapshot_path(directory, "codex"),
                recorded,
            )

            resolved = resolved_executor_capability_snapshot("codex", directory)

        self.assertEqual(resolved["recorded_at"], "2026-08-13T12:01:00Z")
        self.assertEqual(
            resolved["capabilities"]["edit_format_patch"]["evidence_ref"],
            "probe:patch-edit",
        )
        self.assertEqual(resolved["capabilities"]["persistent_eval"], {"status": "unknown"})

    def test_briefing_renders_the_frozen_handoff_snapshot_verbatim(self) -> None:
        snapshot = prepared_executor_capability_snapshot("codex")
        session = {
            "session_id": "sess-1",
            "status": "prompt_handoff_prepared",
            "selected_executor_profile": "codex",
            "prompt_handoff": {
                "schema_version": "coding_handoff/v1",
                "selected_executor_profile": "codex",
                "executor_capability_snapshot": snapshot,
            },
        }

        briefing = build_coding_briefing(session)
        contract = briefing["work_summary"]["handoff_contract"]

        self.assertEqual(contract["executor_capability_snapshot"], snapshot)
        self.assertEqual(contract["executor_capability"]["schema_version"], "executor_capability/v1")

    def test_briefing_omits_invalid_or_wrong_owner_snapshots(self) -> None:
        invalid_snapshots = (
            {
                "executor": "codex",
                "raw_prompt": "SECRET",
            },
            build_executor_capability_snapshot(
                executor="claude-code",
                capabilities={"edit_format_patch": {"status": "unknown"}},
                recorded_at="2026-08-13T12:01:00Z",
            ),
        )
        for snapshot in invalid_snapshots:
            with self.subTest(snapshot=snapshot):
                briefing = build_coding_briefing(
                    {
                        "session_id": "sess-invalid",
                        "status": "prompt_handoff_prepared",
                        "selected_executor_profile": "codex",
                        "prompt_handoff": {
                            "schema_version": "coding_handoff/v1",
                            "selected_executor_profile": "codex",
                            "executor_capability_snapshot": snapshot,
                        },
                    }
                )

                contract = briefing["work_summary"]["handoff_contract"]
                self.assertNotIn("executor_capability_snapshot", contract)
                self.assertNotIn("SECRET", str(briefing))

    def test_delegation_freezes_the_resolved_snapshot_for_briefing(self) -> None:
        with TemporaryDirectory() as tmp:
            directory = Path(tmp)
            recorded = build_executor_capability_snapshot(
                executor="codex",
                capabilities={
                    "edit_format_patch": {
                        "status": "host_observed",
                        "scope": {"surface": "local_cli"},
                        "evidence_ref": "probe:patch-edit",
                        "observed_at": "2026-08-13T12:00:00Z",
                    }
                },
                recorded_at="2026-08-13T12:01:00Z",
            )
            write_executor_capability_snapshot(
                executor_capability_snapshot_path(directory, "codex"),
                recorded,
            )
            payload = build_coding_delegation_payload(
                "Implement the accepted task with tests.",
                executor_target="codex",
                capability_snapshot_directory=directory,
            )

        handoff = payload["executor_handoff"]
        snapshot = handoff["executor_capability_snapshot"]
        briefing = build_coding_briefing(
            {
                "session_id": "sess-recorded",
                "status": "prompt_handoff_prepared",
                "selected_executor_profile": "codex",
                "prompt_handoff": handoff,
            }
        )

        self.assertEqual(snapshot["recorded_at"], recorded["recorded_at"])
        self.assertEqual(
            snapshot["capabilities"]["edit_format_patch"],
            recorded["capabilities"]["edit_format_patch"],
        )
        self.assertEqual(
            briefing["work_summary"]["handoff_contract"]["executor_capability_snapshot"],
            snapshot,
        )
        briefing["work_summary"]["handoff_contract"]["executor_capability_snapshot"][
            "capabilities"
        ]["edit_format_patch"]["evidence_ref"] = "mutated"
        self.assertEqual(
            snapshot["capabilities"]["edit_format_patch"]["evidence_ref"],
            "probe:patch-edit",
        )

    def test_delegation_handoff_and_owner_fit_share_one_snapshot_read(self) -> None:
        recorded = build_executor_capability_snapshot(
            executor="codex",
            capabilities={
                "parallel_agents": {
                    "status": "host_observed",
                    "scope": {"surface": "local_cli"},
                    "evidence_ref": "probe:parallel",
                    "observed_at": "2026-08-13T12:00:00Z",
                }
            },
            recorded_at="2026-08-13T12:01:00Z",
        )

        def snapshots(_directory, owners):
            return tuple((owner, recorded if owner == "codex" else None) for owner in owners)

        with mock.patch(
            "omh.coding.coding_delegation.owner_capability_snapshots",
            side_effect=snapshots,
        ) as reader:
            payload = build_coding_delegation_payload(
                "Implement the accepted task with tests.",
                executor_target="codex",
            )

        reader.assert_called_once()
        self.assertEqual(
            payload["executor_handoff"]["executor_capability_snapshot"]["recorded_at"],
            recorded["recorded_at"],
        )

    def test_fanout_contract_deep_freezes_recorded_and_prepared_owner_snapshots(self) -> None:
        recorded = build_executor_capability_snapshot(
            executor="codex",
            capabilities={
                "edit_format_patch": {
                    "status": "host_observed",
                    "scope": {"surface": "local_cli"},
                    "evidence_ref": "probe:patch-edit",
                    "observed_at": "2026-08-13T12:00:00Z",
                }
            },
            recorded_at="2026-08-13T12:01:00Z",
        )
        prepared = prepared_executor_capability_snapshot(
            "claude-code",
            recorded_at="2026-08-13T12:02:00Z",
        )

        contract = build_fanout_contract(
            "Implement the accepted feature.",
            [
                {
                    "unit_id": "code",
                    "title": "Code",
                    "owner": "codex",
                    "file_scope": ["src/"],
                },
                {
                    "unit_id": "docs",
                    "title": "Docs",
                    "owner": "claude-code",
                    "file_scope": ["docs/"],
                },
            ],
            capability_snapshots={"codex": recorded, "claude-code": prepared},
        )
        by_unit = {str(unit["unit_id"]): unit for unit in contract["units"]}
        recorded["capabilities"]["edit_format_patch"]["evidence_ref"] = "mutated"
        prepared["capabilities"]["worktree_isolation"]["status"] = "unknown"

        self.assertEqual(
            by_unit["code"]["handoff"]["executor_capability_snapshot"]["capabilities"][
                "edit_format_patch"
            ]["evidence_ref"],
            "probe:patch-edit",
        )
        self.assertEqual(
            by_unit["docs"]["handoff"]["executor_capability_snapshot"]["capabilities"][
                "worktree_isolation"
            ]["status"],
            "prepared",
        )

    def test_fanout_contract_rejects_invalid_or_owner_mismatched_snapshots(self) -> None:
        units = [
            {"unit_id": "code", "owner": "codex", "file_scope": ["src/"]},
            {"unit_id": "docs", "owner": "claude-code", "file_scope": ["docs/"]},
        ]
        wrong_owner = build_executor_capability_snapshot(
            executor="claude-code",
            capabilities={"edit_format_patch": {"status": "unknown"}},
            recorded_at="2026-08-13T12:01:00Z",
        )
        for snapshot in ("not-a-snapshot", wrong_owner):
            with self.subTest(snapshot=snapshot), self.assertRaises(ValueError):
                build_fanout_contract(
                    "Implement the accepted feature.",
                    units,
                    capability_snapshots={
                        "codex": snapshot,
                        "claude-code": prepared_executor_capability_snapshot(
                            "claude-code",
                            recorded_at="2026-08-13T12:02:00Z",
                        ),
                    },
                )

    def test_briefing_does_not_invent_a_snapshot_for_a_legacy_handoff(self) -> None:
        session = {
            "session_id": "sess-legacy",
            "status": "prompt_handoff_prepared",
            "selected_executor_profile": "codex",
            "prompt_handoff": {
                "schema_version": "coding_handoff/v1",
                "selected_executor_profile": "codex",
            },
        }

        briefing = build_coding_briefing(session)

        contract = briefing["work_summary"]["handoff_contract"]
        self.assertNotIn("executor_capability_snapshot", contract)
        self.assertEqual(contract["executor_capability"]["schema_version"], "executor_capability/v1")

    def test_legacy_capability_api_is_derived_from_the_unified_vocabulary(self) -> None:
        legacy = capability_for_profile("codex")

        self.assertEqual(legacy["schema_version"], "executor_capability/v1")
        self.assertEqual(legacy["profile"], "codex")
        self.assertEqual(legacy["edit_format_support"]["patch"], "unknown")
        self.assertEqual(
            legacy["provenance"],
            {"source": "", "observed_at": None, "executor_version": None},
        )

        observed = build_executor_capability_snapshot(
            executor="codex",
            capabilities={
                "edit_format_patch": {
                    "status": "host_observed",
                    "scope": {"surface": "one_session"},
                    "evidence_ref": "probe:scoped-patch",
                    "observed_at": "2026-08-13T12:00:00Z",
                }
            },
            recorded_at="2026-08-13T12:01:00Z",
        )
        from omh.coding.executor_capabilities import legacy_executor_capability_projection

        self.assertEqual(
            legacy_executor_capability_projection(observed)["edit_format_support"]["patch"],
            "unknown",
        )

    def test_descriptive_vocabulary_is_executor_neutral_and_absent_from_routing(self) -> None:
        vendor_terms = ("codex", "claude", "openai", "anthropic", "gpt", "sonnet")
        for name in DESCRIPTIVE_CAPABILITY_NAMES:
            with self.subTest(name=name):
                self.assertFalse(any(term in name for term in vendor_terms))

        repo_root = Path(__file__).resolve().parents[1]
        matches = [
            f"{path.relative_to(repo_root)}:{line_number}:{line}"
            for path in sorted((repo_root / "src" / "routing").rglob("*.py"))
            for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1)
            if any(name in line for name in DESCRIPTIVE_CAPABILITY_NAMES)
        ]
        self.assertEqual(matches, [])


if __name__ == "__main__":
    unittest.main()
