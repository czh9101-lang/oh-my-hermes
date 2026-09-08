from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from _cli_harness import run_cli


class DesignDirectionIterationsCliTests(unittest.TestCase):
    def test_Given_prepared_iteration_When_shown_revised_selected_Then_cli_preserves_the_current_lineage(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "omh"
            preview = Path(tmp) / "preview.html"
            base = [
                "--omh-home", str(home), "ops", "design-direction-iterations", "prepare",
                "--surface", "workflow_screen", "--audience", "operator", "--primary-task", "decide",
                "--platform", "web", "--mode", "new",
                "--context-reference", "design_system:design_ref_a1b2c3d4e5f60718:project_local",
                "--option", "a:task_first:restrained_neutral:system_sans:single_column:progress_trace:placeholder_copy",
                "--option", "b:evidence_first:contextual_accent:editorial_serif:split_panel:evidence_rail:generic_glass",
                "--source-revision-digest", "a" * 64,
                "--criteria-revision", "direction-fit-v1", "--criteria-dimension", "clarity", "--score-threshold", "90",
                "--html", str(preview),
            ]
            code, stdout, _ = run_cli(base)
            self.assertEqual(code, 0)
            prepared = json.loads(stdout)["iteration"]
            self.assertTrue(preview.is_file())
            self.assertIn(prepared["snapshots"][0]["revision_digest"], preview.read_text(encoding="utf-8"))

            iteration_id = prepared["iteration_id"]
            parent = prepared["snapshots"][-1]["revision_digest"]
            revised_args = [
                "--omh-home", str(home), "ops", "design-direction-iterations", "revise", iteration_id,
                "--parent-revision-digest", parent, "--feedback-reference", "feedback-001", "--feedback-delta", "palette",
                "--surface", "workflow_screen", "--audience", "operator", "--primary-task", "decide", "--platform", "web", "--mode", "new",
                "--context-reference", "design_system:design_ref_a1b2c3d4e5f60718:project_local",
                "--option", "a:task_first:restrained_neutral:system_sans:single_column:progress_trace:placeholder_copy",
                "--option", "b:evidence_first:restrained_neutral:editorial_serif:split_panel:evidence_rail:generic_glass",
                "--successor", "preserved:a:a", "--successor", "revised:b:b",
                "--criteria-revision", "direction-fit-v1", "--criteria-dimension", "clarity",
            ]
            code, stdout, _ = run_cli(revised_args)
            self.assertEqual(code, 0)
            revised = json.loads(stdout)["iteration"]

            code, stdout, _ = run_cli(["--omh-home", str(home), "ops", "design-direction-iterations", "show", iteration_id])
            self.assertEqual(code, 0)
            shown = json.loads(stdout)["iteration"]
            self.assertEqual(shown["snapshots"][-1]["revision_digest"], revised["snapshots"][-1]["revision_digest"])

            option_ref = revised["snapshots"][-1]["option_refs"][1]
            code, stdout, _ = run_cli(["--omh-home", str(home), "ops", "design-direction-iterations", "select", iteration_id, "--option-ref", option_ref, "--remember-this"])
            self.assertEqual(code, 0)
            selected = json.loads(stdout)["iteration"]
            self.assertEqual(selected["terminal"]["accepted_option_ref"], option_ref)
            self.assertEqual(selected["memory_promotion"]["request"]["action"], "memory-new")

    def test_Given_old_parent_When_revised_Then_cli_refuses_the_stale_preview(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "omh"
            args = [
                "--omh-home", str(home), "ops", "design-direction-iterations", "prepare",
                "--surface", "workflow_screen", "--audience", "operator", "--primary-task", "decide", "--platform", "web", "--mode", "new",
                "--context-reference", "design_system:design_ref_a1b2c3d4e5f60718:project_local",
                "--option", "a:task_first:restrained_neutral:system_sans:single_column:progress_trace:placeholder_copy",
                "--option", "b:evidence_first:contextual_accent:editorial_serif:split_panel:evidence_rail:generic_glass",
                "--source-revision-digest", "a" * 64, "--criteria-revision", "direction-fit-v1", "--criteria-dimension", "clarity", "--score-threshold", "90",
            ]
            _, stdout, _ = run_cli(args)
            prepared = json.loads(stdout)["iteration"]
            stale = prepared["snapshots"][0]["revision_digest"]
            revise = [
                "--omh-home", str(home), "ops", "design-direction-iterations", "revise", prepared["iteration_id"],
                "--parent-revision-digest", stale, "--feedback-reference", "feedback-001", "--feedback-delta", "palette",
                "--surface", "workflow_screen", "--audience", "operator", "--primary-task", "decide", "--platform", "web", "--mode", "new",
                "--context-reference", "design_system:design_ref_a1b2c3d4e5f60718:project_local",
                "--option", "a:task_first:restrained_neutral:system_sans:single_column:progress_trace:placeholder_copy",
                "--option", "b:evidence_first:restrained_neutral:editorial_serif:split_panel:evidence_rail:generic_glass",
                "--successor", "preserved:a:a", "--successor", "revised:b:b", "--criteria-revision", "direction-fit-v1", "--criteria-dimension", "clarity",
            ]
            self.assertEqual(run_cli(revise)[0], 0)
            stale_revise = [*revise]
            stale_revise[stale_revise.index("feedback-001")] = "feedback-002"
            code, _, stderr = run_cli(stale_revise)
            self.assertNotEqual(code, 0)
            self.assertIn("stale parent", stderr)


if __name__ == "__main__":
    unittest.main()
