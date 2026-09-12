from __future__ import annotations

import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from _cli_harness import run_cli
from _local_package import load_local_package

load_local_package()
from omh.memory import (
    LifecycleCandidateError,
    approve_project_memory_candidate,
    build_project_memory_recall_pack,
    build_project_memory_review,
    capture_project_memory_candidate,
)
from project_identity_fixture import memory_paths as resolve_paths


def _cli(home: Path, *args):
    status, stdout, stderr = run_cli(["--omh-home", str(home / ".omh"), "--hermes-home", str(home / ".hermes"), *args])
    assert status == 0, f"{args} failed: {stderr or stdout}"
    return json.loads(stdout)


class CorrectionInterviewPathTests(unittest.TestCase):
    """The 'is this memory right? no, fix it' interview must survive the CLI."""

    def test_plain_approval_fails_closed_on_lifecycle_candidates(self) -> None:
        with TemporaryDirectory() as tmp:
            home = Path(tmp)
            paths = resolve_paths(home / ".omh", home / ".hermes")
            captured = capture_project_memory_candidate(paths, "Deploys go through staging first", record_type="decision")
            record = approve_project_memory_candidate(paths, captured["candidate"]["candidate_id"])["record"]
            _cli(home, "memory", "correct", record["record_id"], "Deploys go through staging, then canary", "--revision", "1", "--apply")

            correction = next(
                candidate
                for candidate in (json.loads(p.read_text()) for p in (paths.memory_dir / "candidates").glob("*.json"))
                if candidate.get("lifecycle") == "correction"
            )
            with self.assertRaises(LifecycleCandidateError):
                approve_project_memory_candidate(paths, str(correction["candidate_id"]))
            self.assertFalse(
                (paths.memory_dir / "records" / f"{record['record_id']}.json").exists(),
                "the guard must not mint any record; r1 stays superseded in history until reapproval",
            )

    def test_cli_approve_routes_corrections_through_reapproval(self) -> None:
        with TemporaryDirectory() as tmp:
            home = Path(tmp)
            paths = resolve_paths(home / ".omh", home / ".hermes")
            captured = capture_project_memory_candidate(
                paths, "Deploys go through staging first", record_type="decision", observed="codex"
            )
            record = approve_project_memory_candidate(paths, captured["candidate"]["candidate_id"])["record"]
            _cli(home, "memory", "correct", record["record_id"], "Deploys go through staging, then canary", "--revision", "1", "--apply")
            correction = next(
                candidate
                for candidate in (json.loads(p.read_text()) for p in (paths.memory_dir / "candidates").glob("*.json"))
                if candidate.get("lifecycle") == "correction"
            )

            result = _cli(home, "memory", "approve", str(correction["candidate_id"]))

            self.assertTrue(result.get("applied"), str(result.get("reason_code")))
            live = json.loads((paths.memory_dir / "records" / f"{record['record_id']}.json").read_text())
            self.assertEqual(live["revision"], 2)
            self.assertIn("canary", live["summary"])
            self.assertEqual(live["record_type"], "decision", "the replacement payload survives the one approve verb")
            self.assertEqual(live["perspective"], {"observer": "hermes", "observed": "codex"})
            pack = build_project_memory_recall_pack(paths, "staging canary", observed="codex")
            self.assertIn(record["record_id"], {item["record_id"] for item in pack["included_records"]})

    def test_include_stale_surfaces_revalidation_stale_records_for_inspection(self) -> None:
        with TemporaryDirectory() as tmp:
            paths = resolve_paths(Path(tmp) / ".omh", Path(tmp) / ".hermes")
            captured = capture_project_memory_candidate(paths, "CI runner uses ubuntu", stale_after_days=1)
            record = approve_project_memory_candidate(paths, captured["candidate"]["candidate_id"])["record"]
            from omh.plugin_bundle.omh.memory_governance import canonical_payload_digest

            record_path = paths.memory_dir / "records" / f"{record['record_id']}.json"
            stored = json.loads(record_path.read_text())
            stored["staleness"]["stale_after"] = "2026-01-01T00:00:00Z"
            stored["revalidation"] = {"deadline": "2026-01-01T00:00:00Z"}
            digest = canonical_payload_digest(stored)
            stored["admission"]["payload_digest"] = digest
            review_path = paths.memory_dir / "reviews" / f"{stored['admission']['review_id']}.json"
            review = json.loads(review_path.read_text())
            review["payload_digest"] = digest
            review_path.write_text(json.dumps(review))
            record_path.write_text(json.dumps(stored))

            default_pack = build_project_memory_recall_pack(paths, "ubuntu runner")
            self.assertIn(
                record["record_id"],
                {entry["record_id"] for entry in default_pack["excluded_records"]},
                "stale records stay excluded by default with a stated reason",
            )

            stale_pack = build_project_memory_recall_pack(paths, "ubuntu runner", include_stale=True)
            [item] = [i for i in stale_pack["included_records"] if i["record_id"] == record["record_id"]]
            self.assertEqual(item["staleness"]["state"], "stale", "the inspection surface marks it stale, not fresh")
            self.assertFalse(item["replay_evaluation"]["eligible"], "stale inclusion never launders replay eligibility")

    def test_include_stale_never_resurrects_expired_records(self) -> None:
        with TemporaryDirectory() as tmp:
            paths = resolve_paths(Path(tmp) / ".omh", Path(tmp) / ".hermes")
            captured = capture_project_memory_candidate(paths, "volatile borrow note", retention_class="volatile", ttl_days=1)
            record = approve_project_memory_candidate(paths, captured["candidate"]["candidate_id"])["record"]
            from datetime import datetime, timedelta, timezone

            future = datetime.now(timezone.utc) + timedelta(days=8)
            pack = build_project_memory_recall_pack(paths, "", include_stale=True, now=future)
            self.assertNotIn(record["record_id"], {i["record_id"] for i in pack["included_records"]})

    def test_review_card_says_when_the_memory_was_captured(self) -> None:
        with TemporaryDirectory() as tmp:
            paths = resolve_paths(Path(tmp) / ".omh", Path(tmp) / ".hermes")
            capture_project_memory_candidate(paths, "pending interview subject")
            [card] = build_project_memory_review(paths)["cards"]
            self.assertTrue(card.get("created_at"), "the interview needs 'when was this remembered'")


if __name__ == "__main__":
    unittest.main()
