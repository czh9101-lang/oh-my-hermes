from __future__ import annotations

from pathlib import Path
import tempfile
import unittest

from _handoff_risk_fixture import invoke, repository


class HandoffRiskExitTests(unittest.TestCase):
    def test_S6_exit_matrix_when_brief_verdict_changes(self):
        # Given
        cases = (
            ("git reset --hard", [], 0, "high_risk"),
            ("git reset --hard", ["--strict"], 1, "high_risk"),
            ("Do not run git reset --hard", ["--strict"], 0, "clear"),
            ("a" * 65537, ["--strict"], 2, None),
        )
        for brief, options, expected, verdict in cases:
            with self.subTest(expected=expected, verdict=verdict):
                # When
                code, report = invoke(["--brief-stdin", *options], brief)
                # Then
                self.assertEqual(code, expected)
                self.assertEqual(report.get("verdict"), verdict)

    def test_S6_advisory_when_dirty_without_high_signal(self):
        with tempfile.TemporaryDirectory() as root:
            # Given
            repo = repository(Path(root))
            (repo / "tracked.txt").write_text("changed")
            # When
            code, report = invoke(["--repo", str(repo), "--strict"])
            # Then
            self.assertEqual(code, 0)
            self.assertEqual(report["verdict"], "advisory")
