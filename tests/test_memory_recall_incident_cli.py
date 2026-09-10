from __future__ import annotations

import hashlib
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from _cli_harness import run_cli


class MemoryRecallIncidentCliTests(unittest.TestCase):
    def test_pending_claim_diagnosis_is_private_and_explicitly_persisted(self) -> None:
        for persist in (False, True):
            with self.subTest(persist=persist), TemporaryDirectory() as directory:
                # Given: a real pending candidate in isolated OMH/Hermes homes.
                root = Path(directory)
                home = root / "omh"
                prefix = ["--omh-home", str(home), "--hermes-home", str(root / "hermes")]
                summary = "PRIVATE_CLAIM_SENTINEL"
                status, stdout, stderr = run_cli(prefix + ["memory", "capture", summary])
                self.assertEqual(status, 0, stderr)
                candidate_id = json.loads(stdout)["candidate"]["candidate_id"]
                candidate_path = home / "memory" / "candidates" / f"{candidate_id}.json"
                before = candidate_path.read_bytes()
                anchor = (
                    ["--claim-digest", hashlib.sha256(summary.encode()).hexdigest()]
                    if persist
                    else ["--record-id", candidate_id]
                )
                args = prefix + [
                    "memory", "recall-incident", *anchor,
                    "--query", "PRIVATE_QUERY_SENTINEL",
                    "--session-id", "PRIVATE_SESSION_SENTINEL",
                    "--provider-served-count", "0",
                ]
                if persist:
                    args.append("--write")

                # When: the actual parser/handler diagnoses that expected claim.
                status, stdout, stderr = run_cli(args)

                # Then: evidence is scoped, diagnosis is read-only, saving is opt-in.
                self.assertEqual(status, 0, stderr)
                incident = json.loads(stdout)
                self.assertEqual(incident["schema_version"], "memory_recall_incident/v1")
                self.assertEqual(incident["stage"], "pending_or_rejected")
                self.assertEqual(incident["anchor"]["candidate_id"], candidate_id)
                self.assertFalse(incident["authorizes_mutation"])
                self.assertIsNone(incident["delivery_observed"])
                self.assertEqual(
                    incident["evidence_surfaces"]["live_prefetch_receipt"]["status"],
                    "unavailable",
                )
                self.assertEqual(
                    incident["evidence_surfaces"]["provider_recall_status"]["status"],
                    "not_authoritative",
                )
                for sentinel in (summary, "PRIVATE_QUERY_SENTINEL", "PRIVATE_SESSION_SENTINEL"):
                    self.assertNotIn(sentinel, stdout)
                self.assertEqual(candidate_path.read_bytes(), before)
                incidents = home / "memory" / "incidents"
                if persist:
                    saved = incidents / f"{incident['incident_id']}.json"
                    self.assertEqual(json.loads(saved.read_text()), incident)
                else:
                    self.assertFalse(incidents.exists())
