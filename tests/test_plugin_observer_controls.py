"""Independent completeness and dispatch controls for observer mutation coverage."""
from __future__ import annotations

from pathlib import Path
from threading import Event, Thread
import unittest
from unittest.mock import patch

import test_plugin_observer as fixtures
from omh.system.paths import OmhPaths
from omh.workflows.session_activity_receipts import read_session_activity_receipts


class ObserverControlTests(fixtures.ObserverFixture):
    def test_O4_terminal_after_late_start_remains_floor(self):
        with self.home() as tmp:
            module, observer, paths = self.engine(Path(tmp))
            # Given: contiguous sequencing without an observed session start.
            observer.enqueue(self.event(module, 0, "tool_call"))
            # When: an observed terminal (not shutdown, which independently forces floor).
            observer.enqueue(self.event(module, 1, "session_end"))
            self.assertTrue(observer.flush())
            # Then: terminal does not repair the missing start.
            receipt = read_session_activity_receipts(paths)[0]
            self.assertTrue(receipt["boundary"]["final"])
            self.assertEqual(receipt["metrics"]["tool_calls"]["measurement"], "floor")

    def test_O4_terminal_after_sequence_gap_remains_floor(self):
        with self.home() as tmp:
            module, observer, paths = self.engine(Path(tmp))
            # Given: observed start with exactly one sequence gap.
            observer.enqueue(self.event(module, 0, "session_start"))
            observer.enqueue(self.event(module, 2, "tool_call"))
            # When: observed terminal, without another uncertainty source.
            observer.enqueue(self.event(module, 3, "session_end"))
            self.assertTrue(observer.flush())
            # Then: the gap alone lowers the observed metric.
            receipt = read_session_activity_receipts(paths)[0]
            self.assertTrue(receipt["boundary"]["final"])
            self.assertEqual(receipt["metrics"]["tool_calls"]["measurement"], "floor")

    def test_O4_recovery_without_stale_replay_remains_floor(self):
        with self.home() as tmp:
            module, observer, paths = self.engine(Path(tmp))
            observer.enqueue(self.event(module, 0, "session_start"))
            observer.enqueue(self.event(module, 1, "tool_call"))
            self.assertTrue(observer.flush())
            # Given: actual aggregate checkpoint, no replay/gap to force partial independently.
            restored = OmhPaths(Path(tmp) / "restored", paths.hermes_home)
            restored.runtime_dir.mkdir(parents=True)
            (restored.runtime_dir / observer.checkpoint_path.name).write_bytes(observer.checkpoint_path.read_bytes())
            other = module.ActivityObserver(restored, observer.profile_ref)
            self.observers.append(other)
            # When: next contiguous event is a terminal.
            other.enqueue(self.event(module, 2, "session_end"))
            self.assertTrue(other.flush())
            # Then: restart alone lowers completeness.
            receipt = read_session_activity_receipts(restored)[0]
            self.assertEqual(receipt["metrics"]["tool_calls"],
                             {"value": 1, "availability": "observed", "measurement": "floor"})

    def test_O4_queue_loss_without_sequence_gap_remains_floor(self):
        with self.home() as tmp:
            module, observer, paths = self.engine(Path(tmp))
            entered, release = Event(), Event()
            original = module.ingest_session_activity_receipt

            def held_writer(*args, **kwargs):
                entered.set()
                if not release.wait(30):
                    raise TimeoutError("fixture writer release missing")
                return original(*args, **kwargs)

            with patch.object(module, "ingest_session_activity_receipt", held_writer):
                try:
                    observer.enqueue(self.event(module, 0, "session_start", room="held"))
                    observer.enqueue(self.event(module, 1, "session_end", room="held"))
                    self.assertTrue(entered.wait(10))
                    # Given: a full queue of valid metadata; the dropped event is a replay,
                    # so no producer sequence gap independently establishes the floor.
                    observer.enqueue(self.event(module, 0, "session_start"))
                    event = self.event(module, 1, "tool_call")
                    for _ in range(1023):
                        self.assertTrue(observer.enqueue(event))
                    self.assertFalse(observer.enqueue(event))
                finally:
                    release.set()
                self.assertTrue(observer.flush())
            # When: next contiguous terminal closes the affected room.
            observer.enqueue(self.event(module, 2, "session_end"))
            self.assertTrue(observer.flush())
            # Then: queue loss alone prevents exactness without inflating the replay.
            receipt = read_session_activity_receipts(paths)[-1]
            self.assertEqual(receipt["metrics"]["tool_calls"],
                             {"value": 1, "availability": "observed", "measurement": "floor"})

    def test_O8_callback_returns_while_storage_is_held(self):
        with self.home() as tmp:
            module, observer, paths = self.engine(Path(tmp))
            entered, release, returned = Event(), Event(), Event()
            accepted: list[bool] = []
            original = module.ingest_session_activity_receipt

            def held_writer(*args, **kwargs):
                entered.set()
                if not release.wait(30):
                    raise TimeoutError("fixture writer release missing")
                return original(*args, **kwargs)

            def submit_terminal():
                accepted.append(observer.enqueue(self.event(module, 1, "session_end")))
                returned.set()

            with patch.object(module, "ingest_session_activity_receipt", held_writer):
                # Given: listeners exist before terminal submission; storage can be
                # released only by the test's finally block, never by timing luck.
                observer.enqueue(self.event(module, 0, "session_start"))
                caller = Thread(target=submit_terminal, name="observer-control-caller")
                try:
                    caller.start()  # When
                    self.assertTrue(entered.wait(10))
                    # Then: dispatch returns while the writer still awaits release.
                    self.assertTrue(returned.wait(2), "callback waited for held storage")
                    self.assertEqual(accepted, [True])
                    self.assertEqual(read_session_activity_receipts(paths), [])
                finally:
                    release.set()
                    caller.join(10)
                    self.assertFalse(caller.is_alive(), "owned callback caller did not exit")
                self.assertTrue(observer.flush())


if __name__ == "__main__":
    unittest.main()
