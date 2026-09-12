"""Explicit local producer engine. No supported live Hermes adapter exists yet.

Importing this module does not collect anything. Only constructing ActivityObserver
starts its owned worker; plugin registration never does so on unsupported hosts.
"""
from __future__ import annotations

from collections import deque
from collections.abc import Mapping
import json
from threading import Condition, Event, Thread
from time import monotonic
from typing import TypedDict, assert_never, final

from omh.system.paths import OmhPaths
from omh.system.local_store import atomic_write_json
from omh.workflows.session_activity_receipts import ingest_session_activity_receipt
from .activity_observer_events import ActivityEvent, EventError, EventValue, Kind, OpaqueRef, opaque_ref, parse_event
from .activity_observer_state import JSON, Room

__all__ = ["ActivityObserver", "opaque_ref"]


class ObserverStatus(TypedDict):
    readiness: str
    compatibility: str
    dropped: int
    gapped: int
    rejected: int
    write_failed: int
    last_outcome: str


@final
class ActivityObserver:
    """Single-owner aggregation with bounded nonblocking submission.

    The condition protects queue/control counters only, never aggregation or I/O.
    Call close and check its result before disposing the invocation's state home.
    """
    def __init__(self, paths: OmhPaths, profile_ref: OpaqueRef) -> None:
        self.paths, self.profile_ref = paths, profile_ref
        self.checkpoint_path = paths.runtime_dir / f"group-activity-{profile_ref[7:]}.json"
        self._condition = Condition()
        self._pending: deque[ActivityEvent | Event] = deque()
        self._rooms: dict[tuple[OpaqueRef, OpaqueRef, OpaqueRef], Room] = {}
        self._terminal: dict[tuple[OpaqueRef, OpaqueRef, OpaqueRef], tuple[float, Room]] = {}
        self._closing = False
        self._done = Event()
        self._status: ObserverStatus = {"readiness": "local_engine", "compatibility": "normalized_input_only",
            "dropped": 0, "gapped": 0, "rejected": 0, "write_failed": 0, "last_outcome": "not_observed"}
        self._worker = Thread(target=self._run, name="omh-group-activity", daemon=True)
        self._worker.start()

    def enqueue(self, raw: Mapping[str, EventValue]) -> bool:
        """Parse and enqueue only; never wait for aggregation, checkpoint, or receipt I/O."""
        try:
            event = parse_event(raw, self.profile_ref)
        except EventError as exc:
            self._reject(exc.code)
            return False
        with self._condition:
            if self._closing or len(self._pending) >= 1024:
                self._status["dropped"] = min(10**9, self._status["dropped"] + 1)
                self._status["last_outcome"] = "queue_dropped" if not self._closing else "observer_closed"
                return False
            self._pending.append(event)
            self._condition.notify()
        return True

    def status(self) -> ObserverStatus:
        with self._condition:
            return self._status.copy()

    def flush(self, timeout: float = 10) -> bool:
        """Await a queue barrier subscribed before enqueue, not timing-based polling."""
        barrier = Event()
        with self._condition:
            if self._closing:
                return self._done.is_set()
            if not self._condition.wait_for(lambda: len(self._pending) < 1024, timeout):
                return False
            self._pending.append(barrier)
            self._condition.notify()
        return barrier.wait(timeout)

    def close(self, timeout: float = 10) -> bool:
        """Bounded unload drain. A timeout reports loss, never blocks a host indefinitely."""
        with self._condition:
            self._closing = True
            self._condition.notify()
        if not self._done.wait(timeout):
            with self._condition:
                self._status["dropped"] = min(10**9, self._status["dropped"] + 1)
                self._status["last_outcome"] = "drain_timeout"
            return False
        self._worker.join()
        return True

    def _reject(self, reason: str) -> None:
        with self._condition:
            self._status["rejected"] = min(10**9, self._status["rejected"] + 1)
            self._status["last_outcome"] = reason

    def _failed_write(self) -> None:
        with self._condition:
            self._status["write_failed"] = min(10**9, self._status["write_failed"] + 1)
            self._status["last_outcome"] = "write_failed"

    def _recover(self) -> None:
        try:
            with self.checkpoint_path.open("rb") as handle:
                raw = handle.read(131073)
            if len(raw) > 131072:
                raise EventError("invalid_checkpoint")
            data: JSON = json.loads(raw)
            if not isinstance(data, dict) or set(data) != {"schema", "rooms"} or data["schema"] != "omh_group_activity_checkpoint/v1":
                raise EventError("invalid_checkpoint")
            records = data["rooms"]
            if not isinstance(records, list) or len(records) > 64:
                raise EventError("invalid_checkpoint")
            recovered = [Room.recover(record, self.profile_ref) for record in records]
            self._rooms = {(room.profile, room.room, room.session): room for room in recovered}
        except FileNotFoundError:
            return
        except (EventError, ValueError, UnicodeError):
            self._reject("invalid_checkpoint")
            # Missing trust in a checkpoint permanently lowers subsequent intervals.
            with self._condition:
                self._status["dropped"] += 1
        except OSError:
            self._failed_write()
            with self._condition:
                self._status["dropped"] += 1

    def _checkpoint(self) -> None:
        try:
            atomic_write_json(self.checkpoint_path, {"schema": "omh_group_activity_checkpoint/v1",
                "rooms": [room.checkpoint() for room in self._rooms.values()]}, private=True)
        except OSError:
            self._failed_write()
            for room in self._rooms.values():
                room.partial = True

    def _emit(self, room: Room, final: bool) -> None:
        room.partial |= bool(self.status()["dropped"])
        try:
            result = ingest_session_activity_receipt(self.paths, room.receipt(final), expected_profile_ref=self.profile_ref)
            with self._condition:
                self._status["last_outcome"] = result["outcome"]
        except (OSError, ValueError):
            self._failed_write()

    def _aggregate(self, event: ActivityEvent) -> None:
        now = monotonic()
        self._terminal = {key: value for key, value in self._terminal.items() if now - value[0] < 86400}
        terminal = self._terminal.get(event.scope)
        if terminal is not None:
            if not terminal[1].is_replay(event):
                self._reject("stale_after_final")
            return  # Admission remains authoritative after this bounded cache expires.
        room = self._rooms.get(event.scope)
        if room is None:
            if len(self._rooms) >= 64:
                with self._condition:
                    self._status["dropped"] = min(10**9, self._status["dropped"] + 1)
                    self._status["last_outcome"] = "room_capacity"
                return
            room = Room.start(event)
            self._rooms[event.scope] = room
        try:
            outcome = room.accept(event)
        except EventError as exc:
            self._reject(exc.code)
            return
        match outcome:
            case "duplicate":
                return
            case "gap":
                with self._condition:
                    self._status["gapped"] = min(10**9, self._status["gapped"] + 1)
            case "accepted":
                pass
            case unreachable:
                assert_never(unreachable)
        match event.kind:
            case Kind.SESSION_END:
                self._emit(room, True)
                del self._rooms[event.scope]
                if len(self._terminal) >= 64:
                    del self._terminal[next(iter(self._terminal))]
                self._terminal[event.scope] = (now, room)
            case Kind.SESSION_START | Kind.MEMBER_START | Kind.MEMBER_COMPLETE | Kind.TOOL_CALL | Kind.TOOL_ERROR | Kind.COMPACTION:
                pass
            case unreachable:
                assert_never(unreachable)
        self._checkpoint()

    def _run(self) -> None:
        try:
            self._recover()
            while True:
                with self._condition:
                    # Wake at the earliest retention deadline, not a polling interval.
                    expiry = min((stamp + 86400 for stamp, _ in self._terminal.values()), default=None)
                    timeout = max(0, expiry - monotonic()) if expiry is not None else None
                    _ = self._condition.wait_for(lambda: bool(self._pending) or self._closing, timeout)
                    if self._closing and not self._pending:
                        break
                    item = self._pending.popleft() if self._pending else None
                    self._condition.notify_all()
                now = monotonic()
                self._terminal = {key: value for key, value in self._terminal.items() if now - value[0] < 86400}
                match item:
                    case None:
                        continue
                    case Event():
                        item.set()
                    case ActivityEvent():
                        try:
                            self._aggregate(item)
                        except (OSError, ValueError):
                            with self._condition:
                                self._status["dropped"] = min(10**9, self._status["dropped"] + 1)
                                self._status["last_outcome"] = "aggregation_failed"
                    case unreachable:
                        assert_never(unreachable)
            if self._rooms:
                for room in self._rooms.values():
                    self._emit(room, False)
                self._checkpoint()
        finally:
            self._done.set()
