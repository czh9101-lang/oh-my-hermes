from __future__ import annotations

import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from _cli_harness import run_cli
from _local_package import load_local_package

load_local_package()
from omh.commands.main import build_parser
from omh.goal_ledger import goal_ledger_path, read_goal_ledger
from omh.paths import resolve_paths
from omh.record_revision import APPLIED_MUTATIONS_LIMIT, MAX_MUTATION_ID_CHARS, applied_mutation_key

# The digest helper is deliberately private; the deep module is imported here
# so the planted replay entry below matches what the real cancel computes
# instead of re-implementing the recipe in the test.
from omh.workflows.goal_ledger import _mutation_digest


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
                args = parser.parse_args(
                    ["goal", subcommand, "--goal", "g", *extra, "--expected-revision", "4", "--mutation-id", "m-1"]
                )
                self.assertEqual(args.expected_revision, 4)
                self.assertEqual(args.mutation_id, "m-1")
                # Absent flags must stay "no guard requested", not 0 / "0".
                defaults = parser.parse_args(["goal", subcommand, "--goal", "g", *extra])
                self.assertIsNone(defaults.expected_revision)
                self.assertEqual(defaults.mutation_id, "")

    def test_cancel_reports_the_terminal_state_and_refuses_later_checkpoints(self) -> None:
        with TemporaryDirectory() as tmp:
            base = _base(Path(tmp))
            _create(base)

            status, stdout, stderr = run_cli(base + ["goal", "cancel", "--goal", "goal-cli-guard", "--reason", "Superseded"])

            self.assertEqual(status, 0, stderr)
            payload = json.loads(stdout)
            self.assertTrue(payload["cancelled"])
            self.assertTrue(payload["applied"])
            self.assertFalse(payload["replayed"])
            self.assertEqual(payload["goal"]["status"], "cancelled")
            self.assertEqual(payload["completion_gate"]["next_action"], "show_status")

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
            payload = json.loads(stdout)
            self.assertTrue(payload["failed"])
            self.assertTrue(payload["applied"])
            self.assertFalse(payload["replayed"])
            self.assertEqual(payload["goal"]["status"], "failed")
            self.assertEqual(payload["goal"]["failure_reason_code"], "target_not_found")
            self.assertEqual(payload["completion_gate"]["next_action"], "show_status")

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
                run_cli(
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
            stale_revision = read_goal_ledger(paths, "goal-cli-guard")["record_revision"]
            # Another writer moves the record on while the stale call is in flight.
            run_cli(base + ["goal", "checkpoint", "--goal", "goal-cli-guard", "--summary", "First", "--status", "in_progress"])

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
            self.assertEqual(len(read_goal_ledger(paths, "goal-cli-guard")["checkpoints"]), 1)

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
            first = json.loads(first_stdout)
            second = json.loads(second_stdout)
            self.assertFalse(first["replayed"])
            self.assertTrue(second["replayed"])
            # A replay still reports applied=true: the checkpoint the caller
            # asked for is in the record, it was just written by the first try.
            self.assertTrue(second["applied"])
            self.assertEqual(first["goal"]["record_revision"], second["goal"]["record_revision"])
            paths = resolve_paths(root / ".omh", root / ".hermes")
            self.assertEqual(len(read_goal_ledger(paths, "goal-cli-guard")["checkpoints"]), 1)

    def test_repeated_cancel_mutation_id_replays_without_a_second_transition(self) -> None:
        with TemporaryDirectory() as tmp:
            base = _base(Path(tmp))
            _create(base)
            command = base + ["goal", "cancel", "--goal", "goal-cli-guard", "--mutation-id", "cancel-retry"]

            first_status, first_stdout, _first_stderr = run_cli(command)
            second_status, second_stdout, _second_stderr = run_cli(command)

            first = json.loads(first_stdout)
            second = json.loads(second_stdout)
            self.assertEqual((first_status, second_status), (0, 0))
            self.assertFalse(first["replayed"])
            self.assertTrue(second["replayed"])
            self.assertTrue(second["cancelled"])
            self.assertEqual(first["goal"]["record_revision"], second["goal"]["record_revision"])

    def test_mutation_id_reused_across_operations_is_not_swallowed_as_a_replay(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            base = _base(root)
            _create(base)
            # A connector deriving mutation ids from one upstream message id
            # sends both a checkpoint and a blocker under that id; the blocker
            # is different intent and must still apply.
            run_cli(
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
            payload = json.loads(stdout)
            self.assertFalse(payload["replayed"])
            self.assertTrue(payload["applied"])
            paths = resolve_paths(root / ".omh", root / ".hermes")
            stored = read_goal_ledger(paths, "goal-cli-guard")
            self.assertEqual(len(stored["checkpoints"]), 1)
            self.assertEqual(len(stored["blockers"]), 1)

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
            self.assertFalse(json.loads(first_stdout)["replayed"])
            self.assertTrue(json.loads(second_stdout)["replayed"])
            paths = resolve_paths(root / ".omh", root / ".hermes")
            checkpoints = read_goal_ledger(paths, "goal-cli-guard")["checkpoints"]
            self.assertEqual(len(checkpoints), 1)
            checkpoint_id = checkpoints[0]["checkpoint_id"]
            self.assertNotIn("/", checkpoint_id)
            self.assertNotIn(":", checkpoint_id)

    def test_same_mutation_id_with_different_content_is_refused_not_replayed(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            base = _base(root)
            _create(base)
            run_cli(
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
            blockers = read_goal_ledger(paths, "goal-cli-guard")["blockers"]
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
            goal = json.loads(path.read_text(encoding="utf-8"))
            goal["applied_mutations"] = {
                applied_mutation_key("cancel_goal_ledger", "cancel-1"): {
                    "operation": "cancel_goal_ledger",
                    "record_revision": int(goal["record_revision"]),
                    "result_digest": _mutation_digest("cancel_goal_ledger", ""),
                }
            }
            path.write_text(json.dumps(goal, sort_keys=True), encoding="utf-8")

            status, stdout, stderr = run_cli(
                base + ["goal", "cancel", "--goal", "goal-cli-guard", "--mutation-id", "cancel-1"]
            )

            self.assertEqual(status, 1, stderr)
            payload = json.loads(stdout)
            self.assertTrue(payload["replayed"])
            self.assertFalse(payload["cancelled"])
            self.assertFalse(payload["applied"])
            self.assertEqual(payload["goal"]["status"], "active")

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
            evicted = json.loads(goal_ledger_path(paths, "goal-cli-guard").read_text(encoding="utf-8"))
            self.assertGreaterEqual(int(evicted["applied_mutations_floor_revision"]), 1)
            self.assertNotIn(
                applied_mutation_key("record_goal_blocker", "turn-EVICT-ME"), evicted["applied_mutations"]
            )

            status, stdout, stderr = run_cli(retry)

            self.assertEqual(status, 0, stderr)
            payload = json.loads(stdout)
            self.assertTrue(payload["applied"])
            self.assertTrue(payload["replayed"])
            stored = read_goal_ledger(paths, "goal-cli-guard")
            self.assertEqual([item["blocker_id"] for item in stored["blockers"]], ["turn-EVICT-ME"])
            self.assertEqual(int(stored["record_revision"]), int(evicted["record_revision"]))

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


if __name__ == "__main__":
    unittest.main()
