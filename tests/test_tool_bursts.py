"""Contracts for parallel tool-call burst observation.

Hermes dispatches a model turn's batched tool calls concurrently, but the
transcript renders only a collapsed "Tool calls (N)" group. The
pre_tool_call hook ticks this ledger per call; ticks inside one short
window are the concurrent batch, and the HUD brands the latest fresh one
as a parallel shot.
"""

import tempfile
import unittest
from pathlib import Path

from omh.plugin_bundle.omh.runtime_reader import read_omh_hud
from omh.plugin_bundle.omh.tool_bursts import (
    BURST_FRESH_SECONDS,
    MAX_OPEN_TOOL_CALLS,
    MAX_TOOL_BURST_ENTRIES,
    TOOL_CALL_OPEN_TTL_SECONDS,
    latest_parallel_shot,
    record_tool_call,
    record_tool_call_close,
    tool_bursts_path,
    tool_call_activity,
)

NOW = 1_787_040_000.0


class ToolBurstLedgerTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.home = str(Path(self._tmp.name) / "omh")

    def test_calls_within_the_window_group_into_one_parallel_shot(self):
        for offset, tool in ((0.0, "read_file"), (0.1, "read_file"), (0.2, "terminal"), (0.3, "search_files")):
            record_tool_call(tool, omh_home=self.home, now=NOW + offset)
        shot = latest_parallel_shot(self.home, now=NOW + 5)
        self.assertEqual(shot["status"], "observed")
        self.assertEqual(shot["size"], 4)
        self.assertEqual(shot["distinct_tools"], 3)
        self.assertIn("not proof", shot["claim_boundary"])

    def test_sequential_calls_separated_by_round_trips_never_form_a_shot(self):
        for offset in (0.0, 10.0, 25.0):
            record_tool_call("terminal", omh_home=self.home, now=NOW + offset)
        self.assertEqual(latest_parallel_shot(self.home, now=NOW + 30)["status"], "idle")

    def test_a_stale_burst_expires_from_the_projection(self):
        record_tool_call("read_file", omh_home=self.home, now=NOW)
        record_tool_call("read_file", omh_home=self.home, now=NOW + 0.2)
        self.assertEqual(latest_parallel_shot(self.home, now=NOW + 1)["status"], "observed")
        self.assertEqual(
            latest_parallel_shot(self.home, now=NOW + BURST_FRESH_SECONDS + 1)["status"],
            "idle",
        )

    def test_the_ledger_is_capped_and_a_blank_tool_name_is_ignored(self):
        record_tool_call("", omh_home=self.home, now=NOW)
        self.assertFalse(tool_bursts_path(self.home).exists())
        for index in range(MAX_TOOL_BURST_ENTRIES + 10):
            record_tool_call("terminal", omh_home=self.home, now=NOW + index * 5)
        import json

        record = json.loads(tool_bursts_path(self.home).read_text(encoding="utf-8"))
        self.assertEqual(len(record["entries"]), MAX_TOOL_BURST_ENTRIES)

    def test_open_call_stays_live_until_a_matching_close(self):
        record_tool_call("terminal", omh_home=self.home, now=NOW, tool_call_id="call-1", turn_id="turn-1")

        activity = tool_call_activity(self.home, now=NOW + 5)

        self.assertTrue(activity["live"])
        self.assertEqual(activity["open_call_count"], 1)
        self.assertEqual(activity["oldest_open_elapsed_seconds"], 5)
        self.assertTrue(activity["oldest_open_started_at"])

        record_tool_call_close("call-1", omh_home=self.home, now=NOW + 5.5)
        closed = tool_call_activity(self.home, now=NOW + 6)

        self.assertFalse(closed["live"])
        self.assertEqual(closed["open_call_count"], 0)
        self.assertEqual(closed["oldest_open_elapsed_seconds"], None)

    def test_a_call_with_no_tool_call_id_never_opens(self):
        record_tool_call("terminal", omh_home=self.home, now=NOW)

        activity = tool_call_activity(self.home, now=NOW + 1)

        self.assertFalse(activity["live"])
        self.assertEqual(activity["open_call_count"], 0)

    def test_an_open_call_expires_after_the_ttl_instead_of_staying_live_forever(self):
        record_tool_call("terminal", omh_home=self.home, now=NOW, tool_call_id="call-1", turn_id="turn-1")

        still_open = tool_call_activity(self.home, now=NOW + TOOL_CALL_OPEN_TTL_SECONDS - 1)
        expired = tool_call_activity(self.home, now=NOW + TOOL_CALL_OPEN_TTL_SECONDS + 1)

        self.assertTrue(still_open["live"])
        self.assertFalse(expired["live"])
        self.assertEqual(expired["open_call_count"], 0)

    def test_a_close_with_no_matching_open_is_a_silent_no_op(self):
        record_tool_call_close("never-opened", omh_home=self.home, now=NOW)

        self.assertFalse(tool_bursts_path(self.home).exists())

    def test_the_open_ledger_is_capped_under_pathological_growth(self):
        for index in range(MAX_OPEN_TOOL_CALLS + 10):
            record_tool_call(
                "terminal",
                omh_home=self.home,
                now=NOW + index,
                tool_call_id=f"call-{index}",
                turn_id="turn-1",
            )

        activity = tool_call_activity(self.home, now=NOW + MAX_OPEN_TOOL_CALLS + 10)

        self.assertLessEqual(activity["open_call_count"], MAX_OPEN_TOOL_CALLS)

    def test_the_latest_shot_reports_its_open_and_completed_split(self):
        record_tool_call("read_file", omh_home=self.home, now=NOW, tool_call_id="call-1", turn_id="turn-1")
        record_tool_call("terminal", omh_home=self.home, now=NOW + 0.1, tool_call_id="call-2", turn_id="turn-1")
        record_tool_call_close("call-2", omh_home=self.home, now=NOW + 0.2)

        shot = latest_parallel_shot(self.home, now=NOW + 1)
        activity = tool_call_activity(self.home, now=NOW + 1)

        self.assertEqual(shot["status"], "observed")
        self.assertEqual(shot["open_count"], 1)
        self.assertEqual(shot["completed_count"], 1)
        self.assertEqual(activity["latest_shot"]["open_count"], 1)
        self.assertTrue(activity["live"])

    def test_the_hud_payload_carries_the_latest_shot(self):
        # The reader checks freshness against wall time, so record against
        # wall time here.
        import time

        base = time.time()
        record_tool_call("read_file", omh_home=self.home, now=base)
        record_tool_call("terminal", omh_home=self.home, now=base + 0.3)
        hermes = str(Path(self._tmp.name) / "hermes")
        payload = read_omh_hud(self.home, hermes)
        shot = payload["parallel_shot"]
        self.assertEqual(shot["status"], "observed")
        self.assertEqual(shot["size"], 2)
        self.assertEqual(shot["distinct_tools"], 2)
        self.assertIn("activity", payload)
        self.assertFalse(payload["activity"]["live"])
        self.assertEqual(payload["activity"]["open_call_count"], 0)

    def test_the_hud_payload_carries_live_open_call_activity(self):
        import time

        base = time.time()
        record_tool_call("terminal", omh_home=self.home, now=base, tool_call_id="call-1", turn_id="turn-1")
        hermes = str(Path(self._tmp.name) / "hermes")

        payload = read_omh_hud(self.home, hermes)

        self.assertTrue(payload["activity"]["live"])
        self.assertEqual(payload["activity"]["open_call_count"], 1)


if __name__ == "__main__":
    unittest.main()
