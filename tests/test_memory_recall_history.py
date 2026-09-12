from __future__ import annotations

from datetime import datetime, timezone
import hashlib
from importlib import import_module
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from _cli_harness import run_cli
from _local_package import load_local_package

load_local_package()
from project_identity_fixture import memory_paths as resolve_paths
from omh.workflows import memory
from omh.workflows.memory_lifecycle import build_memory_correction
from omh.workflows.memory_lifecycle_executor import execute_memory_lifecycle


class MemoryRecallHistoryTests(unittest.TestCase):
    def test_corrected_claim_is_diagnosed_from_history(self) -> None:
        for anchor_kind in ("record", "digest"):
            with self.subTest(anchor_kind=anchor_kind), TemporaryDirectory() as directory:
                # Given a real correction that moves the old claim to history.
                root = Path(directory)
                paths = resolve_paths(root / "omh", root / "hermes")
                summary = "PRIVATE_OLD_CLAIM prefer structured output"
                candidate = memory.capture_project_memory_candidate(paths, summary)["candidate"]
                assert isinstance(candidate, dict)
                record = memory.approve_project_memory_candidate(
                    paths, candidate["candidate_id"],
                )["record"]
                assert isinstance(record, dict)
                plan = build_memory_correction(
                    paths, record["record_id"], record["revision"],
                    "PRIVATE_NEW_CLAIM prefer concise output",
                    now=datetime(2026, 9, 10, tzinfo=timezone.utc),
                )
                execute_memory_lifecycle(paths, plan)
                before = {p: p.read_bytes() for p in paths.memory_dir.rglob("*.json")}
                anchor = (
                    ["--record-id", record["record_id"]]
                    if anchor_kind == "record"
                    else ["--claim-digest", hashlib.sha256(summary.encode()).hexdigest()]
                )

                # When the public CLI diagnoses the superseded claim.
                status, stdout, stderr = run_cli([
                    "--omh-home", str(paths.omh_home),
                    "--hermes-home", str(paths.hermes_home),
                    "memory", "recall-incident", *anchor,
                ])

                # Then history, not absence or a pending successor, explains it.
                self.assertEqual(status, 0, stderr)
                incident = json.loads(stdout)
                self.assertEqual(incident["stage"], "invalid_or_superseded")
                self.assertEqual(incident["reason_code"], "superseded")
                self.assertEqual(incident["anchor"]["record_id"], record["record_id"])
                self.assertEqual(incident["evidence_surfaces"]["omh_history"]["status"], "observed")
                self.assertFalse(incident["authorizes_mutation"])
                self.assertNotIn("PRIVATE_OLD_CLAIM", stdout)
                self.assertNotIn("PRIVATE_NEW_CLAIM", stdout)
                self.assertEqual(before, {p: p.read_bytes() for p in paths.memory_dir.rglob("*.json")})

    def test_history_digest_lookup_does_not_cross_project_scope(self) -> None:
        with TemporaryDirectory() as directory:
            # Given a superseded claim owned by another project.
            root = Path(directory)
            paths = resolve_paths(root / "omh", root / "hermes")
            summary = "Foreign preference"
            candidate = memory.capture_project_memory_candidate(
                paths, summary, scope_ref="foreign-project",
            )["candidate"]
            assert isinstance(candidate, dict)
            record = memory.approve_project_memory_candidate(
                paths, candidate["candidate_id"],
            )["record"]
            assert isinstance(record, dict)
            execute_memory_lifecycle(paths, build_memory_correction(
                paths, record["record_id"], record["revision"], "Replacement preference",
                now=datetime(2026, 9, 10, tzinfo=timezone.utc),
            ))

            # When the default project looks up the old claim's digest.
            api = import_module("omh.workflows.memory_recall_incident")
            incident = api.build_memory_recall_incident(paths, api.RecallIncidentRequest(
                claim_digest=hashlib.sha256(summary.encode()).hexdigest(),
            ))

            # Then no historical record identity crosses the scope boundary.
            self.assertEqual(incident["stage"], "not_found")
            self.assertNotIn("record_id", incident["anchor"])

    def test_corrupt_history_cannot_prove_claim_absence(self) -> None:
        with TemporaryDirectory() as directory:
            # Given a history store the reader cannot completely inspect.
            root = Path(directory)
            paths = resolve_paths(root / "omh", root / "hermes")
            history = paths.memory_dir / "history"
            history.mkdir(parents=True)
            (history / "broken.json").write_text("{")

            # When looking for a claim without current-record evidence.
            api = import_module("omh.workflows.memory_recall_incident")
            incident = api.build_memory_recall_incident(
                paths, api.RecallIncidentRequest(claim_digest="a" * 64),
            )

            # Then missing evidence is unresolved, never an observed absence.
            self.assertEqual(incident["stage"], "unresolved")
            self.assertEqual(incident["evidence_surfaces"]["omh_history"]["status"], "unavailable")
