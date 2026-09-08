from __future__ import annotations

import json
import os
import subprocess
import sys
import unittest
from collections.abc import Callable
from pathlib import Path
from unittest.mock import patch
from tempfile import TemporaryDirectory
from typing import Protocol

from _cli_harness import run_cli
from _local_package import load_local_package
from _typing_support import override

load_local_package()
from omh.commands.main import build_parser
from omh.goal_ledger import goal_ledger_path, read_goal_ledger
from omh.paths import resolve_paths
from omh.quality.working_tree_fingerprint import WorkingTreeFingerprint, WorkingTreeFingerprintState, working_tree_content_fingerprint
from omh.record_revision import APPLIED_MUTATIONS_LIMIT, MAX_MUTATION_ID_CHARS, applied_mutation_key
from test_working_tree_fingerprint import _git, _init_repo, is_object_list, is_object_mapping

# The digest helper is deliberately private; the deep module is imported here
# so the planted replay entry below matches what the real cancel computes
# instead of re-implementing the recipe in the test.
from omh.workflows import goal_ledger as goal_workflow


class _GoalPort(Protocol):
    def _mutation_digest(self, *parts: object) -> str: ...


class _GoalAccess(_GoalPort, Protocol):
    def digest(self: _GoalPort, *parts: object) -> str:
        return self._mutation_digest(*parts)


_goal_port: _GoalPort = goal_workflow


def _object(value: object) -> dict[str, object]:
    assert is_object_mapping(value)
    result: dict[str, object] = {}
    for key, item in value.items():
        assert isinstance(key, str)
        result[key] = item
    return result


def _json(text: str, *, decode: Callable[[str], object] = json.loads) -> dict[str, object]:
    return _object(decode(text))


def _items(value: object) -> list[dict[str, object]]:
    assert is_object_list(value)
    return [_object(item) for item in value]


def _integer(value: object) -> int:
    assert isinstance(value, int)
    return value


def _text(value: object) -> str:
    assert isinstance(value, str)
    return value


def _base(root: Path) -> list[str]:
    return ["--omh-home", str(root / ".omh"), "--hermes-home", str(root / ".hermes")]


def _create(base: list[str], goal_id: str = "goal-cli-guard") -> None:
    status, _stdout, stderr = run_cli(
        base
        + [
            "goal",
            "create",
            "--goal-id",
            goal_id,
            "--objective",
            "Ship the stale mutation guard",
            "--criterion",
            "Guard is verified",
        ]
    )
    if status != 0:  # pragma: no cover - only trips when the fixture itself breaks
        raise AssertionError(f"goal create failed: {stderr}")


class GoalCliRevisionGuardTests(unittest.TestCase):
    @override
    def setUp(self) -> None:
        # Ledger tests need a supported, stable workspace, not whichever Git
        # normalization attributes happen to be in the test runner's checkout.
        repository = TemporaryDirectory()
        self.addCleanup(repository.cleanup)
        root = Path(repository.name)
        _init_repo(root)
        collector = patch(
            "omh.commands.goal.working_tree_content_fingerprint",
            side_effect=lambda: working_tree_content_fingerprint(root),
        )
        _ = collector.start()
        self.addCleanup(collector.stop)

    def test_revision_guard_flag_names_are_pinned_on_every_mutation_subcommand(self) -> None:
        parser = build_parser()

        for subcommand, extra in (
            ("checkpoint", ["--summary", "s"]),
            ("blocker", ["--summary", "s"]),
            ("complete", []),
            ("cancel", []),
            ("fail", ["--summary", "s", "--reason-code", "target_not_found"]),
        ):
            with self.subTest(subcommand=subcommand):
                args = _object(vars(parser.parse_args(
                    ["goal", subcommand, "--goal", "g", *extra, "--expected-revision", "4", "--mutation-id", "m-1"],
                )))
                self.assertEqual(args["expected_revision"], 4)
                self.assertEqual(args["mutation_id"], "m-1")
                # Absent flags must stay "no guard requested", not 0 / "0".
                defaults = _object(vars(parser.parse_args(["goal", subcommand, "--goal", "g", *extra])))
                self.assertIsNone(defaults["expected_revision"])
                self.assertEqual(defaults["mutation_id"], "")

    def test_cancel_reports_the_terminal_state_and_refuses_later_checkpoints(self) -> None:
        with TemporaryDirectory() as tmp:
            base = _base(Path(tmp))
            _create(base)

            status, stdout, stderr = run_cli(base + ["goal", "cancel", "--goal", "goal-cli-guard", "--reason", "Superseded"])

            self.assertEqual(status, 0, stderr)
            payload = _json(stdout)
            self.assertTrue(payload["cancelled"])
            self.assertTrue(payload["applied"])
            self.assertFalse(payload["replayed"])
            self.assertEqual(_object(payload["goal"])["status"], "cancelled")
            self.assertEqual(_object(payload["completion_gate"])["next_action"], "show_status")

            status, stdout, stderr = run_cli(
                base + ["goal", "checkpoint", "--goal", "goal-cli-guard", "--summary", "After cancel"]
            )

            self.assertEqual(status, 2)
            self.assertEqual(stdout, "")
            self.assertTrue(stderr.startswith("omh: "), stderr)
            self.assertIn("cancelled", stderr)
            self.assertIn("terminal", stderr)
            self.assertNotIn("Traceback", stderr)

    def test_fail_reports_the_terminal_state_and_refuses_later_checkpoints(self) -> None:
        # #H: `goal fail` reaches a negative-conclusive verdict distinct from
        # `goal cancel` (operator decision) and `goal blocker` (recoverable).
        with TemporaryDirectory() as tmp:
            base = _base(Path(tmp))
            _create(base)

            status, stdout, stderr = run_cli(
                base
                + [
                    "goal", "fail", "--goal", "goal-cli-guard",
                    "--summary", "The target does not exist in this environment.",
                    "--reason-code", "target_not_found",
                ]
            )

            self.assertEqual(status, 0, stderr)
            payload = _json(stdout)
            self.assertTrue(payload["failed"])
            self.assertTrue(payload["applied"])
            self.assertFalse(payload["replayed"])
            self.assertEqual(_object(payload["goal"])["status"], "failed")
            self.assertEqual(_object(payload["goal"])["failure_reason_code"], "target_not_found")
            self.assertEqual(_object(payload["completion_gate"])["next_action"], "show_status")

            status, stdout, stderr = run_cli(
                base + ["goal", "checkpoint", "--goal", "goal-cli-guard", "--summary", "After failure"]
            )

            self.assertEqual(status, 2)
            self.assertEqual(stdout, "")
            self.assertTrue(stderr.startswith("omh: "), stderr)
            self.assertIn("failed", stderr)
            self.assertIn("terminal", stderr)
            self.assertNotIn("Traceback", stderr)

    def test_fail_rejects_an_unsupported_reason_code_at_the_parser(self) -> None:
        with TemporaryDirectory() as tmp:
            base = _base(Path(tmp))
            _create(base)

            with self.assertRaises(SystemExit):
                _ = run_cli(
                    base
                    + [
                        "goal", "fail", "--goal", "goal-cli-guard",
                        "--summary", "s", "--reason-code", "not_a_real_code",
                    ]
                )

    def test_stale_expected_revision_exits_non_zero_with_a_readable_message(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            base = _base(root)
            _create(base)
            paths = resolve_paths(root / ".omh", root / ".hermes")
            stale_revision = _object(read_goal_ledger(paths, "goal-cli-guard"))["record_revision"]
            # Another writer moves the record on while the stale call is in flight.
            _ = run_cli(base + ["goal", "checkpoint", "--goal", "goal-cli-guard", "--summary", "First", "--status", "in_progress"])

            status, stdout, stderr = run_cli(
                base
                + [
                    "goal",
                    "checkpoint",
                    "--goal",
                    "goal-cli-guard",
                    "--summary",
                    "Racing",
                    "--status",
                    "in_progress",
                    "--expected-revision",
                    str(stale_revision),
                ]
            )

            self.assertEqual(status, 2)
            self.assertEqual(stdout, "")
            self.assertTrue(stderr.startswith("omh: "), stderr)
            self.assertIn("record_revision", stderr)
            self.assertNotIn("Traceback", stderr)
            self.assertEqual(len(_items(_object(read_goal_ledger(paths, "goal-cli-guard"))["checkpoints"])), 1)

    def test_checkpoint_collects_one_complete_workspace_identity(self) -> None:
        # Given: a checkpoint request and one collector result for its transaction.
        # When: the public CLI records the checkpoint.
        # Then: the exact collected fingerprint is persisted after one collection.
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            base = _base(root)
            _create(base)
            fingerprint = WorkingTreeFingerprint(
                WorkingTreeFingerprintState.CLEAN, "f" * 64, "a" * 40, 8
            )
            with patch("omh.commands.goal.working_tree_content_fingerprint", return_value=fingerprint) as collect:
                status, stdout, stderr = run_cli(
                    base + ["goal", "checkpoint", "--goal", "goal-cli-guard", "--summary", "Recorded", "--status", "in_progress"]
                )

            self.assertEqual(status, 0, stderr)
            self.assertEqual(collect.call_count, 1)
            self.assertEqual(_items(_object(_json(stdout)["goal"])["checkpoints"])[0]["observed_tree"], "f" * 64)

    def test_normalized_workspace_refuses_checkpoint_without_ledger_writes(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            workspace = root / "workspace"
            workspace.mkdir()
            _init_repo(workspace)
            _ = (workspace / ".gitattributes").write_text("tracked.txt text\n", encoding="utf-8")
            base = _base(root)
            _create(base)
            before = {path: path.read_bytes() for path in (root / ".omh").rglob("*") if path.is_file()}

            with patch(
                "omh.commands.goal.working_tree_content_fingerprint",
                side_effect=lambda: working_tree_content_fingerprint(workspace),
            ) as collect:
                status, stdout, stderr = run_cli(
                    base + ["goal", "checkpoint", "--goal", "goal-cli-guard", "--summary", "Unsupported workspace"]
                )

            self.assertEqual(status, 2)
            self.assertEqual(stdout, "")
            self.assertIn("unsupported", stderr)
            self.assertEqual(collect.call_count, 1)
            self.assertEqual(
                {path: path.read_bytes() for path in (root / ".omh").rglob("*") if path.is_file()},
                before,
            )

    def test_repeated_mutation_id_leaves_one_item_and_reports_replayed(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            base = _base(root)
            _create(base)
            command = base + [
                "goal",
                "checkpoint",
                "--goal",
                "goal-cli-guard",
                "--summary",
                "Only once",
                "--status",
                "in_progress",
                "--mutation-id",
                "cp-retry",
            ]

            first_status, first_stdout, first_stderr = run_cli(command)
            second_status, second_stdout, second_stderr = run_cli(command)

            self.assertEqual(first_status, 0, first_stderr)
            self.assertEqual(second_status, 0, second_stderr)
            first = _json(first_stdout)
            second = _json(second_stdout)
            self.assertFalse(first["replayed"])
            self.assertTrue(second["replayed"])
            # A replay still reports applied=true: the checkpoint the caller
            # asked for is in the record, it was just written by the first try.
            self.assertTrue(second["applied"])
            self.assertEqual(_object(first["goal"])["record_revision"], _object(second["goal"])["record_revision"])
            paths = resolve_paths(root / ".omh", root / ".hermes")
            self.assertEqual(len(_items(_object(read_goal_ledger(paths, "goal-cli-guard"))["checkpoints"])), 1)

    def test_repeated_cancel_mutation_id_replays_without_a_second_transition(self) -> None:
        with TemporaryDirectory() as tmp:
            base = _base(Path(tmp))
            _create(base)
            command = base + ["goal", "cancel", "--goal", "goal-cli-guard", "--mutation-id", "cancel-retry"]

            first_status, first_stdout, _first_stderr = run_cli(command)
            second_status, second_stdout, _second_stderr = run_cli(command)

            first = _json(first_stdout)
            second = _json(second_stdout)
            self.assertEqual((first_status, second_status), (0, 0))
            self.assertFalse(first["replayed"])
            self.assertTrue(second["replayed"])
            self.assertTrue(second["cancelled"])
            self.assertEqual(_object(first["goal"])["record_revision"], _object(second["goal"])["record_revision"])

    def test_mutation_id_reused_across_operations_is_not_swallowed_as_a_replay(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            base = _base(root)
            _create(base)
            # A connector deriving mutation ids from one upstream message id
            # sends both a checkpoint and a blocker under that id; the blocker
            # is different intent and must still apply.
            _ = run_cli(
                base
                + [
                    "goal",
                    "checkpoint",
                    "--goal",
                    "goal-cli-guard",
                    "--summary",
                    "Checkpoint under the shared id",
                    "--status",
                    "in_progress",
                    "--mutation-id",
                    "shared-turn-1",
                ]
            )

            status, stdout, stderr = run_cli(
                base
                + [
                    "goal",
                    "blocker",
                    "--goal",
                    "goal-cli-guard",
                    "--summary",
                    "Blocker under the shared id",
                    "--mutation-id",
                    "shared-turn-1",
                ]
            )

            self.assertEqual(status, 0, stderr)
            payload = _json(stdout)
            self.assertFalse(payload["replayed"])
            self.assertTrue(payload["applied"])
            paths = resolve_paths(root / ".omh", root / ".hermes")
            stored = _object(read_goal_ledger(paths, "goal-cli-guard"))
            self.assertEqual(len(_items(stored["checkpoints"])), 1)
            self.assertEqual(len(_items(stored["blockers"])), 1)

    def test_connector_style_mutation_ids_are_accepted_by_the_goal_cli(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            base = _base(root)
            _create(base)
            # Upstream message ids carry ':' and '/'; goals must not be the one
            # surface that rejects them (wrapper sessions and loop cycles do not).
            command = base + [
                "goal",
                "checkpoint",
                "--goal",
                "goal-cli-guard",
                "--summary",
                "Snowflake id",
                "--status",
                "in_progress",
                "--mutation-id",
                "slack:C123/p1700000000.000100",
            ]

            first_status, first_stdout, first_stderr = run_cli(command)
            second_status, second_stdout, _second_stderr = run_cli(command)

            self.assertEqual(first_status, 0, first_stderr)
            self.assertEqual(second_status, 0)
            self.assertFalse(_json(first_stdout)["replayed"])
            self.assertTrue(_json(second_stdout)["replayed"])
            paths = resolve_paths(root / ".omh", root / ".hermes")
            checkpoints = _items(_object(read_goal_ledger(paths, "goal-cli-guard"))["checkpoints"])
            self.assertEqual(len(checkpoints), 1)
            checkpoint_id = _text(checkpoints[0]["checkpoint_id"])
            self.assertNotIn("/", checkpoint_id)
            self.assertNotIn(":", checkpoint_id)

    def test_same_mutation_id_with_different_content_is_refused_not_replayed(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            base = _base(root)
            _create(base)
            _ = run_cli(
                base
                + [
                    "goal",
                    "blocker",
                    "--goal",
                    "goal-cli-guard",
                    "--summary",
                    "Original blocker",
                    "--mutation-id",
                    "blocker-1",
                ]
            )

            status, stdout, stderr = run_cli(
                base
                + [
                    "goal",
                    "blocker",
                    "--goal",
                    "goal-cli-guard",
                    "--summary",
                    "A completely different blocker",
                    "--mutation-id",
                    "blocker-1",
                ]
            )

            self.assertEqual(status, 2)
            self.assertEqual(stdout, "")
            self.assertTrue(stderr.startswith("omh: "), stderr)
            self.assertNotIn("Traceback", stderr)
            paths = resolve_paths(root / ".omh", root / ".hermes")
            blockers = _items(_object(read_goal_ledger(paths, "goal-cli-guard"))["blockers"])
            self.assertEqual(len(blockers), 1)
            self.assertEqual(blockers[0]["summary"], "Original blocker")

    def test_a_cancel_replayed_away_is_reported_as_not_cancelled_and_exits_non_zero(self) -> None:
        # The defect this pins: cmd_goal_cancel printed the goal and returned 0
        # whenever the call did not raise, so a cancel whose mutation replayed
        # away left an active goal that read as successfully cancelled. The
        # applied_mutations entry below is planted directly to force exactly
        # that state, which operation scoping otherwise makes hard to reach.
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            base = _base(root)
            _create(base)
            paths = resolve_paths(root / ".omh", root / ".hermes")
            path = goal_ledger_path(paths, "goal-cli-guard")
            goal = _json(path.read_text(encoding="utf-8"))
            goal["applied_mutations"] = {
                applied_mutation_key("cancel_goal_ledger", "cancel-1"): {
                    "operation": "cancel_goal_ledger",
                    "record_revision": _integer(goal["record_revision"]),
                    "result_digest": _GoalAccess.digest(_goal_port, "cancel_goal_ledger", ""),
                }
            }
            _ = path.write_text(json.dumps(goal, sort_keys=True), encoding="utf-8")

            status, stdout, stderr = run_cli(
                base + ["goal", "cancel", "--goal", "goal-cli-guard", "--mutation-id", "cancel-1"]
            )

            self.assertEqual(status, 1, stderr)
            payload = _json(stdout)
            self.assertTrue(payload["replayed"])
            self.assertFalse(payload["cancelled"])
            self.assertFalse(payload["applied"])
            self.assertEqual(_object(payload["goal"])["status"], "active")

    def test_a_retry_after_eviction_with_only_a_mutation_id_leaves_one_blocker(self) -> None:
        # The exact issue #828 repro, end to end through the CLI: the goal CLI
        # accepts --mutation-id independently of --expected-revision, so the
        # eviction floor never fires for this call. Before the fix the retry
        # exited 0 with applied=true/replayed=false and produced TWO blockers
        # sharing one blocker_id, which validate_goal_ledger still called ok.
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            base = _base(root)
            _create(base)
            paths = resolve_paths(root / ".omh", root / ".hermes")
            retry = base + [
                "goal",
                "blocker",
                "--goal",
                "goal-cli-guard",
                "--summary",
                "the original blocker",
                "--mutation-id",
                "turn-EVICT-ME",
            ]
            self.assertEqual(run_cli(retry)[0], 0)
            for index in range(APPLIED_MUTATIONS_LIMIT + 10):
                status, _stdout, stderr = run_cli(
                    base
                    + [
                        "goal",
                        "checkpoint",
                        "--goal",
                        "goal-cli-guard",
                        "--summary",
                        f"filler {index}",
                        "--status",
                        "in_progress",
                        "--mutation-id",
                        f"filler-{index:04d}",
                    ]
                )
                self.assertEqual(status, 0, stderr)
            evicted = _json(goal_ledger_path(paths, "goal-cli-guard").read_text(encoding="utf-8"))
            self.assertGreaterEqual(_integer(evicted["applied_mutations_floor_revision"]), 1)
            self.assertNotIn(
                applied_mutation_key("record_goal_blocker", "turn-EVICT-ME"), _object(evicted["applied_mutations"])
            )

            status, stdout, stderr = run_cli(retry)

            self.assertEqual(status, 0, stderr)
            payload = _json(stdout)
            self.assertTrue(payload["applied"])
            self.assertTrue(payload["replayed"])
            stored = _object(read_goal_ledger(paths, "goal-cli-guard"))
            self.assertEqual([item["blocker_id"] for item in _items(stored["blockers"])], ["turn-EVICT-ME"])
            self.assertEqual(_integer(stored["record_revision"]), _integer(evicted["record_revision"]))

    def test_an_over_long_mutation_id_is_rejected_with_nothing_written(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            base = _base(root)
            _create(base)
            paths = resolve_paths(root / ".omh", root / ".hermes")
            path = goal_ledger_path(paths, "goal-cli-guard")
            before = path.read_bytes()

            status, stdout, stderr = run_cli(
                base
                + [
                    "goal",
                    "checkpoint",
                    "--goal",
                    "goal-cli-guard",
                    "--summary",
                    "Oversized retry token",
                    "--status",
                    "in_progress",
                    "--mutation-id",
                    "x" * 100_000,
                ]
            )

            self.assertEqual(status, 2)
            self.assertEqual(stdout, "")
            self.assertTrue(stderr.startswith("omh: "), stderr)
            self.assertIn(f"at most {MAX_MUTATION_ID_CHARS} characters", stderr)
            self.assertNotIn("Traceback", stderr)
            self.assertEqual(path.read_bytes(), before)

    def test_missing_goal_reports_a_readable_error_not_a_traceback(self) -> None:
        with TemporaryDirectory() as tmp:
            base = _base(Path(tmp))

            status, stdout, stderr = run_cli(base + ["goal", "cancel", "--goal", "no-such-goal"])

            self.assertEqual(status, 2)
            self.assertEqual(stdout, "")
            self.assertTrue(stderr.startswith("omh: "), stderr)
            self.assertNotIn("Traceback", stderr)


class GoalCliRawWorkspaceTests(unittest.TestCase):
    def test_real_cli_replays_staging_and_revert_but_conflicts_on_raw_byte_change(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            workspace = root / "workspace with spaces-\u00ff"
            workspace.mkdir()
            _init_repo(workspace)
            # Real Git precedence, rather than an injected collector result.
            _git(workspace, "config", "--unset-all", "core.autocrlf")
            _git(workspace, "config", "--add", "core.autocrlf", "true")
            _git(workspace, "config", "--add", "core.autocrlf", "false")
            environment = dict(os.environ, OMH_OUTPUT="json", PYTHONDONTWRITEBYTECODE="1")
            environment["PYTHONPATH"] = str(Path(__file__).resolve().parents[1] / "src")
            command = [sys.executable, "-m", "omh.cli", *_base(root)]

            def invoke(arguments: list[str]) -> subprocess.CompletedProcess[str]:
                return subprocess.run(
                    command + arguments, cwd=workspace, env=environment,
                    capture_output=True, text=True, check=False, timeout=30,
                )

            created = invoke(["goal", "create", "--goal-id", "raw", "--objective", "Observe bytes", "--criterion", "No false replay"])
            self.assertEqual(created.returncode, 0, created.stderr)
            tracked = workspace / "tracked.txt"
            payload = b"raw\r\nbytes\x1aafter\x00\xff"
            _ = tracked.write_bytes(payload)
            # CPython's executable-extension mode must not make a stable
            # Windows checkpoint unreadable or disagree with Git's tree mode.
            _ = (workspace / "program.exe").write_bytes(payload)
            checkpoint = ["goal", "checkpoint", "--goal", "raw", "--summary", "Observed", "--status", "in_progress", "--mutation-id", "raw-retry"]
            first = invoke(checkpoint)
            self.assertEqual(first.returncode, 0, first.stderr)
            recorded = _json(first.stdout)
            self.assertFalse(recorded["replayed"])
            checkpoints = _items(_object(recorded["goal"])["checkpoints"])
            self.assertEqual(len(checkpoints), 1)
            self.assertEqual(len(_text(checkpoints[0]["observed_tree"])), 64)
            ledger = goal_ledger_path(resolve_paths(root / ".omh", root / ".hermes"), "raw")
            before = ledger.read_bytes()
            for stage in (True, False):
                _git(workspace, *(["add", "tracked.txt"] if stage else ["restore", "--staged", "tracked.txt"]))
                replay = invoke(checkpoint)
                self.assertEqual(replay.returncode, 0, replay.stderr)
                self.assertTrue(_json(replay.stdout)["replayed"])
                self.assertEqual(ledger.read_bytes(), before)
            # Same length, changed AFTER Ctrl-Z: a text descriptor would hide it.
            _ = tracked.write_bytes(payload[:-1] + b"\xfe")
            conflict = invoke(checkpoint)
            self.assertEqual(conflict.returncode, 2, conflict.stderr)
            self.assertEqual(conflict.stdout, "")
            self.assertEqual(ledger.read_bytes(), before)
            _ = tracked.write_bytes(payload)
            reverted = invoke(checkpoint)
            self.assertEqual(reverted.returncode, 0, reverted.stderr)
            self.assertTrue(_json(reverted.stdout)["replayed"])
            self.assertEqual(ledger.read_bytes(), before)


if __name__ == "__main__":
    _ = unittest.main()
