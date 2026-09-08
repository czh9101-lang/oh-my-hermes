from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Literal
import os
import subprocess
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from _local_package import load_local_package

load_local_package()

from omh.workflows.browser_skill_promotion_approval import (
    BrowserSkillPromotionApprovalError,
    NativePromotionPreflight,
    NativeWritePolicy,
    PromotionNativeHost,
    approve_browser_skill_promotion,
    read_browser_skill_promotion_approval_receipt,
    review_browser_skill_promotion,
)
from omh.workflows.browser_workflow_learning_store import (
    approve_browser_workflow_trace,
    replay_stored_browser_workflow_trace,
    write_browser_workflow_trace,
)


@dataclass
class _Host(PromotionNativeHost):
    required: bool = False
    trusted: bool = True
    security_verdict: Literal["safe", "caution", "dangerous"] = "safe"
    structure_error: str | None = None
    lint_errors: tuple[str, ...] = ()
    inspections: int = 0

    def inspect(
        self, project_root: Path, package: Mapping[str, str]
    ) -> NativePromotionPreflight:
        from omh.workflows.browser_skill_promotion_approval import _package_digest

        self.inspections += 1
        if self.required:
            policy = NativeWritePolicy(
                requirement="required",
                approval="not_obtained",
                support="unsupported",
                revision="a" * 64,
            )
        else:
            policy = NativeWritePolicy(
                requirement="not_required",
                approval="not_applicable",
                support="available",
                revision="a" * 64,
            )
        return NativePromotionPreflight(
            schema_version="browser_skill_promotion_native_preflight/v1",
            project_root=str(project_root),
            package_digest=_package_digest(package),
            trusted=self.trusted,
            structure_error=self.structure_error,
            lint_errors=self.lint_errors,
            security_verdict=self.security_verdict,
            policy=policy,
        )


class BrowserSkillPromotionApprovalTests(unittest.TestCase):
    def test_real_git_trace_review_binds_every_reviewed_value_and_persists_once(self) -> None:
        with _project() as root:
            trace_id = _approved_trace(root)
            host = _Host()
            review = review_browser_skill_promotion(
                root, trace_id, "checkout-confirmation", host=host
            )
            plan = review["plan"]
            self.assertEqual(plan["source"]["trace_id"], trace_id)
            self.assertEqual(review["native_preflight"]["security_verdict"], "safe")

            receipt = approve_browser_skill_promotion(
                root,
                trace_id,
                "checkout-confirmation",
                reviewed_diff_digest=plan["diff_digest"],
                reviewer_identity="operator@example.com",
                host=host,
            )
            duplicate = approve_browser_skill_promotion(
                root,
                trace_id,
                "checkout-confirmation",
                reviewed_diff_digest=plan["diff_digest"],
                reviewer_identity="operator@example.com",
                host=host,
            )

            self.assertEqual(duplicate, receipt)
            self.assertEqual(
                read_browser_skill_promotion_approval_receipt(
                    root, receipt["receipt_id"]
                ),
                receipt,
            )
            self.assertEqual(
                {
                    "project_identity",
                    "trace_id",
                    "trace_revision",
                    "trace_digest",
                    "fixture_digests",
                    "generic_draft_digest",
                    "generation",
                    "package_digest",
                    "entry_digest",
                    "manifest_digest",
                    "base_package_digest",
                    "reviewed_diff_digest",
                    "policy_revision",
                    "operation",
                    "activation_id",
                    "payload_digest",
                    "rollback_of",
                    "base_digest",
                    "base_entry_digest",
                    "previous_generation",
                },
                set(receipt) - {"schema_version", "receipt_id", "reviewer_identity", "target_path", "project_root", "native_preflight_digest"},
            )
            receipts = list(
                (root / ".omh" / "browser-skill-promotions" / "receipts").glob("*.json")
            )
            self.assertEqual(len(receipts), 1)

    def test_changed_target_or_review_digest_cannot_become_an_approval(self) -> None:
        with _project() as root:
            trace_id = _approved_trace(root)
            host = _Host()
            review = review_browser_skill_promotion(
                root, trace_id, "checkout-confirmation", host=host
            )
            target = root / ".hermes" / "skills" / "checkout-confirmation"
            target.mkdir(parents=True)
            (target / "SKILL.md").write_text("user-owned bytes\n", encoding="utf-8")

            with self.assertRaisesRegex(
                BrowserSkillPromotionApprovalError, "current exact diff"
            ):
                approve_browser_skill_promotion(
                    root,
                    trace_id,
                    "checkout-confirmation",
                    reviewed_diff_digest=review["plan"]["diff_digest"],
                    reviewer_identity="operator@example.com",
                    host=host,
                )
            self.assertFalse(
                (root / ".omh" / "browser-skill-promotions" / "receipts").exists()
            )

    def test_native_failures_are_not_boolean_approvals_and_write_no_receipt(self) -> None:
        with _project() as root:
            trace_id = _approved_trace(root)
            for host, phrase in (
                (_Host(required=True), "required but unsupported"),
                (_Host(trusted=False), "not trusted"),
                (_Host(security_verdict="dangerous"), "security scan"),
                (_Host(lint_errors=("error",)), "lint"),
            ):
                with self.subTest(phrase=phrase), self.assertRaisesRegex(
                    BrowserSkillPromotionApprovalError, phrase
                ):
                    review_browser_skill_promotion(
                        root, trace_id, "checkout-confirmation", host=host
                    )
            self.assertFalse(
                (root / ".omh" / "browser-skill-promotions" / "receipts").exists()
            )

    def test_receipt_storage_refuses_dangling_or_redirected_symlink(self) -> None:
        with _project() as root:
            trace_id = _approved_trace(root)
            host = _Host()
            review = review_browser_skill_promotion(
                root, trace_id, "checkout-confirmation", host=host
            )
            promotions = root / ".omh" / "browser-skill-promotions"
            promotions.parent.mkdir(exist_ok=True)
            os.symlink(root / "missing-target", promotions)

            with self.assertRaisesRegex(
                BrowserSkillPromotionApprovalError, "symlink"
            ):
                approve_browser_skill_promotion(
                    root,
                    trace_id,
                    "checkout-confirmation",
                    reviewed_diff_digest=review["plan"]["diff_digest"],
                    reviewer_identity="operator@example.com",
                    host=host,
                )


def _project():
    class Project:
        def __enter__(self) -> Path:
            self.temporary_directory = TemporaryDirectory()
            self.root = Path(self.temporary_directory.name).resolve() / "project"
            self.root.mkdir()
            subprocess.run(
                ["git", "init", "--quiet", str(self.root)],
                check=True,
                capture_output=True,
            )
            return self.root

        def __exit__(self, *args: object) -> None:
            self.temporary_directory.cleanup()

    return Project()


def _approved_trace(root: Path) -> str:
    from test_browser_workflow_learning import _trace

    trace = write_browser_workflow_trace(_trace("click"), root)
    approve_browser_workflow_trace(root, str(trace["trace_id"]), str(trace["digest"]))
    replay_stored_browser_workflow_trace(
        root, str(trace["trace_id"]), {"fixture_id": "positive"}
    )
    return str(trace["trace_id"])


if __name__ == "__main__":
    unittest.main()
