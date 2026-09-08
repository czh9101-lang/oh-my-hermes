from __future__ import annotations

from abc import ABC
import argparse
import errno
import hashlib
import json
import os
import stat
from collections.abc import Callable, Generator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from typing import Protocol
import subprocess
import unittest
from unittest.mock import patch

from _local_package import load_local_package
from _typing_support import override
load_local_package()

import omh.workflows.browser_skill_promotion as lifecycle
import omh.workflows.browser_skill_promotion_plan as promotion_plan
from test_browser_skill_promotion_plan import (
    crt_descriptor_io as _crt_descriptor_io,
    digest_value as _digest_value,
    json_object,
    mapping as _mapping,
    project as _project,
    text as _text,
)

from omh.workflows.browser_skill_promotion import (
    BrowserSkillPromotionError, approve_browser_skill_lifecycle,
    approve_browser_skill_removal, approve_browser_skill_rollback,
    browser_skill_promotion_status, promote_approved_browser_skill,
    review_browser_skill_lifecycle, review_browser_skill_removal,
    review_browser_skill_rollback,
)
from omh.workflows.browser_skill_promotion_approval import BrowserSkillPromotionApprovalError, NativePromotionPreflight, NativeWritePolicy, PromotionNativeHost
from omh.workflows.browser_workflow_learning import JsonObject
from omh.workflows.browser_workflow_learning_store import approve_browser_workflow_trace, replay_stored_browser_workflow_trace, resolved_browser_workflow_promotion_reference, write_browser_workflow_trace


class _LifecyclePort(Protocol):
    """Exact private I/O and staging probes; callers still execute real producers."""

    def _fsync_directory(self, path: Path) -> None: ...
    def _write_exact(self, path: Path, text: str, *, replace: bool, private: bool = False) -> None: ...
    def _read_bytes(self, path: Path) -> bytes: ...
    def _managed_inventory(self, root: Path, skill_name: str, *, allow_unindexed_generation: str | None = None) -> dict[str, str]: ...
    def _stage_and_verify(self, root: Path, skill_name: str, plan: Mapping[str, object]) -> None: ...
    def _state_root(self, root: Path, skill_name: str) -> Path: ...


class _LifecycleProbe(_LifecyclePort, ABC):
    @staticmethod
    def fsync_directory(module: _LifecyclePort, path: Path) -> None:
        module._fsync_directory(path)

    @staticmethod
    def write_exact(module: _LifecyclePort, path: Path, text: str, *, replace: bool) -> None:
        module._write_exact(path, text, replace=replace)

    @staticmethod
    def read_bytes(module: _LifecyclePort, path: Path) -> bytes:
        return module._read_bytes(path)

    @staticmethod
    def managed_inventory(module: _LifecyclePort, root: Path, skill_name: str) -> dict[str, str]:
        return module._managed_inventory(root, skill_name)

    @staticmethod
    def staging(module: _LifecyclePort) -> Callable[[Path, str, Mapping[str, object]], None]:
        return module._stage_and_verify

    @staticmethod
    def state_root(module: _LifecyclePort, root: Path, skill_name: str) -> Path:
        return module._state_root(root, skill_name)


class _CommandNamespace(argparse.Namespace):
    func: object = None


@dataclass
class Host(PromotionNativeHost):
    calls: int = 0
    required: bool = False
    @override
    def inspect(self, project_root: Path, package: Mapping[str, str]) -> NativePromotionPreflight:
        package_digest = hashlib.sha256(b"".join(
            name.encode("utf-8") + b"\0" + package[name].encode("utf-8")
            for name in sorted(package)
        )).hexdigest()
        self.calls += 1
        policy = NativeWritePolicy("required", "not_obtained", "b" * 64, "unsupported") if self.required else NativeWritePolicy("not_required", "not_applicable", "a" * 64, "available")
        return NativePromotionPreflight("browser_skill_promotion_native_preflight/v1", str(project_root), package_digest, True, None, (), "safe", policy)


class BrowserSkillPromotionLifecycleTests(unittest.TestCase):
    def test_project_fixtures_are_lazy_nested_and_clean_up_failed_setup(self) -> None:
        for factory in (project, _project):
            with self.subTest(fixture=factory.__name__):
                with patch("tempfile.mkdtemp", side_effect=AssertionError("eager allocation")):
                    manager = factory()
                with manager as outer:
                    self.assertTrue((outer / ".git").is_dir())
                    with factory() as inner:
                        self.assertNotEqual(inner, outer)
                        self.assertTrue((inner / ".git").is_dir())
                    self.assertFalse(inner.parent.exists())
                    self.assertTrue((outer / ".git").is_dir())
                self.assertFalse(outer.parent.exists())

                failure = OSError(errno.EIO, "fixture setup failed")
                attempted: list[Path] = []

                def fail_git(argv: list[str], *, check: bool, capture_output: bool) -> None:
                    self.assertEqual(argv[:3], ["git", "init", "-q"])
                    self.assertTrue(check)
                    self.assertTrue(capture_output)
                    root = Path(argv[-1])
                    self.assertTrue(root.is_dir())
                    attempted.append(root)
                    raise failure

                failed_setup = factory()
                with patch.object(subprocess, "run", side_effect=fail_git) as git:
                    with self.assertRaises(OSError) as raised:
                        with failed_setup:
                            self.fail("failed setup yielded a project")
                self.assertIs(raised.exception, failure)
                git.assert_called_once()
                self.assertEqual(len(attempted), 1)
                self.assertFalse(attempted[0].parent.exists())

                failed_body: Path | None = None
                with self.assertRaises(OSError) as raised:
                    with factory() as failed_body:
                        self.assertTrue((failed_body / ".git").is_dir())
                        raise failure
                self.assertIs(raised.exception, failure)
                assert failed_body is not None
                self.assertFalse(failed_body.parent.exists())

    def test_windows_directory_boundary_preserves_full_lifecycle_and_file_sync(self) -> None:
        opened: dict[int, Path] = {}
        synced: set[Path] = set()
        directory_attempts: list[Path] = []
        entry_replacements: list[Path] = []

        def windows_open(path: str | os.PathLike[str], flags: int, mode: int = 0o777, *, dir_fd: int | None = None) -> int:
            path = Path(path)
            if path.is_dir():
                directory_attempts.append(path)
                raise PermissionError(errno.EACCES, "CRT cannot open directories", str(path))
            descriptor = os.open(path, flags, mode, dir_fd=dir_fd)
            opened[descriptor] = path
            return descriptor

        def sync_file(descriptor: int) -> None:
            self.assertTrue(stat.S_ISREG(os.fstat(descriptor).st_mode))
            os.fsync(descriptor)
            synced.add(opened[descriptor])

        def replace_synced(source: str | os.PathLike[str], destination: str | os.PathLike[str]) -> None:
            source, destination = Path(source), Path(destination)
            self.assertIn(source, synced)
            synced.remove(source)
            os.replace(source, destination)
            if destination.name == "SKILL.md":
                entry_replacements.append(destination)

        seam = SimpleNamespace(**vars(os))
        seam.name = "nt"
        seam.open = windows_open
        seam.fsync = sync_file
        seam.replace = replace_synced
        with patch.object(lifecycle, "os", seam):
            # Reuse the real install/repeat/update/rollback/removal scenario;
            # only the platform I/O seam differs from its native-host run.
            self.test_install_repeat_update_rollback_and_remove_retain_immutable_history()
        self.assertEqual(directory_attempts, [])
        self.assertEqual(len(entry_replacements), 3)
        self.assertTrue(synced)  # Immutable resources/indexes were synced too.

    def test_file_sync_failure_blocks_visibility_on_windows_and_posix(self) -> None:
        for platform in ("nt", "posix"):
            with self.subTest(platform=platform), project() as root:
                trace = approved_trace(root)
                host = Host()
                receipt = approve(root, trace, host)
                seam = SimpleNamespace(**vars(os))
                seam.name = platform
                failure = OSError(errno.EIO, "file sync failed")
                with patch.object(lifecycle, "os", seam), patch.object(seam, "fsync", side_effect=failure) as sync:
                    with self.assertRaises(OSError) as raised:
                        _ = promote_approved_browser_skill(root, _digest_value(receipt["receipt_id"], "receipt id"), host=host)
                self.assertIs(raised.exception, failure)
                sync.assert_called_once()
                self.assertFalse((root / ".hermes" / "skills" / "checkout-confirmation" / "SKILL.md").exists())

    def test_posix_directory_open_and_sync_errors_still_propagate(self) -> None:
        seam = SimpleNamespace(**vars(os))
        seam.name = "posix"
        failure = OSError(errno.EIO, "directory sync failed")
        with patch.object(lifecycle, "os", seam):
            with patch.object(seam, "open", side_effect=failure):
                with self.assertRaises(BrowserSkillPromotionError) as raised:
                    _LifecycleProbe.fsync_directory(lifecycle, Path("unused-directory"))
                self.assertIs(raised.exception.__cause__, failure)
            # A descriptor sentinel keeps this POSIX negative control runnable
            # on Windows without pretending CRT supports directory handles.
            with patch.object(seam, "open", return_value=123), patch.object(
                seam, "fsync", side_effect=failure
            ), patch.object(seam, "close") as close:
                with self.assertRaises(BrowserSkillPromotionError) as raised:
                    _LifecycleProbe.fsync_directory(lifecycle, Path("unused-directory"))
                self.assertIs(raised.exception.__cause__, failure)
                close.assert_called_once_with(123)

    def test_crt_write_readback_cannot_conceal_physical_newline_expansion(self) -> None:
        with TemporaryDirectory() as temporary:
            path = Path(temporary).resolve() / "entry.md"
            reviewed = b"reviewed\ncontent\n"
            with _crt_descriptor_io(lifecycle):
                _LifecycleProbe.write_exact(lifecycle, path, reviewed.decode("utf-8"), replace=False)
                self.assertEqual(_LifecycleProbe.read_bytes(lifecycle, path), reviewed)
                self.assertEqual(path.read_bytes(), reviewed)

    def test_lifecycle_reads_preserve_crlf_and_ctrl_z_bytes(self) -> None:
        with TemporaryDirectory() as temporary:
            path = Path(temporary).resolve() / "entry.md"
            raw = b"reviewed\r\ncontent\x1aafter-eof\n"
            _ = path.write_bytes(raw)
            with _crt_descriptor_io(lifecycle):
                self.assertEqual(_LifecycleProbe.read_bytes(lifecycle, path), raw)

    def test_crt_promotion_installs_exact_reviewed_bytes_and_reuses_without_writes(self) -> None:
        with project() as root:
            root = root.resolve()
            trace = approved_trace(root)
            host = Host()
            review = review_browser_skill_lifecycle(root, trace, "checkout-confirmation", host=host)
            plan = _mapping(review, "plan")
            receipt = approve_browser_skill_lifecycle(
                root, trace, "checkout-confirmation", reviewed_diff_digest=_digest_value(plan["diff_digest"], "diff digest"),
                reviewer_identity="operator", host=host,
            )
            target = root / ".hermes" / "skills" / "checkout-confirmation"
            with _crt_descriptor_io(lifecycle), _crt_descriptor_io(promotion_plan):
                installed = promote_approved_browser_skill(root, _digest_value(receipt["receipt_id"], "receipt id"), host=host)
                self.assertEqual(installed["status"], "active")
                for name, text in _text(plan, "package").items():
                    with self.subTest(path=name):
                        self.assertEqual((target / name).read_bytes(), text.encode("utf-8"))
                self.assertEqual(hashlib.sha256((target / "SKILL.md").read_bytes()).hexdigest(), receipt["entry_digest"])
                manifest = json_object((target / f"resources/{plan['generation']}/manifest.json").read_bytes())
                for name, digest in _text(manifest, "files").items():
                    self.assertEqual(hashlib.sha256((target / name).read_bytes()).hexdigest(), digest)
                self.assertEqual(_LifecycleProbe.managed_inventory(lifecycle, root, "checkout-confirmation"), plan["package"])
                with patch.object(lifecycle, "_write_exact", side_effect=AssertionError("duplicate write")), patch.object(
                    lifecycle, "_write_observation", side_effect=AssertionError("duplicate observation")
                ):
                    reused = promote_approved_browser_skill(root, _digest_value(receipt["receipt_id"], "receipt id"), host=host)
                self.assertTrue(reused["reused"])
                self.assertEqual(reused["generation"], installed["generation"])

    def test_install_repeat_update_rollback_and_remove_retain_immutable_history(self) -> None:
        with project() as root:
            trace = approved_trace(root)
            host = Host()
            receipt = approve(root, trace, host)
            installed = promote_approved_browser_skill(root, _digest_value(receipt["receipt_id"], "receipt id"), host=host)
            self.assertEqual(installed["status"], "active")
            target = root / ".hermes" / "skills" / "checkout-confirmation"
            first_entry = (target / "SKILL.md").read_text(encoding="utf-8")
            first_generation = _digest_value(installed["generation"], "generation")
            self.assertEqual(promote_approved_browser_skill(root, _digest_value(receipt["receipt_id"], "receipt id"), host=host)["reused"], True)

            changed_trace = approved_trace(root, action="submit")
            review = review_browser_skill_lifecycle(root, changed_trace, "checkout-confirmation", host=host)
            self.assertEqual(_mapping(review, "plan")["operation"], "update")
            update_receipt = approve_browser_skill_lifecycle(root, changed_trace, "checkout-confirmation", reviewed_diff_digest=_digest_value(_mapping(review, "plan")["diff_digest"], "diff digest"), reviewer_identity="operator", host=host)
            updated = promote_approved_browser_skill(root, _digest_value(update_receipt["receipt_id"], "receipt id"), host=host)
            self.assertNotEqual(updated["generation"], first_generation)
            self.assertEqual((target / "resources" / first_generation / "entry.md").read_text(encoding="utf-8"), first_entry)

            rollback_review = review_browser_skill_rollback(root, "checkout-confirmation", first_generation, host=host)
            rollback_receipt = approve_browser_skill_rollback(root, "checkout-confirmation", first_generation, reviewed_diff_digest=_digest_value(_mapping(rollback_review, "plan")["diff_digest"], "diff digest"), reviewer_identity="operator", host=host)
            rolled_back = promote_approved_browser_skill(root, _digest_value(rollback_receipt["receipt_id"], "receipt id"), host=host)
            self.assertEqual(rolled_back["status"], "rolled_back")
            self.assertTrue((target / "resources" / first_generation / "manifest.json").is_file())

            removal_review = review_browser_skill_removal(root, "checkout-confirmation", host=host)
            removal = approve_browser_skill_removal(root, "checkout-confirmation", reviewed_diff_digest=_digest_value(_mapping(removal_review, "plan")["diff_digest"], "diff digest"), reviewer_identity="operator", host=host)
            self.assertEqual(promote_approved_browser_skill(root, _digest_value(removal["receipt_id"], "receipt id"), host=host)["status"], "removed")
            self.assertFalse((target / "SKILL.md").exists())
            self.assertTrue((target / "resources" / first_generation / "entry.md").is_file())

    def test_unchanged_identity_reuses_receipt_without_native_probe(self) -> None:
        with project() as root:
            trace = approved_trace(root)
            host = Host(); receipt = approve(root, trace, host)
            _ = promote_approved_browser_skill(root, _digest_value(receipt["receipt_id"], "receipt id"), host=host)
            before = host.calls
            review = review_browser_skill_lifecycle(root, trace, "checkout-confirmation", host=host)
            self.assertEqual(_mapping(review, "plan")["operation"], "unchanged")
            duplicate = approve_browser_skill_lifecycle(root, trace, "checkout-confirmation", reviewed_diff_digest=_digest_value(_mapping(review, "plan")["diff_digest"], "diff digest"), reviewer_identity="operator", host=host)
            self.assertEqual(duplicate, receipt)
            self.assertEqual(host.calls, before)

    def test_repeat_and_status_are_write_free_and_preserve_promotion_time(self) -> None:
        with project() as root:
            trace = approved_trace(root)
            receipt = approve(root, trace, Host())
            first = promote_approved_browser_skill(root, _digest_value(receipt["receipt_id"], "receipt id"), host=Host())
            import omh.workflows.browser_skill_promotion as lifecycle
            with patch.object(lifecycle, "_write_observation", side_effect=AssertionError("unexpected write")):
                repeated = promote_approved_browser_skill(root, _digest_value(receipt["receipt_id"], "receipt id"), host=Host())
                checked = browser_skill_promotion_status(root, "checkout-confirmation")
                unchecked = browser_skill_promotion_status(root, "checkout-confirmation", check_source=False)
            self.assertTrue(repeated["reused"])
            self.assertEqual(repeated["promoted_at"], first["promoted_at"])
            self.assertEqual(checked["status"], "active")
            self.assertEqual(unchecked["status"], "active_unchecked")

    def test_external_entry_removal_never_replays_cached_active_state(self) -> None:
        with project() as root:
            trace = approved_trace(root)
            receipt = approve(root, trace, Host())
            _ = promote_approved_browser_skill(root, _digest_value(receipt["receipt_id"], "receipt id"), host=Host())
            (root / ".hermes" / "skills" / "checkout-confirmation" / "SKILL.md").unlink()
            self.assertEqual(browser_skill_promotion_status(root, "checkout-confirmation")["status"], "inactive")

    def test_reused_promote_rechecks_drift_and_never_reports_stale_active(self) -> None:
        with project() as root:
            trace = approved_trace(root)
            receipt = approve(root, trace, Host())
            _ = promote_approved_browser_skill(root, _digest_value(receipt["receipt_id"], "receipt id"), host=Host())
            _ = replay_stored_browser_workflow_trace(root, trace, {"fixture_id": "negative_fresh"})
            result = promote_approved_browser_skill(root, _digest_value(receipt["receipt_id"], "receipt id"), host=Host())
            self.assertEqual(result["status"], "stale")
            self.assertFalse((root / ".hermes" / "skills" / "checkout-confirmation" / "SKILL.md").exists())

    def test_native_policy_flip_after_approval_blocks_visibility_commit(self) -> None:
        with project() as root:
            trace = approved_trace(root)
            host = Host(); receipt = approve(root, trace, host)
            host.required = True
            with self.assertRaisesRegex(BrowserSkillPromotionApprovalError, "required but unsupported"):
                _ = promote_approved_browser_skill(root, _digest_value(receipt["receipt_id"], "receipt id"), host=host)
            self.assertFalse((root / ".hermes" / "skills" / "checkout-confirmation" / "SKILL.md").exists())

    def test_rehashed_resource_manifest_and_index_are_not_owned(self) -> None:
        with project() as root:
            trace = approved_trace(root)
            receipt = approve(root, trace, Host())
            promoted = promote_approved_browser_skill(root, _digest_value(receipt["receipt_id"], "receipt id"), host=Host())
            target = root / ".hermes" / "skills" / "checkout-confirmation"
            generation = _digest_value(promoted["generation"], "generation")
            procedure = target / "resources" / generation / "procedure.md"
            _ = procedure.write_text("forged\n", encoding="utf-8")
            manifest_path = target / "resources" / generation / "manifest.json"
            manifest = json_object(manifest_path.read_text())
            files = manifest["files"]
            assert isinstance(files, dict)
            files[f"resources/{generation}/procedure.md"] = hashlib.sha256(b"forged\n").hexdigest()
            _ = manifest_path.write_text(json.dumps(manifest, sort_keys=True, separators=(",", ":")) + "\n", encoding="utf-8")
            index_path = root / ".omh" / "browser-skill-promotions" / "checkout-confirmation" / "activation-by-entry" / f"{receipt['entry_digest']}.json"
            index = json_object(index_path.read_text())
            index["manifest_digest"] = hashlib.sha256(manifest_path.read_bytes()).hexdigest()
            _ = index_path.write_text(json.dumps(index, sort_keys=True, separators=(",", ":")) + "\n", encoding="utf-8")
            status = browser_skill_promotion_status(root, "checkout-confirmation")
            self.assertEqual(status["status"], "unverified_managed_state")
            self.assertTrue((target / "SKILL.md").exists())

    def test_source_and_target_changes_during_staging_fail_closed(self) -> None:
        with project() as root:
            trace = approved_trace(root)
            receipt = approve(root, trace, Host())
            import omh.workflows.browser_skill_promotion as lifecycle
            original_stage = _LifecycleProbe.staging(lifecycle)
            import omh.workflows.browser_skill_promotion_plan as plan_module
            original_reference = resolved_browser_workflow_promotion_reference
            staged = False
            def mutate_source(root: Path, skill_name: str, plan: Mapping[str, object]) -> None:
                nonlocal staged
                original_stage(root, skill_name, plan)
                staged = True
            def source_after_stage(cwd: str | Path | None, trace_id: str) -> JsonObject:
                if staged:
                    raise BrowserSkillPromotionError("source changed during staging")
                return original_reference(cwd, trace_id)
            with patch.object(lifecycle, "_stage_and_verify", mutate_source), patch.object(plan_module, "resolved_browser_workflow_promotion_reference", source_after_stage):
                with self.assertRaisesRegex(BrowserSkillPromotionError, "source changed"):
                    _ = promote_approved_browser_skill(root, _digest_value(receipt["receipt_id"], "receipt id"), host=Host())
            self.assertFalse((root / ".hermes" / "skills" / "checkout-confirmation" / "SKILL.md").exists())

    def test_target_change_during_staging_fails_before_visibility(self) -> None:
        with project() as root:
            trace = approved_trace(root)
            receipt = approve(root, trace, Host())
            import omh.workflows.browser_skill_promotion as lifecycle
            original = _LifecycleProbe.staging(lifecycle)
            def mutate_target(root: Path, skill_name: str, plan: Mapping[str, object]) -> None:
                original(root, skill_name, plan)
                target = root / ".hermes" / "skills" / "checkout-confirmation"
                _ = (target / "unmanaged.md").write_text("changed during stage\n", encoding="utf-8")
            with patch.object(lifecycle, "_stage_and_verify", mutate_target):
                with self.assertRaisesRegex(BrowserSkillPromotionError, "unmanaged"):
                    _ = promote_approved_browser_skill(root, _digest_value(receipt["receipt_id"], "receipt id"), host=Host())
            self.assertFalse((root / ".hermes" / "skills" / "checkout-confirmation" / "SKILL.md").exists())

    def test_slug_and_state_symlinks_are_refused_before_lock_side_effects(self) -> None:
        with project() as root:
            trace = approved_trace(root)
            with self.assertRaisesRegex(BrowserSkillPromotionError, "slug"):
                _ = review_browser_skill_lifecycle(root, trace, "../escape", host=Host())
            state = root / ".omh" / "browser-skill-promotions"
            state.parent.mkdir(exist_ok=True)
            state.symlink_to(root / "elsewhere")
            import omh.workflows.browser_skill_promotion as lifecycle
            with self.assertRaisesRegex(BrowserSkillPromotionError, "symlink"):
                _ = _LifecycleProbe.state_root(lifecycle, root, "checkout-confirmation")

    def test_drift_deactivates_only_verified_skill_entry(self) -> None:
        with project() as root:
            trace = approved_trace(root)
            receipt = approve(root, trace, Host())
            _ = promote_approved_browser_skill(root, _digest_value(receipt["receipt_id"], "receipt id"), host=Host())
            target = root / ".hermes" / "skills" / "checkout-confirmation"
            resource = next((target / "resources").glob("*/entry.md"))
            # A second distinct mismatch moves the trace from stale to quarantined.
            _ = replay_stored_browser_workflow_trace(root, trace, {"fixture_id": "negative_fresh"})
            status = browser_skill_promotion_status(root, "checkout-confirmation")
            self.assertEqual(status["status"], "stale")
            self.assertFalse((target / "SKILL.md").exists())
            self.assertTrue(resource.exists())

    def test_quarantined_trace_is_not_reported_as_stale(self) -> None:
        with project() as root:
            trace = approved_trace(root)
            receipt = approve(root, trace, Host())
            _ = promote_approved_browser_skill(root, _digest_value(receipt["receipt_id"], "receipt id"), host=Host())
            _ = replay_stored_browser_workflow_trace(root, trace, {"fixture_id": "negative"})
            _ = replay_stored_browser_workflow_trace(root, trace, {"fixture_id": "negative_fresh"})
            self.assertEqual(browser_skill_promotion_status(root, "checkout-confirmation")["status"], "quarantined")

    def test_unmanaged_sibling_refuses_without_deleting_it(self) -> None:
        with project() as root:
            trace = approved_trace(root)
            target = root / ".hermes" / "skills" / "checkout-confirmation"
            target.mkdir(parents=True)
            sibling = target / "notes.md"; _ = sibling.write_text("user bytes\n", encoding="utf-8")
            with self.assertRaisesRegex(BrowserSkillPromotionError, "unmanaged"):
                _ = review_browser_skill_lifecycle(root, trace, "checkout-confirmation", host=Host())
            self.assertEqual(sibling.read_text(encoding="utf-8"), "user bytes\n")

    def test_cli_leaf_registers_all_operation_commands(self) -> None:
        from omh.commands.browser_skill_promotion import add_browser_skill_promotion_commands
        parser = argparse.ArgumentParser()
        parent = parser.add_subparsers(dest="root", required=True)
        add_browser_skill_promotion_commands(parent)
        for command in ("diff", "approve", "promote", "status", "rollback", "remove", "retry"):
            with self.subTest(command=command):
                extras: list[str] = []
                if command in {"diff", "approve"}:
                    extras += ["--trace-id", "bwt-" + "a" * 24]
                if command == "approve":
                    extras += ["--reviewed-diff-digest", "a" * 64, "--reviewer", "operator"]
                if command in {"promote", "retry"}:
                    extras += ["--receipt-id", "a" * 64]
                if command == "rollback":
                    extras += ["--generation", "a" * 64]
                skill_arg = [] if command in {"promote", "retry"} else ["--skill-name", "checkout-confirmation"]
                args = parser.parse_args(["promotion", command, "--project-root", ".", *skill_arg, *extras], namespace=_CommandNamespace())
                self.assertTrue(callable(args.func))

    def test_removal_repeat_is_safe_and_status_retains_removed_observation(self) -> None:
        with project() as root:
            trace = approved_trace(root)
            receipt = approve(root, trace, Host())
            _ = promote_approved_browser_skill(root, _digest_value(receipt["receipt_id"], "receipt id"), host=Host())
            review = review_browser_skill_removal(root, "checkout-confirmation", host=Host())
            removal = approve_browser_skill_removal(root, "checkout-confirmation", reviewed_diff_digest=_digest_value(_mapping(review, "plan")["diff_digest"], "diff digest"), reviewer_identity="operator", host=Host())
            self.assertEqual(promote_approved_browser_skill(root, _digest_value(removal["receipt_id"], "receipt id"), host=Host())["status"], "removed")
            self.assertTrue(promote_approved_browser_skill(root, _digest_value(removal["receipt_id"], "receipt id"), host=Host())["reused"])
            self.assertEqual(browser_skill_promotion_status(root, "checkout-confirmation")["status"], "removed")

    def test_reviewed_reinstall_after_removal_uses_new_base_activation(self) -> None:
        with project() as root:
            first_trace = approved_trace(root)
            first = approve(root, first_trace, Host())
            first_result = promote_approved_browser_skill(root, _digest_value(first["receipt_id"], "receipt id"), host=Host())
            review = review_browser_skill_removal(root, "checkout-confirmation", host=Host())
            removal = approve_browser_skill_removal(root, "checkout-confirmation", reviewed_diff_digest=_digest_value(_mapping(review, "plan")["diff_digest"], "diff digest"), reviewer_identity="operator", host=Host())
            _ = promote_approved_browser_skill(root, _digest_value(removal["receipt_id"], "receipt id"), host=Host())
            second_trace = approved_trace(root, action="submit")
            second = approve(root, second_trace, Host())
            second_result = promote_approved_browser_skill(root, _digest_value(second["receipt_id"], "receipt id"), host=Host())
            self.assertNotEqual(first_result["generation"], second_result["generation"])

    def test_post_entry_observation_crash_recovers_from_entry_index_truth(self) -> None:
        with project() as root:
            trace = approved_trace(root)
            receipt = approve(root, trace, Host())
            import omh.workflows.browser_skill_promotion as lifecycle
            with patch.object(lifecycle, "_observe", side_effect=RuntimeError("crash after entry")):
                with self.assertRaisesRegex(RuntimeError, "crash after entry"):
                    _ = promote_approved_browser_skill(root, _digest_value(receipt["receipt_id"], "receipt id"), host=Host())
            self.assertEqual(browser_skill_promotion_status(root, "checkout-confirmation")["status"], "active")

    def test_partial_pre_entry_resources_need_explicit_retry(self) -> None:
        with project() as root:
            trace = approved_trace(root)
            host = Host(); receipt = approve(root, trace, host)
            # Simulate a crash after only immutable resource staging by placing
            # the exact approved resource bytes, never an entry or index.
            plan = _mapping(review_browser_skill_lifecycle(root, trace, "checkout-confirmation", host=host), "plan")
            target = root / ".hermes" / "skills" / "checkout-confirmation"
            for name, text in _text(plan, "package").items():
                if name.startswith("resources/"):
                    path = target / name; path.parent.mkdir(parents=True, exist_ok=True); _ = path.write_bytes(text.encode("utf-8"))
            self.assertEqual(browser_skill_promotion_status(root, "checkout-confirmation", check_source=False)["status"], "inactive")
            self.assertEqual(promote_approved_browser_skill(root, _digest_value(receipt["receipt_id"], "receipt id"), host=host)["status"], "active")


def approve(root: Path, trace_id: str, host: Host) -> dict[str, object]:
    review = review_browser_skill_lifecycle(root, trace_id, "checkout-confirmation", host=host)
    return approve_browser_skill_lifecycle(root, trace_id, "checkout-confirmation", reviewed_diff_digest=_digest_value(_mapping(review, "plan")["diff_digest"], "diff digest"), reviewer_identity="operator", host=host)


@contextmanager
def project() -> Generator[Path, None, None]:
    with TemporaryDirectory() as temporary:
        root = Path(temporary) / "project"
        root.mkdir()
        _ = subprocess.run(["git", "init", "-q", str(root)], check=True, capture_output=True)
        yield root


def approved_trace(root: Path, *, action: str = "click") -> str:
    from test_browser_workflow_learning import browser_workflow_trace
    trace = write_browser_workflow_trace(browser_workflow_trace(action), root)
    _ = approve_browser_workflow_trace(root, str(trace["trace_id"]), str(trace["digest"]))
    _ = replay_stored_browser_workflow_trace(root, str(trace["trace_id"]), {"fixture_id": "positive"})
    return str(trace["trace_id"])


if __name__ == "__main__": _ = unittest.main()
