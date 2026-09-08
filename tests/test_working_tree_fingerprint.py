from __future__ import annotations

import os
import subprocess
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from _local_package import load_local_package

load_local_package()

from omh.quality.evidence_records import assess_quality_evidence, build_quality_evidence_package
from omh.quality.working_tree_fingerprint import (
    MAX_GIT_CALLS,
    WorkingTreeFingerprintState,
    working_tree_content_fingerprint,
)


def _git(root: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=root, check=True, capture_output=True)


def _init_repo(root: Path) -> None:
    _git(root, "init")
    _git(root, "config", "user.email", "omh-tests@example.test")
    _git(root, "config", "user.name", "OMH Tests")
    (root / ".gitignore").write_text("ignored/\n", encoding="utf-8")
    (root / "tracked.txt").write_bytes(b"base\x00bytes\n")
    _git(root, "add", ".")
    _git(root, "commit", "-m", "fixture")


def _files(root: Path) -> dict[str, bytes]:
    return {
        path.relative_to(root).as_posix(): path.read_bytes()
        for path in root.rglob("*")
        if path.is_file()
    }


class WorkingTreeFingerprintTests(unittest.TestCase):
    def setUp(self) -> None:
        self._temporary = TemporaryDirectory()
        self.root = Path(self._temporary.name) / "repo"
        self.root.mkdir()
        _init_repo(self.root)

    def tearDown(self) -> None:
        self._temporary.cleanup()

    def test_working_tree_content_fingerprint_is_the_public_freshness_collector(self) -> None:
        # Given: a clean repository.
        # When: its complete working-tree identity is collected twice.
        # Then: both calls return the same full, authoritative identity.
        first = working_tree_content_fingerprint(self.root)
        second = working_tree_content_fingerprint(self.root)

        self.assertEqual(first.state, WorkingTreeFingerprintState.CLEAN)
        self.assertEqual(first.fingerprint, second.fingerprint)
        self.assertEqual(len(str(first.fingerprint)), 64)
        self.assertTrue(first.authoritative)
        self.assertLessEqual(first.git_calls, MAX_GIT_CALLS)

    def test_final_bytes_paths_modes_symlinks_and_untracked_content_change_the_identity(self) -> None:
        # Given: a clean identity.
        # When: each final Git-visible workspace state changes.
        # Then: it receives a distinct dirty identity and a revert restores the base.
        base = working_tree_content_fingerprint(self.root).fingerprint
        tracked = self.root / "tracked.txt"
        tracked.write_bytes(b"changed\xffbytes\n")
        changed = working_tree_content_fingerprint(self.root)
        tracked.write_bytes(b"base\x00bytes\n")
        restored = working_tree_content_fingerprint(self.root)

        self.assertEqual(changed.state, WorkingTreeFingerprintState.DIRTY)
        self.assertNotEqual(changed.fingerprint, base)
        self.assertEqual(restored.fingerprint, base)

        (self.root / "new-path").write_bytes(b"untracked\x00content")
        untracked = working_tree_content_fingerprint(self.root)
        self.assertNotEqual(untracked.fingerprint, base)
        (self.root / "new-path").unlink()

        tracked.chmod(0o755)
        executable = working_tree_content_fingerprint(self.root)
        self.assertNotEqual(executable.fingerprint, base)
        tracked.chmod(0o644)

        os.symlink("tracked.txt", self.root / "link")
        link = working_tree_content_fingerprint(self.root)
        self.assertNotEqual(link.fingerprint, base)
        (self.root / "link").unlink()

        raw_name = os.fsencode(self.root) + b"/nonutf8-\xff"
        try:
            with open(raw_name, "wb") as source:
                source.write(b"raw path")
        except OSError as error:
            self.skipTest(f"filesystem rejects non-UTF-8 path bytes: {error.errno}")
        byte_path = working_tree_content_fingerprint(self.root)
        self.assertNotEqual(byte_path.fingerprint, base)
        os.unlink(raw_name)
        self.assertEqual(working_tree_content_fingerprint(self.root).fingerprint, base)

    def test_staging_and_unstaging_do_not_change_final_content_identity(self) -> None:
        # Given: changed final bytes, including intent-to-add content.
        # When: those bytes move through the real index.
        # Then: staging state does not affect the working-tree identity.
        tracked = self.root / "tracked.txt"
        tracked.write_bytes(b"final bytes")
        unstaged = working_tree_content_fingerprint(self.root).fingerprint
        _git(self.root, "add", "tracked.txt")
        staged = working_tree_content_fingerprint(self.root).fingerprint
        _git(self.root, "restore", "--staged", "tracked.txt")
        unstaged_again = working_tree_content_fingerprint(self.root).fingerprint
        (self.root / "intent").write_bytes(b"intent bytes")
        _git(self.root, "add", "-N", "intent")
        intent_to_add = working_tree_content_fingerprint(self.root)

        self.assertEqual(unstaged, staged)
        self.assertEqual(staged, unstaged_again)
        self.assertEqual(intent_to_add.state, WorkingTreeFingerprintState.DIRTY)
        self.assertLessEqual(intent_to_add.git_calls, MAX_GIT_CALLS)

    def test_collection_leaves_real_git_state_and_ignored_content_untouched(self) -> None:
        # Given: byte snapshots of the checkout and its real Git metadata.
        # When: ignored content is added and a fingerprint is collected.
        # Then: ignored content is excluded and no real state changes.
        before = _files(self.root)
        git_before = _files(self.root / ".git")
        (self.root / "ignored").mkdir()
        (self.root / "ignored" / "secret").write_bytes(b"ignored")
        first = working_tree_content_fingerprint(self.root)
        (self.root / "ignored" / "secret").write_bytes(b"different")
        second = working_tree_content_fingerprint(self.root)

        self.assertEqual(first.fingerprint, second.fingerprint)
        self.assertEqual(_files(self.root / ".git"), git_before)
        self.assertEqual(_files(self.root)["tracked.txt"], before["tracked.txt"])

    def test_committing_unchanged_final_bytes_preserves_content_identity(self) -> None:
        (self.root / "tracked.txt").write_bytes(b"final committed bytes")
        before_commit = working_tree_content_fingerprint(self.root)
        _git(self.root, "add", "tracked.txt")

        _git(self.root, "commit", "-m", "record unchanged final bytes")
        after_commit = working_tree_content_fingerprint(self.root)

        self.assertTrue(before_commit.authoritative)
        self.assertTrue(after_commit.authoritative)
        self.assertEqual(before_commit.fingerprint, after_commit.fingerprint)

    def test_ambient_git_redirection_cannot_change_collection(self) -> None:
        environment = {
            "GIT_DIR": str(self.root.parent / "unrelated-git-dir"),
            "GIT_WORK_TREE": str(self.root.parent / "unrelated-work-tree"),
            "GIT_INDEX_FILE": str(self.root.parent / "unrelated-index"),
            "GIT_CONFIG_COUNT": "1",
            "GIT_CONFIG_KEY_0": "core.attributesfile",
            "GIT_CONFIG_VALUE_0": str(self.root.parent / "unrelated-attributes"),
        }

        with patch.dict(os.environ, environment):
            result = working_tree_content_fingerprint(self.root)

        self.assertEqual(result.state, WorkingTreeFingerprintState.CLEAN)
        self.assertTrue(result.authoritative)

    def test_observed_content_race_is_unavailable(self) -> None:
        tracked = self.root / "tracked.txt"
        tracked.write_bytes(b"changed bytes")
        replacement = self.root / "replacement"
        replacement.write_bytes(b"replacement bytes")
        from omh.quality import working_tree_fingerprint_content

        original = working_tree_fingerprint_content._entry_digest

        def replace_after_hash(path: bytes, metadata: os.stat_result) -> tuple[bytes, bytes] | None:
            result = original(path, metadata)
            if path == os.fsencode(tracked):
                os.replace(replacement, tracked)
            return result

        with patch(
            "omh.quality.working_tree_fingerprint_content._entry_digest",
            side_effect=replace_after_hash,
        ):
            result = working_tree_content_fingerprint(self.root)

        self.assertEqual(result.state, WorkingTreeFingerprintState.UNAVAILABLE)
        self.assertFalse(result.authoritative)

    def test_configured_fsmonitor_and_filter_helpers_never_run(self) -> None:
        # Given: repository-configured fsmonitor and clean-filter helper sentinels.
        # When: a changed path is collected.
        # Then: the collector refuses the filter policy without executing either helper.
        marker = self.root / ".helper-ran"
        helper = self.root / "helper"
        helper.write_text("#!/bin/sh\n: > .helper-ran\n", encoding="utf-8")
        helper.chmod(0o700)
        (self.root / ".gitattributes").write_text("tracked.txt filter=sentinel\n", encoding="utf-8")
        _git(self.root, "add", ".gitattributes", "helper")
        _git(self.root, "commit", "-m", "attributes")
        _git(self.root, "config", "core.fsmonitor", str(helper))
        _git(self.root, "config", "filter.sentinel.clean", str(helper))
        (self.root / "tracked.txt").write_bytes(b"edit\x00bytes\n")

        result = working_tree_content_fingerprint(self.root)

        self.assertEqual(result.state, WorkingTreeFingerprintState.UNSUPPORTED)
        self.assertFalse(marker.exists())

    def test_unsupported_or_unavailable_collection_fails_freshness_closed(self) -> None:
        # Given: a nested repository that the collector cannot normalize safely.
        # When: it is collected and handed to local freshness assessment.
        # Then: the state is explicit and evidence cannot read as fresh.
        nested = self.root / "nested"
        nested.mkdir()
        _init_repo(nested)
        unavailable = working_tree_content_fingerprint(self.root)
        package = build_quality_evidence_package(
            repository_id="r", commit_sha="c", tree_sha="t", title="gate", executor_target="executor"
        )
        assessment = assess_quality_evidence(package, current_fingerprint=unavailable)

        self.assertEqual(unavailable.state, WorkingTreeFingerprintState.UNSUPPORTED)
        self.assertFalse(unavailable.authoritative)
        self.assertEqual(assessment["dimensions"]["source_freshness"]["status"], "unsatisfied")
        self.assertIn("current_tree_unavailable", assessment["reasons"])


if __name__ == "__main__":
    unittest.main()
