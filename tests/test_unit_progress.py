from __future__ import annotations

import unittest

from omh.coding.unit_execution_state import (
    UNIT_STATE_ACCOUNT_LIMIT,
    UNIT_STATE_AWAITING_INPUT,
    UNIT_STATE_DATA_MISSING,
    UNIT_STATE_PERMISSION_BLOCKED,
    UNIT_STATE_PROGRESS_STALLED,
    UNIT_STATE_RUNNING,
    UNIT_TERMINAL_STATES,
)
from omh.coding.unit_progress import (
    PHASE_MARKER_COMMIT_CREATED,
    PHASE_MARKER_RESULT_WRITTEN,
    PHASE_MARKER_TEST_FINISHED,
    PHASE_MARKER_TEST_STARTED,
    REASON_NO_NEW_OUTPUT,
    REASON_PROMPT_AT_END,
    REASON_REPEATED_ERROR_PREFIX,
    UNIT_PROGRESS_MID_RUN_STATES,
    UNIT_PROGRESS_SCHEMA_VERSION,
    UNIT_STALL_AFTER_SECONDS,
    assess_progress,
    empty_progress_evidence,
    progress_state_summary,
    stalled_for_seconds,
)


class AssessProgressTests(unittest.TestCase):
    def test_new_bytes_since_the_previous_reading_are_running(self) -> None:
        first = assess_progress(None, "starting\n", now=100.0)
        self.assertEqual(first["state"], UNIT_STATE_RUNNING)
        self.assertEqual(first["reason"], "")
        self.assertEqual(first["schema_version"], UNIT_PROGRESS_SCHEMA_VERSION)
        self.assertEqual(first["last_new_output_at"], 100.0)

        second = assess_progress(first, "starting\nreading files\n", now=160.0)
        self.assertEqual(second["state"], UNIT_STATE_RUNNING)
        self.assertEqual(second["last_new_output_at"], 160.0)
        self.assertEqual(second["output_lines"], 2)
        self.assertEqual(second["output_bytes"], len("starting\nreading files\n"))

    def test_silence_past_the_threshold_is_a_stall(self) -> None:
        first = assess_progress(None, "working\n", now=0.0)
        quiet = assess_progress(first, "working\n", now=UNIT_STALL_AFTER_SECONDS - 1)
        self.assertEqual(quiet["state"], UNIT_STATE_RUNNING)

        stalled = assess_progress(quiet, "working\n", now=UNIT_STALL_AFTER_SECONDS)
        self.assertEqual(stalled["state"], UNIT_STATE_PROGRESS_STALLED)
        self.assertEqual(stalled["reason"], REASON_NO_NEW_OUTPUT)
        self.assertEqual(stalled_for_seconds(stalled), int(UNIT_STALL_AFTER_SECONDS))
        self.assertIn("no new output for", progress_state_summary(stalled))

    def test_the_same_line_repeating_is_a_stall_even_while_output_grows(self) -> None:
        # The 2026-09-11 incident in miniature: the retry counter and the
        # short sha change every turn, the failure does not. Digit and hex
        # collapse is what keeps the four turns countable as one line.
        loop = "".join(
            f"git checkout {sha} -- src/{path}.py\n"
            f"error: unable to read file (case collision), retrying (n={n})\n"
            for n, (sha, path) in enumerate(
                (
                    ("1a2b3c4", "Alpha"),
                    ("5d6e7f8", "Beta"),
                    ("9a0b1c2", "Gamma"),
                    ("3d4e5f6", "Delta"),
                ),
                start=1,
            )
        )
        previous = assess_progress(None, "preparing worktree\n", now=0.0)
        growing = assess_progress(previous, "preparing worktree\n" + loop, now=30.0)

        self.assertGreater(growing["output_bytes"], previous["output_bytes"])
        self.assertEqual(growing["state"], UNIT_STATE_PROGRESS_STALLED)
        self.assertTrue(growing["reason"].startswith(REASON_REPEATED_ERROR_PREFIX))
        self.assertIn("retrying (n=<n>)", growing["reason"])
        self.assertEqual(growing["repeat_count"], 4)
        # Output grew, so the stall clock was NOT what fired here.
        self.assertEqual(growing["last_new_output_at"], 30.0)

    def test_a_trailing_prompt_with_no_new_output_is_awaiting_input(self) -> None:
        asked = "applying patch\nOverwrite existing file? (y/n) "
        first = assess_progress(None, asked, now=0.0)
        self.assertEqual(first["state"], UNIT_STATE_RUNNING)

        waiting = assess_progress(first, asked, now=5.0)
        self.assertEqual(waiting["state"], UNIT_STATE_AWAITING_INPUT)
        self.assertEqual(waiting["reason"], REASON_PROMPT_AT_END)

    def test_a_bare_question_mark_only_counts_once_output_stopped(self) -> None:
        first = assess_progress(None, "Should I rebase onto main?\n", now=0.0)
        self.assertEqual(first["state"], UNIT_STATE_RUNNING)
        moved_on = assess_progress(first, "Should I rebase onto main?\nrebasing\n", now=5.0)
        self.assertEqual(moved_on["state"], UNIT_STATE_RUNNING)

    def test_a_session_limit_refusal_is_an_account_limit(self) -> None:
        first = assess_progress(None, "thinking\n", now=0.0)
        refused = assess_progress(
            first,
            "thinking\nYou've hit your session limit · resets 6:10pm\n",
            now=10.0,
        )
        self.assertEqual(refused["state"], UNIT_STATE_ACCOUNT_LIMIT)
        self.assertTrue(refused["reason"].startswith("account_limit:"))

    def test_a_denial_is_permission_blocked(self) -> None:
        blocked = assess_progress(None, "touch .git/index\nPermission denied\n", now=0.0)
        self.assertEqual(blocked["state"], UNIT_STATE_PERMISSION_BLOCKED)
        self.assertTrue(blocked["reason"].startswith("permission_blocked:"))

    def test_a_missing_object_is_data_missing(self) -> None:
        missing = assess_progress(None, "git merge origin/main\nfatal: bad object 1a2b3c4\n", now=0.0)
        self.assertEqual(missing["state"], UNIT_STATE_DATA_MISSING)
        self.assertTrue(missing["reason"].startswith("data_missing:"))

    def test_phase_markers_accumulate_from_what_the_tools_actually_print(self) -> None:
        text = (
            "test_routes (tests.test_cli.CliTests) ... ok\n"
            "Ran 3 tests in 0.412s\n"
            '{"schema_version": "fanout_unit_result/v1"}\n'
            "[claude/topic 1a2b3c4] add unit progress\n"
            " 2 files changed, 40 insertions(+)\n"
        )
        evidence = assess_progress(None, text, now=0.0)
        self.assertEqual(
            evidence["phase_markers"],
            [
                PHASE_MARKER_TEST_STARTED,
                PHASE_MARKER_TEST_FINISHED,
                PHASE_MARKER_RESULT_WRITTEN,
                PHASE_MARKER_COMMIT_CREATED,
            ],
        )

    def test_no_terminal_state_is_ever_assigned_mid_run(self) -> None:
        # `failed` and `verified` belong to the intake path after exit. A
        # mid-run reading of stdout must not pre-empt the result contract.
        self.assertFalse(set(UNIT_PROGRESS_MID_RUN_STATES) & set(UNIT_TERMINAL_STATES))
        for text in ("", "done\n", "FAILED\n", "all checks passed\n"):
            self.assertIn(assess_progress(None, text, now=0.0)["state"], UNIT_PROGRESS_MID_RUN_STATES)

    def test_a_freshly_dispatched_unit_is_running_not_stalled(self) -> None:
        empty = empty_progress_evidence(now=500.0)
        self.assertEqual(empty["state"], UNIT_STATE_RUNNING)
        self.assertEqual(stalled_for_seconds(empty), 0)
        self.assertEqual(progress_state_summary(empty), UNIT_STATE_RUNNING)

    def test_result_record_presence_is_carried_not_derived(self) -> None:
        evidence = assess_progress(None, "working\n", now=0.0, result_record_present=True)
        self.assertIs(evidence["result_record_present"], True)
        self.assertIs(assess_progress(None, "working\n", now=0.0)["result_record_present"], False)


if __name__ == "__main__":
    unittest.main()
