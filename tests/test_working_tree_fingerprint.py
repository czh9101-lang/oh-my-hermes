from __future__ import annotations

import hashlib
import os
import stat
import subprocess
import sys
import unittest
from collections.abc import Callable
from dataclasses import dataclass
from functools import cached_property
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from typing import Protocol, TypeGuard, final
from unittest.mock import patch

from _local_package import load_local_package
from _typing_support import override

load_local_package()

from omh.quality import working_tree_fingerprint_content as content
from omh.quality import working_tree_fingerprint_windows as windows
from omh.quality.evidence_records import assess_quality_evidence, build_quality_evidence_package
from omh.quality.working_tree_fingerprint import (
    MAX_GIT_CALLS,
    WorkingTreeFingerprintState,
    working_tree_content_fingerprint,
)


__all__ = [
    "annotations", "hashlib", "os", "stat", "subprocess", "sys", "unittest", "cached_property",
    "Path", "TemporaryDirectory", "SimpleNamespace", "patch", "load_local_package",
    "assess_quality_evidence", "build_quality_evidence_package", "MAX_GIT_CALLS",
    "WorkingTreeFingerprintState", "working_tree_content_fingerprint",
    "WorkingTreeFingerprintTests", "_git", "_init_repo", "ContentOS",
    "entry_digest", "path_stat", "Callable", "dataclass", "Protocol", "final",
    "override", "content", "windows", "TypeGuard", "is_object_mapping", "is_object_list",
]


class _ContentPort(Protocol):
    def _entry_digest(self, path: bytes, metadata: os.stat_result | windows.Metadata) -> tuple[bytes, bytes] | None: ...
    def _path_stat(self, path: bytes) -> os.stat_result | windows.Metadata: ...


class _ContentAccess(_ContentPort, Protocol):
    def digest(self: _ContentPort) -> Callable[[bytes, os.stat_result | windows.Metadata], tuple[bytes, bytes] | None]:
        return self._entry_digest

    def observe(self: _ContentPort, path: bytes) -> os.stat_result | windows.Metadata:
        return self._path_stat(path)


_content_port: _ContentPort = content


# Capture the real function before race tests patch its module binding.
entry_digest = _ContentAccess.digest(_content_port)


def path_stat(path: bytes) -> os.stat_result | windows.Metadata:
    return _ContentAccess.observe(_content_port, path)


def is_object_mapping(value: object) -> TypeGuard[dict[object, object]]:
    return isinstance(value, dict)


def is_object_list(value: object) -> TypeGuard[list[object]]:
    return isinstance(value, list)


@final
@dataclass
class ContentOS:
    """Real descriptor operations; O_NOFOLLOW is absent to exercise fallback."""

    name: str = "posix"
    O_RDONLY: int = os.O_RDONLY
    O_BINARY: int = getattr(os, "O_BINARY", 0)
    path = os.path
    fsencode = staticmethod(os.fsencode)
    readlink = staticmethod(os.readlink)
    close = staticmethod(os.close)
    open: Callable[[bytes, int], int] = os.open
    read: Callable[[int, int], bytes] = os.read
    lstat: Callable[[bytes], os.stat_result] = os.lstat
    fstat: Callable[[int], os.stat_result] = os.fstat


@final
@dataclass
class _NativeObservations:
    stat: Callable[[bytes], windows.Metadata]
    open: Callable[[bytes], int]
    fstat: Callable[[int], windows.Metadata]
    Metadata = windows.Metadata


def _git(root: Path, *args: str) -> None:
    _ = subprocess.run(["git", *args], cwd=root, check=True, capture_output=True)


def _init_repo(root: Path) -> None:
    _git(root, "init")
    _git(root, "config", "user.email", "omh-tests@example.test")
    _git(root, "config", "user.name", "OMH Tests")
    _git(root, "config", "core.autocrlf", "false")
    _git(root, "config", "gc.auto", "0")
    _git(root, "config", "maintenance.auto", "false")
    _ = (root / ".gitignore").write_text("ignored/\n", encoding="utf-8")
    _ = (root / "tracked.txt").write_bytes(b"base\x00bytes\n")
    _git(root, "add", ".")
    _git(root, "commit", "-m", "fixture")


def _files(root: Path) -> dict[str, bytes]:
    return {
        path.relative_to(root).as_posix(): path.read_bytes()
        for path in root.rglob("*")
        if path.is_file()
    }


class WorkingTreeFingerprintTests(unittest.TestCase):
    @override
    def setUp(self) -> None:
        self.addCleanup(self.__dict__.pop, "root", None)
        self.addCleanup(self.__dict__.pop, "_temporary", None)
        _ = self.root
        self.root.mkdir()
        _init_repo(self.root)

    @cached_property
    def _temporary(self) -> TemporaryDirectory[str]:
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        return temporary

    @cached_property
    def root(self) -> Path:
        return Path(self._temporary.name) / "repo"

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
        _ = tracked.write_bytes(b"changed\xffbytes\n")
        changed = working_tree_content_fingerprint(self.root)
        _ = tracked.write_bytes(b"base\x00bytes\n")
        restored = working_tree_content_fingerprint(self.root)

        self.assertEqual(changed.state, WorkingTreeFingerprintState.DIRTY)
        self.assertNotEqual(changed.fingerprint, base)
        self.assertEqual(restored.fingerprint, base)

        _ = (self.root / "new-path").write_bytes(b"untracked\x00content")
        untracked = working_tree_content_fingerprint(self.root)
        self.assertTrue(untracked.authoritative)
        self.assertNotEqual(untracked.fingerprint, base)
        (self.root / "new-path").unlink()

        tracked.chmod(0o755)
        executable = working_tree_content_fingerprint(self.root)
        self.assertTrue(executable.authoritative)
        if os.name == "nt":
            # Windows chmod changes read-only, not a POSIX executable bit.
            self.assertFalse(tracked.stat().st_mode & stat.S_IXUSR)
            self.assertEqual(executable.fingerprint, base)
        else:
            self.assertNotEqual(executable.fingerprint, base)
        tracked.chmod(0o644)

        os.symlink("tracked.txt", self.root / "link")
        link = working_tree_content_fingerprint(self.root)
        self.assertTrue(link.authoritative)
        self.assertNotEqual(link.fingerprint, base)
        (self.root / "link").unlink()

        # Windows and Darwin filesystems require Unicode; Linux also accepts
        # undecodable bytes. Exercise native path bytes without skipping a case.
        name = os.fsencode("unicode-\u00ff") if sys.platform in {"win32", "darwin"} else b"nonutf8-\xff"
        raw_name = os.path.join(os.fsencode(self.root), name)
        with open(raw_name, "wb") as source:
            _ = source.write(b"raw path")
        byte_path = working_tree_content_fingerprint(self.root)
        self.assertTrue(byte_path.authoritative)
        self.assertNotEqual(byte_path.fingerprint, base)
        os.unlink(raw_name)
        self.assertEqual(working_tree_content_fingerprint(self.root).fingerprint, base)

    @unittest.skipUnless(os.name == "posix", "POSIX executable-bit semantics")
    def test_filemode_false_cannot_hide_final_executable_bit_changes(self) -> None:
        _git(self.root, "config", "core.fileMode", "false")
        tracked = self.root / "tracked.txt"
        tracked.chmod(0o644)
        before = working_tree_content_fingerprint(self.root)

        tracked.chmod(0o755)
        ordinary_status = subprocess.check_output(
            ["git", "status", "--porcelain"], cwd=self.root,
        )
        after = working_tree_content_fingerprint(self.root)

        self.assertEqual(ordinary_status, b"")
        self.assertEqual(after.state, WorkingTreeFingerprintState.DIRTY)
        self.assertNotEqual(after.fingerprint, before.fingerprint)
        tracked.chmod(0o644)
        self.assertEqual(working_tree_content_fingerprint(self.root).fingerprint, before.fingerprint)
        self.assertLessEqual(after.git_calls, MAX_GIT_CALLS)

    def test_enabled_autocrlf_is_not_an_authoritative_raw_byte_snapshot(self) -> None:
        plain = self.root / "plain.txt"
        _ = plain.write_bytes(b"line\n")
        _git(self.root, "add", "plain.txt")
        _git(self.root, "commit", "-m", "plain text fixture")
        for setting in ("true", "input"):
            with self.subTest(setting=setting):
                _git(self.root, "config", "core.autocrlf", setting)
                _ = plain.write_bytes(b"line\r\n")
                _git(self.root, "add", "plain.txt")

                result = working_tree_content_fingerprint(self.root)

                self.assertEqual(result.state, WorkingTreeFingerprintState.UNSUPPORTED)
                self.assertFalse(result.authoritative)
                self.assertIsNone(result.fingerprint)
        _git(self.root, "config", "core.autocrlf", "false")
        _ = plain.write_bytes(b"line\n")
        _git(self.root, "add", "plain.txt")
        self.assertTrue(working_tree_content_fingerprint(self.root).authoritative)

    def test_effective_content_config_overrides_inherited_values(self) -> None:
        # Git emits included/lower-precedence values before the effective value.
        inherited = self.root / ".git" / "inherited-config"
        _ = inherited.write_text("[core]\n autocrlf = true\n sparseCheckout = true\n", encoding="utf-8")
        overrides = self.root / ".git" / "override-config"
        _ = overrides.write_text("[core]\n autocrlf = false\n sparseCheckout = false\n", encoding="utf-8")
        _git(self.root, "config", "include.path", str(inherited))
        _git(self.root, "config", "--add", "include.path", str(overrides))
        self.assertEqual(
            subprocess.check_output(["git", "config", "--get", "core.autocrlf"], cwd=self.root), b"false\n",
        )
        clean = working_tree_content_fingerprint(self.root)
        self.assertEqual(clean.state, WorkingTreeFingerprintState.CLEAN)
        _ = (self.root / "tracked.txt").write_bytes(b"raw\r\nbytes\x1aafter")
        dirty = working_tree_content_fingerprint(self.root)
        self.assertEqual(dirty.state, WorkingTreeFingerprintState.DIRTY)
        self.assertLessEqual(dirty.git_calls, MAX_GIT_CALLS)
        for key, setting in (("core.autocrlf", "true"), ("core.autocrlf", "input"), ("core.sparseCheckout", "true")):
            with self.subTest(key=key, setting=setting):
                _git(self.root, "config", "--file", str(overrides), key, setting)
                refused = working_tree_content_fingerprint(self.root)
                self.assertEqual(refused.state, WorkingTreeFingerprintState.UNSUPPORTED)
                self.assertIsNone(refused.fingerprint)
                _git(self.root, "config", "--file", str(overrides), key, "false")

    def test_descriptor_reads_preserve_raw_bytes_without_nofollow_flag(self) -> None:
        from omh.quality import working_tree_fingerprint_content as content

        payload = b"raw\r\nbytes\x1aafter\x00\xff"
        tracked = self.root / "tracked.txt"
        _ = tracked.write_bytes(payload)
        expected = hashlib.sha1(b"blob " + str(len(payload)).encode() + b"\0" + payload).hexdigest().encode()
        # Narrow CRT semantic simulation: real descriptors/stat checks, but text
        # descriptors translate CRLF and treat Ctrl-Z as EOF unless binary opens.
        binary_flag = 1 << 30
        text_descriptors: set[int] = set()
        opened_flags: list[int] = []

        def crt_open(path: bytes, flags: int) -> int:
            descriptor = os.open(path, (flags & ~binary_flag) | getattr(os, "O_BINARY", 0))
            opened_flags.append(flags)
            if not flags & binary_flag:
                text_descriptors.add(descriptor)
            return descriptor

        def crt_read(descriptor: int, size: int) -> bytes:
            value = os.read(descriptor, size)
            if descriptor in text_descriptors:
                return value.split(b"\x1a", 1)[0].replace(b"\r\n", b"\n")
            return value

        platform_os = ContentOS()
        # Exercise the portable missing-flag branch, with comparable descriptor
        # observations even when the test host itself is Windows.
        def descriptor_stat(path: bytes) -> os.stat_result:
            fd = os.open(path, os.O_RDONLY | getattr(os, "O_BINARY", 0))
            try:
                return os.fstat(fd)
            finally:
                os.close(fd)

        platform_os.name = "posix"
        platform_os.lstat = descriptor_stat
        platform_os.O_BINARY = binary_flag
        platform_os.open = crt_open
        platform_os.read = crt_read
        with patch.object(content, "os", platform_os):
            observed = entry_digest(os.fsencode(tracked), descriptor_stat(os.fsencode(tracked)))
        self.assertEqual(observed, (b"100644", expected))
        self.assertEqual(len(opened_flags), 1)
        self.assertTrue(opened_flags[0] & binary_flag)
        self.assertEqual(tracked.read_bytes(), payload)

    def test_without_nofollow_a_replaced_named_path_is_rejected_before_read(self) -> None:
        from omh.quality import working_tree_fingerprint_content as content

        tracked = self.root / "tracked.txt"
        with tracked.open("rb") as source:
            metadata = os.fstat(source.fileno())
        # Model a final-component symlink pointing back at the original inode:
        # fstat alone still matches, but lstat must reject it before os.read.
        link_fields = list(metadata)
        link_fields[0] = stat.S_IFLNK | 0o777
        link_metadata = os.stat_result(link_fields, {
            "st_mtime_ns": metadata.st_mtime_ns, "st_ctime_ns": metadata.st_ctime_ns,
        })
        platform_os = ContentOS(lstat=lambda _path: link_metadata)
        with patch.object(content, "os", platform_os), patch.object(platform_os, "read") as read:
            with self.assertRaises(content.ContentRace):
                _ = entry_digest(os.fsencode(tracked), metadata)
        read.assert_not_called()

    def test_index_flags_still_refuse_hidden_content(self) -> None:
        for flag in ("assume-unchanged", "skip-worktree"):
            with self.subTest(flag=flag):
                _git(self.root, "update-index", "--" + flag, "tracked.txt")
                _ = (self.root / "tracked.txt").write_bytes(b"hidden edit")
                result = working_tree_content_fingerprint(self.root)
                self.assertFalse(result.authoritative)
                self.assertIsNone(result.fingerprint)
                _git(self.root, "update-index", "--no-" + flag, "tracked.txt")
        self.assertEqual(working_tree_content_fingerprint(self.root).state, WorkingTreeFingerprintState.DIRTY)

    def test_windows_stable_cpython_path_and_descriptor_shapes_remain_readable(self) -> None:
        from omh.quality import working_tree_fingerprint_content as content

        payload = b"raw\r\nbytes\x1aafter\x00\xff"
        tracked = self.root / "stable.exe"
        _ = tracked.write_bytes(payload)
        tracked.chmod(0o644)
        metadata = tracked.lstat()
        descriptor_values = list(metadata)
        descriptor_values[0] = stat.S_IFREG | 0o666
        descriptor_metadata = os.stat_result(descriptor_values, {
            "st_mtime_ns": metadata.st_mtime_ns, "st_ctime_ns": metadata.st_ctime_ns,
        })
        canonical = content.windows.Metadata(
            metadata.st_dev, metadata.st_ino, metadata.st_size,
            metadata.st_mtime_ns, metadata.st_ctime_ns, metadata.st_ctime_ns - 10000000,
            0x80, 1,
        )
        expected = hashlib.sha1(b"blob " + str(len(payload)).encode() + b"\0" + payload).hexdigest().encode()
        for shape in ("sharing_fallback_311", "birth_vs_change_312", "executable_extension"):
            with self.subTest(shape=shape):
                opened = False

                def path_stat(_path: bytes) -> os.stat_result:
                    values = list(descriptor_values)
                    ctime_ns = metadata.st_ctime_ns
                    if shape == "sharing_fallback_311" and opened:
                        values[1], values[2] = 0, 0
                    if shape == "birth_vs_change_312":
                        ctime_ns -= 10000000
                    if shape == "executable_extension":
                        values[0] = descriptor_metadata.st_mode | 0o111
                    return os.stat_result(values, {
                        "st_mtime_ns": metadata.st_mtime_ns, "st_ctime_ns": ctime_ns,
                    })

                def open_file(path: bytes, _flags: int = 0) -> int:
                    nonlocal opened
                    opened = True
                    return os.open(path, os.O_RDONLY | getattr(os, "O_BINARY", 0))

                platform_os = ContentOS(
                    name="nt", lstat=path_stat, open=open_file,
                    fstat=lambda _fd: descriptor_metadata,
                )
                # Real descriptors/reads, with coherent native observations in
                # place of CPython's incompatible path/fd representations.
                native = _NativeObservations(
                    stat=lambda _path: canonical, open=open_file, fstat=lambda _fd: canonical,
                )
                with patch.object(content, "os", platform_os), patch.object(content, "windows", native, create=True):
                    observed = content.overlay(self.root, [b"stable.exe"], {})
                self.assertEqual(observed, [(b"stable.exe", b"100644", expected)])

    def test_executable_extensions_match_git_before_and_after_commit(self) -> None:
        for suffix in ("exe", "bat", "cmd", "com"):
            with self.subTest(suffix=suffix):
                candidate = self.root / ("program." + suffix)
                _ = candidate.write_bytes(b"raw\r\nprogram\x1aafter")
                candidate.chmod(0o644)
                if os.name == "nt":
                    from omh.quality import working_tree_fingerprint_windows as windows

                    descriptor = windows.open(os.fsencode(candidate))
                    try:
                        self.assertEqual(windows.stat(os.fsencode(candidate)), windows.fstat(descriptor))
                        self.assertEqual(os.read(descriptor, 1024), b"raw\r\nprogram\x1aafter")
                        with self.assertRaises(PermissionError):
                            with candidate.open("r+b"):
                                pass
                        with self.assertRaises(PermissionError):
                            candidate.unlink()
                    finally:
                        os.close(descriptor)
                before = working_tree_content_fingerprint(self.root)
                self.assertTrue(before.authoritative)
                _git(self.root, "add", candidate.name)
                staged = working_tree_content_fingerprint(self.root)
                _git(self.root, "restore", "--staged", candidate.name)
                unstaged = working_tree_content_fingerprint(self.root)
                repeated = working_tree_content_fingerprint(self.root)
                self.assertTrue(unstaged.authoritative)
                self.assertTrue(repeated.authoritative)
                self.assertEqual(before.fingerprint, unstaged.fingerprint)
                self.assertEqual(before.fingerprint, repeated.fingerprint)
                self.assertEqual(candidate.read_bytes(), b"raw\r\nprogram\x1aafter")
                _git(self.root, "add", candidate.name)
                _git(self.root, "commit", "-m", "executable extension fixture")
                committed = working_tree_content_fingerprint(self.root)
                self.assertTrue(staged.authoritative)
                self.assertTrue(committed.authoritative)
                self.assertEqual(before.fingerprint, staged.fingerprint)
                self.assertEqual(before.fingerprint, committed.fingerprint)

    def test_content_normalization_attributes_are_explicitly_unsupported(self) -> None:
        for attribute in ("text", "eol=crlf", "working-tree-encoding=UTF-16LE", "ident", "crlf", "crlf=input"):
            with self.subTest(attribute=attribute):
                _ = (self.root / ".gitattributes").write_text(
                    f"tracked.txt {attribute}\n", encoding="utf-8",
                )

                result = working_tree_content_fingerprint(self.root)

                self.assertEqual(result.state, WorkingTreeFingerprintState.UNSUPPORTED)
                self.assertIsNone(result.fingerprint)

    def test_ident_and_legacy_crlf_cannot_restore_authority_or_replay_after_staging(self) -> None:
        cases = (
            ("ident", b"$Id$\n", b"$Id: replaced $\n"),
            ("crlf", b"line\n", b"line\r\n"),
            ("crlf=input", b"line\n", b"line\r\n"),
        )
        for number, (attribute, initial, changed) in enumerate(cases):
            with self.subTest(attribute=attribute):
                plain = self.root / "plain"
                _ = (self.root / ".gitattributes").write_text(f"plain {attribute}\n", encoding="utf-8")
                _ = plain.write_bytes(initial)
                _git(self.root, "add", ".")
                _git(self.root, "commit", "-m", f"normalization fixture {number}")
                before_collection = _files(self.root)
                initial_result = working_tree_content_fingerprint(self.root)
                self.assertEqual(_files(self.root), before_collection)
                _ = plain.write_bytes(changed)
                before_collection = _files(self.root)
                unstaged_result = working_tree_content_fingerprint(self.root)
                self.assertEqual(_files(self.root), before_collection)
                _git(self.root, "add", "plain")
                before_collection = _files(self.root)
                staged_result = working_tree_content_fingerprint(self.root)
                self.assertEqual(_files(self.root), before_collection)
                self.assertEqual(plain.read_bytes(), changed)
                self.assertEqual(
                    subprocess.check_output(["git", "status", "--porcelain"], cwd=self.root), b"",
                )

                state = Path(self._temporary.name) / f"goal-state-{number}"
                environment = dict(os.environ)
                environment["PYTHONPATH"] = str(Path(__file__).resolve().parents[1] / "src")
                environment["PYTHONDONTWRITEBYTECODE"] = "1"
                command = [
                    sys.executable, "-m", "omh.cli",
                    "--omh-home", str(state / ".omh"), "--hermes-home", str(state / ".hermes"),
                ]
                created = subprocess.run(
                    command + ["goal", "create", "--goal-id", "normalization", "--objective", "Verify raw bytes", "--criterion", "No false replay"],
                    cwd=self.root, env=environment, capture_output=True, text=True, check=False,
                )
                self.assertEqual(created.returncode, 0, created.stderr)
                ledger_before = _files(state)
                checkpoint = command + [
                    "goal", "checkpoint", "--goal", "normalization", "--summary", "Observed bytes",
                    "--status", "in_progress", "--mutation-id", "same-request",
                ]
                for _ in range(2):
                    refused = subprocess.run(
                        checkpoint, cwd=self.root, env=environment, capture_output=True,
                        text=True, check=False,
                    )
                    self.assertEqual(refused.returncode, 2, refused.stderr)
                    self.assertEqual(refused.stdout, "")
                    self.assertIn("unsupported", refused.stderr)
                    self.assertEqual(_files(state), ledger_before)
                for result in (initial_result, unstaged_result, staged_result):
                    self.assertEqual(result.state, WorkingTreeFingerprintState.UNSUPPORTED)
                    self.assertFalse(result.authoritative)
                    self.assertIsNone(result.fingerprint)
                    self.assertLessEqual(result.git_calls, MAX_GIT_CALLS)

    def test_ambient_common_directory_cannot_redirect_repository_identity(self) -> None:
        other = Path(self._temporary.name) / "other"
        other.mkdir()
        _init_repo(other)
        _ = (other / "tracked.txt").write_bytes(b"other repository")
        _git(other, "add", "tracked.txt")
        _git(other, "commit", "-m", "different repository fixture")
        expected = working_tree_content_fingerprint(self.root)

        with patch.dict(os.environ, {"GIT_COMMON_DIR": str(other / ".git")}):
            observed = working_tree_content_fingerprint(self.root)

        self.assertTrue(observed.authoritative)
        self.assertEqual(observed.fingerprint, expected.fingerprint)

    def test_staging_and_unstaging_do_not_change_final_content_identity(self) -> None:
        # Given: changed final bytes, including intent-to-add content.
        # When: those bytes move through the real index.
        # Then: staging state does not affect the working-tree identity.
        tracked = self.root / "tracked.txt"
        _ = tracked.write_bytes(b"final bytes")
        unstaged = working_tree_content_fingerprint(self.root).fingerprint
        _git(self.root, "add", "tracked.txt")
        staged = working_tree_content_fingerprint(self.root).fingerprint
        _git(self.root, "restore", "--staged", "tracked.txt")
        unstaged_again = working_tree_content_fingerprint(self.root).fingerprint
        _ = (self.root / "intent").write_bytes(b"intent bytes")
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
        _ = (self.root / "ignored" / "secret").write_bytes(b"ignored")
        first = working_tree_content_fingerprint(self.root)
        _ = (self.root / "ignored" / "secret").write_bytes(b"different")
        second = working_tree_content_fingerprint(self.root)

        self.assertEqual(first.fingerprint, second.fingerprint)
        self.assertEqual(_files(self.root / ".git"), git_before)
        self.assertEqual(_files(self.root)["tracked.txt"], before["tracked.txt"])

    def test_committing_unchanged_final_bytes_preserves_content_identity(self) -> None:
        _ = (self.root / "tracked.txt").write_bytes(b"final committed bytes")
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
        _ = tracked.write_bytes(b"changed bytes")
        replacement = self.root / "replacement"
        _ = replacement.write_bytes(b"replacement bytes")
        original = entry_digest

        def replace_after_hash(path: bytes, metadata: os.stat_result | windows.Metadata) -> tuple[bytes, bytes] | None:
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
        _ = helper.write_text("#!/bin/sh\n: > .helper-ran\n", encoding="utf-8")
        helper.chmod(0o700)
        _ = (self.root / ".gitattributes").write_text("tracked.txt filter=sentinel\n", encoding="utf-8")
        _git(self.root, "add", ".gitattributes", "helper")
        _git(self.root, "commit", "-m", "attributes")
        _git(self.root, "config", "core.fsmonitor", str(helper))
        _git(self.root, "config", "filter.sentinel.clean", str(helper))
        _ = (self.root / "tracked.txt").write_bytes(b"edit\x00bytes\n")

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
        dimensions = assessment["dimensions"]
        assert is_object_mapping(dimensions)
        freshness = dimensions["source_freshness"]
        assert is_object_mapping(freshness)
        self.assertEqual(freshness["status"], "unsatisfied")
        reasons = assessment["reasons"]
        assert is_object_list(reasons)
        self.assertIn("current_tree_unavailable", reasons)


if __name__ == "__main__":
    _ = unittest.main()
