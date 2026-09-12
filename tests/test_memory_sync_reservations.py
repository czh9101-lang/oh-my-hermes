"""Reservation regressions: the fixture host, not OMH, performs each write."""
from __future__ import annotations

from concurrent.futures import ProcessPoolExecutor
from contextlib import contextmanager
import json
import multiprocessing
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from _local_package import load_local_package

load_local_package()
from omh.system.local_store import atomic_write_json
from omh.workflows import memory_sync_reservations as reservations


def scope(**changes: str) -> reservations.SyncScope:
    return reservations.SyncScope(**{
        "provider_id": "provider-local", "provider_mode": "local",
        "profile_ref": "qa-profile", "session_ref": "session-a",
        "policy_digest": "sha256:" + "a" * 64, **changes,
    })


def digest(number: int) -> str:
    return "sha256:" + f"{number:064x}"


def concurrent_host(path: str, barrier: object, attempt: str, inputs: tuple[str, ...]) -> str:
    # Each spawned process gets its own API instance. Subscribe before claiming.
    getattr(barrier, "wait")(timeout=15)
    decision = reservations.claim_sync(Path(path), scope(), attempt, inputs)
    if decision.decision == "claimed":
        # Append without deduplication: a broken gate produces multiple real writes.
        with Path(path).with_suffix(".writes").open("a", encoding="utf-8") as output:
            _ = output.write(attempt + "\n")
    return decision.decision


class MemorySyncReservationsTests(unittest.TestCase):
    def test_attempt_input_and_coalesced_identities(self) -> None:
        with TemporaryDirectory() as temporary:
            path = Path(temporary) / "reservations.json"
            first = reservations.claim_sync(path, scope(), "attempt-1", (digest(1), digest(2)))
            self.assertEqual(first.decision, "claimed")
            self.assertFalse(first.authorizes_provider_write)
            self.assertFalse(first.submission_observed)
            for attempt, inputs, reason in (
                ("attempt-1", (digest(2), digest(1)), "duplicate_attempt"),
                ("attempt-1", (digest(3),), "conflicting_attempt"),
                ("attempt-2", (digest(1),), "duplicate_input"),
                ("attempt-3", (digest(2), digest(4)), "duplicate_input"),
                ("attempt-4", (digest(4),), "duplicate_input"),
            ):
                result = reservations.claim_sync(path, scope(), attempt, inputs)
                self.assertEqual((result.decision, result.reason), ("held", reason))
            self.assertEqual(reservations.claim_sync(path, scope(), "attempt-5", (digest(5),)).decision, "claimed")

    def test_interruptions_and_host_reports_never_release(self) -> None:
        for outcome in (None, "unknown", "written", "not_written"):
            with self.subTest(outcome=outcome), TemporaryDirectory() as temporary:
                path = Path(temporary) / "reservations.json"
                _ = reservations.claim_sync(path, scope(), "attempt-1", (digest(1),))
                # None is interruption before a report, before OR after the host write.
                if outcome is not None:
                    result = reservations.report_sync_outcome(path, scope(), "attempt-1", (digest(1),), outcome)
                    self.assertEqual(result.host_outcome, outcome)
                    self.assertFalse(result.submission_observed)
                self.assertEqual(reservations.claim_sync(path, scope(), "attempt-1", (digest(1),)).decision, "held")
                self.assertEqual(reservations.claim_sync(path, scope(), "attempt-2", (digest(1),)).decision, "held")

    def test_conflicting_outcome_and_scope_reports_fail_closed(self) -> None:
        with TemporaryDirectory() as temporary:
            path = Path(temporary) / "reservations.json"
            _ = reservations.claim_sync(path, scope(), "attempt-1", (digest(1),))
            _ = reservations.report_sync_outcome(path, scope(), "attempt-1", (digest(1),), "written")
            before = path.read_bytes()
            for active, attempt, inputs, outcome in (
                (scope(), "attempt-1", (digest(1),), "not_written"),
                (scope(session_ref="foreign"), "attempt-1", (digest(1),), "written"),
                (scope(), "attempt-2", (digest(1),), "written"),
                (scope(), "attempt-1", (digest(2),), "written"),
            ):
                with self.assertRaises(ValueError):
                    _ = reservations.report_sync_outcome(path, active, attempt, inputs, outcome)
                self.assertEqual(before, path.read_bytes())
            _ = reservations.report_sync_outcome(path, scope(), "attempt-1", (digest(1),), "written")
            self.assertEqual(before, path.read_bytes())

    def test_scopes_are_independent_and_do_not_expose_foreign_identities(self) -> None:
        with TemporaryDirectory() as temporary:
            path = Path(temporary) / "reservations.json"
            for active in (scope(), *(scope(**{key: value}) for key, value in (
                ("provider_id", "other"), ("provider_mode", "hosted"),
                ("profile_ref", "other"), ("session_ref", "other"),
                ("policy_digest", digest(999)),
            ))):
                result = reservations.claim_sync(path, active, "attempt-1", (digest(1),))
                self.assertEqual(result.decision, "claimed")
                self.assertNotIn("attempt-1", repr(result))

    def test_queue_and_timeout_skips_are_persisted_and_not_retried(self) -> None:
        with TemporaryDirectory() as temporary:
            path = Path(temporary) / "reservations.json"
            for index, reason in enumerate(("skipped_queue", "skipped_timeout")):
                inputs = (digest(index),)
                result = reservations.claim_sync(path, scope(), f"skip-{index}", inputs, skip_reason=reason)
                self.assertEqual((result.decision, result.reason), ("skipped", reason))
                self.assertEqual(reservations.claim_sync(path, scope(), f"retry-{index}", inputs).decision, "held")
                with self.assertRaises(ValueError):
                    _ = reservations.report_sync_outcome(path, scope(), f"skip-{index}", inputs, "written")
            data = path.read_text(encoding="utf-8")
            for reason in ("skipped_queue", "skipped_timeout"):
                self.assertIn(reason, data)

    def test_cross_process_claims_count_actual_fixture_writes(self) -> None:
        context = multiprocessing.get_context("spawn")
        for duplicate_attempt in (True, False):
            with self.subTest(duplicate_attempt=duplicate_attempt), TemporaryDirectory() as temporary:
                path = Path(temporary) / "reservations.json"
                with context.Manager() as manager:
                    barrier = manager.Barrier(4)
                    with ProcessPoolExecutor(max_workers=4, mp_context=context) as executor:
                        futures = [executor.submit(concurrent_host, str(path), barrier,
                                   "attempt-1" if duplicate_attempt else f"attempt-{index}",
                                   (digest(1), digest(index + 2))) for index in range(4)]
                        decisions = [future.result(timeout=25) for future in futures]
                self.assertEqual(decisions.count("claimed"), 1)
                self.assertEqual(len(path.with_suffix(".writes").read_text().splitlines()), 1)
                self.assertEqual(decisions.count("held"), 3)

    def test_invalid_metadata_never_persists_or_echoes_content(self) -> None:
        with TemporaryDirectory() as temporary:
            path = Path(temporary) / "reservations.json"
            sentinel = "raw prompt\nQA_PRIVATE_CONTENT"
            for key in ("provider_id", "provider_mode", "profile_ref", "session_ref", "policy_digest"):
                with self.assertRaises(ValueError) as error:
                    _ = scope(**{key: sentinel})
                self.assertNotIn(sentinel, str(error.exception))
            for attempt, inputs, skip in ((sentinel, (digest(1),), None),
                                          ("attempt", (sentinel,), None),
                                          ("attempt", (), None),
                                          ("attempt", tuple(digest(n) for n in range(25)), None),
                                          ("attempt", (digest(1),), sentinel)):
                with self.assertRaises(ValueError) as error:
                    _ = reservations.claim_sync(path, scope(), attempt, inputs, skip_reason=skip)
                self.assertNotIn(sentinel, str(error.exception))
                self.assertFalse(path.exists())

    def test_corruption_capacity_and_unavailable_lock_hold(self) -> None:
        with TemporaryDirectory() as temporary:
            path = Path(temporary) / "reservations.json"
            for text in ("{", "{}", '{"schema_version":"foreign","entries":[]}',
                         '{"schema_version":"memory_sync_reservations/v1","entries":[{}]}'):
                _ = path.write_text(text, encoding="utf-8")
                with self.assertRaises(ValueError):
                    _ = reservations.claim_sync(path, scope(), "attempt", (digest(1),))
                self.assertEqual(path.read_text(), text)
            path.unlink()
            with patch.object(reservations, "MAX_RESERVATIONS", 1):
                _ = reservations.claim_sync(path, scope(), "attempt", (digest(1),))
                with self.assertRaises(ValueError):
                    _ = reservations.claim_sync(path, scope(), "other", (digest(2),))
                self.assertEqual(reservations.claim_sync(path, scope(), "attempt", (digest(1),)).decision, "held")
            @contextmanager
            def unenforced(*_args: object, **_kwargs: object):
                yield {"enforced": False}
            before = path.read_bytes()
            with patch.object(reservations, "file_lock", unenforced), self.assertRaises(RuntimeError):
                _ = reservations.claim_sync(path, scope(), "other", (digest(2),))
            self.assertEqual(path.read_bytes(), before)

    def test_atomic_write_interruptions_fail_closed(self) -> None:
        with TemporaryDirectory() as temporary:
            path = Path(temporary) / "reservations.json"
            def write_then_interrupt(path: Path, data: dict[str, object], *, private: bool) -> None:
                atomic_write_json(path, data, private=private)
                raise OSError("fixture interruption after atomic replacement")
            with patch.object(reservations, "atomic_write_json", side_effect=OSError("fixture before replacement")):
                with self.assertRaises(OSError):
                    _ = reservations.claim_sync(path, scope(), "attempt", (digest(1),))
            self.assertFalse(path.exists())
            with patch.object(reservations, "atomic_write_json", side_effect=write_then_interrupt):
                with self.assertRaises(OSError):
                    _ = reservations.claim_sync(path, scope(), "attempt", (digest(1),))
            self.assertEqual(reservations.claim_sync(path, scope(), "retry", (digest(1),)).decision, "held")
            self.assertEqual(json.loads(path.read_text())["entries"][0]["host_outcome"], "unknown")


if __name__ == "__main__":
    _ = unittest.main()
