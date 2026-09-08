from __future__ import annotations

import argparse
import json
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from tempfile import TemporaryDirectory
import subprocess
import unittest
from unittest.mock import patch

from _local_package import load_local_package
load_local_package()

from omh.workflows.browser_skill_promotion import (
    BrowserSkillPromotionError, approve_browser_skill_lifecycle,
    approve_browser_skill_removal, approve_browser_skill_rollback,
    browser_skill_promotion_status, promote_approved_browser_skill,
    review_browser_skill_lifecycle, review_browser_skill_removal,
    review_browser_skill_rollback,
)
from omh.workflows.browser_skill_promotion_approval import BrowserSkillPromotionApprovalError, NativePromotionPreflight, NativeWritePolicy, PromotionNativeHost
from omh.workflows.browser_workflow_learning_store import approve_browser_workflow_trace, replay_stored_browser_workflow_trace, write_browser_workflow_trace


@dataclass
class Host(PromotionNativeHost):
    calls: int = 0
    required: bool = False
    def inspect(self, project_root: Path, package: Mapping[str, str]) -> NativePromotionPreflight:
        from omh.workflows.browser_skill_promotion_approval import _package_digest
        self.calls += 1
        policy = NativeWritePolicy("required", "not_obtained", "b" * 64, "unsupported") if self.required else NativeWritePolicy("not_required", "not_applicable", "a" * 64, "available")
        return NativePromotionPreflight("browser_skill_promotion_native_preflight/v1", str(project_root), _package_digest(package), True, None, (), "safe", policy)


class BrowserSkillPromotionLifecycleTests(unittest.TestCase):
    def test_install_repeat_update_rollback_and_remove_retain_immutable_history(self) -> None:
        with project() as root:
            trace = approved_trace(root)
            host = Host()
            receipt = approve(root, trace, host)
            installed = promote_approved_browser_skill(root, receipt["receipt_id"], host=host)
            self.assertEqual(installed["status"], "active")
            target = root / ".hermes" / "skills" / "checkout-confirmation"
            first_entry = (target / "SKILL.md").read_text(encoding="utf-8")
            first_generation = installed["generation"]
            self.assertEqual(promote_approved_browser_skill(root, receipt["receipt_id"], host=host)["reused"], True)

            changed_trace = approved_trace(root, action="submit")
            review = review_browser_skill_lifecycle(root, changed_trace, "checkout-confirmation", host=host)
            self.assertEqual(review["plan"]["operation"], "update")
            update_receipt = approve_browser_skill_lifecycle(root, changed_trace, "checkout-confirmation", reviewed_diff_digest=review["plan"]["diff_digest"], reviewer_identity="operator", host=host)
            updated = promote_approved_browser_skill(root, update_receipt["receipt_id"], host=host)
            self.assertNotEqual(updated["generation"], first_generation)
            self.assertEqual((target / "resources" / first_generation / "entry.md").read_text(encoding="utf-8"), first_entry)

            rollback_review = review_browser_skill_rollback(root, "checkout-confirmation", first_generation, host=host)
            rollback_receipt = approve_browser_skill_rollback(root, "checkout-confirmation", first_generation, reviewed_diff_digest=rollback_review["plan"]["diff_digest"], reviewer_identity="operator", host=host)
            rolled_back = promote_approved_browser_skill(root, rollback_receipt["receipt_id"], host=host)
            self.assertEqual(rolled_back["status"], "rolled_back")
            self.assertTrue((target / "resources" / first_generation / "manifest.json").is_file())

            removal_review = review_browser_skill_removal(root, "checkout-confirmation", host=host)
            removal = approve_browser_skill_removal(root, "checkout-confirmation", reviewed_diff_digest=removal_review["plan"]["diff_digest"], reviewer_identity="operator", host=host)
            self.assertEqual(promote_approved_browser_skill(root, removal["receipt_id"], host=host)["status"], "removed")
            self.assertFalse((target / "SKILL.md").exists())
            self.assertTrue((target / "resources" / first_generation / "entry.md").is_file())

    def test_unchanged_identity_reuses_receipt_without_native_probe(self) -> None:
        with project() as root:
            trace = approved_trace(root)
            host = Host(); receipt = approve(root, trace, host)
            promote_approved_browser_skill(root, receipt["receipt_id"], host=host)
            before = host.calls
            review = review_browser_skill_lifecycle(root, trace, "checkout-confirmation", host=host)
            self.assertEqual(review["plan"]["operation"], "unchanged")
            duplicate = approve_browser_skill_lifecycle(root, trace, "checkout-confirmation", reviewed_diff_digest=review["plan"]["diff_digest"], reviewer_identity="operator", host=host)
            self.assertEqual(duplicate, receipt)
            self.assertEqual(host.calls, before)

    def test_repeat_and_status_are_write_free_and_preserve_promotion_time(self) -> None:
        with project() as root:
            trace = approved_trace(root)
            receipt = approve(root, trace, Host())
            first = promote_approved_browser_skill(root, receipt["receipt_id"], host=Host())
            import omh.workflows.browser_skill_promotion as lifecycle
            with patch.object(lifecycle, "_write_observation", side_effect=AssertionError("unexpected write")):
                repeated = promote_approved_browser_skill(root, receipt["receipt_id"], host=Host())
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
            promote_approved_browser_skill(root, receipt["receipt_id"], host=Host())
            (root / ".hermes" / "skills" / "checkout-confirmation" / "SKILL.md").unlink()
            self.assertEqual(browser_skill_promotion_status(root, "checkout-confirmation")["status"], "inactive")

    def test_reused_promote_rechecks_drift_and_never_reports_stale_active(self) -> None:
        with project() as root:
            trace = approved_trace(root)
            receipt = approve(root, trace, Host())
            promote_approved_browser_skill(root, receipt["receipt_id"], host=Host())
            replay_stored_browser_workflow_trace(root, trace, {"fixture_id": "negative_fresh"})
            result = promote_approved_browser_skill(root, receipt["receipt_id"], host=Host())
            self.assertEqual(result["status"], "stale")
            self.assertFalse((root / ".hermes" / "skills" / "checkout-confirmation" / "SKILL.md").exists())

    def test_native_policy_flip_after_approval_blocks_visibility_commit(self) -> None:
        with project() as root:
            trace = approved_trace(root)
            host = Host(); receipt = approve(root, trace, host)
            host.required = True
            with self.assertRaisesRegex(BrowserSkillPromotionApprovalError, "required but unsupported"):
                promote_approved_browser_skill(root, receipt["receipt_id"], host=host)
            self.assertFalse((root / ".hermes" / "skills" / "checkout-confirmation" / "SKILL.md").exists())

    def test_rehashed_resource_manifest_and_index_are_not_owned(self) -> None:
        with project() as root:
            trace = approved_trace(root)
            receipt = approve(root, trace, Host())
            promoted = promote_approved_browser_skill(root, receipt["receipt_id"], host=Host())
            target = root / ".hermes" / "skills" / "checkout-confirmation"
            generation = promoted["generation"]
            procedure = target / "resources" / generation / "procedure.md"
            procedure.write_text("forged\n", encoding="utf-8")
            manifest_path = target / "resources" / generation / "manifest.json"
            manifest = json.loads(manifest_path.read_text())
            manifest["files"][f"resources/{generation}/procedure.md"] = __import__("hashlib").sha256(b"forged\n").hexdigest()
            manifest_path.write_text(json.dumps(manifest, sort_keys=True, separators=(",", ":")) + "\n", encoding="utf-8")
            index_path = root / ".omh" / "browser-skill-promotions" / "checkout-confirmation" / "activation-by-entry" / f"{receipt['entry_digest']}.json"
            index = json.loads(index_path.read_text())
            index["manifest_digest"] = __import__("hashlib").sha256(manifest_path.read_bytes()).hexdigest()
            index_path.write_text(json.dumps(index, sort_keys=True, separators=(",", ":")) + "\n", encoding="utf-8")
            status = browser_skill_promotion_status(root, "checkout-confirmation")
            self.assertEqual(status["status"], "unverified_managed_state")
            self.assertTrue((target / "SKILL.md").exists())

    def test_source_and_target_changes_during_staging_fail_closed(self) -> None:
        with project() as root:
            trace = approved_trace(root)
            receipt = approve(root, trace, Host())
            import omh.workflows.browser_skill_promotion as lifecycle
            original_stage = lifecycle._stage_and_verify
            import omh.workflows.browser_skill_promotion_plan as plan_module
            original_reference = plan_module.resolved_browser_workflow_promotion_reference
            staged = False
            def mutate_source(*args: object, **kwargs: object) -> None:
                nonlocal staged
                original_stage(*args, **kwargs)
                staged = True
            def source_after_stage(*args: object, **kwargs: object) -> object:
                if staged:
                    raise BrowserSkillPromotionError("source changed during staging")
                return original_reference(*args, **kwargs)
            with patch.object(lifecycle, "_stage_and_verify", mutate_source), patch.object(plan_module, "resolved_browser_workflow_promotion_reference", source_after_stage):
                with self.assertRaisesRegex(BrowserSkillPromotionError, "source changed"):
                    promote_approved_browser_skill(root, receipt["receipt_id"], host=Host())
            self.assertFalse((root / ".hermes" / "skills" / "checkout-confirmation" / "SKILL.md").exists())

    def test_target_change_during_staging_fails_before_visibility(self) -> None:
        with project() as root:
            trace = approved_trace(root)
            receipt = approve(root, trace, Host())
            import omh.workflows.browser_skill_promotion as lifecycle
            original = lifecycle._stage_and_verify
            def mutate_target(*args: object, **kwargs: object) -> None:
                original(*args, **kwargs)
                target = root / ".hermes" / "skills" / "checkout-confirmation"
                (target / "unmanaged.md").write_text("changed during stage\n", encoding="utf-8")
            with patch.object(lifecycle, "_stage_and_verify", mutate_target):
                with self.assertRaisesRegex(BrowserSkillPromotionError, "unmanaged"):
                    promote_approved_browser_skill(root, receipt["receipt_id"], host=Host())
            self.assertFalse((root / ".hermes" / "skills" / "checkout-confirmation" / "SKILL.md").exists())

    def test_slug_and_state_symlinks_are_refused_before_lock_side_effects(self) -> None:
        with project() as root:
            trace = approved_trace(root)
            with self.assertRaisesRegex(BrowserSkillPromotionError, "slug"):
                review_browser_skill_lifecycle(root, trace, "../escape", host=Host())
            state = root / ".omh" / "browser-skill-promotions"
            state.parent.mkdir(exist_ok=True)
            state.symlink_to(root / "elsewhere")
            import omh.workflows.browser_skill_promotion as lifecycle
            with self.assertRaisesRegex(BrowserSkillPromotionError, "symlink"):
                lifecycle._state_root(root, "checkout-confirmation")

    def test_drift_deactivates_only_verified_skill_entry(self) -> None:
        with project() as root:
            trace = approved_trace(root)
            receipt = approve(root, trace, Host())
            promote_approved_browser_skill(root, receipt["receipt_id"], host=Host())
            target = root / ".hermes" / "skills" / "checkout-confirmation"
            resource = next((target / "resources").glob("*/entry.md"))
            # A second distinct mismatch moves the trace from stale to quarantined.
            replay_stored_browser_workflow_trace(root, trace, {"fixture_id": "negative_fresh"})
            status = browser_skill_promotion_status(root, "checkout-confirmation")
            self.assertEqual(status["status"], "stale")
            self.assertFalse((target / "SKILL.md").exists())
            self.assertTrue(resource.exists())

    def test_quarantined_trace_is_not_reported_as_stale(self) -> None:
        with project() as root:
            trace = approved_trace(root)
            receipt = approve(root, trace, Host())
            promote_approved_browser_skill(root, receipt["receipt_id"], host=Host())
            replay_stored_browser_workflow_trace(root, trace, {"fixture_id": "negative"})
            replay_stored_browser_workflow_trace(root, trace, {"fixture_id": "negative_fresh"})
            self.assertEqual(browser_skill_promotion_status(root, "checkout-confirmation")["status"], "quarantined")

    def test_unmanaged_sibling_refuses_without_deleting_it(self) -> None:
        with project() as root:
            trace = approved_trace(root)
            target = root / ".hermes" / "skills" / "checkout-confirmation"
            target.mkdir(parents=True)
            sibling = target / "notes.md"; sibling.write_text("user bytes\n", encoding="utf-8")
            with self.assertRaisesRegex(BrowserSkillPromotionError, "unmanaged"):
                review_browser_skill_lifecycle(root, trace, "checkout-confirmation", host=Host())
            self.assertEqual(sibling.read_text(encoding="utf-8"), "user bytes\n")

    def test_cli_leaf_registers_all_operation_commands(self) -> None:
        from omh.commands.browser_skill_promotion import add_browser_skill_promotion_commands
        parser = argparse.ArgumentParser()
        parent = parser.add_subparsers(dest="root", required=True)
        add_browser_skill_promotion_commands(parent)
        for command in ("diff", "approve", "promote", "status", "rollback", "remove", "retry"):
            with self.subTest(command=command):
                extras = []
                if command in {"diff", "approve"}:
                    extras += ["--trace-id", "bwt-" + "a" * 24]
                if command == "approve":
                    extras += ["--reviewed-diff-digest", "a" * 64, "--reviewer", "operator"]
                if command in {"promote", "retry"}:
                    extras += ["--receipt-id", "a" * 64]
                if command == "rollback":
                    extras += ["--generation", "a" * 64]
                skill_arg = [] if command in {"promote", "retry"} else ["--skill-name", "checkout-confirmation"]
                args = parser.parse_args(["promotion", command, "--project-root", ".", *skill_arg, *extras])
                self.assertTrue(callable(args.func))

    def test_removal_repeat_is_safe_and_status_retains_removed_observation(self) -> None:
        with project() as root:
            trace = approved_trace(root)
            receipt = approve(root, trace, Host())
            promote_approved_browser_skill(root, receipt["receipt_id"], host=Host())
            review = review_browser_skill_removal(root, "checkout-confirmation", host=Host())
            removal = approve_browser_skill_removal(root, "checkout-confirmation", reviewed_diff_digest=review["plan"]["diff_digest"], reviewer_identity="operator", host=Host())
            self.assertEqual(promote_approved_browser_skill(root, removal["receipt_id"], host=Host())["status"], "removed")
            self.assertTrue(promote_approved_browser_skill(root, removal["receipt_id"], host=Host())["reused"])
            self.assertEqual(browser_skill_promotion_status(root, "checkout-confirmation")["status"], "removed")

    def test_reviewed_reinstall_after_removal_uses_new_base_activation(self) -> None:
        with project() as root:
            first_trace = approved_trace(root)
            first = approve(root, first_trace, Host())
            first_result = promote_approved_browser_skill(root, first["receipt_id"], host=Host())
            review = review_browser_skill_removal(root, "checkout-confirmation", host=Host())
            removal = approve_browser_skill_removal(root, "checkout-confirmation", reviewed_diff_digest=review["plan"]["diff_digest"], reviewer_identity="operator", host=Host())
            promote_approved_browser_skill(root, removal["receipt_id"], host=Host())
            second_trace = approved_trace(root, action="submit")
            second = approve(root, second_trace, Host())
            second_result = promote_approved_browser_skill(root, second["receipt_id"], host=Host())
            self.assertNotEqual(first_result["generation"], second_result["generation"])

    def test_post_entry_observation_crash_recovers_from_entry_index_truth(self) -> None:
        with project() as root:
            trace = approved_trace(root)
            receipt = approve(root, trace, Host())
            import omh.workflows.browser_skill_promotion as lifecycle
            with patch.object(lifecycle, "_observe", side_effect=RuntimeError("crash after entry")):
                with self.assertRaisesRegex(RuntimeError, "crash after entry"):
                    promote_approved_browser_skill(root, receipt["receipt_id"], host=Host())
            self.assertEqual(browser_skill_promotion_status(root, "checkout-confirmation")["status"], "active")

    def test_partial_pre_entry_resources_need_explicit_retry(self) -> None:
        with project() as root:
            trace = approved_trace(root)
            host = Host(); receipt = approve(root, trace, host)
            # Simulate a crash after only immutable resource staging by placing
            # the exact approved resource bytes, never an entry or index.
            plan = review_browser_skill_lifecycle(root, trace, "checkout-confirmation", host=host)["plan"]
            target = root / ".hermes" / "skills" / "checkout-confirmation"
            for name, text in plan["package"].items():
                if name.startswith("resources/"):
                    path = target / name; path.parent.mkdir(parents=True, exist_ok=True); path.write_text(text, encoding="utf-8")
            self.assertEqual(browser_skill_promotion_status(root, "checkout-confirmation", check_source=False)["status"], "inactive")
            self.assertEqual(promote_approved_browser_skill(root, receipt["receipt_id"], host=host)["status"], "active")


def approve(root: Path, trace_id: str, host: Host) -> dict[str, object]:
    review = review_browser_skill_lifecycle(root, trace_id, "checkout-confirmation", host=host)
    return approve_browser_skill_lifecycle(root, trace_id, "checkout-confirmation", reviewed_diff_digest=review["plan"]["diff_digest"], reviewer_identity="operator", host=host)


def project():
    class Project:
        def __enter__(self) -> Path:
            self.temp = TemporaryDirectory(); self.root = Path(self.temp.name) / "project"; self.root.mkdir()
            subprocess.run(["git", "init", "-q", str(self.root)], check=True, capture_output=True)
            return self.root
        def __exit__(self, *args: object) -> None: self.temp.cleanup()
    return Project()


def approved_trace(root: Path, *, action: str = "click") -> str:
    from test_browser_workflow_learning import _trace
    trace = write_browser_workflow_trace(_trace(action), root)
    approve_browser_workflow_trace(root, str(trace["trace_id"]), str(trace["digest"]))
    replay_stored_browser_workflow_trace(root, str(trace["trace_id"]), {"fixture_id": "positive"})
    return str(trace["trace_id"])


if __name__ == "__main__": unittest.main()
