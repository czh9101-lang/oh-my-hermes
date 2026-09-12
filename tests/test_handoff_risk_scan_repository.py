from __future__ import annotations

import os
from pathlib import Path
import tempfile
import subprocess
import sys
import unittest
from unittest.mock import patch

from _handoff_risk_fixture import git, invoke, repository
from omh.quality import handoff_risk_repository as metadata


class HandoffRiskRepositoryTests(unittest.TestCase):
    def test_S5_error_when_git_output_pipe_overflows(self):
        # Given: a real child, only the Git process-launch boundary is replaced.
        process = subprocess.Popen([sys.executable, "-c", "import sys; sys.stdout.buffer.write(b'x' * 2097152)"],
                                   stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
        # When
        with patch("omh.quality.handoff_risk_repository.subprocess.Popen", return_value=process):
            code, report = invoke(["--repo", "."])
        # Then
        self.assertEqual(code, 2)
        self.assertEqual(report["error_category"], "metadata_oversized")
        self.assertIsNotNone(process.returncode)

    def test_S5_error_when_git_process_timeout(self):
        # Given: the child waits for input that nobody sends; no timing-luck sleep.
        process = subprocess.Popen([sys.executable, "-c", "import sys; sys.stdin.buffer.read()"],
                                   stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
        # When: time itself is the bounded subprocess behavior under test.
        with patch("omh.quality.handoff_risk_repository.subprocess.Popen", return_value=process), patch.object(metadata, "_GIT_TIMEOUT", 0):
            code, report = invoke(["--repo", "."])
        # Then
        self.assertEqual(code, 2)
        self.assertEqual(report["error_category"], "repository_timeout")
        self.assertIsNotNone(process.returncode)

    def test_S4_unchanged_when_repository_has_executable_clean_filter(self):
        with tempfile.TemporaryDirectory() as root:
            # Given: a real Git filter would fail if any content hash were requested.
            repo = repository(Path(root))
            (repo / ".env").write_text("synthetic")
            git(repo, "add", ".env")
            git(repo, "commit", "-m", "secret-path")
            git(repo, "config", "filter.risk-fixture.clean", "exit 97")
            git(repo, "config", "filter.risk-fixture.required", "true")
            (repo / ".gitattributes").write_text("* filter=risk-fixture\n")
            (repo / ".env").write_text("SYNTHETIC_CHANGED_SECRET")
            before = {str(path.relative_to(repo)): path.read_bytes() for path in repo.rglob("*") if path.is_file()}
            # When
            code, report = invoke(["--repo", str(repo)])
            # Then
            self.assertEqual(code, 0)
            self.assertEqual(report["input_summary"]["dirty_count"], 1)
            self.assertEqual(before, {str(path.relative_to(repo)): path.read_bytes() for path in repo.rglob("*") if path.is_file()})

    def test_S5_error_when_metadata_limits_exceeded(self):
        with tempfile.TemporaryDirectory() as root:
            # Given
            repo = repository(Path(root))
            real_git = metadata._git
            for payload in (b"x\0" * 10001, b"x" * 1048577):
                with self.subTest(size=len(payload)):
                    def supplied(path: Path, args: tuple[str, ...]) -> tuple[int, bytes]:
                        if args == ("ls-files", "--others", "--exclude-standard", "-z"):
                            return 0, payload
                        return real_git(path, args)
                    # When
                    with patch.object(metadata, "_git", supplied):
                        code, report = invoke(["--repo", str(repo)])
                    # Then
                    self.assertEqual(code, 2)
                    self.assertEqual(report["error_category"], "metadata_oversized")
                    self.assertIsNone(report["verdict"])

    def test_S4_environment_isolation_when_git_environment_poisoned(self):
        with tempfile.TemporaryDirectory() as root:
            # Given
            repo = repository(Path(root))
            # When
            with patch.dict(os.environ, {"GIT_DIR": str(Path(root) / "foreign"), "GIT_CONFIG_COUNT": "1", "GIT_CONFIG_KEY_0": "core.fsmonitor", "GIT_CONFIG_VALUE_0": "exit 97"}):
                code, report = invoke(["--repo", str(repo)])
            # Then
            self.assertEqual(code, 0)
            self.assertEqual(report["verdict"], "clear")

    def test_S3_dirty_when_index_change_or_deletion_or_unborn(self):
        with tempfile.TemporaryDirectory() as root:
            # Given
            repo = repository(Path(root))
            (repo / "tracked.txt").write_text("staged")
            git(repo, "add", ".")
            # When
            code, report = invoke(["--repo", str(repo)])
            # Then
            self.assertEqual(code, 0)
            self.assertEqual(report["input_summary"]["dirty_count"], 1)

    def test_S3_clear_when_detached_head(self):
        with tempfile.TemporaryDirectory() as root:
            # Given
            repo = repository(Path(root))
            git(repo, "checkout", "--detach")
            # When
            code, report = invoke(["--repo", str(repo)])
            # Then
            self.assertEqual(code, 0)
            self.assertEqual(report["verdict"], "clear")

    def test_S3_dirty_when_tracked_file_removed(self):
        with tempfile.TemporaryDirectory() as root:
            # Given
            repo = repository(Path(root))
            (repo / "tracked.txt").unlink()
            # When
            code, report = invoke(["--repo", str(repo)])
            # Then
            self.assertEqual(code, 0)
            self.assertEqual(report["input_summary"]["dirty_count"], 1)

    def test_S3_clear_when_repository_unborn(self):
        with tempfile.TemporaryDirectory() as root:
            # Given
            repo = Path(root)
            git(repo, "init", "-b", "main")
            # When
            code, report = invoke(["--repo", str(repo)])
            # Then
            self.assertEqual(code, 0)
            self.assertEqual(report["verdict"], "clear")
