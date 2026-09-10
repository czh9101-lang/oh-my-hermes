"""Contracts for the append-only store mechanics `system/append_only_store.py` owns.

These are the properties that used to live twice, once in
`workflows/external_effect_receipts.py` and once in
`workflows/approval_receipts.py`, and that a third record family would have
copied a third time. They are asserted here against the shared module directly,
with no receipt and no approval in sight, because the mechanics are what is
shared -- neither family's vocabulary, key set, or claim boundary is.

Each consumer test file still covers the same mechanic through its own public
API. That is not redundant: those tests prove the family wired the mechanic up,
these prove the mechanic itself, and the cases below are the ones no single
consumer fully exercises -- a torn tail that a *later* record still survives, a
chain broken in all four ways in one store, and a sidecar that lands when the
store beside it cannot be written at all.
"""

from __future__ import annotations

import json
import threading
import unittest
from json import JSONDecodeError
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from _credential_fixtures import AWS_ACCESS_KEY_ID
from _local_package import load_local_package

load_local_package()
from omh.system.append_only_store import (
    RAW_OR_HIDDEN_KEYS,
    URL_SHAPED,
    append_sidecar_line,
    append_store_line,
    closed_vocabulary_value,
    digest_ref,
    is_unsafe_metadata_line,
    is_url_shaped,
    latest_record_in,
    mint_record_id,
    opaque_ref,
    record_fingerprint,
    redacted_ref,
    reference_errors,
    store_errors,
    supersede_chain_errors,
)
from omh.system import local_store
from omh.system.local_store import read_jsonl_objects

LABEL = "test_record"


def _record(record_id: str, *, run_id: str = "run-1", supersedes: str = "") -> dict[str, str]:
    return {"receipt_id": record_id, "run_id": run_id, "supersedes_receipt_ref": supersedes}


def _no_errors(record: dict) -> list[str]:
    return []


def _writers_that_landed(path: Path) -> tuple[list[int], int]:
    """The writer indices on disk, and how many lines did not parse.

    Splitting the two is what tells an interleave from a dropped append: only
    an interleave leaves a spliced line behind.
    """
    landed: list[int] = []
    unparseable = 0
    if not path.exists():
        # Every append was dropped before it created the store: no lines, and
        # nothing raised. Reporting that as zero landed keeps the assertion
        # message the diagnosis instead of a FileNotFoundError from the probe.
        return landed, unparseable
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            landed.append(json.loads(line)["writer"])
        except (JSONDecodeError, KeyError, TypeError):
            unparseable += 1
    return landed, unparseable


class TornTailTests(unittest.TestCase):
    """A short write must cost exactly one line, never the next record."""

    def test_an_append_onto_an_unterminated_tail_starts_its_own_line(self) -> None:
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "store.jsonl"
            append_store_line(path, {"id": "a"})
            # A short write: the process died mid-line, so there is no newline.
            with path.open("r+b") as handle:
                handle.seek(0, 2)
                handle.write(b'{"id": "b", "half')

            append_store_line(path, {"id": "c"})

            raw = path.read_bytes()
            self.assertTrue(raw.endswith(b"\n"))
            lines = raw.decode("utf-8").splitlines()
            self.assertEqual(len(lines), 3)
            self.assertEqual(json.loads(lines[0]), {"id": "a"})
            self.assertEqual(lines[1], '{"id": "b", "half')
            # The record after the torn one is intact and parses on its own.
            self.assertEqual(json.loads(lines[2]), {"id": "c"})

    def test_an_unterminated_tail_is_one_store_error_and_later_records_still_parse(self) -> None:
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "store.jsonl"
            append_store_line(path, _record("r-1"))
            with path.open("r+b") as handle:
                handle.seek(0, 2)
                handle.write(b'{"receipt_id": "r-torn')
            append_store_line(path, _record("r-2"))
            append_store_line(path, _record("r-3"))

            records, read_errors = read_jsonl_objects(path)
            # The torn line is the only casualty, and the store says so once.
            self.assertEqual(len(read_errors), 1)
            self.assertEqual([record["receipt_id"] for record in records], ["r-1", "r-2", "r-3"])

            count, errors = store_errors(
                path, records, read_errors, run_id=None, validate_record=_no_errors, label=LABEL
            )
            self.assertEqual(count, 3)
            self.assertEqual(errors, read_errors)

    def test_a_torn_tail_is_the_stores_fault_and_never_a_scoped_runs(self) -> None:
        """A line that does not parse has no `run_id`, so it faults no run."""
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "store.jsonl"
            append_store_line(path, _record("r-1", run_id="run-1"))
            with path.open("r+b") as handle:
                handle.seek(0, 2)
                handle.write(b'{"receipt_id": "r-torn')
            append_store_line(path, _record("r-2", run_id="run-2"))

            records, read_errors = read_jsonl_objects(path)
            count, errors = store_errors(
                path, records, read_errors, run_id="run-1", validate_record=_no_errors, label=LABEL
            )
            self.assertEqual(count, 1)
            self.assertEqual(errors, [])

    def test_every_line_is_written_binary_with_sorted_keys_and_no_carriage_return(self) -> None:
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "store.jsonl"
            append_store_line(path, {"z": 1, "a": 2, "m": 3})

            raw = path.read_bytes()
            self.assertNotIn(b"\r", raw)
            self.assertEqual(raw, b'{"a": 2, "m": 3, "z": 1}\n')


class ConcurrentAppendTests(unittest.TestCase):
    def test_barrier_synced_appends_do_not_interleave(self) -> None:
        """Every writer computes the same end offset unless the lock is real.

        Unlocked, two appends that start together land on top of each other and
        the store loses exactly the lines it exists to keep.
        """
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "store.jsonl"
            writers = 16
            barrier = threading.Barrier(writers)
            raised: list[Exception] = []

            def append(index: int) -> None:
                barrier.wait()
                try:
                    append_sidecar_line(path, {"writer": index, "payload": "x" * 64})
                except Exception as exc:  # reported on the main thread, never silently dropped
                    raised.append(exc)

            threads = [threading.Thread(target=append, args=(index,)) for index in range(writers)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join()

            self.assertEqual(raised, [])
            landed, unparseable = _writers_that_landed(path)
            # A short count has two causes the count alone cannot tell apart,
            # and a bare `13 != 16` left a Windows failure with three live
            # theories. An interleave splices records and leaves unparseable
            # lines behind; a swallowed OSError inside `append_sidecar_line`
            # means the append never happened and every surviving line is
            # intact. Report both, so the next failure says which it was.
            detail = f"missing={sorted(set(range(writers)) - set(landed))} unparseable={unparseable}"
            self.assertEqual(len(landed) + unparseable, writers, detail)
            self.assertEqual(sorted(landed), sorted(range(writers)), detail)

    def test_a_denied_chmod_on_the_lock_sidecar_never_costs_a_line(self) -> None:
        """Windows denies the sidecar's chmod; the record must still land.

        `file_lock` prepares its lock sidecar through `ensure_file` before it
        takes the lock, so barrier-synchronized writers all run that chmod at
        once against a file the others already hold open. Windows refuses it
        for that window and POSIX never does. Unretried, the denial escapes
        `file_lock` into `append_sidecar_line`, whose documented best-effort
        swallow drops it without a word: the line is gone, nothing is raised,
        and the count comes back short with no way to tell that apart from a
        lock that failed to hold.
        """
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "store.jsonl"
            writers = 16
            barrier = threading.Barrier(writers)
            raised: list[Exception] = []
            denials: dict[int, int] = {}
            guard = threading.Lock()
            real_chmod = Path.chmod

            def denied_while_shared(target: Path, mode: int, *args: object, **kwargs: object) -> None:
                if target.name.endswith(".lock"):
                    with guard:
                        thread = threading.get_ident()
                        denials[thread] = denials.get(thread, 0) + 1
                        seen = denials[thread]
                    if seen <= 2:
                        raise PermissionError(32, "used by another process")
                real_chmod(target, mode, *args, **kwargs)

            def append(index: int) -> None:
                barrier.wait()
                try:
                    append_sidecar_line(path, {"writer": index, "payload": "x" * 64})
                except Exception as exc:  # reported on the main thread, never silently dropped
                    raised.append(exc)

            threads = [threading.Thread(target=append, args=(index,)) for index in range(writers)]
            with (
                patch.object(local_store.os, "name", "nt"),
                patch.object(Path, "chmod", denied_while_shared),
            ):
                for thread in threads:
                    thread.start()
                for thread in threads:
                    thread.join()

            self.assertEqual(raised, [])
            landed, unparseable = _writers_that_landed(path)
            self.assertEqual(unparseable, 0)
            self.assertEqual(sorted(landed), sorted(range(writers)))


class SidecarTests(unittest.TestCase):
    def test_the_sidecar_lands_when_the_store_beside_it_cannot_be_written(self) -> None:
        """The failure log is the record that has to survive the store's outage."""
        with TemporaryDirectory() as tmp:
            store_path = Path(tmp) / "store.jsonl"
            # An unwritable store: the path is a directory, so the append fails.
            store_path.mkdir()
            with self.assertRaises(OSError):
                append_store_line(store_path, {"id": "a"})

            sidecar_path = store_path.with_name("store_failures.jsonl")
            append_sidecar_line(sidecar_path, {"outcome": "not_written", "id": "a"})

            failures, read_errors = read_jsonl_objects(sidecar_path)
            self.assertEqual(read_errors, [])
            self.assertEqual(failures, [{"outcome": "not_written", "id": "a"}])

    def test_an_unwritable_sidecar_is_swallowed_rather_than_raised(self) -> None:
        """There is nothing further to try, and the caller already holds the result."""
        with TemporaryDirectory() as tmp:
            sidecar_path = Path(tmp) / "failures.jsonl"
            sidecar_path.mkdir()

            self.assertIsNone(append_sidecar_line(sidecar_path, {"outcome": "refused"}))


class SupersedeChainTests(unittest.TestCase):
    """A chain is a line, not a graph."""

    def test_a_self_cycle_is_rejected(self) -> None:
        path = Path("store.jsonl")
        errors = supersede_chain_errors(
            path, [_record("r-1", supersedes="r-1")], run_id=None, label=LABEL
        )
        self.assertEqual(errors, [f"{path}:1: {LABEL} supersedes_receipt_ref must not name itself: r-1"])

    def test_a_dangling_link_is_rejected(self) -> None:
        path = Path("store.jsonl")
        errors = supersede_chain_errors(
            path, [_record("r-1"), _record("r-2", supersedes="r-missing")], run_id=None, label=LABEL
        )
        self.assertEqual(
            errors,
            [f"{path}:2: {LABEL} supersedes_receipt_ref does not name an earlier receipt: r-missing"],
        )

    def test_a_forward_link_is_dangling_because_order_is_arrival_order(self) -> None:
        path = Path("store.jsonl")
        errors = supersede_chain_errors(
            path, [_record("r-1", supersedes="r-2"), _record("r-2")], run_id=None, label=LABEL
        )
        self.assertEqual(
            errors,
            [f"{path}:1: {LABEL} supersedes_receipt_ref does not name an earlier receipt: r-2"],
        )

    def test_a_duplicate_record_id_is_rejected(self) -> None:
        path = Path("store.jsonl")
        errors = supersede_chain_errors(path, [_record("r-1"), _record("r-1")], run_id=None, label=LABEL)
        self.assertEqual(errors, [f"{path}:2: {LABEL} receipt_id is not unique: r-1"])

    def test_a_fork_is_rejected(self) -> None:
        path = Path("store.jsonl")
        records = [_record("r-1"), _record("r-2", supersedes="r-1"), _record("r-3", supersedes="r-1")]
        errors = supersede_chain_errors(path, records, run_id=None, label=LABEL)
        self.assertEqual(
            errors,
            [
                f"{path}:3: {LABEL} supersedes_receipt_ref forks the chain: "
                "r-1 is already superseded by r-2"
            ],
        )

    def test_a_line_chain_of_any_length_is_accepted(self) -> None:
        records = [_record("r-1")]
        records += [_record(f"r-{index}", supersedes=f"r-{index - 1}") for index in range(2, 8)]
        self.assertEqual(supersede_chain_errors(Path("s.jsonl"), records, run_id=None, label=LABEL), [])

    def test_scoping_a_report_never_invents_a_broken_chain(self) -> None:
        """A link into a record outside the scope still names a record that exists."""
        records = [
            _record("r-1", run_id="run-1"),
            _record("r-2", run_id="run-2", supersedes="r-1"),
        ]
        self.assertEqual(
            supersede_chain_errors(Path("s.jsonl"), records, run_id="run-2", label=LABEL), []
        )

    def test_an_out_of_scope_fault_is_not_reported_against_the_scoped_run(self) -> None:
        records = [_record("r-1", run_id="run-2", supersedes="r-1"), _record("r-9", run_id="run-1")]
        self.assertEqual(
            supersede_chain_errors(Path("s.jsonl"), records, run_id="run-1", label=LABEL), []
        )

    def test_the_label_is_the_callers_and_nothing_else(self) -> None:
        """The base names no record family; every error line is the caller's own."""
        errors = supersede_chain_errors(
            Path("s.jsonl"), [_record("r-1", supersedes="r-1")], run_id=None, label="approval_receipt"
        )
        self.assertIn("approval_receipt supersedes_receipt_ref must not name itself", errors[0])
        self.assertNotIn("external_effect_receipt", errors[0])


class StoreErrorTests(unittest.TestCase):
    def test_record_faults_are_prefixed_with_path_and_arrival_index(self) -> None:
        path = Path("store.jsonl")
        records = [_record("r-1"), _record("r-2")]
        count, errors = store_errors(
            path,
            records,
            [],
            run_id=None,
            validate_record=lambda record: [f"{LABEL} is bad: {record['receipt_id']}"],
            label=LABEL,
        )
        self.assertEqual(count, 2)
        self.assertEqual(
            errors,
            [f"{path}:1: {LABEL} is bad: r-1", f"{path}:2: {LABEL} is bad: r-2"],
        )

    def test_the_index_is_the_stores_own_and_not_the_scoped_position(self) -> None:
        """Scoping narrows what is reported, never how a line is addressed."""
        path = Path("store.jsonl")
        records = [_record("r-1", run_id="run-1"), _record("r-2", run_id="run-2")]
        count, errors = store_errors(
            path,
            records,
            [],
            run_id="run-2",
            validate_record=lambda record: ["bad"],
            label=LABEL,
        )
        self.assertEqual(count, 1)
        self.assertEqual(errors, [f"{path}:2: bad"])


class ReferenceGuardTests(unittest.TestCase):
    class _Refused(ValueError):
        pass

    def test_opaque_ref_raises_the_callers_own_error(self) -> None:
        with self.assertRaises(self._Refused) as caught:
            opaque_ref("", field="thing id", error=self._Refused)
        self.assertEqual(str(caught.exception), "thing id is required")

        with self.assertRaises(self._Refused) as caught:
            opaque_ref("https://example.com/x", field="thing id", error=self._Refused)
        self.assertEqual(str(caught.exception), "thing id must be an opaque identifier, not a URL")

        with self.assertRaises(self._Refused):
            opaque_ref("bad\x1bref", field="thing id", error=self._Refused)

        self.assertEqual(opaque_ref("  slack:message-1  ", field="thing id", error=self._Refused),
                         "slack:message-1")

    def test_every_url_marker_is_refused_by_both_the_raiser_and_the_collector(self) -> None:
        for marker in URL_SHAPED:
            with self.subTest(marker=marker):
                value = f"ref{marker}tail"
                self.assertTrue(is_url_shaped(value))
                with self.assertRaises(self._Refused):
                    opaque_ref(value, field="thing id", error=self._Refused)
                self.assertEqual(
                    reference_errors(value, field="id", label=LABEL, required=True),
                    [f"{LABEL} id must be an opaque identifier, not a URL"],
                )

    def test_reference_errors_collects_rather_than_raises(self) -> None:
        self.assertEqual(
            reference_errors(None, field="id", label=LABEL, required=True),
            [f"{LABEL} id must be a string"],
        )
        self.assertEqual(
            reference_errors("", field="id", label=LABEL, required=True),
            [f"{LABEL} id is required"],
        )
        self.assertEqual(reference_errors("", field="id", label=LABEL, required=False), [])
        self.assertEqual(reference_errors("ref-1", field="id", label=LABEL, required=True), [])

    def test_a_digest_ref_is_stable_bounded_and_non_navigable(self) -> None:
        folded = digest_ref("https://example.com/very/long/path?token=secret")
        self.assertEqual(folded, digest_ref("https://example.com/very/long/path?token=secret"))
        self.assertTrue(folded.startswith("ref-"))
        self.assertEqual(len(folded), len("ref-") + 12)
        self.assertFalse(is_url_shaped(folded))

    def test_redacted_ref_keeps_opaque_handles_and_folds_everything_else(self) -> None:
        self.assertEqual(redacted_ref("slack:message-1", field="ref"), "slack:message-1")
        self.assertEqual(redacted_ref("", field="ref"), "")
        for unsafe in ("https://example.com/x", AWS_ACCESS_KEY_ID, "bad\x1bref", "a" * 200):
            with self.subTest(unsafe=unsafe):
                folded = redacted_ref(unsafe, field="ref")
                self.assertTrue(folded.startswith("ref-"))
                self.assertFalse(is_url_shaped(folded))


class BoundedTextTests(unittest.TestCase):
    def test_links_paths_secrets_and_control_characters_are_all_unsafe(self) -> None:
        for unsafe in (
            "https://example.com/x",
            "see /etc/passwd",
            "what? no",
            "tag#1",
            "line\nbreak",
            "erase\x1b[2K\r",
            "C:\\Users\\me",
            "\\\\host\\share",
            AWS_ACCESS_KEY_ID,
        ):
            with self.subTest(unsafe=unsafe):
                self.assertTrue(is_unsafe_metadata_line(unsafe))

    def test_one_bounded_metadata_line_is_safe(self) -> None:
        for safe in ("", "ci run observed", "merge rejected by policy", "review submitted"):
            with self.subTest(safe=safe):
                self.assertFalse(is_unsafe_metadata_line(safe))


class SelectionAndIdentityTests(unittest.TestCase):
    def test_the_last_record_carrying_an_identity_wins(self) -> None:
        records = [
            {"effect_id": "ci:run-1", "receipt_id": "r-1"},
            {"effect_id": "merge:run-1", "receipt_id": "r-2"},
            {"effect_id": "ci:run-1", "receipt_id": "r-3"},
        ]
        self.assertEqual(latest_record_in(records, key="effect_id", value="ci:run-1")["receipt_id"], "r-3")
        self.assertEqual(latest_record_in(records, key="effect_id", value="none:0"), {})

    def test_the_match_is_returned_as_found_and_not_copied(self) -> None:
        """A caller that needs a copy makes one; a caller that does not must not pay."""
        record = {"effect_id": "ci:run-1", "receipt_id": "r-1"}
        self.assertIs(latest_record_in([record], key="effect_id", value="ci:run-1"), record)

    def test_a_fingerprint_ignores_every_key_outside_its_identity_tuple(self) -> None:
        keys = ("action", "effect_id")
        first = {"action": "ci_run", "effect_id": "ci:run-1", "observed_at": "2026-01-01T00:00:00Z"}
        second = {"action": "ci_run", "effect_id": "ci:run-1", "observed_at": "2026-06-06T06:06:06Z"}
        self.assertEqual(record_fingerprint(first, keys), record_fingerprint(second, keys))
        self.assertNotEqual(
            record_fingerprint(first, keys),
            record_fingerprint({**first, "action": "merge"}, keys),
        )

    def test_a_fingerprint_survives_a_value_json_cannot_serialize(self) -> None:
        """A hand-edited store is read before it is judged, so nothing may raise here."""
        keys = ("action",)
        self.assertEqual(len(record_fingerprint({"action": object()}, keys)), 64)

    def test_two_ids_minted_from_one_identity_in_one_second_still_differ(self) -> None:
        identity = {"decided_at": "2026-08-06T12:00:00Z", "decision": "granted"}
        first = mint_record_id(prefix="record", identity=identity)
        second = mint_record_id(prefix="record", identity=identity)
        self.assertNotEqual(first, second)
        # Same identity digest, different random tail: the prefix and digest are
        # what a chain is read by, the tail is what stops a collision.
        self.assertEqual(first.rsplit("-", 1)[0], second.rsplit("-", 1)[0])
        self.assertTrue(first.startswith("record-"))
        self.assertFalse(is_url_shaped(first))

    def test_the_identity_digest_is_order_independent(self) -> None:
        self.assertEqual(
            mint_record_id(prefix="r", identity={"a": "1", "b": "2"}).rsplit("-", 1)[0],
            mint_record_id(prefix="r", identity={"b": "2", "a": "1"}).rsplit("-", 1)[0],
        )


class ClosedVocabularyTests(unittest.TestCase):
    def test_a_value_outside_the_vocabulary_renders_empty(self) -> None:
        allowed = ("granted", "denied", "revoked")
        self.assertEqual(closed_vocabulary_value("granted", allowed), "granted")
        self.assertEqual(closed_vocabulary_value("approved", allowed), "")
        self.assertEqual(closed_vocabulary_value(None, allowed), "")
        self.assertEqual(closed_vocabulary_value("\x1b[2Kgranted", allowed), "")


class RawOrHiddenKeyTests(unittest.TestCase):
    def test_the_guard_names_every_shape_raw_output_arrives_under(self) -> None:
        for key in ("prompt", "raw_output", "chain_of_thought", "transcript", "stdout", "url"):
            with self.subTest(key=key):
                self.assertIn(key, RAW_OR_HIDDEN_KEYS)

    def test_the_guard_is_lowercase_so_callers_can_fold_before_matching(self) -> None:
        self.assertEqual(RAW_OR_HIDDEN_KEYS, frozenset(key.lower() for key in RAW_OR_HIDDEN_KEYS))

    def test_both_record_families_share_one_set_rather_than_two(self) -> None:
        """Two copies is how one gains a key and the other silently does not."""
        from omh.workflows import approval_receipts, external_effect_receipts

        self.assertIs(external_effect_receipts.RAW_OR_HIDDEN_KEYS, RAW_OR_HIDDEN_KEYS)
        self.assertIs(approval_receipts.RAW_OR_HIDDEN_KEYS, RAW_OR_HIDDEN_KEYS)


if __name__ == "__main__":
    unittest.main()
