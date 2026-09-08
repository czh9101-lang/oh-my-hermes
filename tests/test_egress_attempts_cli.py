from __future__ import annotations

import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from _cli_harness import run_cli
from test_egress_attempt_receipts import _request

from omh.plugin_bundle.omh.egress_attempt_receipts import AttemptStore


class EgressAttemptCliTests(unittest.TestCase):
    def test_reader_distinguishes_unknown_from_returned_without_minting(self) -> None:
        with TemporaryDirectory() as temporary:
            home = Path(temporary) / ".omh"
            store = AttemptStore(home)
            pending = store.open_attempt(**_request("pending"))
            returned = store.open_attempt(**_request("returned"))
            store.record_terminal(returned["attempt_id"], "returned")
            before = store.database_path.read_bytes()

            status, stdout, stderr = run_cli(
                ["--omh-home", str(home), "runtime", "egress-attempts"]
            )

            self.assertEqual(status, 0, stderr)
            payload = json.loads(stdout)
            self.assertEqual(payload["schema_version"], "runtime_egress_attempts_view/v1")
            rows = {row["attempt_id"]: row for row in payload["attempts"]}
            self.assertEqual(rows[pending["attempt_id"]]["terminal_state"], "unknown")
            self.assertEqual(rows[returned["attempt_id"]]["terminal_state"], "returned")
            self.assertEqual(payload["attempt_count"], 2)
            self.assertEqual(store.database_path.read_bytes(), before)

    def test_empty_reader_does_not_create_storage_and_limit_is_bounded(self) -> None:
        with TemporaryDirectory() as temporary:
            home = Path(temporary) / ".omh"
            status, stdout, stderr = run_cli(
                ["--omh-home", str(home), "runtime", "egress-attempts", "--limit", "1"]
            )
            self.assertEqual(status, 0, stderr)
            self.assertEqual(json.loads(stdout)["attempt_count"], 0)
            self.assertFalse(home.exists())

            status, _, _ = run_cli(
                ["--omh-home", str(home), "runtime", "egress-attempts", "--limit", "201"]
            )
            self.assertNotEqual(status, 0)
            self.assertFalse(home.exists())


if __name__ == "__main__":
    unittest.main()
