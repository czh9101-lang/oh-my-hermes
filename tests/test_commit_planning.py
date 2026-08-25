"""Contracts for the deterministic commit-split planner.

The plan operationalizes the five Commit Planning rules from
wrapper-routing.md: complete non-overlapping coverage, lockfile-manifest
pairing, fixed dependency order, bounded reviewable groups, and a stated
claim boundary (a prepared plan is never a commit).
"""

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from omh.coding.commit_planning import (
    COMMIT_PLAN_SCHEMA_VERSION,
    ChangedFile,
    CommitPlanError,
    build_commit_plan,
    parse_status_porcelain_z,
)


def _paths(plan):
    return sorted(path for commit in plan["commits"] for path in commit["files"])


class PorcelainParsingTest(unittest.TestCase):
    def test_parses_modified_added_deleted_and_untracked(self):
        payload = " M src/a.py\0A  src/b.py\0 D docs/gone.md\0?? notes.txt\0"
        files = parse_status_porcelain_z(payload)
        self.assertEqual(
            [(f.path, f.status) for f in files],
            [("src/a.py", "M"), ("src/b.py", "A"), ("docs/gone.md", "D"), ("notes.txt", "?")],
        )

    def test_rename_entries_carry_the_original_path(self):
        payload = "R  src/new.py\0src/old.py\0 M other.py\0"
        files = parse_status_porcelain_z(payload)
        self.assertEqual(files[0].path, "src/new.py")
        self.assertEqual(files[0].renamed_from, "src/old.py")
        self.assertEqual(files[1].path, "other.py")

    def test_garbage_entries_are_rejected(self):
        with self.assertRaises(CommitPlanError):
            parse_status_porcelain_z("garbage-without-status\0")


class PlanInvariantsTest(unittest.TestCase):
    def test_every_file_lands_in_exactly_one_commit(self):
        files = [
            ChangedFile("src/pkg/core.py", "M"),
            ChangedFile("tests/test_core.py", "M"),
            ChangedFile("docs/guide.md", "M"),
            ChangedFile("pyproject.toml", "M"),
            ChangedFile("uv.lock", "M"),
            ChangedFile(".editorconfig", "M"),
        ]
        plan = build_commit_plan(files)
        self.assertEqual(plan["schema_version"], COMMIT_PLAN_SCHEMA_VERSION)
        self.assertEqual(_paths(plan), sorted(f.path for f in files))
        self.assertEqual(plan["changed_file_count"], len(files))

    def test_manifest_and_lockfile_share_a_commit(self):
        plan = build_commit_plan(
            [
                ChangedFile("pyproject.toml", "M"),
                ChangedFile("uv.lock", "M"),
                ChangedFile("src/app.py", "M"),
            ]
        )
        deps_commits = [c for c in plan["commits"] if c["category"] == "deps"]
        self.assertEqual(len(deps_commits), 1)
        self.assertEqual(sorted(deps_commits[0]["files"]), ["pyproject.toml", "uv.lock"])

    def test_monorepo_workspace_manifest_joins_the_root_lockfile_commit(self):
        # A per-directory split would land a lockfile describing a manifest
        # that has not changed yet; the single deps group prevents it.
        plan = build_commit_plan(
            [
                ChangedFile("apps/web/package.json", "M"),
                ChangedFile("package-lock.json", "M"),
            ]
        )
        deps_commits = [c for c in plan["commits"] if c["category"] == "deps"]
        self.assertEqual(len(deps_commits), 1)
        self.assertEqual(
            sorted(deps_commits[0]["files"]),
            ["apps/web/package.json", "package-lock.json"],
        )

    def test_renames_carry_their_old_path_for_staging(self):
        plan = build_commit_plan(
            [ChangedFile("src/new.py", "R", renamed_from="src/old.py")]
        )
        commit = plan["commits"][0]
        self.assertEqual(commit["renames"], {"src/new.py": "src/old.py"})

    def test_unmerged_entries_are_a_named_refusal(self):
        with self.assertRaises(CommitPlanError):
            parse_status_porcelain_z("UU conflicted.py\0")

    def test_duplicate_paths_are_a_named_refusal(self):
        with self.assertRaises(CommitPlanError):
            build_commit_plan([ChangedFile("src/a.py", "M"), ChangedFile("src/a.py", "A")])

    def test_doc_and_config_suffixes_outrank_test_directory_ancestry(self):
        plan = build_commit_plan(
            [
                ChangedFile("docs/spec/api.md", "M"),
                ChangedFile("spec/openapi.yaml", "M"),
            ]
        )
        by_path = {path: c["category"] for c in plan["commits"] for path in c["files"]}
        self.assertEqual(by_path["docs/spec/api.md"], "docs")
        self.assertEqual(by_path["spec/openapi.yaml"], "config")

    def test_test_pairs_with_same_stem_source(self):
        plan = build_commit_plan(
            [
                ChangedFile("src/pkg/router.py", "M"),
                ChangedFile("tests/test_router.py", "M"),
                ChangedFile("tests/test_unrelated.py", "M"),
            ]
        )
        paired = [c for c in plan["commits"] if "src/pkg/router.py" in c["files"]]
        self.assertEqual(len(paired), 1)
        self.assertIn("tests/test_router.py", paired[0]["files"])
        standalone = [c for c in plan["commits"] if "tests/test_unrelated.py" in c["files"]]
        self.assertEqual(standalone[0]["category"], "tests")

    def test_fixed_category_order(self):
        plan = build_commit_plan(
            [
                ChangedFile("docs/guide.md", "M"),
                ChangedFile("src/app.py", "M"),
                ChangedFile("package.json", "M"),
                ChangedFile("package-lock.json", "M"),
                ChangedFile(".prettierrc.json", "M"),
                ChangedFile("tests/test_extra.py", "M"),
            ]
        )
        categories = [commit["category"] for commit in plan["commits"]]
        self.assertEqual(categories, sorted(categories, key=["deps", "source", "tests", "docs", "config"].index))
        self.assertEqual(plan["commits"][0]["category"], "deps")

    def test_deterministic_output(self):
        files = [
            ChangedFile("src/b.py", "M"),
            ChangedFile("src/a.py", "M"),
            ChangedFile("docs/x.md", "M"),
        ]
        first = json.dumps(build_commit_plan(files), sort_keys=True)
        second = json.dumps(build_commit_plan(list(reversed(files))), sort_keys=True)
        self.assertEqual(first, second)

    def test_claim_boundary_is_present(self):
        plan = build_commit_plan([ChangedFile("src/a.py", "M")])
        self.assertIn("not a commit", plan["claim_boundary"])

    def test_bounded_file_count(self):
        files = [ChangedFile(f"src/f{i}.py", "M") for i in range(2001)]
        with self.assertRaises(CommitPlanError):
            build_commit_plan(files)

    def test_empty_path_is_rejected(self):
        with self.assertRaises(CommitPlanError):
            build_commit_plan([ChangedFile("", "M")])


class CliIntegrationTest(unittest.TestCase):
    def test_commit_plan_reads_a_real_repo_read_only(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            subprocess.run(["git", "init", "-q"], cwd=root, check=True)
            (root / "src").mkdir()
            (root / "src" / "app.py").write_text("print('hi')\n", encoding="utf-8")
            (root / "README.md").write_text("# demo\n", encoding="utf-8")
            head_before = subprocess.run(
                ["git", "status", "--porcelain"], cwd=root, capture_output=True, text=True, check=True
            ).stdout
            result = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "omh.cli",
                    "coding",
                    "commit-plan",
                    "--repo-root",
                    str(root),
                ],
                capture_output=True,
                text=True,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            plan = json.loads(result.stdout)
            self.assertEqual(plan["schema_version"], COMMIT_PLAN_SCHEMA_VERSION)
            self.assertEqual(
                sorted(p for c in plan["commits"] for p in c["files"]),
                ["README.md", "src/app.py"],
            )
            # Read-only proof: the probe changed nothing.
            head_after = subprocess.run(
                ["git", "status", "--porcelain"], cwd=root, capture_output=True, text=True, check=True
            ).stdout
            self.assertEqual(head_before, head_after)

    def test_status_file_input_is_hermetic(self):
        with tempfile.TemporaryDirectory() as tmp:
            status_file = Path(tmp) / "status.z"
            status_file.write_bytes(b" M src/a.py\x00?? notes.txt\x00")
            result = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "omh.cli",
                    "coding",
                    "commit-plan",
                    "--status-file",
                    str(status_file),
                ],
                capture_output=True,
                text=True,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            plan = json.loads(result.stdout)
            self.assertEqual(plan["changed_file_count"], 2)


if __name__ == "__main__":
    unittest.main()
