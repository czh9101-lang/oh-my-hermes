from __future__ import annotations

import hashlib
import importlib
import importlib.util
import json
import sqlite3
import time
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from _local_package import load_local_package

load_local_package()


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def _request(call: str = "call-1") -> dict[str, object]:
    return {
        "session_id": "session-1",
        "tool_call_id": call,
        "tool_name": "egress_probe_sensitive_tool",
        "action_class": "message_send",
        "destination_class": "chat_channel",
        "request_fingerprint": _digest(call),
        "effect_id": _digest("effect"),
        "destination_digest": _digest("destination"),
        "payload_digest": _digest("payload"),
        "payload_bytes": 37,
        "idempotency_key": _digest("idempotency"),
        "approval_ref": _digest("host-approval"),
    }


class EgressAttemptStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        module_name = "omh.plugin_bundle.omh.egress_attempt_receipts"
        self.assertIsNotNone(importlib.util.find_spec(module_name))
        self.module = importlib.import_module(module_name)
        self.temporary = TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.home = Path(self.temporary.name) / ".omh"
        self.store = self.module.AttemptStore(self.home)

    def test_attempt_is_durable_and_public_rows_are_bounded_metadata(self) -> None:
        first = self.store.open_attempt(**_request())

        reopened = self.module.AttemptStore(self.home)
        replay = reopened.open_attempt(**_request())
        self.assertEqual(first["disposition"], "created")
        self.assertEqual(replay["disposition"], "already_recorded")
        self.assertEqual(first["attempt_id"], replay["attempt_id"])
        rows = reopened.public_rows()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["row_type"], "attempt")
        self.assertNotIn("session-1", json.dumps(rows))
        self.assertNotIn("call-1", json.dumps(rows))
        self.assertLessEqual(
            len(json.dumps(rows[0], sort_keys=True, separators=(",", ":")).encode()),
            1024,
        )

    def test_changed_request_cannot_reuse_a_call_identity(self) -> None:
        self.store.open_attempt(**_request())
        changed = {**_request(), "request_fingerprint": _digest("different")}

        with self.assertRaises(self.module.AttemptStoreError):
            self.store.open_attempt(**changed)
        self.assertEqual(len(self.store.public_rows()), 1)

    def test_terminal_is_unique_and_conflicting_observations_are_refused(self) -> None:
        attempt = self.store.open_attempt(**_request())
        first = self.store.record_terminal(attempt["attempt_id"], "returned")
        duplicate = self.store.record_terminal(attempt["attempt_id"], "returned")

        self.assertFalse(first["already_recorded"])
        self.assertTrue(duplicate["already_recorded"])
        with self.assertRaises(self.module.AttemptStoreError):
            self.store.record_terminal(attempt["attempt_id"], "error")
        rows = self.store.public_rows()
        self.assertEqual([row["row_type"] for row in rows], ["attempt", "terminal"])
        for row in rows:
            self.assertLessEqual(len(json.dumps(row, separators=(",", ":")).encode()), 1024)

    def test_unknown_outcome_is_preserved_across_reopening(self) -> None:
        attempt = self.store.open_attempt(**_request())
        self.store.record_terminal(attempt["attempt_id"], "unknown")

        reopened = self.module.AttemptStore(self.home)
        replay = reopened.open_attempt(**_request())

        self.assertEqual(replay["disposition"], "already_recorded")
        self.assertEqual(replay["attempt_id"], attempt["attempt_id"])
        self.assertEqual(replay["terminal_state"], "unknown")
        self.assertEqual(len(reopened.public_rows()), 2)

    def test_each_supported_terminal_state_remains_distinct(self) -> None:
        for state in ("blocked", "cancelled", "error", "returned", "unknown"):
            with self.subTest(state=state):
                attempt = self.store.open_attempt(**_request(state))
                observed = self.store.record_terminal(attempt["attempt_id"], state)
                self.assertEqual(observed["terminal_state"], state)
        with self.assertRaises(self.module.AttemptStoreError):
            self.store.record_terminal(attempt["attempt_id"], "delivered")

    def test_writer_lock_fails_within_the_bound_without_appending(self) -> None:
        self.store.open_attempt(**_request("seed"))
        with sqlite3.connect(self.store.database_path) as holder:
            holder.execute("BEGIN IMMEDIATE")
            started = time.monotonic()
            with self.assertRaises(self.module.AttemptStoreError):
                self.store.open_attempt(**_request("blocked"))
            elapsed = time.monotonic() - started
            holder.rollback()

        self.assertLess(elapsed, 0.1)
        self.assertEqual(len(self.store.public_rows()), 1)

    def test_corruption_is_not_replaced_with_an_empty_store(self) -> None:
        self.store.database_path.parent.mkdir(parents=True)
        self.store.database_path.write_bytes(b"not a database")

        with self.assertRaises(self.module.AttemptStoreError):
            self.store.open_attempt(**_request())
        self.assertEqual(self.store.database_path.read_bytes(), b"not a database")

    def test_unredacted_values_are_rejected_before_storage(self) -> None:
        for key, value in (
            ("destination_digest", "https://private.example.test/path"),
            ("payload_digest", "private message body"),
            ("effect_id", "/Users/operator/secret"),
            ("idempotency_key", "sk-live-secret"),
            ("approval_ref", "approval-from-model"),
        ):
            with self.subTest(key=key):
                with self.assertRaises(self.module.AttemptStoreError):
                    self.store.open_attempt(**{**_request(), key: value})
        self.assertFalse(self.home.exists())

    def test_storage_oserror_cannot_be_reported_as_a_durable_attempt(self) -> None:
        with patch.object(Path, "mkdir", side_effect=OSError("injected storage failure")):
            with self.assertRaises(self.module.AttemptStoreError):
                self.store.open_attempt(**_request())
        self.assertFalse(self.home.exists())

    def test_optional_refs_are_null_not_hashes_of_missing_labels(self) -> None:
        request = {**_request(), "approval_ref": None, "idempotency_key": None}
        self.store.open_attempt(**request)
        row = self.store.public_rows()[0]
        self.assertIsNone(row["approval_ref"])
        self.assertIsNone(row["idempotency_key"])


if __name__ == "__main__":
    unittest.main()
