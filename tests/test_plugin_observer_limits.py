from __future__ import annotations

import json
from pathlib import Path
import unittest
from unittest.mock import patch

import test_plugin_observer as fixtures
from omh.maintenance.doctor import run_doctor
from omh.maintenance.probe import probe_capabilities
from omh.system.paths import OmhPaths
from omh.workflows.session_activity_receipts import read_session_activity_receipts


class ObserverLimitsTests(fixtures.ObserverFixture):
    def test_O9_doctor_and_probe_advertise_unsupported_contract(self):
        with self.home() as tmp:
            paths = OmhPaths(Path(tmp) / "omh", Path(tmp) / "hermes")  # Given
            checks, probe = run_doctor(paths), probe_capabilities(paths)  # When
            status = {check.name: check for check in checks}.get("group_chat_activity")  # Then
            assert status is not None
            self.assertTrue(status.ok)
            self.assertFalse(status.observed)
            self.assertEqual(probe.get("group_chat_activity", {}).get("compatibility"),
                             "member_activity_contract_unsupported")

    def test_O6_new_events_after_terminal_are_rejected(self):
        with self.home() as tmp:
            module, observer, paths = self.engine(Path(tmp))
            self.lifecycle(module, observer)  # Given
            observer.enqueue(self.event(module, 8, "tool_call"))  # When
            self.assertTrue(observer.flush())
            self.assertEqual(observer.status()["last_outcome"], "stale_after_final")  # Then
            self.assertEqual(observer.status()["rejected"], 1)
            self.assertEqual(len(read_session_activity_receipts(paths)), 1)

    def test_O8_aggregation_failure_preserves_worker_and_reports_loss(self):
        with self.home() as tmp:
            module, observer, paths = self.engine(Path(tmp))  # Given
            with patch.object(observer, "_aggregate", side_effect=OSError("SYNTHETIC_PRIVATE_SENTINEL")):
                observer.enqueue(self.event(module, 0, "session_start"))  # When
                self.assertTrue(observer.flush(1))
            self.assertGreater(observer.status()["dropped"], 0)  # Then
            self.assertEqual(observer.status()["last_outcome"], "aggregation_failed")
            self.assertNotIn("SYNTHETIC_PRIVATE_SENTINEL", json.dumps(observer.status()))
            self.assertEqual(read_session_activity_receipts(paths), [])

    def test_O4_O5_topology_and_dedupe_caps_never_recover_exactness(self):
        with self.home() as tmp:
            module, observer, paths = self.engine(Path(tmp))  # Given
            observer.enqueue(self.event(module, 0, "session_start"))
            for index in range(1, 258):
                event = self.event(module, 1, "tool_call")
                event.update(sequence=index, event_ref=module.opaque_ref(b"fixture-key", str(index)),
                             member_ref=module.opaque_ref(b"fixture-key", f"m{index}"),
                             turn_ref=module.opaque_ref(b"fixture-key", f"t{index}"))
                observer.enqueue(event)
            self.assertTrue(observer.flush())
            terminal = self.event(module, 2, "session_end")
            terminal["sequence"] = 258
            observer.enqueue(terminal)  # When: terminal after member/turn overflow.
            self.assertTrue(observer.flush())
            receipt = read_session_activity_receipts(paths)[0]  # Then
            self.assertEqual(receipt["metrics"]["tool_calls"]["value"], 256)
            self.assertEqual(receipt["metrics"]["tool_calls"]["measurement"], "floor")

    def test_O7_terminal_metadata_expires_at_retention_deadline(self):
        with self.home() as tmp:
            module, observer, _ = self.engine(Path(tmp))
            with patch.object(module, "monotonic", return_value=0):
                self.lifecycle(module, observer)  # Given: finalized cache at monotonic zero.
            with patch.object(module, "monotonic", return_value=86401):
                self.assertTrue(observer.flush())  # When: exact queue barrier wakes the worker.
                self.assertEqual(len(observer._terminal), 0)  # Then: even without another activity event.

    def test_O7_oversized_kind_is_rejected_without_formatting_input(self):
        class ReprForbidden(str):
            def __repr__(self):
                raise AssertionError("unbounded kind was formatted")
        with self.home() as tmp:
            module, observer, _ = self.engine(Path(tmp))  # Given
            event = self.event(module, 0, "session_start")
            event["kind"] = ReprForbidden("x" * 65536)
            accepted = observer.enqueue(event)  # When
            self.assertFalse(accepted)  # Then
            self.assertEqual(observer.status()["last_outcome"], "unknown_kind")

    def test_O3_duplicate_active_event_cannot_inflate_counters(self):
        with self.home() as tmp:
            module, observer, paths = self.engine(Path(tmp))
            observer.enqueue(self.event(module, 0, "session_start"))  # Given
            event = self.event(module, 1, "tool_call")
            for _ in range(3):
                observer.enqueue(event)
            observer.enqueue(self.event(module, 2, "session_end"))  # When
            self.assertTrue(observer.flush())
            self.assertEqual(read_session_activity_receipts(paths)[0]["metrics"]["tool_calls"]["value"], 1)  # Then

    def test_O4_dedupe_overflow_retains_keys_and_stops_counting(self):
        from omh.plugin_bundle.omh.activity_observer_events import parse_event
        from omh.plugin_bundle.omh.activity_observer_state import Room
        with self.home() as tmp:
            module, observer, _ = self.engine(Path(tmp))
            first = parse_event(self.event(module, 0, "session_start"), observer.profile_ref)
            room = Room.start(first)
            room.accept(first)  # Given
            for index in range(1, 4098):  # When: exceed room identity capacity without queue pressure.
                event = self.event(module, 1, "tool_call")
                event.update(sequence=index, event_ref=module.opaque_ref(b"fixture-key", str(index)))
                room.accept(parse_event(event, observer.profile_ref))
            self.assertEqual(len(room.seen), 4096)  # Then
            self.assertEqual(room.counts["tool_calls"], 4095)
            self.assertTrue(room.partial)
            self.assertIn(first.event_ref, room.seen)

    def test_O4_room_capacity_drops_never_evict_active_intervals(self):
        with self.home() as tmp:
            module, observer, _ = self.engine(Path(tmp))  # Given
            for room in range(65):
                observer.enqueue(self.event(module, 0, "session_start", room=f"r{room}"))  # When
            self.assertTrue(observer.flush())
            checkpoint = json.loads(observer.checkpoint_path.read_text())  # Then
            self.assertEqual(len(checkpoint["rooms"]), 64)
            self.assertEqual(observer.status()["dropped"], 1)


if __name__ == "__main__":
    unittest.main()
