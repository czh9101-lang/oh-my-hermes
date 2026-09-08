from __future__ import annotations

import hashlib
import json
import subprocess
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from _local_package import load_local_package

load_local_package()

from omh.workflows.browser_skill_promotion_plan import (
    BrowserSkillPromotionPlanError,
    build_browser_skill_promotion_plan,
)
from omh.workflows.browser_workflow_learning_store import (
    approve_browser_workflow_trace,
    read_browser_workflow_trace,
    replay_stored_browser_workflow_trace,
    resolved_browser_workflow_promotion_reference,
    write_browser_workflow_trace,
)


class BrowserSkillPromotionPlanTests(unittest.TestCase):
    def test_plan_uses_real_git_bound_public_reference_and_is_deterministic(self) -> None:
        with _project() as root:
            trace = _passing_trace(root)
            reference = resolved_browser_workflow_promotion_reference(root, trace["trace_id"])

            first = build_browser_skill_promotion_plan(root, trace["trace_id"], "checkout-confirmation")
            second = build_browser_skill_promotion_plan(root, trace["trace_id"], "checkout-confirmation")

            self.assertEqual(first, second)
            self.assertEqual(first["project_identity"], reference["project_identity"])
            self.assertEqual(first["source"]["trace_digest"], reference["trace_digest"])
            self.assertEqual(first["source"]["trace_revision"], reference["trace_revision"])
            self.assertEqual(first["source"]["replay_digest"], reference["replay_digest"])
            self.assertEqual(first["target_path"], str(root / ".hermes" / "skills" / "checkout-confirmation"))
            self.assertLess(len(first["package"]["SKILL.md"].encode("utf-8")), 8 * 1024)
            self.assertNotIn('"steps"', first["package"]["SKILL.md"])
            self.assertIn("resources/", first["package"]["SKILL.md"])
            self.assertEqual(
                first["package"]["SKILL.md"],
                first["package"][f"resources/{first['generation']}/entry.md"],
            )
            self.assertNotIn(first["package_digest"], "\n".join(first["package"].values()))

    def test_plan_reuses_generic_skill_draft_checks_without_activating_or_writing_a_draft(self) -> None:
        with _project() as root:
            trace = _passing_trace(root)

            plan = build_browser_skill_promotion_plan(root, trace["trace_id"], "checkout-confirmation")

            self.assertTrue(plan["generic_draft_check"]["ok"])
            self.assertEqual(plan["generic_draft"]["lifecycle"]["state"], "inactive")
            self.assertFalse(plan["generic_draft"]["lifecycle"]["installed"])
            self.assertEqual(plan["generic_draft_digest"], hashlib.sha256(
                _canonical(plan["generic_draft"])
            ).hexdigest())
            self.assertFalse((root / ".omh" / "learning" / "skill-drafts").exists())

    def test_diff_replaces_entry_and_adds_resources_without_deleting_unmanaged_files(self) -> None:
        with _project() as root:
            trace = _passing_trace(root)
            target = root / ".hermes" / "skills" / "checkout-confirmation"
            target.mkdir(parents=True)
            (target / "SKILL.md").write_text("old entry\n", encoding="utf-8")
            (target / "obsolete.txt").write_text("remove me\n", encoding="utf-8")

            plan = build_browser_skill_promotion_plan(root, trace["trace_id"], "checkout-confirmation")

            self.assertIn(f"--- {target / 'SKILL.md'}", plan["diff"])
            self.assertIn(f"+++ {target / 'SKILL.md'}", plan["diff"])
            self.assertIn("-old entry", plan["diff"])
            self.assertNotIn(str(target / "obsolete.txt"), plan["diff"])
            self.assertNotIn("-remove me", plan["diff"])
            self.assertIn(f"+++ {target / 'resources' / plan['generation'] / 'trace.json'}", plan["diff"])
            self.assertEqual(plan["diff_digest"], hashlib.sha256(plan["diff"].encode("utf-8")).hexdigest())

    def test_prior_generation_is_in_reviewed_entry_before_package_and_diff_digests(self) -> None:
        with _project() as root:
            trace = _passing_trace(root)
            first = build_browser_skill_promotion_plan(root, trace["trace_id"], "checkout-confirmation")
            second = build_browser_skill_promotion_plan(
                root,
                trace["trace_id"],
                "checkout-confirmation",
                previous_generation=first["generation"],
                existing_files=first["package"],
            )

            self.assertNotEqual(first["generation"], second["generation"])
            metadata = json.loads(next(line.split(": ", 1)[1] for line in second["package"]["SKILL.md"].splitlines() if line.startswith("omh_browser_promotion: ")))
            self.assertEqual(metadata["previous_generation"], first["generation"])
            self.assertNotEqual(first["package_digest"], second["package_digest"])
            self.assertNotEqual(first["diff_digest"], second["diff_digest"])
            manifest = second["package"][f"resources/{second['generation']}/manifest.json"]
            self.assertIn(hashlib.sha256(second["package"]["SKILL.md"].encode()).hexdigest(), manifest)
            self.assertNotIn(hashlib.sha256(manifest.encode()).hexdigest(), manifest)

    def test_plan_refuses_any_non_passing_public_projection(self) -> None:
        with _project() as root:
            trace = _passing_trace(root)
            reference = dict(resolved_browser_workflow_promotion_reference(root, trace["trace_id"]))
            for key, value in (("replay_status", "stale"), ("lifecycle_status", "quarantined"), ("trace_revision", 0)):
                with self.subTest(key=key):
                    changed = dict(reference)
                    changed[key] = value
                    with self.assertRaisesRegex(BrowserSkillPromotionPlanError, "promotion reference"):
                        build_browser_skill_promotion_plan(
                            root,
                            trace["trace_id"],
                            "checkout-confirmation",
                            reference=changed,
                        )


def _project():
    class Project:
        def __enter__(self) -> Path:
            self.temporary_directory = TemporaryDirectory()
            self.root = Path(self.temporary_directory.name).resolve() / "project"
            self.root.mkdir()
            subprocess.run(["git", "init", "-q", str(self.root)], check=True, capture_output=True)
            return self.root

        def __exit__(self, *args: object) -> None:
            self.temporary_directory.cleanup()

    return Project()


def _passing_trace(root: Path) -> dict[str, object]:
    from test_browser_workflow_learning import _trace

    trace = write_browser_workflow_trace(_trace("click"), root)
    approve_browser_workflow_trace(root, str(trace["trace_id"]), str(trace["digest"]))
    replay_stored_browser_workflow_trace(root, str(trace["trace_id"]), {"fixture_id": "positive"})
    return read_browser_workflow_trace(root, str(trace["trace_id"]))


def _canonical(value: object) -> bytes:
    import json

    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8")
