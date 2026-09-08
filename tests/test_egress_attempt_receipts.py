from __future__ import annotations

import hashlib
import importlib
import importlib.util
import json
import sqlite3
import sys
import time
import unittest
from contextlib import closing
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import Mock, patch

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
        with closing(sqlite3.connect(self.store.database_path)) as holder:
            holder.execute("BEGIN IMMEDIATE")
            started = time.monotonic()
            with self.assertRaises(self.module.AttemptStoreError):
                self.store.open_attempt(**_request("blocked"))
            elapsed = time.monotonic() - started
            holder.rollback()

        with self.assertRaises(sqlite3.ProgrammingError):
            holder.execute("SELECT 1")
        self.assertLess(elapsed, 0.1)
        self.assertEqual(len(self.store.public_rows()), 1)

    def test_cold_schema_is_atomic_and_repeated_open_does_not_write(self) -> None:
        connect = sqlite3.connect
        visible_objects: list[int] = []
        database_path = self.store.database_path

        class ObservedSchema(sqlite3.Connection):
            def execute(self, sql, *args, **kwargs):
                cursor = super().execute(sql, *args, **kwargs)
                if sql.lstrip().startswith("CREATE"):
                    with closing(connect(database_path)) as observer:
                        visible_objects.append(observer.execute(
                            "SELECT count(*) FROM sqlite_master WHERE name NOT LIKE 'sqlite_%'"
                        ).fetchone()[0])
                return cursor

        with patch.object(sqlite3, "connect", side_effect=lambda *a, **kw: connect(
            *a, **kw, factory=ObservedSchema,
        )):
            connection, created = self.store._connect()
            with closing(connection):
                self.assertTrue(created)
                self.assertFalse(connection.in_transaction)
        self.assertEqual(visible_objects, [0, 0, 0, 0])

        with closing(connect(database_path)) as observer:
            self.assertEqual(set(observer.execute(
                "SELECT type, name FROM sqlite_master WHERE name NOT LIKE 'sqlite_%'"
            )), {
                ("table", "egress_attempts"), ("table", "egress_attempt_terminals"),
                ("index", "egress_attempts_identity_lookup"),
                ("index", "egress_attempts_recent_lookup"),
            })
            self.assertEqual(observer.execute("PRAGMA integrity_check").fetchall(), [("ok",)])
            version = observer.execute("PRAGMA data_version").fetchone()
            before = database_path.read_bytes()
            # Existing-schema reads must not acquire a writer lock or commit changes.
            observer.execute("BEGIN IMMEDIATE")
            connection, created = self.module.AttemptStore(self.home)._connect()
            with closing(connection):
                self.assertFalse(created)
                self.assertFalse(connection.in_transaction)
                self.assertEqual(connection.execute("PRAGMA synchronous").fetchone(), (2,))
                self.assertEqual(connection.execute("PRAGMA journal_mode").fetchone(), ("delete",))
                self.assertEqual(connection.execute("PRAGMA foreign_keys").fetchone(), (1,))
            self.assertEqual(self.store.public_rows(), [])
            observer.rollback()
            self.assertEqual(observer.execute("PRAGMA data_version").fetchone(), version)
            self.assertEqual(database_path.read_bytes(), before)

    def test_cold_schema_failure_rolls_back_and_blocks_registered_handler(self) -> None:
        connect = sqlite3.connect
        for failure in ("ddl", "commit"):
            with self.subTest(failure=failure):
                self.home = Path(self.temporary.name) / failure
                self.store = self.module.AttemptStore(self.home)
                denied: list[str] = []

                def failing_connect(*args, **kwargs):
                    connection = connect(*args, **kwargs)

                    def authorize(action, first, second, database, trigger):
                        if (
                            failure == "ddl" and action == sqlite3.SQLITE_CREATE_TABLE
                            and first == "egress_attempt_terminals"
                        ) or (
                            failure == "commit" and action == sqlite3.SQLITE_TRANSACTION
                            and first == "COMMIT" and connection.total_changes == 0
                        ):
                            denied.append(failure)
                            return sqlite3.SQLITE_DENY
                        return sqlite3.SQLITE_OK

                    connection.set_authorizer(authorize)
                    return connection

                handler = Mock(return_value="local-spy-returned")
                wrapper, args, _, _ = self._registered_handler(handler)
                with patch.object(sqlite3, "connect", side_effect=failing_connect):
                    result = wrapper(args, session_id="session-1")
                handler.assert_not_called()
                self.assertIn("error", json.loads(result))
                self.assertEqual(denied, [failure])
                # Inspect directly: public_rows would repair a partially committed schema.
                with closing(connect(self.store.database_path)) as observer:
                    self.assertEqual(observer.execute("SELECT name FROM sqlite_master").fetchall(), [])
                    self.assertEqual(observer.execute("PRAGMA integrity_check").fetchall(), [("ok",)])
                # After removing the fault, the same cold store can initialize normally.
                wrapper, args, _, _ = self._registered_handler(handler)
                self.assertEqual(wrapper(args, session_id="session-1"), "local-spy-returned")
                wrapper, args, _, _ = self._registered_handler(handler)
                self.assertIn("error", json.loads(wrapper(args, session_id="session-1")))
                handler.assert_called_once()
                self.assertEqual([row["row_type"] for row in self.store.public_rows()], ["attempt"])

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

    def _registered_handler(self, handler):
        guard_module = importlib.import_module("omh.plugin_bundle.omh.egress_attempts")

        entry = SimpleNamespace(
            handler=handler, max_result_size_chars=None, dynamic_schema_overrides=None,
            is_async=False, toolset="test", schema={}, check_fn=None,
            requires_env=[], description="", emoji="",
        )
        entries = {"send_probe": entry}
        hooks = {}

        def register_tool(name, toolset, schema, wrapper, **kwargs):
            entries[name] = SimpleNamespace(handler=wrapper)

        ctx = SimpleNamespace(register_hook=hooks.__setitem__, register_tool=register_tool)
        registry = SimpleNamespace(get_entry=lambda name, **kwargs: entries.get(name))
        with patch.dict(sys.modules, {"tools.registry": SimpleNamespace(registry=registry)}):
            guard_module.register(ctx, {
                "omh_home": str(self.home),
                "tools": {"send_probe": {
                    "action_class": "message_send", "destination_class": "chat_channel",
                    "destination_arg": "channel", "payload_arg": "body",
                }},
            })
        identity = {"tool_name": "send_probe", "session_id": "session-1", "tool_call_id": "call-1"}
        directive = hooks["pre_tool_call"](**identity)
        self.assertEqual(directive["action"], "modify")
        args = {"channel": "private-room", "body": "payload", **directive["args"]}
        return entries["send_probe"].handler, args, hooks, identity

    def test_windows_full_commit_precedes_handler_without_directory_open(self) -> None:
        events: list[str] = []
        test = self

        class ObservedConnection(sqlite3.Connection):
            def commit(self) -> None:
                test.assertEqual(self.execute("PRAGMA synchronous").fetchone(), (2,))
                test.assertEqual(self.execute("PRAGMA journal_mode").fetchone(), ("delete",))
                super().commit()
                if self.total_changes:
                    events.append("committed")

        def handler(args, **kwargs):
            rows = self.module.AttemptStore(self.home).public_rows()
            self.assertEqual([row["row_type"] for row in rows], ["attempt"])
            self.assertEqual(args, {"channel": "private-room", "body": "payload"})
            events.append("handler")
            return "local-spy-returned"

        wrapper, args, hooks, identity = self._registered_handler(handler)
        connect = sqlite3.connect
        with (
            patch.object(self.module, "os", wraps=self.module.os) as platform_os,
            patch.object(sqlite3, "connect", side_effect=lambda *a, **kw: connect(
                *a, **kw, factory=ObservedConnection,
            )),
        ):
            # Only OMH's platform seam is simulated; SQLite and pathlib stay real.
            platform_os.name = "nt"
            platform_os.open.side_effect = PermissionError("Windows CRT rejects directories")
            self.assertEqual(wrapper(args, session_id="session-1"), "local-spy-returned")
            self.assertEqual(events, ["committed", "handler"])
            hooks["post_tool_call"](**identity, status="ok")
            hooks["post_tool_call"](**identity, status="ok")
            self.assertEqual(events, ["committed", "handler", "committed"])
            replay = hooks["pre_tool_call"](**identity)
            replay_args = {"channel": "private-room", "body": "payload", **replay["args"]}
            self.assertIn("error", json.loads(wrapper(replay_args, session_id="session-1")))
            self.assertEqual(events, ["committed", "handler", "committed"])
            platform_os.open.assert_not_called()
            platform_os.fsync.assert_not_called()
        rows = self.module.AttemptStore(self.home).public_rows()
        self.assertEqual([row["row_type"] for row in rows], ["attempt", "terminal"])
        self.assertEqual(rows[1]["terminal_state"], "returned")

    def test_windows_commit_failure_blocks_handler_and_does_not_mint_attempt(self) -> None:
        commits: list[str] = []
        test = self

        class FailedCommit(sqlite3.Connection):
            def commit(self) -> None:
                test.assertEqual(self.execute("PRAGMA synchronous").fetchone(), (2,))
                test.assertEqual(self.execute("PRAGMA journal_mode").fetchone(), ("delete",))
                if self.total_changes:
                    test.assertTrue(self.in_transaction)
                    test.assertEqual(self.execute("SELECT count(*) FROM egress_attempts").fetchone(), (1,))
                    commits.append("attempt")
                    raise sqlite3.OperationalError("injected SQLITE_IOERR_FSYNC")
                super().commit()
                commits.append("schema")

        handler = Mock()
        wrapper, args, _, _ = self._registered_handler(handler)
        connect = sqlite3.connect
        with (
            patch.object(self.module, "os", wraps=self.module.os) as platform_os,
            patch.object(sqlite3, "connect", side_effect=lambda *a, **kw: connect(
                *a, **kw, factory=FailedCommit,
            )),
        ):
            platform_os.name = "nt"
            self.assertFalse(self.store.database_path.exists())
            started = time.monotonic()
            result = wrapper(args, session_id="session-1")
            elapsed = time.monotonic() - started
        self.assertIn("error", json.loads(result))
        handler.assert_not_called()
        self.assertLess(elapsed, 0.1)
        self.assertEqual(commits, ["schema", "attempt"])
        self.assertEqual(self.store.public_rows(), [])

    def test_posix_directory_failures_block_handler_after_commit(self) -> None:
        for operation in ("open", "fsync"):
            with self.subTest(operation=operation):
                self.home = Path(self.temporary.name) / operation
                self.store = self.module.AttemptStore(self.home)
                handler = Mock()
                wrapper, args, _, _ = self._registered_handler(handler)
                with patch.object(self.module, "os", wraps=self.module.os) as platform_os:
                    platform_os.name = "posix"
                    # Simulate descriptor operations so the negative control also runs on Windows.
                    platform_os.open.return_value = 123
                    platform_os.fsync.return_value = None
                    platform_os.close.return_value = None
                    getattr(platform_os, operation).side_effect = OSError("injected directory failure")
                    started = time.monotonic()
                    result = wrapper(args, session_id="session-1")
                    elapsed = time.monotonic() - started
                    getattr(platform_os, operation).assert_called_once()
                    if operation == "fsync":
                        platform_os.close.assert_called_once_with(123)
                self.assertIn("error", json.loads(result))
                handler.assert_not_called()
                self.assertLess(elapsed, 0.1)
                rows = self.store.public_rows()
                self.assertEqual([row["row_type"] for row in rows], ["attempt"])
                # A committed-but-unconfirmed attempt remains unresolved, never replayable.
                wrapper, args, _, _ = self._registered_handler(handler)
                self.assertIn("error", json.loads(wrapper(args, session_id="session-1")))
                handler.assert_not_called()
                self.assertEqual(self.store.public_rows(), rows)


if __name__ == "__main__":
    unittest.main()
