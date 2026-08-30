from __future__ import annotations

import json
import re
import threading
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock

from _local_package import load_local_package

load_local_package()
from omh.goal_ledger import (
    GOAL_STATUSES,
    cancel_goal_ledger,
    complete_goal_ledger,
    create_goal_ledger,
    goal_ledger_path,
    read_goal_ledger,
    record_goal_blocker,
    record_goal_checkpoint,
    record_goal_quality_gate,
    validate_goal_ledger,
)
from omh.goal_loop import (
    block_loop_queue_item,
    create_loop_cycle,
    loop_cycle_path,
    observe_loop_queue_item,
    read_loop_cycle,
    tick_loop_runtime,
    validate_loop_cycle,
)
from omh.local_store import read_json_object
from omh.paths import resolve_paths
from omh.record_revision import (
    APPLIED_MUTATIONS_FLOOR_KEY,
    APPLIED_MUTATIONS_KEY,
    APPLIED_MUTATIONS_LIMIT,
    MAX_MUTATION_ID_CHARS,
    MAX_OPERATION_CHARS,
    RECORD_REVISION_KEY,
    ConflictingMutationReplay,
    DuplicateMutationReplay,
    MutationHistoryEvicted,
    StaleRecordMutation,
    applied_mutation_key,
    applied_mutations_floor_of,
    guarded_record_update,
    record_revision_of,
    require_not_terminal,
    revision_field_errors,
    validated_mutation_id,
    validated_operation,
)
from omh.runtime_records import WRAPPER_SESSION_RECORD_KEYS
from test_local_store_locking import FakeMsvcrt
from omh.wrapper.executor_sessions import (
    ExecutorSessionError,
    attach_executor_session,
    open_executor_session,
    record_executor_session_result,
)
from omh.wrapper_sessions import (
    WrapperSessionError,
    create_or_resume_wrapper_session,
    prepare_wrapper_session_handoff,
    read_wrapper_session,
    record_plan_decision,
    select_wrapper_session_executor,
)

try:
    import fcntl as _fcntl  # noqa: F401 - import used only to probe platform availability

    HAS_FCNTL = True
except ImportError:
    HAS_FCNTL = False


def _bump(current: dict[str, object]) -> dict[str, object]:
    return {**current, "value": int(current.get("value", 0)) + 1}


class RecordRevisionHelperTests(unittest.TestCase):
    def test_revision_starts_at_one_and_increments_once_per_mutation(self) -> None:
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "record.json"

            first = guarded_record_update(path, mutate=_bump, operation="probe", lock_name="record.json", default={})
            second = guarded_record_update(path, mutate=_bump, operation="probe", lock_name="record.json")
            third = guarded_record_update(path, mutate=_bump, operation="probe", lock_name="record.json")

            self.assertEqual(first[RECORD_REVISION_KEY], 1)
            self.assertEqual(second[RECORD_REVISION_KEY], 2)
            self.assertEqual(third[RECORD_REVISION_KEY], 3)
            self.assertEqual(third["value"], 3)
            self.assertEqual(record_revision_of(read_json_object(path)), 3)

    def test_record_revision_of_rejects_bools_and_missing_values(self) -> None:
        self.assertEqual(record_revision_of(None), 0)
        self.assertEqual(record_revision_of({}), 0)
        self.assertEqual(record_revision_of({RECORD_REVISION_KEY: True}), 0)
        self.assertEqual(record_revision_of({RECORD_REVISION_KEY: "3"}), 0)
        self.assertEqual(record_revision_of({RECORD_REVISION_KEY: -2}), 0)
        self.assertEqual(record_revision_of({RECORD_REVISION_KEY: 7}), 7)

    def test_stale_mutation_is_rejected_and_leaves_the_file_byte_identical(self) -> None:
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "record.json"
            guarded_record_update(path, mutate=_bump, operation="probe", lock_name="record.json", default={})
            guarded_record_update(path, mutate=_bump, operation="probe", lock_name="record.json")
            before = path.read_bytes()

            with self.assertRaises(StaleRecordMutation) as caught:
                guarded_record_update(
                    path,
                    mutate=_bump,
                    operation="probe",
                    lock_name="record.json",
                    expected_revision=1,
                )

            self.assertEqual(path.read_bytes(), before)
            self.assertEqual(caught.exception.expected_revision, 1)
            self.assertEqual(caught.exception.current_revision, 2)
            self.assertIn("record_revision 2", str(caught.exception))

    def test_stale_rejection_never_runs_the_mutate_callable(self) -> None:
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "record.json"
            guarded_record_update(path, mutate=_bump, operation="probe", lock_name="record.json", default={})
            calls: list[int] = []

            def mutate(current: dict[str, object]) -> dict[str, object]:
                calls.append(1)
                return _bump(current)

            with self.assertRaises(StaleRecordMutation):
                guarded_record_update(path, mutate=mutate, operation="probe", lock_name="record.json", expected_revision=99)

            self.assertEqual(calls, [])

    def test_matching_expected_revision_applies_the_mutation(self) -> None:
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "record.json"
            first = guarded_record_update(path, mutate=_bump, operation="probe", lock_name="record.json", default={})

            second = guarded_record_update(
                path,
                mutate=_bump,
                operation="probe",
                lock_name="record.json",
                expected_revision=first[RECORD_REVISION_KEY],
            )

            self.assertEqual(second[RECORD_REVISION_KEY], 2)
            self.assertEqual(second["value"], 2)

    def test_replayed_mutation_id_returns_the_original_outcome_without_a_write(self) -> None:
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "record.json"
            applied = guarded_record_update(
                path,
                mutate=_bump,
                operation="probe",
                lock_name="record.json",
                mutation_id="mutation-1",
                default={},
            )
            after_first = path.read_bytes()

            replay = guarded_record_update(
                path,
                mutate=_bump,
                operation="probe",
                lock_name="record.json",
                mutation_id="mutation-1",
                on_replay=lambda record, entry: {"digest": entry["result_digest"]},
            )

            self.assertIsInstance(replay, DuplicateMutationReplay)
            assert isinstance(replay, DuplicateMutationReplay)
            self.assertEqual(path.read_bytes(), after_first)
            self.assertEqual(replay.record_revision, applied[RECORD_REVISION_KEY])
            self.assertEqual(replay.record["value"], 1)
            self.assertEqual(
                replay.outcome,
                {"digest": applied[APPLIED_MUTATIONS_KEY]["probe:mutation-1"]["result_digest"]},
            )

    def test_distinct_mutation_ids_each_apply_once(self) -> None:
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "record.json"
            guarded_record_update(path, mutate=_bump, operation="probe", lock_name="record.json", mutation_id="a", default={})
            final = guarded_record_update(path, mutate=_bump, operation="probe", lock_name="record.json", mutation_id="b")

            self.assertEqual(final["value"], 2)
            self.assertEqual(final[RECORD_REVISION_KEY], 2)
            self.assertEqual(sorted(final[APPLIED_MUTATIONS_KEY]), ["probe:a", "probe:b"])

    def test_applied_mutations_stay_bounded_and_keep_the_most_recent(self) -> None:
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "record.json"
            total = APPLIED_MUTATIONS_LIMIT + 12
            record: dict[str, object] = {}
            for index in range(total):
                record = guarded_record_update(
                    path,
                    mutate=_bump,
                    operation="probe",
                    lock_name="record.json",
                    mutation_id=f"mutation-{index:03d}",
                    default={},
                )

            applied = record[APPLIED_MUTATIONS_KEY]
            self.assertEqual(len(applied), APPLIED_MUTATIONS_LIMIT)
            self.assertEqual(record[RECORD_REVISION_KEY], total)
            self.assertIn(f"probe:mutation-{total - 1:03d}", applied)
            self.assertNotIn("probe:mutation-000", applied)
            for entry in applied.values():
                self.assertEqual(sorted(entry), ["operation", "record_revision", "result_digest"])
                self.assertIsInstance(entry["record_revision"], int)
                self.assertIsInstance(entry["result_digest"], str)

    def test_applied_mutation_entries_hold_only_scalar_values(self) -> None:
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "record.json"
            record = guarded_record_update(
                path,
                mutate=lambda current: {**current, "nested": {"deep": [1, 2, 3]}},
                operation="probe",
                lock_name="record.json",
                mutation_id="scalars",
                default={},
            )

            for entry in record[APPLIED_MUTATIONS_KEY].values():
                for value in entry.values():
                    self.assertIsInstance(value, (int, str))
                self.assertEqual(entry["operation"], "probe")

    def test_mutate_returning_none_skips_the_write_and_the_revision_bump(self) -> None:
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "record.json"
            guarded_record_update(path, mutate=_bump, operation="probe", lock_name="record.json", default={})
            before = path.read_bytes()

            unchanged = guarded_record_update(path, mutate=lambda current: None, operation="probe", lock_name="record.json")

            self.assertEqual(path.read_bytes(), before)
            self.assertEqual(unchanged[RECORD_REVISION_KEY], 1)

    def test_validate_failure_leaves_the_record_untouched(self) -> None:
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "record.json"
            guarded_record_update(path, mutate=_bump, operation="probe", lock_name="record.json", default={})
            before = path.read_bytes()

            def reject(record: dict[str, object]) -> None:
                raise ValueError("record failed validation")

            with self.assertRaisesRegex(ValueError, "record failed validation"):
                guarded_record_update(path, mutate=_bump, operation="probe", lock_name="record.json", validate=reject)

            self.assertEqual(path.read_bytes(), before)

    def test_missing_record_without_default_raises_file_not_found(self) -> None:
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "absent.json"

            with self.assertRaises(FileNotFoundError):
                guarded_record_update(path, mutate=_bump, operation="probe", lock_name="absent.json")

    def test_lock_file_is_a_sibling_dotfile_and_never_the_record(self) -> None:
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "record.json"
            guarded_record_update(path, mutate=_bump, operation="probe", lock_name="record.json", default={})

            self.assertEqual(read_json_object(path), {**{"value": 1}, RECORD_REVISION_KEY: 1, APPLIED_MUTATIONS_KEY: {}})
            self.assertTrue((Path(tmp) / ".record.json.lock").exists())

    def test_revision_compare_is_enforced_without_fcntl(self) -> None:
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "record.json"
            with mock.patch("omh.system.local_store.fcntl", None):
                guarded_record_update(path, mutate=_bump, operation="probe", lock_name="record.json", default={})
                before = path.read_bytes()

                with self.assertRaises(StaleRecordMutation):
                    guarded_record_update(path, mutate=_bump, operation="probe", lock_name="record.json", expected_revision=99)

                self.assertEqual(path.read_bytes(), before)
                replay = guarded_record_update(
                    path,
                    mutate=_bump,
                    operation="probe",
                    lock_name="record.json",
                    mutation_id="only-once",
                )
                self.assertEqual(replay[RECORD_REVISION_KEY], 2)
                second = guarded_record_update(
                    path,
                    mutate=_bump,
                    operation="probe",
                    lock_name="record.json",
                    mutation_id="only-once",
                )
                self.assertIsInstance(second, DuplicateMutationReplay)

    def test_stale_rejection_fires_against_a_record_that_does_not_exist_yet(self) -> None:
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "record.json"

            with self.assertRaises(StaleRecordMutation) as caught:
                guarded_record_update(
                    path,
                    mutate=_bump,
                    operation="probe",
                    lock_name="record.json",
                    default={},
                    expected_revision=1,
                )

            self.assertEqual(caught.exception.current_revision, 0)
            self.assertFalse(path.exists())

    def test_expected_revision_zero_creates_the_missing_record(self) -> None:
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "record.json"

            created = guarded_record_update(
                path,
                mutate=_bump,
                operation="probe",
                lock_name="record.json",
                default={},
                expected_revision=0,
            )

            self.assertEqual(created[RECORD_REVISION_KEY], 1)

    def test_the_same_mutation_id_on_two_records_is_independent(self) -> None:
        with TemporaryDirectory() as tmp:
            first_path = Path(tmp) / "first.json"
            second_path = Path(tmp) / "second.json"

            first = guarded_record_update(
                first_path,
                mutate=_bump,
                operation="probe",
                lock_name="first.json",
                mutation_id="turn-1",
                default={},
            )
            second = guarded_record_update(
                second_path,
                mutate=_bump,
                operation="probe",
                lock_name="second.json",
                mutation_id="turn-1",
                default={},
            )

            self.assertNotIsInstance(second, DuplicateMutationReplay)
            self.assertEqual(first["value"], 1)
            self.assertEqual(second["value"], 1)
            self.assertEqual(second[RECORD_REVISION_KEY], 1)

    def test_mutate_raising_mid_transaction_leaves_the_record_byte_identical(self) -> None:
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "record.json"
            guarded_record_update(path, mutate=_bump, operation="probe", lock_name="record.json", default={})
            before = path.read_bytes()

            def explode(current: dict[str, object]) -> dict[str, object]:
                current["value"] = 999
                raise RuntimeError("mutation exploded")

            with self.assertRaisesRegex(RuntimeError, "mutation exploded"):
                guarded_record_update(path, mutate=explode, operation="probe", lock_name="record.json")

            self.assertEqual(path.read_bytes(), before)
            stored = read_json_object(path)
            assert stored is not None
            self.assertEqual(stored["value"], 1)

    def test_a_retry_carrying_both_its_mutation_id_and_a_stale_revision_replays(self) -> None:
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "record.json"
            first = guarded_record_update(
                path,
                mutate=_bump,
                operation="probe",
                lock_name="record.json",
                mutation_id="turn-1",
                default={},
            )
            # Another writer moves the record on, so the retry's rendered
            # revision is now stale as well as already applied.
            guarded_record_update(path, mutate=_bump, operation="probe", lock_name="record.json")
            after_second_write = path.read_bytes()

            retry = guarded_record_update(
                path,
                mutate=_bump,
                operation="probe",
                lock_name="record.json",
                mutation_id="turn-1",
                expected_revision=int(first[RECORD_REVISION_KEY]),
            )

            # Replay wins over staleness: a retry of applied work is not a
            # conflict, and reporting it as one would make every retry after
            # any other write look like a lost race.
            self.assertIsInstance(retry, DuplicateMutationReplay)
            assert isinstance(retry, DuplicateMutationReplay)
            self.assertTrue(retry.replayed)
            self.assertEqual(retry.record_revision, 1)
            self.assertEqual(path.read_bytes(), after_second_write)


class OperationScopedMutationTests(unittest.TestCase):
    def test_the_same_mutation_id_under_two_operations_both_apply(self) -> None:
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "record.json"

            blocked = guarded_record_update(
                path,
                mutate=_bump,
                operation="record_goal_blocker",
                lock_name="record.json",
                mutation_id="turn-4711",
                default={},
            )
            cancelled = guarded_record_update(
                path,
                mutate=_bump,
                operation="cancel_goal_ledger",
                lock_name="record.json",
                mutation_id="turn-4711",
            )

            # One client turn id reused by a different operation is different
            # logical intent: swallowing it silently drops the cancel.
            self.assertNotIsInstance(cancelled, DuplicateMutationReplay)
            self.assertEqual(blocked["value"], 1)
            self.assertEqual(cancelled["value"], 2)
            self.assertEqual(cancelled[RECORD_REVISION_KEY], 2)
            self.assertEqual(
                sorted(cancelled[APPLIED_MUTATIONS_KEY]),
                ["cancel_goal_ledger:turn-4711", "record_goal_blocker:turn-4711"],
            )

    def test_the_same_operation_and_id_replays_and_reports_it(self) -> None:
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "record.json"
            guarded_record_update(
                path,
                mutate=_bump,
                operation="record_goal_blocker",
                lock_name="record.json",
                mutation_id="turn-4711",
                default={},
            )
            before = path.read_bytes()

            replay = guarded_record_update(
                path,
                mutate=_bump,
                operation="record_goal_blocker",
                lock_name="record.json",
                mutation_id="turn-4711",
            )

            self.assertIsInstance(replay, DuplicateMutationReplay)
            assert isinstance(replay, DuplicateMutationReplay)
            self.assertTrue(replay.replayed)
            self.assertEqual(replay.operation, "record_goal_blocker")
            self.assertEqual(replay.mutation_id, "turn-4711")
            self.assertEqual(replay.record["value"], 1)
            self.assertEqual(path.read_bytes(), before)

    def test_a_divergent_payload_under_one_operation_is_refused(self) -> None:
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "record.json"
            guarded_record_update(
                path,
                mutate=_bump,
                operation="record_goal_checkpoint",
                lock_name="record.json",
                mutation_id="turn-1",
                mutation_digest="digest-of-first-checkpoint",
                default={},
            )
            before = path.read_bytes()

            with self.assertRaises(ConflictingMutationReplay) as caught:
                guarded_record_update(
                    path,
                    mutate=_bump,
                    operation="record_goal_checkpoint",
                    lock_name="record.json",
                    mutation_id="turn-1",
                    mutation_digest="digest-of-a-different-checkpoint",
                )

            self.assertEqual(path.read_bytes(), before)
            self.assertEqual(caught.exception.operation, "record_goal_checkpoint")
            self.assertEqual(caught.exception.mutation_id, "turn-1")
            self.assertIn("record_goal_checkpoint", str(caught.exception))
            self.assertIn("turn-1", str(caught.exception))

    def test_a_matching_mutation_digest_still_replays(self) -> None:
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "record.json"
            guarded_record_update(
                path,
                mutate=_bump,
                operation="record_goal_checkpoint",
                lock_name="record.json",
                mutation_id="turn-1",
                mutation_digest="same-digest",
                default={},
            )

            replay = guarded_record_update(
                path,
                mutate=_bump,
                operation="record_goal_checkpoint",
                lock_name="record.json",
                mutation_id="turn-1",
                mutation_digest="same-digest",
            )

            self.assertIsInstance(replay, DuplicateMutationReplay)
            assert isinstance(replay, DuplicateMutationReplay)
            self.assertEqual(replay.result_digest, "same-digest")

    def test_operation_is_required_and_may_not_contain_the_separator(self) -> None:
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "record.json"

            with self.assertRaisesRegex(ValueError, "operation is required"):
                guarded_record_update(path, mutate=_bump, operation="", lock_name="record.json", default={})
            with self.assertRaisesRegex(ValueError, "must not contain"):
                guarded_record_update(path, mutate=_bump, operation="a:b", lock_name="record.json", default={})

            self.assertFalse(path.exists())

    def test_applied_mutation_key_is_operation_scoped(self) -> None:
        self.assertEqual(applied_mutation_key("record_plan_decision", "turn-9"), "record_plan_decision:turn-9")


class MutationHistoryFloorTests(unittest.TestCase):
    def _fill(self, path: Path, count: int) -> dict[str, object]:
        record: dict[str, object] = {}
        for index in range(count):
            record = guarded_record_update(
                path,
                mutate=_bump,
                operation="probe",
                lock_name="record.json",
                mutation_id=f"mutation-{index:03d}",
                default={},
            )
        return record

    def test_the_floor_is_absent_until_eviction_happens(self) -> None:
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "record.json"

            record = self._fill(path, APPLIED_MUTATIONS_LIMIT)

            self.assertNotIn(APPLIED_MUTATIONS_FLOOR_KEY, record)
            self.assertEqual(applied_mutations_floor_of(record), 0)
            self.assertEqual(len(record[APPLIED_MUTATIONS_KEY]), APPLIED_MUTATIONS_LIMIT)

    def test_eviction_persists_the_floor_it_moved_to(self) -> None:
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "record.json"

            record = self._fill(path, APPLIED_MUTATIONS_LIMIT + 5)

            stored = read_json_object(path)
            assert stored is not None
            # Revisions 1..5 were evicted, so ids from those revisions are gone.
            self.assertEqual(record[APPLIED_MUTATIONS_FLOOR_KEY], 5)
            self.assertEqual(applied_mutations_floor_of(stored), 5)
            self.assertNotIn("probe:mutation-000", record[APPLIED_MUTATIONS_KEY])

    def test_a_retry_at_or_below_the_floor_is_refused_instead_of_duplicated(self) -> None:
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "record.json"
            record = self._fill(path, APPLIED_MUTATIONS_LIMIT + 5)
            before = path.read_bytes()

            with self.assertRaises(MutationHistoryEvicted) as caught:
                guarded_record_update(
                    path,
                    mutate=_bump,
                    operation="probe",
                    lock_name="record.json",
                    mutation_id="mutation-000",
                    expected_revision=1,
                )

            self.assertEqual(path.read_bytes(), before)
            self.assertEqual(caught.exception.floor_revision, 5)
            self.assertEqual(caught.exception.expected_revision, 1)
            self.assertEqual(caught.exception.current_revision, int(record[RECORD_REVISION_KEY]))
            self.assertIn("re-read the record", str(caught.exception))

    def test_a_retry_above_the_floor_still_applies(self) -> None:
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "record.json"
            record = self._fill(path, APPLIED_MUTATIONS_LIMIT + 5)

            applied = guarded_record_update(
                path,
                mutate=_bump,
                operation="probe",
                lock_name="record.json",
                mutation_id="fresh",
                expected_revision=int(record[RECORD_REVISION_KEY]),
            )

            self.assertNotIsInstance(applied, DuplicateMutationReplay)
            self.assertEqual(applied[RECORD_REVISION_KEY], int(record[RECORD_REVISION_KEY]) + 1)

    def test_the_floor_only_refuses_retries_that_carry_an_expected_revision(self) -> None:
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "record.json"
            record = self._fill(path, APPLIED_MUTATIONS_LIMIT + 5)

            # Documented boundary: without a rendered revision there is nothing
            # to compare against the floor, so the call applies normally.
            applied = guarded_record_update(
                path,
                mutate=_bump,
                operation="probe",
                lock_name="record.json",
                mutation_id="mutation-000",
            )

            self.assertNotIsInstance(applied, DuplicateMutationReplay)
            self.assertEqual(applied[RECORD_REVISION_KEY], int(record[RECORD_REVISION_KEY]) + 1)


class BoundedMutationIdTests(unittest.TestCase):
    def test_an_over_long_mutation_id_is_refused_before_any_file_is_touched(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            path = root / "record.json"

            with self.assertRaises(ValueError) as caught:
                guarded_record_update(
                    path,
                    mutate=_bump,
                    operation="probe",
                    lock_name="record.json",
                    mutation_id="x" * (MAX_MUTATION_ID_CHARS + 1),
                    default={},
                )

            self.assertIn("mutation_id", str(caught.exception))
            self.assertIn(str(MAX_MUTATION_ID_CHARS), str(caught.exception))
            # Nothing was created: not the record, not the lock sidecar, not a
            # temp file. The refusal happens before the lock is taken.
            self.assertEqual(sorted(item.name for item in root.iterdir()), [])

    def test_a_mutation_id_at_the_bound_still_applies(self) -> None:
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "record.json"

            record = guarded_record_update(
                path,
                mutate=_bump,
                operation="probe",
                lock_name="record.json",
                mutation_id="x" * MAX_MUTATION_ID_CHARS,
                default={},
            )

            self.assertEqual(record[RECORD_REVISION_KEY], 1)

    def test_the_bound_leaves_room_for_the_ids_connectors_actually_send(self) -> None:
        # The limit is only defensible if it never rejects an id a real
        # connector sends, so the widest observed shapes are pinned here.
        for label, mutation_id in (
            ("uuid4", "123e4567-e89b-12d3-a456-426614174000"),
            ("ulid", "01ARZ3NDEKTSV4RRFFQ69G5FAV"),
            ("discord-snowflake", "1234567890123456789"),
            ("git-sha", "0" * 40),
            ("slack-composite", "slack:C0123456789/p1700000000.000100"),
        ):
            with self.subTest(label=label):
                self.assertEqual(validated_mutation_id(mutation_id), mutation_id)
                self.assertLess(len(mutation_id) * 2, MAX_MUTATION_ID_CHARS)

    def test_an_over_long_operation_is_refused_the_same_way(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            path = root / "record.json"

            with self.assertRaises(ValueError) as caught:
                guarded_record_update(
                    path,
                    mutate=_bump,
                    operation="o" * (MAX_OPERATION_CHARS + 1),
                    lock_name="record.json",
                    default={},
                )

            self.assertIn("operation", str(caught.exception))
            self.assertIn(str(MAX_OPERATION_CHARS), str(caught.exception))
            self.assertEqual(sorted(item.name for item in root.iterdir()), [])

    def test_every_shipped_operation_name_fits_the_bound(self) -> None:
        # A bound any real operation trips would be a latent outage, so the
        # names actually passed to guarded_record_update are re-derived from
        # source instead of listed by hand.
        source_root = Path(__file__).resolve().parents[1] / "src"
        names: set[str] = set()
        for module in sorted(source_root.rglob("*.py")):
            names.update(re.findall(r"operation=[\"']([^\"']+)[\"']", module.read_text(encoding="utf-8")))

        self.assertTrue(names)
        for name in sorted(names):
            with self.subTest(operation=name):
                self.assertEqual(validated_operation(name), name)

    def test_an_over_long_mutation_id_is_refused_identically_on_every_surface(self) -> None:
        oversized = "x" * (MAX_MUTATION_ID_CHARS + 1)
        with TemporaryDirectory() as tmp:
            paths = resolve_paths(Path(tmp) / ".omh", Path(tmp) / ".hermes")
            create_goal_ledger(
                paths,
                "Bound the retry token",
                [{"id": "AC-bound", "summary": "Ids are bounded"}],
                goal_id="goal-bound",
            )
            cycle = create_loop_cycle(
                paths,
                goal_summary="Bound the retry token",
                goal_reframe="Reject an oversized mutation id on every guarded surface.",
                success_criteria=["Ids are bounded"],
                permission_profile="handoff_only",
            )
            loop_id = str(cycle["loop_id"])
            started = create_or_resume_wrapper_session(
                paths,
                "risky refactor with private-token-123",
                source="discord",
                source_metadata={"source_event_id": "bound-1", "channel_ref": "c1"},
            )
            session_id = str(started["session"]["session_id"])
            goal_path = goal_ledger_path(paths, "goal-bound")
            cycle_path = loop_cycle_path(paths, loop_id)
            before_goal = goal_path.read_bytes()
            before_cycle = cycle_path.read_bytes()
            before_session = read_wrapper_session(paths, session_id)

            for surface, call in (
                (
                    "goal",
                    lambda: record_goal_checkpoint(
                        paths, "goal-bound", "Bounded", status="in_progress", mutation_id=oversized
                    ),
                ),
                ("session", lambda: record_plan_decision(paths, session_id, "accept", mutation_id=oversized)),
                ("loop", lambda: tick_loop_runtime(paths, loop_id, mutation_id=oversized)),
            ):
                with self.subTest(surface=surface):
                    with self.assertRaises(ValueError) as caught:
                        call()
                    self.assertIn(f"at most {MAX_MUTATION_ID_CHARS} characters", str(caught.exception))

            # Identical rejection means identical consequences: nothing written.
            self.assertEqual(goal_path.read_bytes(), before_goal)
            self.assertEqual(cycle_path.read_bytes(), before_cycle)
            self.assertEqual(read_wrapper_session(paths, session_id), before_session)


class GuardedLockReportingTests(unittest.TestCase):
    def test_a_guarded_record_reports_the_lock_that_protected_it(self) -> None:
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "record.json"

            record = guarded_record_update(path, mutate=_bump, operation="probe", lock_name="record.json", default={})

            self.assertTrue(record.lock_enforced)
            self.assertIn(record.lock_mechanism, ("fcntl", "msvcrt"))

    def test_a_fully_degraded_lock_reports_enforced_false_without_persisting_it(self) -> None:
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "record.json"
            with (
                mock.patch("omh.system.local_store.fcntl", None),
                mock.patch("omh.system.local_store.msvcrt", None),
            ):
                record = guarded_record_update(
                    path,
                    mutate=_bump,
                    operation="probe",
                    lock_name="record.json",
                    mutation_id="turn-1",
                    default={},
                )
                replay = guarded_record_update(
                    path,
                    mutate=_bump,
                    operation="probe",
                    lock_name="record.json",
                    mutation_id="turn-1",
                )

            self.assertFalse(record.lock_enforced)
            self.assertEqual(record.lock_mechanism, "none")
            assert isinstance(replay, DuplicateMutationReplay)
            self.assertFalse(replay.lock_enforced)
            # The degraded flag is a property of the call, never of the record.
            stored = read_json_object(path)
            assert stored is not None
            self.assertEqual(sorted(stored), [APPLIED_MUTATIONS_KEY, RECORD_REVISION_KEY, "value"])

    def test_the_msvcrt_path_serializes_racing_updates_when_fcntl_is_absent(self) -> None:
        fake = FakeMsvcrt()
        worker_count = 16
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "record.json"
            guarded_record_update(path, mutate=_bump, operation="probe", lock_name="record.json", default={})
            barrier = threading.Barrier(worker_count)
            applied: list[object] = []
            stale: list[BaseException] = []
            unexpected: list[BaseException] = []
            guard = threading.Lock()

            def worker() -> None:
                barrier.wait()
                try:
                    result = guarded_record_update(
                        path,
                        mutate=_bump,
                        operation="probe",
                        lock_name="record.json",
                        expected_revision=1,
                        timeout_seconds=30.0,
                    )
                except StaleRecordMutation as exc:
                    with guard:
                        stale.append(exc)
                except BaseException as exc:  # noqa: BLE001
                    with guard:
                        unexpected.append(exc)
                else:
                    with guard:
                        applied.append(result)

            with (
                mock.patch("omh.system.local_store.fcntl", None),
                mock.patch("omh.system.local_store.msvcrt", fake),
            ):
                threads = [threading.Thread(target=worker) for _ in range(worker_count)]
                for thread in threads:
                    thread.start()
                for thread in threads:
                    thread.join()

            # Without a real Windows lock every writer read revision 1, passed
            # the compare, and clobbered the others. Under msvcrt exactly one
            # wins and the rest are told the record moved.
            self.assertEqual(unexpected, [])
            self.assertEqual(len(applied), 1)
            self.assertEqual(len(stale), worker_count - 1)
            final = read_json_object(path)
            assert final is not None
            self.assertEqual(final[RECORD_REVISION_KEY], 2)
            self.assertEqual(final["value"], 2)


class RevisionFieldValidationTests(unittest.TestCase):
    def test_absent_revision_fields_are_accepted(self) -> None:
        self.assertEqual(revision_field_errors({}, "record"), [])

    def test_bad_revision_and_applied_mutations_are_reported(self) -> None:
        errors = revision_field_errors({RECORD_REVISION_KEY: -1, APPLIED_MUTATIONS_KEY: []}, "record")

        self.assertIn("record record_revision must be a non-negative integer", errors)
        self.assertIn("record applied_mutations must be an object", errors)

    def test_applied_mutation_entry_shape_is_enforced(self) -> None:
        errors = revision_field_errors(
            {APPLIED_MUTATIONS_KEY: {"m1": {"record_revision": 0, "result_digest": 3, "extra": "x"}}},
            "record",
        )

        self.assertTrue(any("unsupported keys" in error for error in errors))
        self.assertTrue(any("record_revision must be a positive integer" in error for error in errors))
        self.assertTrue(any("result_digest must be a string" in error for error in errors))

    def test_oversized_applied_mutations_are_reported(self) -> None:
        applied = {
            f"m{index}": {"record_revision": index + 1, "result_digest": "d"}
            for index in range(APPLIED_MUTATIONS_LIMIT + 1)
        }

        errors = revision_field_errors({APPLIED_MUTATIONS_KEY: applied}, "record")

        self.assertTrue(any("at most" in error for error in errors))

    def test_a_negative_record_revision_is_the_only_reported_error(self) -> None:
        self.assertEqual(
            revision_field_errors({RECORD_REVISION_KEY: -1}, "goal_ledger"),
            ["goal_ledger record_revision must be a non-negative integer"],
        )

    def test_a_list_applied_mutations_is_the_only_reported_error(self) -> None:
        self.assertEqual(
            revision_field_errors({APPLIED_MUTATIONS_KEY: []}, "loop_cycle"),
            ["loop_cycle applied_mutations must be an object"],
        )

    def test_the_eviction_floor_must_be_a_non_negative_integer(self) -> None:
        self.assertEqual(revision_field_errors({APPLIED_MUTATIONS_FLOOR_KEY: 5}, "record"), [])
        for bad in (-1, True, "5", 1.5):
            with self.subTest(value=bad):
                self.assertEqual(
                    revision_field_errors({APPLIED_MUTATIONS_FLOOR_KEY: bad}, "record"),
                    ["record applied_mutations_floor_revision must be a non-negative integer"],
                )

    def test_entry_operation_is_optional_but_typed_when_present(self) -> None:
        legacy = {APPLIED_MUTATIONS_KEY: {"m1": {"record_revision": 1, "result_digest": "d"}}}
        scoped = {APPLIED_MUTATIONS_KEY: {"probe:m1": {"record_revision": 1, "operation": "probe", "result_digest": "d"}}}
        blank = {APPLIED_MUTATIONS_KEY: {"probe:m1": {"record_revision": 1, "operation": "  ", "result_digest": "d"}}}

        self.assertEqual(revision_field_errors(legacy, "record"), [])
        self.assertEqual(revision_field_errors(scoped, "record"), [])
        self.assertEqual(
            revision_field_errors(blank, "record"),
            ["record applied_mutations[probe:m1].operation must be a non-empty string"],
        )


class RequireNotTerminalTests(unittest.TestCase):
    def test_terminal_status_is_named_in_the_refusal(self) -> None:
        with self.assertRaises(ValueError) as caught:
            require_not_terminal({"status": "cancelled"}, "status", ("complete", "cancelled"), "prepare child work")

        self.assertIn("cancelled", str(caught.exception))
        self.assertIn("prepare child work", str(caught.exception))
        self.assertIn("terminal", str(caught.exception))

    def test_non_terminal_status_passes(self) -> None:
        require_not_terminal({"status": "active"}, "status", ("complete", "cancelled"), "prepare child work")

    def test_custom_error_type_is_raised(self) -> None:
        with self.assertRaises(WrapperSessionError):
            require_not_terminal(
                {"status": "cancelled"},
                "status",
                ("cancelled",),
                "prepare child work",
                error_type=WrapperSessionError,
            )


class GuardedUpdateConcurrencyTests(unittest.TestCase):
    @unittest.skipUnless(HAS_FCNTL, "fcntl advisory locking is POSIX-only")
    def test_concurrent_guarded_updates_do_not_lose_writes(self) -> None:
        worker_count = 24
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "record.json"
            guarded_record_update(path, mutate=lambda current: dict(current), operation="probe", lock_name="record.json", default={})
            barrier = threading.Barrier(worker_count)
            failures: list[BaseException] = []

            def worker(index: int) -> None:
                barrier.wait()
                try:
                    guarded_record_update(
                        path,
                        mutate=lambda current: {**current, f"key-{index}": index},
                        operation="probe",
                        lock_name="record.json",
                        timeout_seconds=30.0,
                    )
                except BaseException as exc:  # noqa: BLE001
                    failures.append(exc)

            threads = [threading.Thread(target=worker, args=(index,)) for index in range(worker_count)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join()

            self.assertEqual(failures, [])
            final = read_json_object(path)
            assert final is not None
            # One seeding write plus one write per worker, and every worker's
            # key survived: no read-modify-write lost an update.
            self.assertEqual(final[RECORD_REVISION_KEY], worker_count + 1)
            for index in range(worker_count):
                self.assertEqual(final.get(f"key-{index}"), index)

    @unittest.skipUnless(HAS_FCNTL, "fcntl advisory locking is POSIX-only")
    def test_concurrent_same_mutation_id_retries_apply_exactly_once(self) -> None:
        worker_count = 16
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "record.json"
            guarded_record_update(path, mutate=lambda current: {"value": 0}, operation="probe", lock_name="record.json", default={})
            barrier = threading.Barrier(worker_count)
            failures: list[BaseException] = []

            def worker() -> None:
                barrier.wait()
                try:
                    guarded_record_update(
                        path,
                        mutate=_bump,
                        operation="probe",
                        lock_name="record.json",
                        mutation_id="retried-once",
                        timeout_seconds=30.0,
                    )
                except BaseException as exc:  # noqa: BLE001
                    failures.append(exc)

            threads = [threading.Thread(target=worker) for _ in range(worker_count)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join()

            self.assertEqual(failures, [])
            final = read_json_object(path)
            assert final is not None
            self.assertEqual(final["value"], 1)
            self.assertEqual(final[RECORD_REVISION_KEY], 2)


class WrapperSessionRevisionGuardTests(unittest.TestCase):
    def test_wrapper_session_record_keys_expose_the_revision_fields(self) -> None:
        self.assertIn(RECORD_REVISION_KEY, WRAPPER_SESSION_RECORD_KEYS)
        self.assertIn(APPLIED_MUTATIONS_KEY, WRAPPER_SESSION_RECORD_KEYS)

    def test_created_session_echoes_the_persisted_revision(self) -> None:
        with TemporaryDirectory() as tmp:
            paths = resolve_paths(Path(tmp) / ".omh", Path(tmp) / ".hermes")

            started = create_or_resume_wrapper_session(paths, "risky refactor", source="discord")
            session_id = str(started["session"]["session_id"])
            stored = read_wrapper_session(paths, session_id)

            assert stored is not None
            self.assertEqual(started["session"][RECORD_REVISION_KEY], 1)
            self.assertEqual(started["status"]["record_revision"], 1)
            self.assertEqual(record_revision_of(stored), 1)

    def test_plan_decision_bumps_the_revision_and_accepts_the_echoed_value(self) -> None:
        with TemporaryDirectory() as tmp:
            paths = resolve_paths(Path(tmp) / ".omh", Path(tmp) / ".hermes")
            started = create_or_resume_wrapper_session(paths, "risky refactor", source="discord")
            session_id = str(started["session"]["session_id"])

            accepted = record_plan_decision(
                paths,
                session_id,
                "accept",
                expected_revision=started["session"][RECORD_REVISION_KEY],
            )

            self.assertEqual(accepted["session"][RECORD_REVISION_KEY], 2)
            self.assertEqual(accepted["status"]["record_revision"], 2)
            self.assertFalse(accepted["replayed"])

    def test_stale_plan_decision_is_rejected_without_a_partial_write(self) -> None:
        with TemporaryDirectory() as tmp:
            paths = resolve_paths(Path(tmp) / ".omh", Path(tmp) / ".hermes")
            started = create_or_resume_wrapper_session(paths, "risky refactor", source="discord")
            session_id = str(started["session"]["session_id"])
            rendered_revision = int(started["session"][RECORD_REVISION_KEY])
            # Another writer moves the session on while the first decision is
            # still in flight against the revision it rendered.
            record_plan_decision(paths, session_id, "revise")
            session_path = paths.runtime_wrapper_sessions_dir / session_id / "session.json"
            before = session_path.read_bytes()

            with self.assertRaises(StaleRecordMutation) as caught:
                record_plan_decision(paths, session_id, "accept", expected_revision=rendered_revision)

            self.assertEqual(session_path.read_bytes(), before)
            self.assertEqual(caught.exception.expected_revision, rendered_revision)
            self.assertEqual(caught.exception.current_revision, 2)
            stored = read_wrapper_session(paths, session_id)
            assert stored is not None
            self.assertEqual(stored["status"], "revision_requested")

    def test_replayed_plan_decision_writes_no_duplicate_event_or_revision(self) -> None:
        with TemporaryDirectory() as tmp:
            paths = resolve_paths(Path(tmp) / ".omh", Path(tmp) / ".hermes")
            started = create_or_resume_wrapper_session(paths, "risky refactor", source="discord")
            session_id = str(started["session"]["session_id"])
            events_path = paths.runtime_wrapper_sessions_dir / session_id / "events.jsonl"

            first = record_plan_decision(paths, session_id, "accept", mutation_id="decide-1")
            events_after_first = events_path.read_text(encoding="utf-8")

            second = record_plan_decision(paths, session_id, "accept", mutation_id="decide-1")

            self.assertFalse(first["replayed"])
            self.assertTrue(second["replayed"])
            self.assertEqual(second["session"][RECORD_REVISION_KEY], first["session"][RECORD_REVISION_KEY])
            self.assertEqual(events_path.read_text(encoding="utf-8"), events_after_first)

    def test_cancelled_session_refuses_executor_selection(self) -> None:
        with TemporaryDirectory() as tmp:
            paths = resolve_paths(Path(tmp) / ".omh", Path(tmp) / ".hermes")
            started = create_or_resume_wrapper_session(paths, "risky refactor", source="discord")
            session_id = str(started["session"]["session_id"])
            record_plan_decision(paths, session_id, "cancel")

            with self.assertRaises(WrapperSessionError) as caught:
                select_wrapper_session_executor(paths, session_id, "codex")

            self.assertIn("cancelled", str(caught.exception))
            self.assertIn("terminal", str(caught.exception))

    def test_cancelled_session_refuses_handoff_preparation(self) -> None:
        with TemporaryDirectory() as tmp:
            paths = resolve_paths(Path(tmp) / ".omh", Path(tmp) / ".hermes")
            started = create_or_resume_wrapper_session(paths, "risky refactor", source="discord")
            session_id = str(started["session"]["session_id"])
            record_plan_decision(paths, session_id, "cancel")

            with self.assertRaises(WrapperSessionError) as caught:
                prepare_wrapper_session_handoff(paths, session_id, "risky refactor")

            self.assertIn("cancelled", str(caught.exception))

    def test_cancelled_session_refuses_every_executor_session_entrypoint(self) -> None:
        with TemporaryDirectory() as tmp:
            paths = resolve_paths(Path(tmp) / ".omh", Path(tmp) / ".hermes")
            started = create_or_resume_wrapper_session(paths, "risky refactor", source="discord")
            session_id = str(started["session"]["session_id"])
            # A handoff_prepared session refuses plan decisions outright, so a
            # cancelled session is always cancelled before the handoff exists.
            # The terminal guard must still fire ahead of the
            # "no prepared handoff" complaint, naming the terminal state.
            record_plan_decision(paths, session_id, "cancel")

            for call in (
                lambda: open_executor_session(paths, session_id),
                lambda: attach_executor_session(paths, session_id, external_session_ref="codex-thread-1"),
                lambda: record_executor_session_result(paths, session_id, result="completed"),
            ):
                with self.assertRaises(ExecutorSessionError) as caught:
                    call()
                self.assertIn("cancelled", str(caught.exception))
                self.assertIn("terminal", str(caught.exception))

    def test_stale_handoff_preparation_starts_no_child_work(self) -> None:
        with TemporaryDirectory() as tmp:
            paths = resolve_paths(Path(tmp) / ".omh", Path(tmp) / ".hermes")
            started = create_or_resume_wrapper_session(paths, "risky refactor", source="discord")
            session_id = str(started["session"]["session_id"])
            accepted = record_plan_decision(paths, session_id, "accept")
            stale_revision = int(accepted["session"][RECORD_REVISION_KEY])
            select_wrapper_session_executor(paths, session_id, "codex")
            session_path = paths.runtime_wrapper_sessions_dir / session_id / "session.json"
            before = session_path.read_bytes()
            runs_before = sorted(paths.runtime_runs_dir.glob("*")) if paths.runtime_runs_dir.exists() else []

            with self.assertRaises(StaleRecordMutation):
                prepare_wrapper_session_handoff(
                    paths,
                    session_id,
                    "risky refactor",
                    expected_revision=stale_revision,
                )

            self.assertEqual(session_path.read_bytes(), before)
            runs_after = sorted(paths.runtime_runs_dir.glob("*")) if paths.runtime_runs_dir.exists() else []
            self.assertEqual(runs_after, runs_before)


class GoalLedgerRevisionGuardTests(unittest.TestCase):
    def _goal(self, paths) -> dict[str, object]:
        return create_goal_ledger(
            paths,
            "Finish the durable goal",
            [{"id": "AC-guard", "summary": "Guard is verified"}],
            goal_id="goal-guard",
        )

    def test_cancelled_is_a_supported_goal_status(self) -> None:
        self.assertIn("cancelled", GOAL_STATUSES)

    def test_goal_revision_starts_at_one_and_survives_validation(self) -> None:
        with TemporaryDirectory() as tmp:
            paths = resolve_paths(Path(tmp) / ".omh", Path(tmp) / ".hermes")

            goal = self._goal(paths)

            self.assertEqual(goal[RECORD_REVISION_KEY], 1)
            self.assertEqual(validate_goal_ledger(goal), {"ok": True, "errors": []})

    def test_stale_checkpoint_is_rejected_without_a_partial_write(self) -> None:
        with TemporaryDirectory() as tmp:
            paths = resolve_paths(Path(tmp) / ".omh", Path(tmp) / ".hermes")
            goal = self._goal(paths)
            stale_revision = int(goal[RECORD_REVISION_KEY])
            record_goal_checkpoint(paths, "goal-guard", "First checkpoint", status="in_progress")
            path = goal_ledger_path(paths, "goal-guard")
            before = path.read_bytes()

            with self.assertRaises(StaleRecordMutation) as caught:
                record_goal_checkpoint(
                    paths,
                    "goal-guard",
                    "Racing checkpoint",
                    status="in_progress",
                    expected_revision=stale_revision,
                )

            self.assertEqual(path.read_bytes(), before)
            self.assertEqual(caught.exception.current_revision, 2)
            self.assertEqual(len(read_goal_ledger(paths, "goal-guard")["checkpoints"]), 1)

    def test_retried_checkpoint_mutation_id_creates_exactly_one_checkpoint(self) -> None:
        with TemporaryDirectory() as tmp:
            paths = resolve_paths(Path(tmp) / ".omh", Path(tmp) / ".hermes")
            self._goal(paths)

            first = record_goal_checkpoint(
                paths, "goal-guard", "Only once", status="in_progress", mutation_id="cp-retry"
            )
            second = record_goal_checkpoint(
                paths, "goal-guard", "Only once", status="in_progress", mutation_id="cp-retry"
            )
            third = record_goal_checkpoint(
                paths, "goal-guard", "Only once", status="in_progress", mutation_id="cp-retry"
            )

            stored = read_goal_ledger(paths, "goal-guard")
            self.assertEqual(len(stored["checkpoints"]), 1)
            self.assertEqual(first[RECORD_REVISION_KEY], 2)
            self.assertEqual(second[RECORD_REVISION_KEY], 2)
            self.assertEqual(third[RECORD_REVISION_KEY], 2)

    def test_retried_blocker_and_quality_gate_mutation_ids_do_not_duplicate(self) -> None:
        with TemporaryDirectory() as tmp:
            paths = resolve_paths(Path(tmp) / ".omh", Path(tmp) / ".hermes")
            self._goal(paths)

            record_goal_blocker(paths, "goal-guard", "Needs approval", mutation_id="blocker-retry")
            record_goal_blocker(paths, "goal-guard", "Needs approval", mutation_id="blocker-retry")
            record_goal_quality_gate(paths, "goal-guard", "Suite green", mutation_id="gate-retry")
            record_goal_quality_gate(paths, "goal-guard", "Suite green", mutation_id="gate-retry")

            stored = read_goal_ledger(paths, "goal-guard")
            self.assertEqual(len(stored["blockers"]), 1)
            self.assertEqual(len(stored["quality_gates"]), 1)

    def test_cancelled_goal_refuses_further_child_work(self) -> None:
        with TemporaryDirectory() as tmp:
            paths = resolve_paths(Path(tmp) / ".omh", Path(tmp) / ".hermes")
            self._goal(paths)

            cancelled = cancel_goal_ledger(paths, "goal-guard", reason="Superseded by another goal")

            self.assertEqual(cancelled["status"], "cancelled")
            self.assertEqual(validate_goal_ledger(cancelled), {"ok": True, "errors": []})
            for call in (
                lambda: record_goal_checkpoint(paths, "goal-guard", "After cancel", status="in_progress"),
                lambda: record_goal_blocker(paths, "goal-guard", "After cancel"),
                lambda: record_goal_quality_gate(paths, "goal-guard", "After cancel"),
                lambda: cancel_goal_ledger(paths, "goal-guard"),
            ):
                with self.assertRaises(ValueError) as caught:
                    call()
                self.assertIn("cancelled", str(caught.exception))
                self.assertIn("terminal", str(caught.exception))

    def test_completed_goal_refuses_further_child_work(self) -> None:
        with TemporaryDirectory() as tmp:
            paths = resolve_paths(Path(tmp) / ".omh", Path(tmp) / ".hermes")
            self._goal(paths)
            record_goal_checkpoint(
                paths,
                "goal-guard",
                "Guard verified",
                criteria_refs=["AC-guard"],
                # "verified" is a proof word, so the completion gate now wants
                # one evidence entry naming the command that proves it.
                evidence_refs=["pytest tests/test_record_revision.py"],
            )
            completed = complete_goal_ledger(
                paths, "goal-guard", evidence_refs=["pytest tests/test_record_revision.py"]
            )
            self.assertTrue(completed["completed"])

            with self.assertRaises(ValueError) as caught:
                record_goal_checkpoint(paths, "goal-guard", "After completion", status="in_progress")

            self.assertIn("complete", str(caught.exception))
            self.assertIn("terminal", str(caught.exception))

    def test_cancelled_goal_completion_gate_reports_the_terminal_state(self) -> None:
        with TemporaryDirectory() as tmp:
            paths = resolve_paths(Path(tmp) / ".omh", Path(tmp) / ".hermes")
            self._goal(paths)
            cancel_goal_ledger(paths, "goal-guard")

            from omh.goal_ledger import build_goal_completion_gate

            gate = build_goal_completion_gate(paths, "goal-guard")

            self.assertFalse(gate["ready"])
            self.assertEqual(gate["goal_status"], "cancelled")
            self.assertIn("goal status is cancelled", gate["summary"])
            self.assertEqual(gate["next_action"], "show_status")


class LoopCycleRevisionGuardTests(unittest.TestCase):
    def _cycle(self, paths) -> dict[str, object]:
        return create_loop_cycle(
            paths,
            goal_summary="Ship the stale mutation guard",
            goal_reframe="Implement the guard, verify it, and prepare the handoff material.",
            success_criteria=["Guard is verified by tests"],
            permission_profile="handoff_only",
        )

    def test_loop_cycle_revision_starts_at_one_and_validates(self) -> None:
        with TemporaryDirectory() as tmp:
            paths = resolve_paths(Path(tmp) / ".omh", Path(tmp) / ".hermes")

            cycle = self._cycle(paths)

            self.assertEqual(cycle[RECORD_REVISION_KEY], 1)
            self.assertEqual(validate_loop_cycle(cycle), {"ok": True, "errors": []})

    def test_stale_queue_observation_is_rejected_without_a_partial_write(self) -> None:
        with TemporaryDirectory() as tmp:
            paths = resolve_paths(Path(tmp) / ".omh", Path(tmp) / ".hermes")
            cycle = self._cycle(paths)
            loop_id = str(cycle["loop_id"])
            ticked = tick_loop_runtime(paths, loop_id)
            queue_id = str(ticked["runtime"]["queue"][0]["queue_id"])
            stale_revision = int(cycle[RECORD_REVISION_KEY])
            path = loop_cycle_path(paths, loop_id)
            before = path.read_bytes()

            with self.assertRaises(StaleRecordMutation) as caught:
                observe_loop_queue_item(
                    paths,
                    loop_id,
                    queue_id,
                    evidence_refs=["wrapper:queue-observation:1"],
                    expected_revision=stale_revision,
                )

            self.assertEqual(path.read_bytes(), before)
            self.assertEqual(caught.exception.current_revision, int(ticked[RECORD_REVISION_KEY]))
            stored = read_loop_cycle(paths, loop_id)
            self.assertEqual(stored["runtime"]["queue"][0]["status"], "prepared_not_observed")

    def test_retried_queue_observation_records_exactly_one_observation(self) -> None:
        with TemporaryDirectory() as tmp:
            paths = resolve_paths(Path(tmp) / ".omh", Path(tmp) / ".hermes")
            cycle = self._cycle(paths)
            loop_id = str(cycle["loop_id"])
            ticked = tick_loop_runtime(paths, loop_id)
            queue_id = str(ticked["runtime"]["queue"][0]["queue_id"])

            first = observe_loop_queue_item(
                paths,
                loop_id,
                queue_id,
                evidence_refs=["wrapper:queue-observation:1"],
                mutation_id="observe-retry",
            )
            second = observe_loop_queue_item(
                paths,
                loop_id,
                queue_id,
                evidence_refs=["wrapper:queue-observation:1"],
                mutation_id="observe-retry",
            )

            self.assertEqual(first[RECORD_REVISION_KEY], second[RECORD_REVISION_KEY])
            item = second["runtime"]["queue"][0]
            self.assertEqual(item["status"], "observed")
            # The retry replayed instead of appending the evidence ref twice.
            self.assertEqual(item["observed_evidence_refs"], ["wrapper:queue-observation:1"])

    def test_a_queue_observation_retry_with_different_evidence_is_refused(self) -> None:
        with TemporaryDirectory() as tmp:
            paths = resolve_paths(Path(tmp) / ".omh", Path(tmp) / ".hermes")
            cycle = self._cycle(paths)
            loop_id = str(cycle["loop_id"])
            ticked = tick_loop_runtime(paths, loop_id)
            queue_id = str(ticked["runtime"]["queue"][0]["queue_id"])
            observe_loop_queue_item(
                paths,
                loop_id,
                queue_id,
                evidence_refs=["wrapper:queue-observation:1"],
                mutation_id="observe-retry",
            )
            path = loop_cycle_path(paths, loop_id)
            before = path.read_bytes()

            # Same id, different evidence: two different observations sharing
            # one id, so replaying would silently drop the second one.
            with self.assertRaises(ConflictingMutationReplay) as caught:
                observe_loop_queue_item(
                    paths,
                    loop_id,
                    queue_id,
                    evidence_refs=["wrapper:queue-observation:2"],
                    mutation_id="observe-retry",
                )

            self.assertEqual(path.read_bytes(), before)
            self.assertEqual(caught.exception.mutation_id, "observe-retry")
            self.assertIn("observe_loop_queue_item", str(caught.exception))

    def test_second_observation_without_mutation_id_still_refuses(self) -> None:
        with TemporaryDirectory() as tmp:
            paths = resolve_paths(Path(tmp) / ".omh", Path(tmp) / ".hermes")
            cycle = self._cycle(paths)
            loop_id = str(cycle["loop_id"])
            ticked = tick_loop_runtime(paths, loop_id)
            queue_id = str(ticked["runtime"]["queue"][0]["queue_id"])
            observe_loop_queue_item(paths, loop_id, queue_id, evidence_refs=["wrapper:queue-observation:1"])
            path = loop_cycle_path(paths, loop_id)
            before = path.read_bytes()

            with self.assertRaisesRegex(ValueError, "only prepared_not_observed"):
                observe_loop_queue_item(paths, loop_id, queue_id, evidence_refs=["wrapper:queue-observation:2"])

            self.assertEqual(path.read_bytes(), before)

    def test_queue_mutators_are_protected_by_status_after_the_id_is_evicted(self) -> None:
        # Loop cycles never materialize a mutation_id into a persisted id -
        # queue_id and cycle_id come from _new_item_id - so the goal ledger's
        # id dedupe does not apply here. The claim that the queue mutators are
        # protected anyway is pinned rather than assumed: with the applied
        # entry gone, the status precondition refuses the retry.
        with TemporaryDirectory() as tmp:
            paths = resolve_paths(Path(tmp) / ".omh", Path(tmp) / ".hermes")
            loop_id = str(self._cycle(paths)["loop_id"])
            ticked = tick_loop_runtime(paths, loop_id)
            queue_id = str(ticked["runtime"]["queue"][0]["queue_id"])
            observe_loop_queue_item(
                paths, loop_id, queue_id, evidence_refs=["wrapper:queue-observation:1"], mutation_id="observe-evict"
            )
            path = loop_cycle_path(paths, loop_id)
            stored = read_loop_cycle(paths, loop_id)
            self.assertNotEqual(str(stored["runtime"]["queue"][0]["queue_id"]), "observe-evict")
            # Force the eviction the bounded map would eventually cause.
            stored[APPLIED_MUTATIONS_KEY] = {}
            stored[APPLIED_MUTATIONS_FLOOR_KEY] = int(stored[RECORD_REVISION_KEY])
            path.write_text(json.dumps(stored, sort_keys=True), encoding="utf-8")
            before = path.read_bytes()

            with self.assertRaisesRegex(ValueError, "only prepared_not_observed"):
                observe_loop_queue_item(
                    paths,
                    loop_id,
                    queue_id,
                    evidence_refs=["wrapper:queue-observation:1"],
                    mutation_id="observe-evict",
                )
            with self.assertRaisesRegex(ValueError, "observed loop queue items cannot be blocked"):
                block_loop_queue_item(paths, loop_id, queue_id, reason="late", mutation_id="block-evict")

            self.assertEqual(path.read_bytes(), before)


class WorkflowStateRevisionTests(unittest.TestCase):
    def test_started_state_carries_a_record_revision(self) -> None:
        from omh.workflow_state import finish_workflow_state, start_workflow_state

        with TemporaryDirectory() as tmp:
            paths = resolve_paths(Path(tmp) / ".omh", Path(tmp) / ".hermes")

            started = start_workflow_state(paths, "plan", note="clarify")
            finished = finish_workflow_state(paths, "plan")

            self.assertEqual(started[RECORD_REVISION_KEY], 1)
            self.assertEqual(finished[RECORD_REVISION_KEY], 2)
            self.assertFalse(finished["active"])

    @unittest.skipUnless(HAS_FCNTL, "fcntl advisory locking is POSIX-only")
    def test_concurrent_starts_do_not_both_become_active(self) -> None:
        from omh.workflow_state import WorkflowStateError, active_workflow_states, start_workflow_state

        worker_count = 8
        with TemporaryDirectory() as tmp:
            paths = resolve_paths(Path(tmp) / ".omh", Path(tmp) / ".hermes")
            barrier = threading.Barrier(worker_count)
            unexpected: list[BaseException] = []

            def worker(index: int) -> None:
                workflow = "plan" if index % 2 == 0 else "ralph"
                barrier.wait()
                try:
                    start_workflow_state(paths, workflow)
                except WorkflowStateError:
                    pass
                except BaseException as exc:  # noqa: BLE001
                    unexpected.append(exc)

            threads = [threading.Thread(target=worker, args=(index,)) for index in range(worker_count)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join()

            self.assertEqual(unexpected, [])
            active, errors = active_workflow_states(paths)
            self.assertEqual(errors, [])
            # The transition check and the writes it authorizes run under one
            # lock, so the "one active workflow" invariant survives the race.
            self.assertEqual(len(active), 1)


if __name__ == "__main__":
    unittest.main()
