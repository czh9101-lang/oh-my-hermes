from __future__ import annotations

from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from _handoff_risk_fixture import invoke, scan


class HandoffRiskErrorsTests(unittest.TestCase):
    def test_S5_scan_error_when_no_input(self):
        # Given / When
        code, report = invoke([])
        # Then
        self.assertEqual(report.get("status"), "scan_error")
        self.assertIsNone(report["verdict"])
        self.assertEqual(code, 2)

    def test_S5_scan_error_when_invalid_input(self):
        with tempfile.TemporaryDirectory() as root:
            # Given
            invalid = Path(root) / "invalid"
            invalid.write_bytes(b"\xff")
            large = Path(root) / "large"
            large.write_bytes(b"a" * 65537)
            cases = (
                (["--brief-file", str(Path(root) / "missing")], "input_unreadable"),
                (["--brief-file", str(invalid)], "invalid_utf8"),
                (["--brief-file", str(large)], "brief_oversized"),
                (["--repo", root], "repository_invalid"),
                (["--repo", str(Path(root) / "missing")], "repository_invalid"),
            )
            for args, category in cases:
                with self.subTest(category=category, args=args):
                    # When
                    code, report = invoke(args)
                    # Then
                    self.assertEqual(report.get("error_category"), category)
                    self.assertEqual(report["status"], "scan_error")
                    self.assertIsNone(report["verdict"])
                    self.assertEqual(code, 2)

    def test_S5_scan_error_when_permission_denied(self):
        # Given / When
        with patch.object(Path, "open", side_effect=PermissionError("SYNTHETIC_PRIVATE_SENTINEL")):
            code, report = invoke(["--brief-file", "unreadable.txt"])
        # Then
        self.assertEqual(report.get("error_category"), "input_unreadable")
        self.assertEqual(code, 2)
        self.assertNotIn("SYNTHETIC_PRIVATE_SENTINEL", str(report))

    def test_S5_scan_error_when_shell_quote_unterminated(self):
        # Given / When
        report = scan('Run rm -rf "unterminated')
        # Then
        self.assertEqual(report.get("status"), "scan_error")
        self.assertIsNone(report["verdict"])

    def test_S5_completed_when_exact_byte_bound(self):
        # Given / When
        report = scan("a" * 65536)
        # Then
        self.assertEqual(report.get("status"), "completed")

    def test_S5_scan_error_when_protected_branch_invalid(self):
        # Given / When
        report = scan("git status", ["--protected-branch", "*"])
        # Then
        self.assertEqual(report.get("error_category"), "protected_branch_invalid")
