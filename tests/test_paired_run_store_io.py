from __future__ import annotations

import os
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from omh.quality.paired_run_decision import build_paired_run_decision
from omh.quality.paired_run_decision_store import (
    PairedRunStoreError,
    append_paired_run_decision,
    latest_paired_run_decision,
)
from test_paired_run_decision_store import _request


class PairedRunStoreIoTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = TemporaryDirectory(prefix="omh-paired-store-io-")
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "explicit" / "decisions.jsonl"
        self.omh_home = (Path(self.temp.name) / ".omh").resolve()

    def test_append_fails_closed_without_descriptor_lock_backend(self) -> None:
        candidate = build_paired_run_decision(_request("d1", None))
        with (
            patch("omh.system.descriptor_lock.fcntl", None),
            patch("omh.system.descriptor_lock.msvcrt", None),
            self.assertRaises(PairedRunStoreError),
        ):
            append_paired_run_decision(self.path, candidate, self.omh_home)
        self.assertFalse(self.path.exists())

    def test_append_rejects_over_budget_payload_atomically(self) -> None:
        first = build_paired_run_decision(_request("d1", None))
        append_paired_run_decision(self.path, first, self.omh_home)
        content = self.path.read_bytes()
        before = content + b" " * (8 * 1_024 * 1_024 - len(content))
        self.path.write_bytes(before)
        candidate = build_paired_run_decision(_request("d2", "d1"))
        with self.assertRaises(PairedRunStoreError):
            append_paired_run_decision(self.path, candidate, self.omh_home)
        self.assertEqual(self.path.read_bytes(), before)
        self.assertEqual(
            latest_paired_run_decision(
                self.path,
                "d1",
                self.omh_home,
            ).decision_id,
            "d1",
        )

    def test_reads_store_through_verified_descriptor(self) -> None:
        append_paired_run_decision(
            self.path,
            build_paired_run_decision(_request("d1", None)),
            self.omh_home,
        )
        with patch.object(
            Path,
            "read_text",
            side_effect=AssertionError("Path.read_text forbidden"),
        ):
            decision = latest_paired_run_decision(
                self.path,
                "d1",
                self.omh_home,
            )
        self.assertEqual(decision.decision_id, "d1")

    def test_ancestor_swap_during_final_store_open_never_writes_outside(self) -> None:
        candidate = build_paired_run_decision(_request("d1", None))
        self.path.parent.mkdir(parents=True)
        safe_parent = self.path.parent.with_name("explicit-safe")
        outside_parent = self.path.parent.with_name("outside-store")
        outside_parent.mkdir()
        outside_target = outside_parent / self.path.name
        outside_target.write_bytes(b"")
        original_open = os.open
        swapped = False

        def racing_open(
            path: str | os.PathLike[str],
            flags: int,
            mode: int = 0o777,
            *,
            dir_fd: int | None = None,
        ) -> int:
            nonlocal swapped
            final_file = Path(path).name == self.path.name
            directory = flags & getattr(os, "O_DIRECTORY", 0)
            if not swapped and final_file and not directory:
                self.path.parent.rename(safe_parent)
                self.path.parent.symlink_to(
                    outside_parent,
                    target_is_directory=True,
                )
                swapped = True
            return original_open(path, flags, mode, dir_fd=dir_fd)

        try:
            with patch(
                "omh.system.secure_regular_file.os.open",
                side_effect=racing_open,
            ):
                try:
                    append_paired_run_decision(
                        self.path,
                        candidate,
                        self.omh_home,
                    )
                except PairedRunStoreError as exc:
                    self.assertTrue(exc.reason)
            self.assertEqual(outside_target.read_bytes(), b"")
        finally:
            if self.path.parent.is_symlink():
                self.path.parent.unlink()
            if safe_parent.exists():
                safe_parent.rename(self.path.parent)

    def test_rejects_symlinked_store_file_and_ancestor(self) -> None:
        candidate = build_paired_run_decision(_request("d1", None))
        outside = Path(self.temp.name) / "outside.jsonl"
        outside.write_text("", encoding="utf-8")
        self.path.parent.mkdir(parents=True)
        self.path.symlink_to(outside)
        with self.assertRaises(PairedRunStoreError):
            append_paired_run_decision(self.path, candidate, self.omh_home)
        with self.assertRaises(PairedRunStoreError):
            latest_paired_run_decision(self.path, "d1", self.omh_home)
        self.assertEqual(outside.read_text(encoding="utf-8"), "")
        self.path.unlink()
        self.path.parent.rmdir()
        outside_dir = Path(self.temp.name) / "outside-dir"
        outside_dir.mkdir()
        self.path.parent.symlink_to(outside_dir, target_is_directory=True)
        with self.assertRaises(PairedRunStoreError):
            append_paired_run_decision(self.path, candidate, self.omh_home)
        with self.assertRaises(PairedRunStoreError):
            latest_paired_run_decision(self.path, "d1", self.omh_home)


if __name__ == "__main__":
    unittest.main()
