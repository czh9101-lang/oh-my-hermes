from __future__ import annotations

import os
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from omh.system.secure_regular_file import (
    SecureFileError,
    append_bytes,
    open_regular_read,
    open_regular_update,
    read_bounded,
)


class SecureRegularFileWindowsTests(unittest.TestCase):
    def test_read_rejects_ancestor_replacement_left_in_place(self) -> None:
        with TemporaryDirectory(prefix="omh-windows-read-race-") as raw:
            root = Path(raw)
            parent = root / "safe"
            parent.mkdir()
            target = parent / "record.json"
            target.write_bytes(b"inside")
            saved = root / "saved"
            outside = root / "outside"
            outside.mkdir()
            (outside / target.name).write_bytes(b"outside")
            original_open = os.open
            swapped = False

            def racing_open(path, flags, mode=0o777, *, dir_fd=None):
                nonlocal swapped
                if not swapped and Path(os.fspath(path)).name == target.name:
                    parent.rename(saved)
                    parent.symlink_to(outside, target_is_directory=True)
                    swapped = True
                return original_open(path, flags, mode, dir_fd=dir_fd)

            racing_os = SimpleNamespace(**vars(os))
            racing_os.open = racing_open
            try:
                with (
                    patch.dict(
                        open_regular_read.__wrapped__.__globals__,
                        {"_uses_windows_fallback": lambda: True, "os": racing_os},
                    ),
                    self.assertRaises(SecureFileError),
                ):
                    with open_regular_read(target) as descriptor:
                        self.fail(f"outside bytes accepted: {read_bounded(descriptor, 32)!r}")
                self.assertEqual((outside / target.name).read_bytes(), b"outside")
            finally:
                if parent.is_symlink():
                    parent.unlink()
                if saved.exists():
                    saved.rename(parent)

    def test_update_rejects_ancestor_replacement_before_any_write(self) -> None:
        with TemporaryDirectory(prefix="omh-windows-update-race-") as raw:
            root = Path(raw)
            parent = root / "safe"
            parent.mkdir()
            target = parent / "records.jsonl"
            target.write_bytes(b"inside\n")
            saved = root / "saved"
            outside = root / "outside"
            outside.mkdir()
            outside_target = outside / target.name
            outside_target.write_bytes(b"outside\n")
            original_open = os.open
            swapped = False

            def racing_open(path, flags, mode=0o777, *, dir_fd=None):
                nonlocal swapped
                if not swapped and Path(os.fspath(path)).name == target.name:
                    parent.rename(saved)
                    parent.symlink_to(outside, target_is_directory=True)
                    swapped = True
                return original_open(path, flags, mode, dir_fd=dir_fd)

            racing_os = SimpleNamespace(**vars(os))
            racing_os.open = racing_open
            try:
                with (
                    patch.dict(
                        open_regular_update.__wrapped__.__globals__,
                        {"_uses_windows_fallback": lambda: True, "os": racing_os},
                    ),
                    self.assertRaises(SecureFileError),
                ):
                    with open_regular_update(target, private=True) as descriptor:
                        append_bytes(descriptor, b"attacker-controlled\n")
                self.assertEqual(outside_target.read_bytes(), b"outside\n")
            finally:
                if parent.is_symlink():
                    parent.unlink()
                if saved.exists():
                    saved.rename(parent)


if __name__ == "__main__":
    unittest.main()
