from __future__ import annotations

import base64
from copy import deepcopy
import hashlib
import os
from pathlib import Path
import shutil
import stat
import subprocess
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import omh.workflows.web_qa_observation_store as observation_store

from _local_package import load_local_package

load_local_package()

from omh.workflows.web_qa_observation_store import (
    WebQaObservationStoreError,
    import_web_qa_observation,
    prepare_web_qa_observation,
    read_web_qa_observation,
)
from test_browser_skill_promotion_plan import _crt_descriptor_io
from test_web_qa_observation import good_receipt, qa_plan


PNG = base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVQIHWP4z8DwHwAFgAI/ScL1aQAAAABJRU5ErkJggg==")


def capture_receipt(plan: dict[str, object], image: bytes = PNG) -> tuple[dict[str, object], str]:
    receipt = good_receipt(plan)
    digest = hashlib.sha256(image).hexdigest()
    evidence = receipt["cells"][0]["channels"]["screenshot"]["evidence"]
    evidence["capture_sha256"] = digest
    evidence["byte_size"] = len(image)
    evidence["review"]["capture_sha256"] = digest
    return receipt, digest


class WebQaObservationStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name) / "project"
        self.root.mkdir()
        subprocess.run(["git", "init", "-q"], cwd=self.root, check=True)
        self.image = self.root / "capture.png"
        self.image.write_bytes(PNG)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_import_preserves_png_bytes_under_crt_text_translation(self) -> None:
        plan = qa_plan()
        receipt, digest = capture_receipt(plan)
        with _crt_descriptor_io(observation_store):
            imported = import_web_qa_observation(self.root, plan, receipt, {digest: self.image})
            run = self.root / ".omh" / "web-visual-qa" / "observations" / plan["run_id"]
            managed = run / imported["captures"][0]["path"]
            self.assertEqual(managed.read_bytes(), PNG)
            self.assertEqual(imported["captures"][0]["sha256"], hashlib.sha256(PNG).hexdigest())
            self.assertEqual(imported["captures"][0]["byte_size"], len(PNG))
            self.assertEqual(imported["observation"]["verdict"], "PASS")
            self.image.unlink()
            with patch.object(observation_store, "_commit", side_effect=AssertionError("duplicate write")), patch(
                "os.chmod", side_effect=AssertionError("metadata write")
            ):
                self.assertEqual(import_web_qa_observation(self.root, plan, receipt, {}), imported)
                self.assertEqual(read_web_qa_observation(self.root, plan["run_id"]), imported)

    def test_metadata_ctrl_z_cannot_hide_trailing_invalid_bytes(self) -> None:
        metadata = self.root / "metadata.json"
        metadata.write_bytes(b'{"value":1}\r\n\x1a{"extra":2}')
        with _crt_descriptor_io(observation_store):
            with self.assertRaises(WebQaObservationStoreError):
                observation_store._read_json(metadata)

    def test_import_re_admits_and_is_idempotent_without_duplicate_files(self) -> None:
        plan = qa_plan()
        receipt, digest = capture_receipt(plan)
        first = import_web_qa_observation(self.root, plan, receipt, {digest: self.image})
        run = self.root / ".omh" / "web-visual-qa" / "observations" / plan["run_id"]
        metadata = run / "metadata.json"
        capture = next((run / "captures").iterdir())
        before = (metadata.stat().st_mtime_ns, capture.stat().st_mtime_ns, len(list((run / "captures").iterdir())))
        second = import_web_qa_observation(self.root, plan, receipt, {digest: self.image})
        self.assertEqual(first["observation"]["verdict"], "PASS")
        self.assertEqual(second["observation"]["verdict"], "PASS")
        self.assertEqual(before, (metadata.stat().st_mtime_ns, capture.stat().st_mtime_ns, len(list((run / "captures").iterdir()))))
        self.assertEqual(read_web_qa_observation(self.root, plan["run_id"])["receipt"], receipt)
        self.assertEqual(prepare_web_qa_observation(_plan_request(plan), self.root)["completion_state"], "completed")

    def test_conflict_corruption_symlink_and_wrong_project_fail_closed(self) -> None:
        plan = qa_plan()
        receipt, digest = capture_receipt(plan)
        # A source symlink is refused before a run directory is made visible.
        linked = self.root / "linked.png"
        linked.symlink_to(self.image)
        with self.assertRaisesRegex(WebQaObservationStoreError, "symlink"):
            import_web_qa_observation(self.root, plan, receipt, {digest: linked})
        import_web_qa_observation(self.root, plan, receipt, {digest: self.image})
        conflict = deepcopy(receipt)
        conflict["adapter"]["session_id_digest"] = hashlib.sha256(b"other").hexdigest()
        with self.assertRaisesRegex(WebQaObservationStoreError, "conflicts"):
            import_web_qa_observation(self.root, plan, conflict, {digest: self.image})
        stored = self.root / ".omh" / "web-visual-qa" / "observations" / plan["run_id"] / "captures"
        managed = next(stored.iterdir())
        managed.write_bytes(b"not-image")
        with self.assertRaisesRegex(WebQaObservationStoreError, "no longer match"):
            read_web_qa_observation(self.root, plan["run_id"])
        other = Path(self.temp.name) / "other"
        other.mkdir()
        with self.assertRaisesRegex(WebQaObservationStoreError, "Git project root"):
            read_web_qa_observation(other, plan["run_id"])

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
        self.image.write_bytes(PNG + b"changed")
        with self.assertRaisesRegex(WebQaObservationStoreError, "do not match"):
            import_web_qa_observation(self.root, plan, receipt, {digest: self.image})
        self.image.write_bytes(PNG)
        receipt, digest = capture_receipt(plan)
        receipt["cells"][0]["channels"]["network"]["evidence"]["headers"] = "Bearer secret"
        with self.assertRaises(WebQaObservationStoreError):
            import_web_qa_observation(self.root, plan, receipt, {digest: self.image})
        receipt, digest = capture_receipt(plan)
        receipt["execution"]["artifact_bytes"] = plan["limits"]["max_artifact_bytes"] + 1
        with self.assertRaisesRegex(WebQaObservationStoreError, "artifact cap"):
            import_web_qa_observation(self.root, plan, receipt, {digest: self.image})
        observations = self.root / ".omh" / "web-visual-qa" / "observations"
        self.assertFalse((observations / plan["run_id"]).exists())

    def test_verified_bytes_are_published_when_source_changes_during_copy(self) -> None:
        plan = qa_plan()
        receipt, digest = capture_receipt(plan)
        write_private = observation_store._write_private
        changed = False

        def write_then_change(path: Path, data: bytes) -> None:
            nonlocal changed
            write_private(path, data)
            if path.suffix == ".png" and not changed:
                changed = True
                self.image.write_bytes(PNG + b"changed-after-verification")

        with patch("omh.workflows.web_qa_observation_store._write_private", side_effect=write_then_change):
            imported = import_web_qa_observation(self.root, plan, receipt, {digest: self.image})
        managed = self.root / ".omh" / "web-visual-qa" / "observations" / plan["run_id"] / imported["captures"][0]["path"]
        self.assertTrue(changed)
        self.assertEqual(managed.read_bytes(), PNG)
        self.assertEqual(read_web_qa_observation(self.root, plan["run_id"])["observation"]["verdict"], "PASS")

    def test_corrupt_staged_capture_is_not_published(self) -> None:
        plan = qa_plan()
        receipt, digest = capture_receipt(plan)
        write_private = observation_store._write_private

        def write_then_corrupt(path: Path, data: bytes) -> None:
            write_private(path, data)
            if path.suffix == ".png":
                path.write_bytes(data + b"corrupt")

        with patch("omh.workflows.web_qa_observation_store._write_private", side_effect=write_then_corrupt):
            with self.assertRaisesRegex(WebQaObservationStoreError, "staged capture"):
                import_web_qa_observation(self.root, plan, receipt, {digest: self.image})
        self.assertFalse((self.root / ".omh" / "web-visual-qa" / "observations" / plan["run_id"]).exists())

    def test_save_failure_and_metadata_bound_roll_back_without_visible_run(self) -> None:
        plan = qa_plan()
        receipt, digest = capture_receipt(plan)
        with patch("omh.workflows.web_qa_observation_store._write_private", side_effect=OSError("disk full")):
            with self.assertRaises(OSError):
                import_web_qa_observation(self.root, plan, receipt, {digest: self.image})
        observations = self.root / ".omh" / "web-visual-qa" / "observations"
        self.assertFalse((observations / plan["run_id"]).exists())
        self.assertEqual([item for item in observations.iterdir() if item.name.startswith(".staging-")], [])
        with patch("omh.workflows.web_qa_observation_store.MAX_STORED_METADATA_BYTES", 64):
            with self.assertRaisesRegex(WebQaObservationStoreError, "complete observation metadata"):
                import_web_qa_observation(self.root, plan, receipt, {digest: self.image})
        self.assertFalse((observations / plan["run_id"]).exists())

    def test_concurrent_same_and_conflicting_imports_have_one_terminal_result(self) -> None:
        plan = qa_plan()
        receipt, digest = capture_receipt(plan)
        barrier = threading.Barrier(2)
        results: list[dict[str, object]] = []
        errors: list[Exception] = []

        def import_receipt(candidate: dict[str, object]) -> None:
            try:
                barrier.wait(timeout=5)
                results.append(import_web_qa_observation(self.root, plan, candidate, {digest: self.image}))
            except Exception as exc:  # asserted below from both workers
                errors.append(exc)

        first = threading.Thread(target=import_receipt, args=(receipt,))
        second = threading.Thread(target=import_receipt, args=(receipt,))
        first.start(); second.start(); first.join(5); second.join(5)
        self.assertFalse(first.is_alive() or second.is_alive())
        self.assertEqual(errors, [])
        self.assertEqual(len(results), 2)
        self.assertEqual(len(list((self.root / ".omh" / "web-visual-qa" / "observations" / plan["run_id"] / "captures").iterdir())), 1)

        conflict = deepcopy(receipt)
        conflict["adapter"]["session_id_digest"] = hashlib.sha256(b"concurrent-conflict").hexdigest()
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
        self.assertEqual(read_web_qa_observation(self.root, plan["run_id"])["receipt"], receipt)

    def test_duplicate_metadata_keys_are_rejected(self) -> None:
        plan = qa_plan()
        receipt, digest = capture_receipt(plan)
        import_web_qa_observation(self.root, plan, receipt, {digest: self.image})
        metadata = self.root / ".omh" / "web-visual-qa" / "observations" / plan["run_id"] / "metadata.json"
        metadata.write_text('{"schema_version":"x","schema_version":"y"}', encoding="utf-8")
        with self.assertRaisesRegex(WebQaObservationStoreError, "malformed"):
            read_web_qa_observation(self.root, plan["run_id"])

    def test_concurrent_parent_creation_does_not_mix_resolution_snapshots(self) -> None:
        self._import_across_publication(existing_parent=False)

    def test_concurrent_run_publication_does_not_mix_resolution_snapshots(self) -> None:
        self._import_across_publication(existing_parent=True)

    def _import_across_publication(self, *, existing_parent: bool) -> None:
        plan = qa_plan()
        receipt, digest = capture_receipt(plan)
        observations = self.root.resolve() / ".omh" / "web-visual-qa" / "observations"
        run = observations / str(plan["run_id"])
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

        def lstat_across_publication(path: Path):
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
        self.assertEqual(read_web_qa_observation(self.root, plan["run_id"]), reused)
        self.assertEqual(len(list((run / "captures").iterdir())), 1)
        self.assertFalse(any(path.name.startswith(".staging-") for path in observations.iterdir()))

    def test_safe_child_refuses_lexical_traversal_and_propagates_io_errors(self) -> None:
        for child in (self.root.parent / "outside", self.root / ".." / "outside", self.root / "inside" / ".." / "capture.png"):
            with self.subTest(child=child), self.assertRaises(WebQaObservationStoreError):
                observation_store._safe_child(self.root, child)
        with patch.object(Path, "lstat", side_effect=PermissionError("denied")):
            with self.assertRaises(PermissionError):
                observation_store._safe_child(self.root, self.image)

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
                            import_web_qa_observation(self.root, plan, receipt, {digest: self.image})
                        with self.assertRaises(WebQaObservationStoreError):
                            prepare_web_qa_observation(_plan_request(plan), self.root)
                        self.assertEqual(list(outside.iterdir()), [])
                    finally:
                        link.rmdir() if kind == "junction" else link.unlink()
            imported = import_web_qa_observation(self.root, plan, receipt, {digest: self.image})
            run = self.root / ".omh/web-visual-qa/observations" / str(plan["run_id"])
            for path in (run / "captures", run):
                with self.subTest(kind=kind, path=path.name):
                    moved = outside / path.name
                    path.rename(moved)
                    self._directory_link(moved, path, kind)
                    try:
                        with self.assertRaises(WebQaObservationStoreError):
                            read_web_qa_observation(self.root, plan["run_id"])
                        with self.assertRaises(WebQaObservationStoreError):
                            import_web_qa_observation(self.root, plan, receipt, {})
                    finally:
                        path.rmdir() if kind == "junction" else path.unlink()
                        moved.rename(path)
            self.assertEqual(read_web_qa_observation(self.root, plan["run_id"]), imported)
            shutil.rmtree(self.root / ".omh")

    def test_reparse_attributes_are_refused_even_without_symlink_mode(self) -> None:
        lstat = Path.lstat
        for target in (self.root, self.image):
            with self.subTest(target=target):
                def reparse_stat(path: Path):
                    info = lstat(path)
                    if path == target:
                        return SimpleNamespace(st_mode=info.st_mode, st_file_attributes=stat.FILE_ATTRIBUTE_REPARSE_POINT)
                    return info

                with patch.object(Path, "lstat", reparse_stat):
                    with self.assertRaises(WebQaObservationStoreError):
                        observation_store._safe_child(self.root, self.image)

    def test_managed_file_symlinks_are_refused_on_readmission(self) -> None:
        plan = qa_plan()
        receipt, digest = capture_receipt(plan)
        imported = import_web_qa_observation(self.root, plan, receipt, {digest: self.image})
        run = self.root / ".omh/web-visual-qa/observations" / str(plan["run_id"])
        for path in (run / "metadata.json", run / imported["captures"][0]["path"]):
            with self.subTest(path=path.name):
                moved = Path(self.temp.name) / path.name
                path.rename(moved)
                path.symlink_to(moved)
                try:
                    with self.assertRaises(WebQaObservationStoreError):
                        read_web_qa_observation(self.root, plan["run_id"])
                    with self.assertRaises(WebQaObservationStoreError):
                        import_web_qa_observation(self.root, plan, receipt, {})
                finally:
                    path.unlink()
                    moved.rename(path)

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
        import_web_qa_observation(self.root, plan, receipt, {digest: self.image})
        other = Path(self.temp.name) / "other-project"
        other.mkdir()
        subprocess.run(["git", "init", "-q"], cwd=other, check=True)
        shutil.copytree(self.root / ".omh", other / ".omh")
        with self.assertRaisesRegex(WebQaObservationStoreError, "another observed Git root"):
            read_web_qa_observation(other, plan["run_id"])


def _plan_request(plan: dict[str, object]) -> dict[str, object]:
    """The plan fixture has normalized data; prepare's public input is raw.

    This minimal conversion is intentionally only used to prove plan does not
    write and sees an existing terminal identity; its canonical result is the
    same plan request fixture used by the admission tests.
    """
    from test_web_qa_observation_plan import observation_request

    request = observation_request()
    request["condition"]["routes"] = request["condition"]["routes"][:1]
    request["condition"]["viewports"] = request["condition"]["viewports"][:1]
    request["condition"]["browsers"] = request["condition"]["browsers"][:1]
    return request


if __name__ == "__main__":
    unittest.main()
