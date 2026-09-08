from __future__ import annotations

import base64
from collections.abc import Callable
from copy import deepcopy
from functools import cached_property
import hashlib
import os
from pathlib import Path
import shutil
import stat
import subprocess
import tempfile
import threading
from types import SimpleNamespace
from typing import Protocol
import unittest
from unittest.mock import patch

import omh.workflows.web_qa_observation_store as observation_store

from _local_package import load_local_package
from _typing_support import override

load_local_package()

from omh.workflows.web_qa_observation_store import (
    WebQaObservationStoreError,
    import_web_qa_observation,
    prepare_web_qa_observation,
    read_web_qa_observation,
)
from test_browser_skill_promotion_plan import crt_descriptor_io
from test_web_qa_observation import good_receipt, object_list, object_value, qa_plan


PNG = base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVQIHWP4z8DwHwAFgAI/ScL1aQAAAABJRU5ErkJggg==")


def string_value(value: object) -> str:
    if not isinstance(value, str):
        raise AssertionError("test fixture must contain a string")
    return value


def capture_receipt(plan: dict[str, object], image: bytes = PNG) -> tuple[dict[str, object], str]:
    receipt = good_receipt(plan)
    digest = hashlib.sha256(image).hexdigest()
    channels = object_value(object_list(receipt["cells"])[0]["channels"])
    evidence = object_value(object_value(channels["screenshot"])["evidence"])
    evidence["capture_sha256"] = digest
    evidence["byte_size"] = len(image)
    object_value(evidence["review"])["capture_sha256"] = digest
    return receipt, digest


class ObservationStorePorts(Protocol):
    """White-box ports structurally checked against the real store module."""

    _read_json: Callable[[Path], dict[str, object]]
    _write_private: Callable[[Path, bytes], None]
    _safe_child: Callable[[Path, Path], None]


class ObservationStoreAccess(ObservationStorePorts, Protocol):
    @staticmethod
    def bind(module: ObservationStorePorts):
        return module._read_json, module._write_private, module._safe_child


read_json, _, safe_child = ObservationStoreAccess.bind(observation_store)


class WebQaObservationStoreTests(unittest.TestCase):
    @override
    def setUp(self) -> None:
        _ = self.image

    @cached_property
    def temp(self) -> tempfile.TemporaryDirectory[str]:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self._cleanup_temp, temporary)
        return temporary

    def _cleanup_temp(self, temporary: tempfile.TemporaryDirectory[str]) -> None:
        try:
            temporary.cleanup()
        finally:
            attributes: dict[str, object] = self.__dict__
            for name in ("image", "root", "temp"):
                _ = attributes.pop(name, None)

    @cached_property
    def root(self) -> Path:
        root = Path(self.temp.name) / "project"
        root.mkdir()
        _ = subprocess.run(["git", "init", "-q"], cwd=root, check=True)
        return root

    @cached_property
    def image(self) -> Path:
        image = self.root / "capture.png"
        _ = image.write_bytes(PNG)
        return image

    def test_import_preserves_png_bytes_under_crt_text_translation(self) -> None:
        plan = qa_plan()
        receipt, digest = capture_receipt(plan)
        with crt_descriptor_io(observation_store):
            imported = import_web_qa_observation(self.root, plan, receipt, {digest: self.image})
            run = self.root / ".omh" / "web-visual-qa" / "observations" / string_value(plan["run_id"])
            managed = run / string_value(object_list(imported["captures"])[0]["path"])
            self.assertEqual(managed.read_bytes(), PNG)
            self.assertEqual(object_list(imported["captures"])[0]["sha256"], hashlib.sha256(PNG).hexdigest())
            self.assertEqual(object_list(imported["captures"])[0]["byte_size"], len(PNG))
            self.assertEqual(object_value(imported["observation"])["verdict"], "PASS")
            self.image.unlink()
            with patch.object(observation_store, "_commit", side_effect=AssertionError("duplicate write")), patch(
                "os.chmod", side_effect=AssertionError("metadata write")
            ):
                self.assertEqual(import_web_qa_observation(self.root, plan, receipt, {}), imported)
                self.assertEqual(read_web_qa_observation(self.root, string_value(plan["run_id"])), imported)

    def test_metadata_ctrl_z_cannot_hide_trailing_invalid_bytes(self) -> None:
        metadata = self.root / "metadata.json"
        _ = metadata.write_bytes(b'{"value":1}\r\n\x1a{"extra":2}')
        with crt_descriptor_io(observation_store):
            with self.assertRaises(WebQaObservationStoreError):
                _ = read_json(metadata)

    def test_import_re_admits_and_is_idempotent_without_duplicate_files(self) -> None:
        plan = qa_plan()
        receipt, digest = capture_receipt(plan)
        first = import_web_qa_observation(self.root, plan, receipt, {digest: self.image})
        run = self.root / ".omh" / "web-visual-qa" / "observations" / string_value(plan["run_id"])
        metadata = run / "metadata.json"
        capture = next((run / "captures").iterdir())
        before = (metadata.stat().st_mtime_ns, capture.stat().st_mtime_ns, len(list((run / "captures").iterdir())))
        second = import_web_qa_observation(self.root, plan, receipt, {digest: self.image})
        self.assertEqual(object_value(first["observation"])["verdict"], "PASS")
        self.assertEqual(object_value(second["observation"])["verdict"], "PASS")
        self.assertEqual(before, (metadata.stat().st_mtime_ns, capture.stat().st_mtime_ns, len(list((run / "captures").iterdir()))))
        self.assertEqual(read_web_qa_observation(self.root, string_value(plan["run_id"]))["receipt"], receipt)
        self.assertEqual(prepare_web_qa_observation(_plan_request(), self.root)["completion_state"], "completed")

    def test_conflict_corruption_symlink_and_wrong_project_fail_closed(self) -> None:
        plan = qa_plan()
        receipt, digest = capture_receipt(plan)
        # A source symlink is refused before a run directory is made visible.
        linked = self.root / "linked.png"
        linked.symlink_to(self.image)
        with self.assertRaisesRegex(WebQaObservationStoreError, "symlink"):
            _ = import_web_qa_observation(self.root, plan, receipt, {digest: linked})
        _ = import_web_qa_observation(self.root, plan, receipt, {digest: self.image})
        conflict = deepcopy(receipt)
        object_value(conflict["adapter"])["session_id_digest"] = hashlib.sha256(b"other").hexdigest()
        with self.assertRaisesRegex(WebQaObservationStoreError, "conflicts"):
            _ = import_web_qa_observation(self.root, plan, conflict, {digest: self.image})
        stored = self.root / ".omh" / "web-visual-qa" / "observations" / string_value(plan["run_id"]) / "captures"
        managed = next(stored.iterdir())
        _ = managed.write_bytes(b"not-image")
        with self.assertRaisesRegex(WebQaObservationStoreError, "no longer match"):
            _ = read_web_qa_observation(self.root, string_value(plan["run_id"]))
        other = Path(self.temp.name) / "other"
        other.mkdir()
        with self.assertRaisesRegex(WebQaObservationStoreError, "Git project root"):
            _ = read_web_qa_observation(other, string_value(plan["run_id"]))

    def test_completed_import_never_mutates_permissions_or_lock_files(self) -> None:
        plan = qa_plan()
        receipt, digest = capture_receipt(plan)
        imported = import_web_qa_observation(self.root, plan, receipt, {digest: self.image})

        self.image.unlink()
        with (
            patch("os.chmod", side_effect=AssertionError("completed import attempted a metadata write")),
            patch.object(observation_store, "file_lock", side_effect=AssertionError("completed import attempted a lock write")),
            patch.object(observation_store, "_ensure_real_directory", side_effect=AssertionError("completed import attempted directory creation")),
            patch.object(observation_store, "_commit", side_effect=AssertionError("completed import attempted publication")),
        ):
            reused = import_web_qa_observation(self.root, plan, receipt, {})

        self.assertEqual(reused, imported)

    def test_mismatched_oversized_and_private_receipts_never_persist(self) -> None:
        plan = qa_plan()
        receipt, digest = capture_receipt(plan)
        _ = self.image.write_bytes(PNG + b"changed")
        with self.assertRaisesRegex(WebQaObservationStoreError, "do not match"):
            _ = import_web_qa_observation(self.root, plan, receipt, {digest: self.image})
        _ = self.image.write_bytes(PNG)
        receipt, digest = capture_receipt(plan)
        channels = object_value(object_list(receipt["cells"])[0]["channels"])
        object_value(object_value(channels["network"])["evidence"])["headers"] = "Bearer secret"
        with self.assertRaises(WebQaObservationStoreError):
            _ = import_web_qa_observation(self.root, plan, receipt, {digest: self.image})
        receipt, digest = capture_receipt(plan)
        artifact_cap = object_value(plan["limits"])["max_artifact_bytes"]
        assert type(artifact_cap) is int
        object_value(receipt["execution"])["artifact_bytes"] = artifact_cap + 1
        with self.assertRaisesRegex(WebQaObservationStoreError, "artifact cap"):
            _ = import_web_qa_observation(self.root, plan, receipt, {digest: self.image})
        observations = self.root / ".omh" / "web-visual-qa" / "observations"
        self.assertFalse((observations / string_value(plan["run_id"])).exists())

    def test_verified_bytes_are_published_when_source_changes_during_copy(self) -> None:
        plan = qa_plan()
        receipt, digest = capture_receipt(plan)
        _, write_private, _ = ObservationStoreAccess.bind(observation_store)
        changed = False

        def write_then_change(path: Path, data: bytes) -> None:
            nonlocal changed
            write_private(path, data)
            if path.suffix == ".png" and not changed:
                changed = True
                _ = self.image.write_bytes(PNG + b"changed-after-verification")

        with patch("omh.workflows.web_qa_observation_store._write_private", side_effect=write_then_change):
            imported = import_web_qa_observation(self.root, plan, receipt, {digest: self.image})
        managed = self.root / ".omh" / "web-visual-qa" / "observations" / string_value(plan["run_id"]) / string_value(object_list(imported["captures"])[0]["path"])
        self.assertTrue(changed)
        self.assertEqual(managed.read_bytes(), PNG)
        self.assertEqual(object_value(read_web_qa_observation(self.root, string_value(plan["run_id"]))["observation"])["verdict"], "PASS")

    def test_corrupt_staged_capture_is_not_published(self) -> None:
        plan = qa_plan()
        receipt, digest = capture_receipt(plan)
        _, write_private, _ = ObservationStoreAccess.bind(observation_store)

        def write_then_corrupt(path: Path, data: bytes) -> None:
            write_private(path, data)
            if path.suffix == ".png":
                _ = path.write_bytes(data + b"corrupt")

        with patch("omh.workflows.web_qa_observation_store._write_private", side_effect=write_then_corrupt):
            with self.assertRaisesRegex(WebQaObservationStoreError, "staged capture"):
                _ = import_web_qa_observation(self.root, plan, receipt, {digest: self.image})
        self.assertFalse((self.root / ".omh" / "web-visual-qa" / "observations" / string_value(plan["run_id"])).exists())

    def test_save_failure_and_metadata_bound_roll_back_without_visible_run(self) -> None:
        plan = qa_plan()
        receipt, digest = capture_receipt(plan)
        with patch("omh.workflows.web_qa_observation_store._write_private", side_effect=OSError("disk full")):
            with self.assertRaises(OSError):
                _ = import_web_qa_observation(self.root, plan, receipt, {digest: self.image})
        observations = self.root / ".omh" / "web-visual-qa" / "observations"
        self.assertFalse((observations / string_value(plan["run_id"])).exists())
        self.assertEqual([item for item in observations.iterdir() if item.name.startswith(".staging-")], [])
        with patch("omh.workflows.web_qa_observation_store.MAX_STORED_METADATA_BYTES", 64):
            with self.assertRaisesRegex(WebQaObservationStoreError, "complete observation metadata"):
                _ = import_web_qa_observation(self.root, plan, receipt, {digest: self.image})
        self.assertFalse((observations / string_value(plan["run_id"])).exists())

    def test_concurrent_same_and_conflicting_imports_have_one_terminal_result(self) -> None:
        plan = qa_plan()
        receipt, digest = capture_receipt(plan)
        barrier = threading.Barrier(2)
        results: list[dict[str, object]] = []
        errors: list[Exception] = []

        def import_receipt(candidate: dict[str, object]) -> None:
            try:
                _ = barrier.wait(timeout=5)
                results.append(import_web_qa_observation(self.root, plan, candidate, {digest: self.image}))
            except Exception as exc:  # asserted below from both workers
                errors.append(exc)

        first = threading.Thread(target=import_receipt, args=(receipt,))
        second = threading.Thread(target=import_receipt, args=(receipt,))
        first.start(); second.start(); first.join(5); second.join(5)
        self.assertFalse(first.is_alive() or second.is_alive())
        self.assertEqual(errors, [])
        self.assertEqual(len(results), 2)
        self.assertEqual(len(list((self.root / ".omh" / "web-visual-qa" / "observations" / string_value(plan["run_id"]) / "captures").iterdir())), 1)

        conflict = deepcopy(receipt)
        object_value(conflict["adapter"])["session_id_digest"] = hashlib.sha256(b"concurrent-conflict").hexdigest()
        barrier = threading.Barrier(2)
        results.clear(); errors.clear()
        first = threading.Thread(target=import_receipt, args=(receipt,))
        second = threading.Thread(target=import_receipt, args=(conflict,))
        first.start(); second.start(); first.join(5); second.join(5)
        self.assertFalse(first.is_alive() or second.is_alive())
        self.assertEqual(len(results), 1)
        self.assertEqual(len(errors), 1)
        self.assertIsInstance(errors[0], WebQaObservationStoreError)
        self.assertIn("conflicts", str(errors[0]))
        self.assertEqual(read_web_qa_observation(self.root, string_value(plan["run_id"]))["receipt"], receipt)

    def test_duplicate_metadata_keys_are_rejected(self) -> None:
        plan = qa_plan()
        receipt, digest = capture_receipt(plan)
        _ = import_web_qa_observation(self.root, plan, receipt, {digest: self.image})
        metadata = self.root / ".omh" / "web-visual-qa" / "observations" / string_value(plan["run_id"]) / "metadata.json"
        _ = metadata.write_text('{"schema_version":"x","schema_version":"y"}', encoding="utf-8")
        with self.assertRaisesRegex(WebQaObservationStoreError, "malformed"):
            _ = read_web_qa_observation(self.root, string_value(plan["run_id"]))

    def test_concurrent_parent_creation_does_not_mix_resolution_snapshots(self) -> None:
        self._import_across_publication(existing_parent=False)

    def test_concurrent_run_publication_does_not_mix_resolution_snapshots(self) -> None:
        self._import_across_publication(existing_parent=True)

    def _import_across_publication(self, *, existing_parent: bool) -> None:
        plan = qa_plan()
        receipt, digest = capture_receipt(plan)
        observations = self.root.resolve() / ".omh" / "web-visual-qa" / "observations"
        run = observations / string_value(plan["run_id"])
        if existing_parent:
            observations.mkdir(parents=True)
        admission_started = threading.Event()
        published = threading.Event()
        reader = threading.current_thread()
        results: list[dict[str, object]] = []
        errors: list[Exception] = []
        resolve = Path.resolve
        lstat = Path.lstat

        def finish_publication() -> None:
            admission_started.set()
            self.assertTrue(published.wait(5), "publisher did not finish")
            self.assertTrue(run.is_dir())

        def resolve_across_publication(path: Path, strict: bool = False) -> Path:
            if threading.current_thread() is reader and path == run and not published.is_set():
                self.assertFalse(run.exists())
                # Model inconsistent non-strict resolution snapshots, not a claim
                # about the spelling returned by the uninstrumented Windows CI.
                before = observations.with_name("missing-prefix-snapshot") / run.name
                finish_publication()
                return before
            return resolve(path, strict=strict)

        def lstat_across_publication(path: Path) -> os.stat_result:
            if threading.current_thread() is reader and path == run and not published.is_set():
                try:
                    return lstat(path)
                finally:
                    finish_publication()
            return lstat(path)

        def publish() -> None:
            try:
                if not admission_started.wait(5):
                    raise AssertionError("reader did not start path admission")
                results.append(import_web_qa_observation(self.root, plan, receipt, {digest: self.image}))
            except Exception as exc:  # surfaced after joining the publisher
                errors.append(exc)
            finally:
                published.set()

        worker = threading.Thread(target=publish)
        with patch.object(Path, "resolve", resolve_across_publication), patch.object(Path, "lstat", lstat_across_publication):
            worker.start()
            try:
                reused = import_web_qa_observation(self.root, plan, receipt, {digest: self.image})
            finally:
                worker.join(5)
        self.assertFalse(worker.is_alive())
        self.assertEqual(errors, [])
        self.assertEqual(results, [reused])
        self.assertEqual(read_web_qa_observation(self.root, string_value(plan["run_id"])), reused)
        self.assertEqual(len(list((run / "captures").iterdir())), 1)
        self.assertFalse(any(path.name.startswith(".staging-") for path in observations.iterdir()))

    def test_safe_child_refuses_lexical_traversal_and_propagates_io_errors(self) -> None:
        for child in (self.root.parent / "outside", self.root / ".." / "outside", self.root / "inside" / ".." / "capture.png"):
            with self.subTest(child=child), self.assertRaises(WebQaObservationStoreError):
                safe_child(self.root, child)
        with patch.object(Path, "lstat", side_effect=PermissionError("denied")):
            with self.assertRaises(PermissionError):
                safe_child(self.root, self.image)

    def test_managed_directory_links_are_refused_before_import_or_reuse(self) -> None:
        plan = qa_plan()
        receipt, digest = capture_receipt(plan)
        outside = Path(self.temp.name) / "outside"
        outside.mkdir()
        kinds = ("symlink", "junction") if os.name == "nt" else ("symlink",)
        for kind in kinds:
            for relative in (".omh", ".omh/web-visual-qa", ".omh/web-visual-qa/observations"):
                with self.subTest(kind=kind, relative=relative):
                    link = self.root / relative
                    link.parent.mkdir(parents=True, exist_ok=True)
                    self._directory_link(outside, link, kind)
                    try:
                        with self.assertRaises(WebQaObservationStoreError):
                            _ = import_web_qa_observation(self.root, plan, receipt, {digest: self.image})
                        with self.assertRaises(WebQaObservationStoreError):
                            _ = prepare_web_qa_observation(_plan_request(), self.root)
                        self.assertEqual(list(outside.iterdir()), [])
                    finally:
                        link.rmdir() if kind == "junction" else link.unlink()
            imported = import_web_qa_observation(self.root, plan, receipt, {digest: self.image})
            run = self.root / ".omh/web-visual-qa/observations" / string_value(plan["run_id"])
            for path in (run / "captures", run):
                with self.subTest(kind=kind, path=path.name):
                    moved = outside / path.name
                    _ = path.rename(moved)
                    self._directory_link(moved, path, kind)
                    try:
                        with self.assertRaises(WebQaObservationStoreError):
                            _ = read_web_qa_observation(self.root, string_value(plan["run_id"]))
                        with self.assertRaises(WebQaObservationStoreError):
                            _ = import_web_qa_observation(self.root, plan, receipt, {})
                    finally:
                        path.rmdir() if kind == "junction" else path.unlink()
                        _ = moved.rename(path)
            self.assertEqual(read_web_qa_observation(self.root, string_value(plan["run_id"])), imported)
            shutil.rmtree(self.root / ".omh")

    def test_reparse_attributes_are_refused_even_without_symlink_mode(self) -> None:
        lstat = Path.lstat
        for target in (self.root, self.image):
            with self.subTest(target=target):
                def reparse_stat(path: Path) -> os.stat_result | SimpleNamespace:
                    info = lstat(path)
                    if path == target:
                        return SimpleNamespace(st_mode=info.st_mode, st_file_attributes=stat.FILE_ATTRIBUTE_REPARSE_POINT)
                    return info

                with patch.object(Path, "lstat", reparse_stat):
                    with self.assertRaises(WebQaObservationStoreError):
                        safe_child(self.root, self.image)

    def test_managed_file_symlinks_are_refused_on_readmission(self) -> None:
        plan = qa_plan()
        receipt, digest = capture_receipt(plan)
        imported = import_web_qa_observation(self.root, plan, receipt, {digest: self.image})
        run = self.root / ".omh/web-visual-qa/observations" / string_value(plan["run_id"])
        for path in (run / "metadata.json", run / string_value(object_list(imported["captures"])[0]["path"])):
            with self.subTest(path=path.name):
                moved = Path(self.temp.name) / path.name
                _ = path.rename(moved)
                path.symlink_to(moved)
                try:
                    with self.assertRaises(WebQaObservationStoreError):
                        _ = read_web_qa_observation(self.root, string_value(plan["run_id"]))
                    with self.assertRaises(WebQaObservationStoreError):
                        _ = import_web_qa_observation(self.root, plan, receipt, {})
                finally:
                    path.unlink()
                    _ = moved.rename(path)

    def _directory_link(self, target: Path, link: Path, kind: str) -> None:
        if kind == "junction":
            import _winapi

            _winapi.CreateJunction(str(target), str(link))
            self.assertTrue(link.lstat().st_file_attributes & stat.FILE_ATTRIBUTE_REPARSE_POINT)
            self.assertFalse(link.is_symlink())
        else:
            link.symlink_to(target, target_is_directory=True)
            self.assertTrue(link.is_symlink())

    def test_copied_completed_run_cannot_change_project_identity(self) -> None:
        plan = qa_plan()
        receipt, digest = capture_receipt(plan)
        _ = import_web_qa_observation(self.root, plan, receipt, {digest: self.image})
        other = Path(self.temp.name) / "other-project"
        other.mkdir()
        _ = subprocess.run(["git", "init", "-q"], cwd=other, check=True)
        _ = shutil.copytree(self.root / ".omh", other / ".omh")
        with self.assertRaisesRegex(WebQaObservationStoreError, "another observed Git root"):
            _ = read_web_qa_observation(other, string_value(plan["run_id"]))


class WebQaObservationStoreFixtureTests(unittest.TestCase):
    def test_resources_are_lazy_cached_and_invalidated_after_cleanup(self) -> None:
        with patch.object(tempfile, "TemporaryDirectory", side_effect=AssertionError("eager allocation")):
            case = WebQaObservationStoreTests("test_metadata_ctrl_z_cannot_hide_trailing_invalid_bytes")
        for _ in range(2):
            case.setUp()
            try:
                temporary, root, image = case.temp, case.root, case.image
                self.assertIs(case.temp, temporary)
                self.assertIs(case.root, root)
                self.assertIs(case.image, image)
                self.assertEqual(image.read_bytes(), PNG)
                self.assertTrue((root / ".git").is_dir())
            finally:
                case.doCleanups()
            self.assertFalse(Path(temporary.name).exists())
            self.assertTrue({"temp", "root", "image"}.isdisjoint(case.__dict__))

    def test_failed_setup_cleans_partial_resources_and_allows_case_reuse(self) -> None:
        for target in ("pathlib.Path.mkdir", "subprocess.run", "pathlib.Path.write_bytes"):
            with self.subTest(target=target), tempfile.TemporaryDirectory() as parent, patch.object(tempfile, "tempdir", parent):
                case = WebQaObservationStoreTests("test_metadata_ctrl_z_cannot_hide_trailing_invalid_bytes")
                failed = unittest.TestResult()
                with patch(target, side_effect=OSError("setup failed")):
                    _ = case.run(failed)
                self.assertEqual(len(failed.errors), 1)
                self.assertIn("OSError: setup failed", failed.errors[0][1])
                self.assertEqual(list(Path(parent).iterdir()), [])
                self.assertTrue({"temp", "root", "image"}.isdisjoint(case.__dict__))
                retried = unittest.TestResult()
                _ = case.run(retried)
                self.assertTrue(retried.wasSuccessful(), retried.errors)
                self.assertEqual(list(Path(parent).iterdir()), [])
                self.assertTrue({"temp", "root", "image"}.isdisjoint(case.__dict__))


def _plan_request() -> dict[str, object]:
    """The plan fixture has normalized data; prepare's public input is raw.

    This minimal conversion is intentionally only used to prove plan does not
    write and sees an existing terminal identity; its canonical result is the
    same plan request fixture used by the admission tests.
    """
    from test_web_qa_observation_plan import observation_request

    request = observation_request()
    condition = object_value(request["condition"])
    condition["routes"] = object_list(condition["routes"])[:1]
    condition["viewports"] = object_list(condition["viewports"])[:1]
    condition["browsers"] = object_list(condition["browsers"])[:1]
    return request


if __name__ == "__main__":
    _ = unittest.main()
