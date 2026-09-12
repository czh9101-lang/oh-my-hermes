from __future__ import annotations

import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from _handoff_risk_fixture import git, invoke, repository, scan


class HandoffRiskScanTests(unittest.TestCase):
    def test_S1_report_when_brief_file_supplied(self):
        # Given / When
        report = scan("Inspect fixture correctness.")
        # Then
        self.assertEqual(report.get("schema_version"), "handoff_risk_scan/v1")
        self.assertEqual(report["status"], "completed")
        self.assertEqual(report["verdict"], "clear")

    def test_S1_report_when_stdin_supplied(self):
        # Given / When
        code, report = invoke(["--brief-stdin"], "Run git reset --hard")
        # Then
        self.assertEqual(code, 0)
        self.assertEqual(report["verdict"], "high_risk")

    def test_S2_redacted_metadata_when_private_brief_supplied(self):
        # Given
        brief = 'Run rm -rf "SYNTHETIC_PRIVATE_SENTINEL"'
        # When
        report = scan(brief)
        # Then
        self.assertEqual(report.get("verdict"), "high_risk")
        self.assertNotIn("SYNTHETIC_PRIVATE_SENTINEL", json.dumps(report))
        self.assertEqual(report["input_summary"]["brief_bytes"], len(brief.encode()))
        self.assertEqual(report["input_summary"]["brief_sha256"], hashlib.sha256(brief.encode()).hexdigest())
        finding = report["findings"][0]
        self.assertEqual(set(finding), {"id", "severity", "confidence", "evidence", "advice_code"})
        self.assertEqual(finding["severity"], "high")
        self.assertEqual(finding["confidence"], "high")
        self.assertLess(len(json.dumps(finding["evidence"])), 250)
        self.assertTrue(report["claim_boundary"])

    def test_S3_workspace_signals_when_metadata_present(self):
        with tempfile.TemporaryDirectory() as root:
            # Given
            repo = repository(Path(root))
            (repo / ".env").write_text("SYNTHETIC_SECRET_SENTINEL")
            git(repo, "add", ".env")
            (repo / "untracked").write_text("synthetic")
            # When
            code, report = invoke(["--repo", str(repo)])
            # Then
            self.assertEqual(code, 0)
            self.assertEqual({f["id"] for f in report["findings"]},
                             {"dirty_worktree", "untracked_files", "tracked_secret_path"})
            self.assertNotIn("SYNTHETIC_SECRET_SENTINEL", json.dumps(report))
            self.assertNotIn(".env", json.dumps(report))

    def test_S4_metadata_only_when_secret_contents_unreadable(self):
        with tempfile.TemporaryDirectory() as root:
            # Given
            repo = repository(Path(root))
            (repo / ".env").write_text("SYNTHETIC_SECRET_SENTINEL")
            git(repo, "add", ".env")
            # When: repo-only analysis has no reason to open any file body.
            with patch("builtins.open", side_effect=AssertionError("unexpected file body read")), patch.object(
                Path, "open", side_effect=AssertionError("unexpected file body read"),
            ):
                code, report = invoke(["--repo", str(repo)])
            # Then
            self.assertEqual(code, 0)
            self.assertIn("tracked_secret_path", {f["id"] for f in report["findings"]})

    def test_S3_template_exclusions_when_exact_suffix_used(self):
        with tempfile.TemporaryDirectory() as root:
            # Given
            repo = repository(Path(root))
            for name in (".env.example", ".env.SAMPLE", "credentials.json.template", "id_rsa.dist"):
                (repo / name).write_text("template")
            git(repo, "add", ".")
            git(repo, "commit", "-m", "templates")
            # When
            code, report = invoke(["--repo", str(repo)])
            # Then
            self.assertEqual(code, 0)
            self.assertEqual(report["verdict"], "clear")

    def test_S3_secret_path_when_under_examples_directory(self):
        with tempfile.TemporaryDirectory() as root:
            # Given
            repo = repository(Path(root))
            (repo / "examples").mkdir()
            (repo / "examples" / ".env").write_text("synthetic")
            git(repo, "add", ".")
            # When
            _, report = invoke(["--repo", str(repo)])
            # Then
            self.assertIn("tracked_secret_path", {f["id"] for f in report.get("findings", [])})

    def test_S3_protected_commit_when_current_branch_known(self):
        with tempfile.TemporaryDirectory() as root:
            # Given
            repo = repository(Path(root))
            # When
            report = scan("Run git commit -am 'fixture'", ["--repo", str(repo)])
            # Then
            self.assertIn("protected_branch_write", {f["id"] for f in report.get("findings", [])})
