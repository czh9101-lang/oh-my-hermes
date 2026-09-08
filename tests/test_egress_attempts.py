from __future__ import annotations

import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from _local_package import load_local_package

load_local_package()

from omh.plugin_bundle.omh.egress_attempts import Guard, PRIVATE_TOKEN, Target


class Entry:
    def __init__(self, handler):
        self.handler = handler


class GuardTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.target = Target("send_probe", "message_send", "chat_channel", "channel", "body")
        self.entries: dict[str, Entry] = {}
        self.guard = Guard(Path(self.temp.name), {"send_probe": self.target}, {}, "", self.entries.get)
        def wrapper(*_args, **_kwargs): return ""
        self.wrapper = wrapper
        self.guard.wrappers["send_probe"] = wrapper
        self.entries["send_probe"] = Entry(wrapper)

    def _pre(self, call: str = "call-1"):
        directive = self.guard.pre(tool_name="send_probe", session_id="session-1", tool_call_id=call)
        self.assertEqual(directive["action"], "modify")
        return directive["args"][PRIVATE_TOKEN]

    def test_final_args_are_durable_before_one_handler_and_private_token_is_stripped(self) -> None:
        token = self._pre()
        token, forwarded, error = self.guard.before_handler(
            "send_probe", {"channel": "private-room", "body": "rewritten", PRIVATE_TOKEN: token},
            {"session_id": "session-1"},
        )
        self.assertIsNone(error)
        self.assertEqual(forwarded, {"channel": "private-room", "body": "rewritten"})
        _, _, duplicate = self.guard.before_handler(
            "send_probe", {"channel": "private-room", "body": "rewritten", PRIVATE_TOKEN: token},
            {"session_id": "session-1"},
        )
        self.assertIn("BLOCKED", duplicate)
        self.guard.post(tool_name="send_probe", args={PRIVATE_TOKEN: token}, session_id="session-1",
                        tool_call_id="call-1", status="ok")
        rows = __import__("omh.plugin_bundle.omh.egress_attempt_receipts", fromlist=["AttemptStore"]).AttemptStore(
            self.temp.name
        ).public_rows()
        self.assertEqual([row["row_type"] for row in rows], ["attempt", "terminal"])
        self.assertIsNone(rows[0]["approval_ref"])
        self.assertIsNone(rows[0]["idempotency_key"])
        self.assertNotIn("private-room", json.dumps(rows))
        self.assertNotIn("rewritten", json.dumps(rows))

    def test_forged_cross_tool_or_identity_never_consumes_or_dispatches_a_token(self) -> None:
        token = self._pre()
        _, _, error = self.guard.before_handler(
            "other_tool", {"channel": "x", "body": "y", PRIVATE_TOKEN: token}, {"session_id": "session-1"}
        )
        self.assertIn("BLOCKED", error)
        _, forwarded, error = self.guard.before_handler(
            "send_probe", {"channel": "x", "body": "y", PRIVATE_TOKEN: token}, {"session_id": "session-1"}
        )
        self.assertIsNone(error)
        self.assertEqual(forwarded["body"], "y")

    def test_duplicate_forged_posts_do_not_write_a_terminal_and_real_post_writes_one(self) -> None:
        token = self._pre()
        _, _, error = self.guard.before_handler(
            "send_probe", {"channel": "x", "body": "y", PRIVATE_TOKEN: token}, {"session_id": "session-1"}
        )
        self.assertIsNone(error)
        self.guard.post(tool_name="send_probe", args={PRIVATE_TOKEN: token}, session_id="forged",
                        tool_call_id="call-1", status="ok")
        self.guard.post(tool_name="send_probe", args={PRIVATE_TOKEN: token}, session_id="session-1",
                        tool_call_id="call-1", status="error")
        self.guard.post(tool_name="send_probe", args={PRIVATE_TOKEN: token}, session_id="session-1",
                        tool_call_id="call-1", status="ok")
        rows = __import__("omh.plugin_bundle.omh.egress_attempt_receipts", fromlist=["AttemptStore"]).AttemptStore(
            self.temp.name
        ).public_rows()
        self.assertEqual([row["row_type"] for row in rows], ["attempt", "terminal"])
        self.assertEqual(rows[1]["terminal_state"], "error")

    def test_disabled_or_non_egress_never_constructs_a_store(self) -> None:
        self.assertIsNone(self.guard.pre(tool_name="read_file", session_id="s", tool_call_id="c"))
        self.assertFalse((Path(self.temp.name) / "runtime").exists())
