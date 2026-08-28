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
    tool_call_projection,
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

    def test_a_close_with_no_matching_open_closes_nothing_but_is_still_observed(self):
        record_tool_call_close("never-opened", omh_home=self.home, now=NOW)

        # Nothing to close, but the file now exists -- post_tool_call firing
        # at all is the evidence the HUD needs, independent of pairing (P2-1).
        self.assertTrue(tool_bursts_path(self.home).exists())
        self.assertTrue(tool_call_activity(self.home, now=NOW)["post_tool_call_observed"])

    def test_liveness_is_unanswerable_until_post_tool_call_is_ever_observed(self):
        record_tool_call("terminal", omh_home=self.home, now=NOW, tool_call_id="call-1")

        unanswered = tool_call_activity(self.home, now=NOW + 1)
        self.assertFalse(unanswered["post_tool_call_observed"])
        # `live` still reports what the ledger has, but the flag beside it
        # is what tells the widget whether to trust that reading at all.
        self.assertTrue(unanswered["live"])

        record_tool_call_close("call-1", omh_home=self.home, now=NOW + 2)

        answered = tool_call_activity(self.home, now=NOW + 3)
        self.assertTrue(answered["post_tool_call_observed"])
        self.assertFalse(answered["live"])

    def test_tool_call_projection_matches_the_separate_calls_from_one_read(self):
        record_tool_call("read_file", omh_home=self.home, now=NOW, tool_call_id="call-1")
        record_tool_call("terminal", omh_home=self.home, now=NOW + 0.1, tool_call_id="call-2")
        record_tool_call_close("call-2", omh_home=self.home, now=NOW + 0.2)

        projection = tool_call_projection(self.home, now=NOW + 1)

        self.assertEqual(projection["parallel_shot"], latest_parallel_shot(self.home, now=NOW + 1))
        self.assertEqual(projection["activity"], tool_call_activity(self.home, now=NOW + 1))
        # The activity block's own latest_shot is the identical object the
        # top-level parallel_shot key carries -- one computation, not two.
        self.assertEqual(projection["activity"]["latest_shot"], projection["parallel_shot"])

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

    def test_the_latest_shot_reports_its_open_and_closed_split(self):
        record_tool_call("read_file", omh_home=self.home, now=NOW, tool_call_id="call-1", turn_id="turn-1")
        record_tool_call("terminal", omh_home=self.home, now=NOW + 0.1, tool_call_id="call-2", turn_id="turn-1")
        record_tool_call_close("call-2", omh_home=self.home, now=NOW + 0.2)

        shot = latest_parallel_shot(self.home, now=NOW + 1)
        activity = tool_call_activity(self.home, now=NOW + 1)

        self.assertEqual(shot["status"], "observed")
        self.assertEqual(shot["open_count"], 1)
        # Not "completed" -- an entry with no tool_call_id or an expired
        # entry lands here too, and neither was observed finishing (P3-1).
        self.assertEqual(shot["closed_or_unobserved_count"], 1)
        self.assertEqual(activity["latest_shot"]["open_count"], 1)
        self.assertTrue(activity["live"])

    def test_the_latest_shot_reports_peak_concurrency_not_dispatch_size(self):
        # Four strictly SEQUENTIAL calls, each closing before the next opens,
        # chained into one burst by the 1.5s grouping window. `size` is 4,
        # but at no point were more than 1 open at once -- the badge must
        # read the true peak, not the dispatch count (P1-2).
        for index in range(4):
            call_id = f"call-{index}"
            record_tool_call("terminal", omh_home=self.home, now=NOW + index * 0.3, tool_call_id=call_id)
            record_tool_call_close(call_id, omh_home=self.home, now=NOW + index * 0.3 + 0.05)

        shot = latest_parallel_shot(self.home, now=NOW + 1)

        self.assertEqual(shot["status"], "observed")
        self.assertEqual(shot["size"], 4)
        self.assertEqual(shot["peak_open_count"], 1)

    def test_the_latest_shot_reports_true_overlap_when_calls_genuinely_stack(self):
        # call-1 opens and is still open when call-2 and call-3 open too --
        # a real 3-way overlap.
        record_tool_call("read_file", omh_home=self.home, now=NOW, tool_call_id="call-1")
        record_tool_call("read_file", omh_home=self.home, now=NOW + 0.1, tool_call_id="call-2")
        record_tool_call("terminal", omh_home=self.home, now=NOW + 0.2, tool_call_id="call-3")
        record_tool_call_close("call-1", omh_home=self.home, now=NOW + 0.3)
        record_tool_call_close("call-2", omh_home=self.home, now=NOW + 0.4)
        record_tool_call_close("call-3", omh_home=self.home, now=NOW + 0.5)

        shot = latest_parallel_shot(self.home, now=NOW + 1)

        self.assertEqual(shot["peak_open_count"], 3)

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
