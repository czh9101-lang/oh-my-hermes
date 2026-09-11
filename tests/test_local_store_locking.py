from __future__ import annotations

import errno
import os
import threading
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock

from _local_package import load_local_package
from _platform_support import HAS_FCNTL, requires_enforced_file_lock

load_local_package()
from omh.local_store import (
    FileLockTimeout,
    atomic_write_text,
    file_lock,
    locked_json_update,
    read_json_object,
)
from omh.system import local_store

class FakeMsvcrt:
    """Stand-in for the Windows locking API, exercised on every platform.

    Real ``msvcrt.locking`` locks a byte region per open handle, so a second
    handle on the same file - even inside the same process - fails with
    ``EACCES``. The fake keys its registry by (device, inode) so two handles on
    one file collide exactly that way, which is the property ``file_lock``
    relies on for mutual exclusion.
    """

    LK_NBLCK = 2
    LK_UNLCK = 0

    def __init__(self) -> None:
        self._guard = threading.Lock()
        self._held: dict[tuple[int, int], int] = {}
        self.modes: list[str] = []

    def locking(self, fd: int, mode: int, nbytes: int) -> None:
        info = os.fstat(fd)
        key = (info.st_dev, info.st_ino)
        with self._guard:
            if mode == self.LK_NBLCK:
                if key in self._held:
                    raise OSError(errno.EACCES, "region already locked")
                self._held[key] = fd
                self.modes.append("lock")
                return
            if mode == self.LK_UNLCK:
                if self._held.get(key) == fd:
                    del self._held[key]
                self.modes.append("unlock")
                return
            raise ValueError(f"unsupported msvcrt mode: {mode}")


class LocalStoreLockingTests(unittest.TestCase):
    @requires_enforced_file_lock
    def test_concurrent_locked_updates_do_not_lose_writes(self) -> None:
        worker_count = 24
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "state.json"
            barrier = threading.Barrier(worker_count)

            def worker(index: int) -> None:
                barrier.wait()
                locked_json_update(path, lambda current: {**current, f"key-{index}": index}, default={})

            threads = [threading.Thread(target=worker, args=(index,)) for index in range(worker_count)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join()

            final = read_json_object(path)
            self.assertIsNotNone(final)
            assert final is not None
            for index in range(worker_count):
                self.assertEqual(final.get(f"key-{index}"), index)

    def test_concurrent_atomic_writes_do_not_collide_on_temp_file(self) -> None:
        writer_count = 24
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "target.json"
            barrier = threading.Barrier(writer_count)
            errors: list[BaseException] = []

            def writer(index: int) -> None:
                barrier.wait()
                try:
                    atomic_write_text(path, f'{{"writer": {index}}}\n')
                except BaseException as exc:  # noqa: BLE001
                    errors.append(exc)

            threads = [threading.Thread(target=writer, args=(index,)) for index in range(writer_count)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join()

            self.assertEqual(errors, [])
            self.assertTrue(path.exists())
            leftover = list(Path(tmp).glob(".target.json.*.tmp"))
            self.assertEqual(leftover, [])

    @requires_enforced_file_lock
    def test_file_lock_times_out_when_already_held(self) -> None:
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "contended.json"
            with file_lock(path, timeout_seconds=5.0) as acquired:
                self.assertTrue(acquired["locked"])
                with self.assertRaises(FileLockTimeout):
                    with file_lock(path, timeout_seconds=0.2, poll_interval=0.01):
                        pass

    @unittest.skipUnless(HAS_FCNTL, "fcntl advisory locking is POSIX-only")
    def test_fcntl_lock_reports_itself_as_enforced(self) -> None:
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "state.json"
            with file_lock(path) as acquired:
                self.assertTrue(acquired["locked"])
                self.assertTrue(acquired["enforced"])
                self.assertEqual(acquired["mechanism"], "fcntl")


class WindowsFileLockTests(unittest.TestCase):
    """The Windows lock path, exercised through a fake on every platform.

    windows-latest is an enforcing CI target, so the msvcrt branch cannot be
    left to run only there.
    """

    def test_msvcrt_takes_the_lock_when_fcntl_is_unavailable(self) -> None:
        fake = FakeMsvcrt()
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "state.json"
            with (
                mock.patch("omh.system.local_store.fcntl", None),
                mock.patch("omh.system.local_store.msvcrt", fake),
            ):
                with file_lock(path) as acquired:
                    self.assertTrue(acquired["locked"])
                    self.assertTrue(acquired["enforced"])
                    self.assertEqual(acquired["mechanism"], "msvcrt")
                    self.assertEqual(fake.modes, ["lock"])

            self.assertEqual(fake.modes, ["lock", "unlock"])
            self.assertTrue((Path(tmp) / ".state.json.lock").exists())

    def test_msvcrt_lock_is_exclusive_and_times_out(self) -> None:
        fake = FakeMsvcrt()
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "contended.json"
            with (
                mock.patch("omh.system.local_store.fcntl", None),
                mock.patch("omh.system.local_store.msvcrt", fake),
            ):
                with file_lock(path, timeout_seconds=5.0) as acquired:
                    self.assertTrue(acquired["enforced"])
                    with self.assertRaises(FileLockTimeout):
                        with file_lock(path, timeout_seconds=0.2, poll_interval=0.01):
                            pass

    def test_msvcrt_lock_serializes_concurrent_updates(self) -> None:
        fake = FakeMsvcrt()
        worker_count = 16
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "state.json"
            barrier = threading.Barrier(worker_count)

            def worker(index: int) -> None:
                barrier.wait()
                locked_json_update(
                    path,
                    lambda current: {**current, f"key-{index}": index},
                    default={},
                    timeout_seconds=30.0,
                )

            with (
                mock.patch("omh.system.local_store.fcntl", None),
                mock.patch("omh.system.local_store.msvcrt", fake),
            ):
                threads = [threading.Thread(target=worker, args=(index,)) for index in range(worker_count)]
                for thread in threads:
                    thread.start()
                for thread in threads:
                    thread.join()

            final = read_json_object(path)
            assert final is not None
            for index in range(worker_count):
                self.assertEqual(final.get(f"key-{index}"), index)

    def test_lock_reports_not_enforced_without_fcntl_or_msvcrt(self) -> None:
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "state.json"
            with (
                mock.patch("omh.system.local_store.fcntl", None),
                mock.patch("omh.system.local_store.msvcrt", None),
            ):
                with file_lock(path, timeout_seconds=0.2) as acquired:
                    self.assertFalse(acquired["locked"])
                    self.assertFalse(acquired["enforced"])
                    self.assertEqual(acquired["mechanism"], "none")
                    self.assertEqual(acquired["reason"], "no_os_file_lock")

            # A lock that was never taken leaves no sidecar behind either.
            self.assertFalse((Path(tmp) / ".state.json.lock").exists())

    def test_preparing_the_lock_sidecar_survives_a_windows_sharing_denial(self) -> None:
        """A denied chmod must not abandon the lock the caller is about to take.

        `file_lock` prepares its sidecar through `ensure_file` *before* it takes
        the lock, so every waiter runs that chmod at once against a file the
        other waiters already hold open. Windows denies chmod for exactly that
        window -- the same one `atomic_write_text` was taught to retry -- and
        POSIX never does, so the denial only ever escaped there.
        """
        attempts: list[int] = []
        real_chmod = Path.chmod

        def denied_twice(self_path: Path, mode: int, *args: object, **kwargs: object) -> None:
            if self_path.name.endswith(".lock"):
                attempts.append(mode)
                if len(attempts) <= 2:
                    raise PermissionError(32, "used by another process")
            real_chmod(self_path, mode, *args, **kwargs)

        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "state.json"
            with (
                mock.patch.object(local_store.os, "name", "nt"),
                mock.patch.object(Path, "chmod", denied_twice),
            ):
                with file_lock(path, private=True) as acquired:
                    self.assertTrue(acquired["enforced"])

            self.assertEqual(len(attempts), 3)

    def test_a_posix_permission_error_on_chmod_is_still_an_error(self) -> None:
        """The retry is a Windows branch, not a swallow: POSIX must still fail."""
        real_chmod = Path.chmod

        def denied(self_path: Path, mode: int, *args: object, **kwargs: object) -> None:
            if self_path.name.endswith(".lock"):
                raise PermissionError(13, "permission denied")
            real_chmod(self_path, mode, *args, **kwargs)

        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "state.json"
            with (
                mock.patch.object(local_store.os, "name", "posix"),
                mock.patch.object(Path, "chmod", denied),
                self.assertRaises(PermissionError),
            ):
                with file_lock(path, private=True):
                    pass


if __name__ == "__main__":
    unittest.main()
