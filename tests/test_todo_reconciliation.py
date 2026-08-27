"""Contracts for the open-plan reconciliation reminder.

A completion claim in chat while the HUD checklist shows open items is a
visible contradiction the user reported directly. An open plan is a
state, not a phrasing, so the guard is stateful: while an established
todo has open items, every pre_llm_call turn carries one compact
reconciliation line; a finished, cleared, or absent plan carries none.
"""

import tempfile
import unittest
from pathlib import Path

from omh.plugin_bundle.omh.hooks.llm_hooks import pre_llm_call
from omh.plugin_bundle.omh.todo_reconciliation import (
    TODO_RECONCILIATION_RULE,
    open_todo_reminder,
)
from omh.plugin_bundle.omh.todo_store import build_todo_record, write_todo


class TodoReconciliationReminderTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.home = str(Path(self._tmp.name) / "omh")

    def _write_plan(self, states):
        items = [
            {"text": f"task {index}", "state": state, "phase": "Review"}
            for index, state in enumerate(states)
        ]
        write_todo(Path(self.home), build_todo_record("plan", items, source="test"))

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

    def test_the_awareness_opt_out_suppresses_the_reminder(self):
        self._write_plan(["done", "active", "pending"])
        payload = pre_llm_call(
            user_message="다 됐어?", omh_home=self.home, include_omh_awareness=False
        )
        self.assertNotIn("[OMH plan todo]", str((payload or {}).get("context", "")))


if __name__ == "__main__":
    unittest.main()
