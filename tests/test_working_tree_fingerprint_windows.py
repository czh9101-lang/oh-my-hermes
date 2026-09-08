"""Deterministic Windows API contracts; host simulation is not native evidence."""
from __future__ import annotations

import ctypes
import errno
import hashlib
import os
import stat
import sys
import unittest
from dataclasses import replace
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import Mock, patch

from _local_package import load_local_package

load_local_package()
from omh.quality import working_tree_fingerprint_content as content
from omh.quality import working_tree_fingerprint_windows as windows


class WindowsHandleApiTests(unittest.TestCase):
    def setUp(self) -> None:
        self.api = SimpleNamespace(
            CreateFileW=Mock(return_value=123), CloseHandle=Mock(return_value=1),
            GetFileType=Mock(return_value=1),
            GetFileInformationByHandleEx=Mock(side_effect=self.information),
        )
        self.identity = (9, (1 << 120) + 7)
        self.pending = False
        self.attributes = 0x80
        self.directory = False
        self.queries = []
        binding = patch.object(windows, "_api", return_value=self.api)
        binding.start()
        self.addCleanup(binding.stop)

    def information(self, handle, kind, pointer, size):
        self.assertEqual(handle, 123)
        structures = {0: windows._BasicInfo, 1: windows._StandardInfo, 18: windows._IdInfo}
        value = ctypes.cast(pointer, ctypes.POINTER(structures[kind])).contents
        self.assertEqual(size, ctypes.sizeof(value))
        self.queries.append(kind)
        if kind == 18:
            value.VolumeSerialNumber = self.identity[0]
            value.FileId[:] = self.identity[1].to_bytes(16, "little")
        elif kind == 0:
            value.CreationTime, value.LastAccessTime = 10, 20
            value.LastWriteTime, value.ChangeTime = 30, 40
            value.FileAttributes = self.attributes
        else:
            value.EndOfFile, value.NumberOfLinks = 17, 1
            value.DeletePending, value.Directory = self.pending, self.directory
        return 1

    def test_native_struct_layout_and_full_width_identity_times(self) -> None:
        self.assertEqual(ctypes.sizeof(windows._IdInfo), 24)
        self.assertEqual(ctypes.sizeof(windows._BasicInfo), 40)
        self.assertEqual(ctypes.sizeof(windows._StandardInfo), 24)
        result = windows.stat(b"C:\\repo\\file.exe")
        self.assertEqual(self.queries, [18, 0, 1])
        self.assertEqual(result, windows.Metadata(9, (1 << 120) + 7, 17, 3000, 4000, 1000, 0x80, 1))
        self.assertEqual(result.st_mode, stat.S_IFREG | 0o666)
        args = self.api.CreateFileW.call_args.args
        self.assertEqual(args, ("C:\\repo\\file.exe", 0x80, 7, None, 3, 0x02200000, None))
        self.api.CloseHandle.assert_called_once_with(123)

    def test_zero_identity_and_pending_deletion_never_become_wildcards(self) -> None:
        for volume, inode, pending in ((0, 7, False), (9, 0, False), (9, 7, True)):
            with self.subTest(volume=volume, inode=inode, pending=pending):
                self.identity, self.pending = (volume, inode), pending
                with self.assertRaises(OSError):
                    windows.stat(b"file")
        self.assertEqual(self.api.CloseHandle.call_count, 3)

    def test_disk_type_and_directory_disagreement_refuse(self) -> None:
        self.api.GetFileType.return_value = 3
        with self.assertRaises(OSError):
            windows.stat(b"pipe")
        self.api.GetFileInformationByHandleEx.assert_not_called()
        self.api.GetFileType.return_value = 1
        self.directory = True
        with self.assertRaises(OSError):
            windows.stat(b"changed-type")
        self.assertEqual(self.api.CloseHandle.call_count, 2)

    def test_reparse_and_readonly_attributes_are_preserved(self) -> None:
        self.attributes = 0x400
        self.assertFalse(stat.S_ISREG(windows.stat(b"reparse").st_mode))
        self.attributes = 1
        self.assertEqual(windows.stat(b"readonly").st_mode, stat.S_IFREG | 0o444)

    def test_query_and_open_failures_are_visible_and_handles_close(self) -> None:
        error = OSError(errno.EACCES, "native query refused")
        with patch.object(windows.ctypes, "get_last_error", return_value=5, create=True), patch.object(
            windows.ctypes, "WinError", return_value=error, create=True,
        ):
            for kind in (18, 0, 1):
                with self.subTest(kind=kind):
                    def query(handle, observed_kind, pointer, size):
                        return 0 if observed_kind == kind else self.information(handle, observed_kind, pointer, size)
                    self.api.GetFileInformationByHandleEx.side_effect = query
                    with self.assertRaises(OSError):
                        windows.stat(b"file")
            self.assertEqual(self.api.CloseHandle.call_count, 3)
            self.api.CreateFileW.return_value = ctypes.c_void_p(-1).value
            with self.assertRaises(OSError):
                windows.stat(b"file")
            self.assertEqual(self.api.CloseHandle.call_count, 3)
            self.api.CloseHandle.return_value = 0
            with self.assertRaises(OSError):
                windows._close_handle(123)

    def test_data_handle_ownership_binary_mode_and_writer_exclusion(self) -> None:
        crt = SimpleNamespace(open_osfhandle=Mock(return_value=42), setmode=Mock(), get_osfhandle=Mock(return_value=123))
        platform_os = SimpleNamespace(**vars(os))
        platform_os.O_NOINHERIT, platform_os.O_BINARY = 0x80, 0x8000
        platform_os.close = Mock()
        with patch.dict(sys.modules, {"msvcrt": crt}), patch.object(windows, "os", platform_os):
            self.assertEqual(windows.open(b"file"), 42)
            self.assertEqual(self.api.CreateFileW.call_args.args[1:3], (0x80000000, 1))
            self.assertEqual(self.api.CreateFileW.call_args.args[4:6], (3, 0x02200000))
            crt.open_osfhandle.assert_called_once_with(123, os.O_RDONLY | 0x80)
            crt.setmode.assert_called_once_with(42, 0x8000)
            self.api.CloseHandle.assert_not_called()  # transferred to CRT owner
            self.assertEqual(windows.fstat(42).st_ino, (1 << 120) + 7)
            crt.get_osfhandle.assert_called_once_with(42)
            crt.setmode.side_effect = OSError(errno.EACCES, "binary mode refused")
            with self.assertRaises(OSError):
                windows.open(b"file")
            platform_os.close.assert_called_once_with(42)
            self.api.CloseHandle.assert_not_called()
            crt.open_osfhandle.side_effect = OSError(errno.EMFILE, "fd allocation failed")
            with self.assertRaises(OSError):
                windows.open(b"file")
            self.api.CloseHandle.assert_called_once_with(123)


class WindowsObservationBoundaryTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.path = self.root / "file.exe"
        self.payload = b"raw\r\nbytes\x1aafter\x00\xff"
        self.path.write_bytes(self.payload)
        self.raw_path = os.fsencode(self.path)
        self.platform_os = SimpleNamespace(**vars(os))
        self.platform_os.name = "nt"
        self.platform_os.read = Mock(wraps=os.read)
        for target, name, value in (
            (content, "os", self.platform_os),
            (windows, "open", Mock(side_effect=lambda path: os.open(path, os.O_RDONLY | getattr(os, "O_BINARY", 0)))),
            (windows, "stat", Mock(side_effect=self.descriptor_stat)),
            (windows, "fstat", Mock(side_effect=lambda fd: self.convert(os.fstat(fd)))),
        ):
            binding = patch.object(target, name, value)
            binding.start()
            self.addCleanup(binding.stop)

    def descriptor_stat(self, path: bytes) -> windows.Metadata:
        # Match the facade's fstat family, not CPython Windows path-stat
        # timestamps/IDs. Reopen the name on every probe to retain race checks.
        descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_BINARY", 0))
        try:
            return self.convert(os.fstat(descriptor))
        finally:
            os.close(descriptor)

    @staticmethod
    def convert(value) -> windows.Metadata:
        attributes = 0x400 if stat.S_ISLNK(value.st_mode) else 0x80
        return windows.Metadata(value.st_dev, value.st_ino, value.st_size, value.st_mtime_ns,
                                value.st_ctime_ns, 1000, attributes, value.st_nlink)

    def test_stable_stream_matches_raw_blob_with_bounded_reads(self) -> None:
        self.payload *= 200000
        self.path.write_bytes(self.payload)
        result = content.overlay(self.root, [b"file.exe"], {})
        digest = hashlib.sha1(b"blob " + str(len(self.payload)).encode() + b"\0" + self.payload).hexdigest().encode()
        self.assertEqual(result, [(b"file.exe", b"100644", digest)])
        self.assertGreater(self.platform_os.read.call_count, 2)
        self.assertTrue(all(call.args[1] <= 1024 * 1024 for call in self.platform_os.read.call_args_list))

    def test_distinct_opened_identity_refuses_before_read(self) -> None:
        before = content._path_stat(self.raw_path)
        replacement = self.root / "replacement"
        replacement.write_bytes(self.payload)
        os.replace(replacement, self.path)
        with self.assertRaises(content.ContentRace):
            content._entry_digest(self.raw_path, before)
        self.platform_os.read.assert_not_called()

    def test_pre_read_named_replacement_refuses_even_when_fd_identity_matches(self) -> None:
        before = content._path_stat(self.raw_path)
        windows.stat.side_effect = None
        windows.stat.return_value = replace(before, st_ino=before.st_ino + 1)
        with self.assertRaises(content.ContentRace):
            content._entry_digest(self.raw_path, before)
        self.platform_os.read.assert_not_called()

    def test_pre_read_reparse_replacement_refuses_without_reading_target(self) -> None:
        before = content._path_stat(self.raw_path)
        windows.fstat.side_effect = None
        windows.fstat.return_value = replace(before, attributes=0x400)
        with self.assertRaises(content.ContentRace):
            content._entry_digest(self.raw_path, before)
        self.platform_os.read.assert_not_called()

    def test_every_native_race_field_is_checked_across_read(self) -> None:
        before = content._path_stat(self.raw_path)
        for field in ("st_dev", "st_ino", "st_size", "st_mtime_ns", "st_ctime_ns", "creation_ns", "attributes", "links"):
            with self.subTest(field=field):
                windows.fstat.side_effect = [before, replace(before, **{field: getattr(before, field) + 1})]
                with self.assertRaises(content.ContentRace):
                    content._entry_digest(self.raw_path, before)

    def test_same_length_byte_change_during_read_fails_closed(self) -> None:
        changed = False

        def read(fd: int, size: int) -> bytes:
            nonlocal changed
            chunk = os.read(fd, size)
            if not changed:
                changed = True
                self.path.write_bytes(self.payload[:-1] + b"x")
                # Deterministic metadata transition; no filesystem clock luck.
                value = self.path.stat()
                os.utime(self.path, ns=(value.st_atime_ns, value.st_mtime_ns + 1000000000))
            return chunk

        self.platform_os.read.side_effect = read
        with self.assertRaises(content.ContentRace):
            content.overlay(self.root, [b"file.exe"], {})

    def test_post_read_named_replacement_fails_closed(self) -> None:
        before = content._path_stat(self.raw_path)
        windows.stat.side_effect = [before, before, replace(before, st_ino=before.st_ino + 1)]
        with self.assertRaises(content.ContentRace):
            content.overlay(self.root, [b"file.exe"], {})


if __name__ == "__main__":
    unittest.main()
