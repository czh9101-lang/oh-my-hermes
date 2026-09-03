from __future__ import annotations

import stat
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import TypedDict

from _local_package import load_local_package
from _platform_support import requires_posix_permissions

load_local_package()
from omh.coding.executor_capability_snapshots import (
    ExecutorCapabilitySnapshotError,
    build_executor_capability_snapshot,
    read_executor_capability_snapshot,
    read_matching_executor_capability_snapshot,
    validate_executor_capability_snapshot,
    write_executor_capability_snapshot,
)


class _LocalWorkflowScope(TypedDict):
    profile: str
    skill_id: str
    environment: str


class _LocalWorkflowCapability(TypedDict):
    status: str
    scope: _LocalWorkflowScope
    evidence_ref: str
    observed_at: str


def _local_workflow_capability(
    *,
    status: str = "host_observed",
    observed_at: str = "2026-08-02T00:00:00Z",
) -> _LocalWorkflowCapability:
    return {
        "status": status,
        "scope": {"profile": "codex", "skill_id": "ultrawork", "environment": "local_codex"},
        "evidence_ref": "operator:codex-skill-catalog",
        "observed_at": observed_at,
    }


class ExecutorCapabilitySnapshotTests(unittest.TestCase):
    def test_builds_host_observed_capability_with_bounded_evidence(self) -> None:
        snapshot = build_executor_capability_snapshot(
            executor="codex",
            capabilities={
                "parallel_agents": {
                    "status": "host_observed",
                    "scope": {"host": "local", "surface": "native_subagents"},
                    "evidence_ref": "host-probe:codex-subagents",
                    "observed_at": "2026-07-15T00:00:00Z",
                },
                "visual_qa": {"status": "unknown"},
            },
            recorded_at="2026-07-15T00:00:01Z",
        )

        self.assertEqual(snapshot["schema_version"], "executor_capability_snapshot/v2")
        self.assertEqual(snapshot["executor"], "codex")
        self.assertEqual(snapshot["capabilities"]["parallel_agents"]["status"], "host_observed")
        self.assertEqual(validate_executor_capability_snapshot(snapshot), [])

    def test_rejects_host_observed_capability_without_scope_or_evidence(self) -> None:
        with self.assertRaisesRegex(ExecutorCapabilitySnapshotError, "scope"):
            build_executor_capability_snapshot(
                executor="codex",
                capabilities={
                    "parallel_agents": {
                        "status": "host_observed",
                        "evidence_ref": "host-probe:codex-subagents",
                        "observed_at": "2026-07-15T00:00:00Z",
                    }
                },
            )

        invalid = {
            "schema_version": "executor_capability_snapshot/v1",
            "executor": "codex",
            "recorded_at": "2026-07-15T00:00:01Z",
            "capabilities": {
                "parallel_agents": {
                    "status": "host_observed",
                    "scope": {"host": "local"},
                    "observed_at": "2026-07-15T00:00:00Z",
                }
            },
        }
        self.assertIn("parallel_agents host_observed capability requires a nonempty evidence_ref", validate_executor_capability_snapshot(invalid))

    @requires_posix_permissions
    def test_persists_private_snapshot_and_rejects_lifecycle_or_raw_material(self) -> None:
        snapshot = build_executor_capability_snapshot(
            executor="claude-code",
            capabilities={
                "worktree_isolation": {"status": "prepared"},
                "browser_or_computer_use": {"status": "unavailable"},
            },
            recorded_at="2026-07-15T00:00:01Z",
        )
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "snapshots" / "claude-code.json"
            write_executor_capability_snapshot(path, snapshot)

            self.assertEqual(read_executor_capability_snapshot(path), snapshot)
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
            self.assertEqual(stat.S_IMODE(path.parent.stat().st_mode), 0o700)

        for forbidden_key in ("execution", "verification", "review", "ci", "merge", "raw_log", "transcript", "reasoning"):
            invalid = dict(snapshot)
            invalid[forbidden_key] = "forbidden"
            self.assertTrue(validate_executor_capability_snapshot(invalid), forbidden_key)
            with self.assertRaises(ExecutorCapabilitySnapshotError):
                write_executor_capability_snapshot(Path("/tmp") / "should-not-write.json", invalid)

    def test_rejects_sensitive_metadata_and_ignores_mismatched_snapshot(self) -> None:
        with self.assertRaisesRegex(ExecutorCapabilitySnapshotError, "sensitive metadata"):
            build_executor_capability_snapshot(
                executor="codex",
                capabilities={
                    "parallel_agents": {
                        "status": "host_observed",
                        "scope": {"api_key": "local"},
                        "evidence_ref": "probe:sk-live-secret",
                        "observed_at": "2026-07-15T00:00:00Z",
                    }
                },
            )
        snapshot = build_executor_capability_snapshot(
            executor="codex", capabilities={"parallel_agents": {"status": "unknown"}}
        )
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "codex.json"
            write_executor_capability_snapshot(path, snapshot)
            self.assertEqual(read_matching_executor_capability_snapshot(path, expected_executor="codex"), snapshot)
            self.assertIsNone(read_matching_executor_capability_snapshot(path, expected_executor="claude-code"))

    def test_rejects_raw_scope_key(self) -> None:
        with self.assertRaisesRegex(ExecutorCapabilitySnapshotError, "scope keys"):
            build_executor_capability_snapshot(
                executor="codex",
                capabilities={
                    "parallel_agents": {
                        "status": "host_observed",
                        "scope": {"raw_message": "not-secret-but-not-metadata"},
                        "evidence_ref": "probe:local",
                        "observed_at": "2026-07-15T00:00:00Z",
                    }
                },
            )

    def test_local_workflow_records_replay_for_observed_and_unavailable_statuses(self) -> None:
        observed = build_executor_capability_snapshot(
            executor="codex",
            capabilities={"local_workflow": _local_workflow_capability()},
            recorded_at="2026-08-02T00:00:01Z",
        )
        unavailable = build_executor_capability_snapshot(
            executor="codex",
            capabilities={
                "local_workflow": _local_workflow_capability(
                    status="unavailable", observed_at="2026-08-02T00:00:00+00:00"
                )
            },
            recorded_at="2026-08-02T00:00:01Z",
        )
        unknown = build_executor_capability_snapshot(
            executor="codex",
            capabilities={"local_workflow": {"status": "unknown"}},
            recorded_at="2026-08-02T00:00:01Z",
        )
        legacy = {
            "schema_version": "executor_capability_snapshot/v1",
            "executor": "codex",
            "recorded_at": "2026-07-15T00:00:01Z",
            "capabilities": {"parallel_agents": {"status": "unknown"}},
        }

        with TemporaryDirectory() as tmp:
            observed_path = Path(tmp) / "observed.json"
            unavailable_path = Path(tmp) / "unavailable.json"
            unknown_path = Path(tmp) / "unknown.json"
            legacy_path = Path(tmp) / "legacy.json"
            write_executor_capability_snapshot(observed_path, observed)
            write_executor_capability_snapshot(unavailable_path, unavailable)
            write_executor_capability_snapshot(unknown_path, unknown)
            write_executor_capability_snapshot(legacy_path, legacy)

            self.assertEqual(read_executor_capability_snapshot(observed_path), observed)
            self.assertEqual(read_executor_capability_snapshot(unavailable_path), unavailable)
            self.assertEqual(read_executor_capability_snapshot(unknown_path), unknown)
            self.assertEqual(read_executor_capability_snapshot(legacy_path), legacy)

    def test_local_workflow_observation_requires_exact_scope_and_evidence(self) -> None:
        valid = _local_workflow_capability()
        for field in ("profile", "skill_id", "environment"):
            malformed = {**valid, "scope": {key: value for key, value in valid["scope"].items() if key != field}}
            with self.assertRaisesRegex(ExecutorCapabilitySnapshotError, "scope"):
                build_executor_capability_snapshot(executor="codex", capabilities={"local_workflow": malformed})
        malformed = {**valid, "scope": {**valid["scope"], "host": "local"}}
        with self.assertRaisesRegex(ExecutorCapabilitySnapshotError, "scope"):
            build_executor_capability_snapshot(executor="codex", capabilities={"local_workflow": malformed})
        malformed = {**valid, "evidence_ref": ""}
        with self.assertRaisesRegex(ExecutorCapabilitySnapshotError, "evidence_ref"):
            build_executor_capability_snapshot(executor="codex", capabilities={"local_workflow": malformed})
        malformed = {**valid, "observed_at": "2026-08-02T00:00:00"}
        with self.assertRaisesRegex(ExecutorCapabilitySnapshotError, "observed_at"):
            build_executor_capability_snapshot(executor="codex", capabilities={"local_workflow": malformed})

    def test_local_workflow_unavailable_rejects_missing_or_sensitive_evidence(self) -> None:
        valid = _local_workflow_capability(status="unavailable")
        for field in ("scope", "evidence_ref", "observed_at"):
            malformed = {key: value for key, value in valid.items() if key != field}
            with self.assertRaises(ExecutorCapabilitySnapshotError):
                build_executor_capability_snapshot(executor="codex", capabilities={"local_workflow": malformed})
        malformed = {**valid, "evidence_ref": "operator:Bearer secret"}
        with self.assertRaisesRegex(ExecutorCapabilitySnapshotError, "sensitive metadata"):
            build_executor_capability_snapshot(executor="codex", capabilities={"local_workflow": malformed})
        malformed = {**valid, "scope": {**valid["scope"], "environment": "github_pat_secret"}}
        with self.assertRaisesRegex(ExecutorCapabilitySnapshotError, "sensitive metadata"):
            build_executor_capability_snapshot(executor="codex", capabilities={"local_workflow": malformed})
        malformed = {**valid, "evidence_ref": "/Users/alice/private-worktree/evidence.json"}
        with self.assertRaises(ExecutorCapabilitySnapshotError):
            build_executor_capability_snapshot(executor="codex", capabilities={"local_workflow": malformed})
        with self.assertRaisesRegex(ExecutorCapabilitySnapshotError, "status"):
            build_executor_capability_snapshot(
                executor="codex", capabilities={"local_workflow": {"status": "prepared"}}
            )

    def test_local_workflow_rejects_hostile_metadata(self) -> None:
        valid = _local_workflow_capability()
        hostile_cases = (
            ("environment control character", {**valid, "scope": {**valid["scope"], "environment": "prod\nOMO:activate"}}),
            ("environment path injection", {**valid, "scope": {**valid["scope"], "environment": "operator: /Users/alice"}}),
            ("evidence path traversal", {**valid, "evidence_ref": "../../private/session.json"}),
            ("evidence control character", {**valid, "evidence_ref": "artifact\n$autopilot"}),
            ("AWS access key", {**valid, "evidence_ref": "operator:akiaiosfodnn7example"}),
            ("private key marker", {**valid, "evidence_ref": "operator:private-key-material"}),
        )
        for case, capability in hostile_cases:
            with self.subTest(case=case):
                with self.assertRaises(ExecutorCapabilitySnapshotError):
                    build_executor_capability_snapshot(
                        executor="codex",
                        capabilities={"local_workflow": capability},
                        recorded_at="2026-08-02T00:00:01Z",
                    )

    def test_local_workflow_rejects_invalid_time_relation(self) -> None:
        valid = _local_workflow_capability()
        for observed_at in (
            "2026-08-02T00:00:02Z",
            "2026-08-01T00:00:00Z",
        ):
            with self.subTest(observed_at=observed_at):
                with self.assertRaises(ExecutorCapabilitySnapshotError):
                    build_executor_capability_snapshot(
                        executor="codex",
                        capabilities={"local_workflow": {**valid, "observed_at": observed_at}},
                        recorded_at="2026-08-02T00:00:01Z",
                    )
