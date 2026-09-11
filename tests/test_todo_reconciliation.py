"""Contracts for the open-plan reconciliation reminder.

A completion claim in chat while the HUD checklist shows open items is a
visible contradiction the user reported directly. An open plan is a
state, not a phrasing, so the guard is stateful: while an established
todo has open items, every pre_llm_call turn carries one compact
reconciliation line; a finished, cleared, or absent plan carries none.

The second half of the same failure is a plan nobody contradicts and
nobody advances: while the reader's stall finding stands, the line also
states how long the checklist has been unchanged.
"""

import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from omh.plugin_bundle.omh.hooks.llm_hooks import pre_llm_call
from omh.plugin_bundle.omh.todo_reconciliation import (
    TODO_CONTINUATION_RULE,
    TODO_RECONCILIATION_RULE,
    TODO_UNCHANGED_RULE,
    open_todo_reminder,
)
from omh.plugin_bundle.omh.todo_store import build_todo_record, todo_path, write_todo
from omh.plugin_bundle.omh.tool_bursts import record_tool_call, record_tool_call_close


class TodoReconciliationReminderTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.home = str(Path(self._tmp.name) / "omh")

    def _write_plan(self, states, session_ref="", minutes_ago=0):
        items = [
            {"text": f"task {index}", "state": state, "phase": "Review"}
            for index, state in enumerate(states)
        ]
        record = build_todo_record("plan", items, source="test", session_ref=session_ref)
        if minutes_ago:
            stamp = datetime.now(timezone.utc) - timedelta(minutes=minutes_ago)
            record["updated_at"] = stamp.isoformat().replace("+00:00", "Z")
        write_todo(Path(self.home), record)
        return todo_path(Path(self.home), session_ref)

    def _observe_a_closed_tool_call(self):
        """Make liveness answerable with nothing left open.

        Without an observed ``post_tool_call`` the reader cannot read
        liveness either way and says so, so a stall test that skipped this
        would be asserting the unanswerable branch by accident.
        """
        record_tool_call("Bash", omh_home=self.home, tool_call_id="call-1")
        record_tool_call_close("call-1", omh_home=self.home)

    def _observe_an_open_tool_call(self):
        """Answerable liveness with one call still open.

        A closed call first, so ``post_tool_call_observed`` is true and the
        reader is not on its unanswerable branch; then an open one, which is
        the state a busy-but-unmoving run is in.
        """
        record_tool_call("Bash", omh_home=self.home, tool_call_id="call-1")
        record_tool_call_close("call-1", omh_home=self.home)
        record_tool_call("Bash", omh_home=self.home, tool_call_id="call-2")

    def test_the_reminder_binds_to_the_turn_session_own_plan(self):
        # A Slack session's open plan is not the TUI session's open work:
        # each turn reconciles against the plan its own session declared.
        self._write_plan(["done", "active", "pending"], session_ref="tui-session")
        self._write_plan(["done", "done"], session_ref="slack-session")

        self.assertIn(
            "[OMH plan todo] 1/3 done",
            open_todo_reminder(omh_home=self.home, session_ref="tui-session"),
        )
        self.assertEqual(open_todo_reminder(omh_home=self.home, session_ref="slack-session"), "")
        payload = pre_llm_call(user_message="다 됐어?", omh_home=self.home, session_id="slack-session")
        self.assertNotIn("[OMH plan todo]", str((payload or {}).get("context", "")))

    def test_an_open_plan_yields_one_compact_line(self):
        self._write_plan(["done", "active", "pending"])
        line = open_todo_reminder(omh_home=self.home)
        self.assertIn("[OMH plan todo] 1/3 done", line)
        self.assertIn("active: task 1", line)
        self.assertIn(TODO_RECONCILIATION_RULE, line)
        self.assertNotIn("\n", line)

    def test_a_finished_plan_yields_nothing(self):
        self._write_plan(["done", "done"])
        self.assertEqual(open_todo_reminder(omh_home=self.home), "")

    def test_an_absent_plan_yields_nothing(self):
        self.assertEqual(open_todo_reminder(omh_home=self.home), "")

    def test_pre_llm_call_carries_the_reminder_every_turn_while_open(self):
        self._write_plan(["done", "active", "pending"])
        for _ in range(2):
            payload = pre_llm_call(user_message="다 됐어?", omh_home=self.home)
            self.assertIsNotNone(payload)
            self.assertIn("[OMH plan todo] 1/3 done", str(payload.get("context", "")))
            self.assertIn("reconcile the checklist", str(payload.get("context", "")))

    def test_a_checklist_left_behind_for_hours_says_so_on_the_turn(self):
        # The observed failure: one item active, three behind it, untouched
        # for two hours while the session answered about other things and
        # ended its turns. The HUD computed the age and nothing said it.
        self._write_plan(["done", "active", "pending", "pending"], minutes_ago=120)
        self._observe_a_closed_tool_call()

        line = open_todo_reminder(omh_home=self.home)

        self.assertIn("unchanged 2h 00m, no tool call in flight", line)
        self.assertIn(TODO_UNCHANGED_RULE, line)
        self.assertIn(TODO_RECONCILIATION_RULE, line)
        self.assertNotIn("\n", line)
        # The line reports; it never upgrades the todo's own claim boundary.
        self.assertIn("not evidence that the work failed", line)
        # And it does ask for the plan to move, with the stop condition stated
        # in the same breath. A checklist that only ever catches a completion
        # claim is a detector; `omh_todo` exists to carry a goal across turns.
        self.assertIn(TODO_CONTINUATION_RULE, line)
        self.assertIn("advance the next item in this turn", line)
        self.assertIn("every item is done or an item is recorded blocked", line)

    def test_a_plan_that_stops_moving_while_calls_run_is_still_a_finding(self):
        # The correction that matters most. Liveness excuses a short pause and
        # nothing longer: a run that spent 36 minutes retrying git workarounds
        # opened and closed tool calls the whole time, so a signal suppressed by
        # mere liveness would have stayed silent through the case it exists for.
        self._write_plan(["done", "active", "pending", "pending"], minutes_ago=120)
        self._observe_an_open_tool_call()

        line = open_todo_reminder(omh_home=self.home)

        self.assertIn("unchanged 2h 00m while calls kept running", line)
        self.assertNotIn("no tool call in flight", line)
        self.assertIn(TODO_CONTINUATION_RULE, line)

    def test_a_recently_updated_checklist_adds_no_unchanged_clause(self):
        # Negative control: the same open plan, moved just now. The clause
        # must be a finding, not ambient text on every open plan.
        self._write_plan(["done", "active", "pending"])
        self._observe_a_closed_tool_call()

        line = open_todo_reminder(omh_home=self.home)

        self.assertIn(TODO_RECONCILIATION_RULE, line)
        self.assertNotIn("unchanged", line)
        self.assertNotIn(TODO_UNCHANGED_RULE, line)

    def test_a_recent_checklist_stays_quiet_while_a_tool_call_is_open(self):
        # Liveness excuses a SHORT pause: a call is open and the plan moved a
        # minute ago, so the open call fully explains the gap and there is
        # nothing to report. Past the threshold the same open call explains
        # nothing, which is what the busy-finding test above pins -- an earlier
        # version of this module suppressed on liveness alone and would have
        # gone silent through a 36-minute retry loop.
        self._write_plan(["done", "active", "pending"], minutes_ago=1)
        self._observe_a_closed_tool_call()
        record_tool_call("Bash", omh_home=self.home, tool_call_id="call-running")

        self.assertNotIn("unchanged", open_todo_reminder(omh_home=self.home))

    def test_an_old_checklist_stays_quiet_when_liveness_is_unanswerable(self):
        # A host that never fires post_tool_call cannot answer "is anything
        # running": its ledger entries can only expire, never close. Silence
        # there must not be inverted into a stall.
        self._write_plan(["done", "active", "pending"], minutes_ago=120)

        self.assertNotIn("unchanged", open_todo_reminder(omh_home=self.home))

    def test_the_awareness_opt_out_suppresses_the_reminder(self):
        self._write_plan(["done", "active", "pending"])
        payload = pre_llm_call(
            user_message="다 됐어?", omh_home=self.home, include_omh_awareness=False
        )
        self.assertNotIn("[OMH plan todo]", str((payload or {}).get("context", "")))


if __name__ == "__main__":
    unittest.main()
