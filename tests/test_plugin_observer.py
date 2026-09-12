from __future__ import annotations

import importlib
import importlib.util
import json
from contextlib import contextmanager
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Event
from typing import TYPE_CHECKING
import unittest
from unittest.mock import patch

from _local_package import load_local_package
load_local_package()
from omh.plugin_bundle import omh as plugin
from omh.paths import OmhPaths
from omh.workflows.session_activity_receipts import (
    METRIC_NAMES, SESSION_ACTIVITY_CONSUMERS, read_session_activity_receipts,
    session_activity_evidence, validate_session_activity_receipt,
)
from test_plugin_distribution import FakeHermesContext

if TYPE_CHECKING:
    from omh.plugin_bundle.omh.activity_observer import ActivityObserver


class EnabledContext(FakeHermesContext):
    def get_config(self, key: str, default: dict[str, bool] | None = None):
        return {"enabled": True} if key == "group_chat_activity" else default


class ObserverFixture(unittest.TestCase):
    def __init__(self, methodName: str = "runTest"):
        super().__init__(methodName)
        self.observers: list[ActivityObserver] = []

    @contextmanager
    def home(self):
        with TemporaryDirectory() as tmp:
            self.observers = []
            try:
                yield tmp
            finally:
                for observer in self.observers:
                    self.assertTrue(observer.close(), "owned worker did not drain")

    def engine(self, root: Path, profile: str = "a"):
        spec = importlib.util.find_spec("omh.plugin_bundle.omh.activity_observer")
        self.assertIsNotNone(spec, "O2: normalized lifecycle producer is absent")
        module = importlib.import_module("omh.plugin_bundle.omh.activity_observer")
        paths = OmhPaths(root / profile, root / "hermes")
        observer = module.ActivityObserver(paths, module.opaque_ref(b"fixture-key", profile))
        self.observers.append(observer)
        return module, observer, paths

    def event(self, module, index: int, kind: str, profile: str = "a", room: str = "r"):
        opaque = lambda value: module.opaque_ref(b"fixture-key", value)
        return dict(schema="omh_group_activity_event/v1", profile_ref=opaque(profile),
                    session_ref=opaque("s"), room_ref=opaque(room),
                    member_ref=None if kind in {"session_start", "session_end"} else opaque("m"),
                    turn_ref=None if kind in {"session_start", "session_end"} else opaque("t"),
                    kind=kind, event_ref=opaque(f"{room}-{index}"), sequence=index,
                    observed_at=f"2026-09-12T00:00:{index:02d}Z")

    def lifecycle(self, module, observer, profile="a", room="r"):
        kinds = ("session_start", "member_start", "tool_call", "tool_call", "tool_error",
                 "compaction", "member_complete", "session_end")
        for index, kind in enumerate(kinds):
            self.assertTrue(observer.enqueue(self.event(module, index, kind, profile, room)))
        self.assertTrue(observer.flush())


class ObserverTests(ObserverFixture):
    def test_O1_disabled_registers_no_observer_or_state(self):
        # Given / When: a host with no opt-in registers the normal plugin.
        context = FakeHermesContext()
        with TemporaryDirectory() as tmp, patch.dict("os.environ", {"OMH_HOME": tmp}):
            plugin.register(context)
            # Then: readiness explicitly disabled, no observer state.
            entry = context.tools["omh_status"]
            assert isinstance(entry, dict)
            handler = entry["args"][2]
            payload = json.loads(handler({}))
            self.assertEqual(payload.get("group_chat_activity", {}).get("readiness"), "disabled")
            self.assertFalse(list(Path(tmp).rglob("*activity*")))
            self.assertFalse(any("member" in name for name in context.hooks))

    def test_O2_full_lifecycle_records_exactly_one_final(self):
        with self.home() as tmp:
            module, observer, paths = self.engine(Path(tmp))  # Given
            self.lifecycle(module, observer)  # When
            receipts = read_session_activity_receipts(paths)  # Then
            self.assertEqual(len(receipts), 1)
            receipt = receipts[0]
            self.assertEqual(validate_session_activity_receipt(receipt), [])
            self.assertEqual(receipt["boundary"], {"kind": "session_end", "final": True, "sequence": 7})
            self.assertEqual(receipt["observed_interval"]["coverage"], "full_session")
            counts = {"tool_calls": 2, "tool_errors": 1, "compaction_boundaries": 1}
            for name in METRIC_NAMES:
                reading = receipt["metrics"][name]
                self.assertEqual(reading["value"], counts.get(name))
                self.assertEqual(reading["measurement"], "exact" if name in counts else "")

    def test_O3_terminal_replay_does_not_duplicate(self):
        with self.home() as tmp:
            module, observer, paths = self.engine(Path(tmp))
            self.lifecycle(module, observer)  # Given
            self.lifecycle(module, observer)  # When: full replay
            self.assertEqual(len(read_session_activity_receipts(paths)), 1)  # Then
            self.assertEqual(read_session_activity_receipts(paths)[0]["metrics"]["tool_calls"]["value"], 2)

    def test_O4_late_gap_and_missing_terminal_are_floors(self):
        for indices in ((2, 3), (0, 2)):
            with self.subTest(indices=indices), self.home() as tmp:
                module, observer, paths = self.engine(Path(tmp))  # Given
                for index in indices:
                    observer.enqueue(self.event(module, index, "session_start" if index == 0 else "tool_call"))
                observer.close()  # When: uncertain process boundary
                receipt = read_session_activity_receipts(paths)[0]  # Then
                self.assertFalse(receipt["boundary"]["final"])
                self.assertEqual(receipt["boundary"]["kind"], "process_exit")
                self.assertNotEqual(receipt["observed_interval"]["coverage"], "full_session")
                self.assertEqual(receipt["metrics"]["tool_calls"]["measurement"], "floor")

    def test_O4_restart_preserves_floor_without_recounting(self):
        with self.home() as tmp:
            module, observer, paths = self.engine(Path(tmp))
            observer.enqueue(self.event(module, 0, "session_start"))
            observer.enqueue(self.event(module, 1, "tool_call"))
            self.assertTrue(observer.flush())
            # Given: copy only the checkpoint, simulating abrupt process loss.
            restored = OmhPaths(Path(tmp) / "restarted", paths.hermes_home)
            checkpoint = observer.checkpoint_path
            restored.runtime_dir.mkdir(parents=True)
            (restored.runtime_dir / checkpoint.name).write_bytes(checkpoint.read_bytes())
            other = module.ActivityObserver(restored, observer.profile_ref)
            self.observers.append(other)
            other.enqueue(self.event(module, 1, "tool_call"))
            other.enqueue(self.event(module, 2, "session_end"))  # When
            self.assertTrue(other.flush())
            receipt = read_session_activity_receipts(restored)[0]  # Then
            self.assertEqual(receipt["metrics"]["tool_calls"],
                             {"availability": "observed", "measurement": "floor", "value": 1})

    def test_O5_profile_room_isolation(self):
        with self.home() as tmp:
            engines = [self.engine(Path(tmp), profile) for profile in ("a", "b")]
            # Given / When: interleaved profile- and room-local streams.
            for index, kind in enumerate(("session_start", "tool_call", "session_end")):
                for profile, (module, observer, _) in zip(("a", "b"), engines):
                    for room in ("r", "r2"):
                        observer.enqueue(self.event(module, index, kind, profile, room))
            for _, observer, paths in engines:  # Then
                self.assertTrue(observer.flush())
                receipts = read_session_activity_receipts(paths)
                self.assertEqual(len(receipts), 2)
                self.assertEqual(len({receipt["session_ref"] for receipt in receipts}), 2)
                self.assertTrue(all(r["metrics"]["tool_calls"]["value"] == 1 for r in receipts))

    def test_O6_invalid_cross_profile_stale_and_conflict_are_bounded(self):
        with self.home() as tmp:
            module, observer, paths = self.engine(Path(tmp))  # Given
            observer.enqueue(self.event(module, 0, "session_start"))
            self.assertTrue(observer.flush())
            invalid = [dict(self.event(module, 1, "tool_call"), kind="unknown"),
                       self.event(module, 1, "tool_call", "foreign"),
                       dict(self.event(module, 1, "tool_call"), room_ref="raw-platform-id"),
                       dict(self.event(module, 0, "tool_call")),
                       dict(self.event(module, 1, "tool_call"), observed_at="2026-09-11T00:00:00Z")]
            for event in invalid:  # When
                observer.enqueue(event)
            self.assertTrue(observer.flush())
            self.assertEqual(observer.status()["rejected"], 5)  # Then
            self.assertEqual(read_session_activity_receipts(paths), [])

    def test_O7_raw_fields_never_enter_queue_or_status(self):
        with self.home() as tmp:
            module, observer, paths = self.engine(Path(tmp))  # Given
            for key in ("prompt", "response", "messages", "transcript", "tool_args", "tool_results",
                        "credential", "approval", "platform_id"):
                event = dict(self.event(module, 0, "session_start"), **{key: "SYNTHETIC_PRIVATE_SENTINEL"})
                self.assertFalse(observer.enqueue(event))  # When
            self.assertTrue(observer.flush())
            self.assertNotIn("SYNTHETIC_PRIVATE_SENTINEL", json.dumps(observer.status()))  # Then
            self.assertEqual(read_session_activity_receipts(paths), [])
            self.assertFalse(observer.checkpoint_path.exists())

    def test_O8_blocked_writer_drops_without_blocking_callback(self):
        with self.home() as tmp:
            module, observer, paths = self.engine(Path(tmp))
            entered, release = Event(), Event()
            original = module.ingest_session_activity_receipt
            def held_writer(*args, **kwargs):
                entered.set()
                if not release.wait(10):
                    raise TimeoutError("fixture release missing")
                return original(*args, **kwargs)
            with patch.object(module, "ingest_session_activity_receipt", held_writer):
                try:
                    observer.enqueue(self.event(module, 0, "session_start"))
                    observer.enqueue(self.event(module, 1, "session_end"))
                    self.assertTrue(entered.wait(10))  # Given: storage held.
                    accepted = [observer.enqueue(self.event(module, 2, "tool_call", room="other"))
                                for _ in range(1025)]  # When: callback returns before release.
                    self.assertEqual(sum(accepted), 1024)  # Then
                    self.assertEqual(observer.status()["dropped"], 1)
                finally:
                    release.set()
                self.assertTrue(observer.flush())
            observer.close()
            self.assertTrue(all(r["observed_interval"]["coverage"] != "full_session"
                                for r in read_session_activity_receipts(paths)[1:]))

    def test_O8_write_failure_is_not_recorded(self):
        with self.home() as tmp:
            module, observer, paths = self.engine(Path(tmp))  # Given
            with patch.object(module, "ingest_session_activity_receipt", side_effect=OSError("private sentinel")):
                self.lifecycle(module, observer)  # When
            self.assertGreater(observer.status()["write_failed"], 0)  # Then
            self.assertEqual(observer.status()["last_outcome"], "write_failed")
            self.assertEqual(read_session_activity_receipts(paths), [])
            self.assertNotIn("private sentinel", json.dumps(observer.status()))

    def test_O9_unsupported_keeps_existing_registration(self):
        context = EnabledContext()  # Given
        plugin.register(context)  # When
        entry = context.tools["omh_status"]
        assert isinstance(entry, dict)
        payload = json.loads(entry["args"][2]({}))  # Then
        status = payload.get("group_chat_activity", {})
        self.assertEqual(status.get("readiness"), "unavailable")
        self.assertEqual(status.get("compatibility"), "member_activity_contract_unsupported")
        self.assertIn("on_session_end", context.hooks)
        self.assertIn("omh_memory", context.tools)
        self.assertFalse(any("member" in name for name in context.hooks))

    def test_O10_all_consumer_projections_preserve_observed_boundary(self):
        with self.home() as tmp:
            module, observer, paths = self.engine(Path(tmp))
            self.lifecycle(module, observer)  # Given
            receipt = read_session_activity_receipts(paths)[0]
            projections = [session_activity_evidence(receipt, name) for name in SESSION_ACTIVITY_CONSUMERS]  # When
            self.assertEqual(len(projections), 6)  # Then
            self.assertTrue(all(p["terminal"] and p["coverage"] == "full_session" for p in projections))


if __name__ == "__main__":
    unittest.main()
