"""Tests for symlinking shared build artifacts into fanout unit worktrees.

Covers the module directly (`fanout_artifact_sharing`) and its wiring into
`dispatch_fanout` / the observation journal / `fanout_retry`'s replay-safety
predicate, per the backlog item "Symlink build artifacts into fanout
worktrees."
"""

from __future__ import annotations

import os
from pathlib import Path
import subprocess
from tempfile import TemporaryDirectory
import unittest
from unittest import mock

from _local_package import load_local_package
from _platform_support import requires_symlinks

load_local_package()

from omh.coding.fanout import build_fanout_contract  # noqa: E402
from omh.coding.fanout_artifact_sharing import (  # noqa: E402
    OPT_OUT_ENV_VAR,
    SHAREABLE_ARTIFACT_ALLOWLIST,
    SHARED_ARTIFACT_SCHEMA_VERSION,
    plan_and_link_shared_artifacts,
    shared_artifacts_opted_out,
)
from omh.coding.fanout_artifacts import write_fanout_contract  # noqa: E402
from omh.coding.fanout_dispatch import dispatch_fanout  # noqa: E402
from omh.runtime.artifacts import read_observation_events_result  # noqa: E402
from omh.system.paths import OmhPaths  # noqa: E402


def _git(repo: Path, *argv: str) -> None:
    subprocess.run(["git", *argv], cwd=str(repo), check=True, capture_output=True, text=True)


def _make_repo(root: Path, *, gitignore_lines: list[str] | None = None) -> tuple[Path, str]:
    repo = root / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    (repo / "seed.txt").write_text("seed\n", encoding="utf-8")
    _git(repo, "add", "seed.txt")
    if gitignore_lines:
        (repo / ".gitignore").write_text("\n".join(gitignore_lines) + "\n", encoding="utf-8")
        _git(repo, "add", ".gitignore")
    _git(repo, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "-m", "init")
    sha = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=str(repo), capture_output=True, text=True, check=True
    ).stdout.strip()
    return repo, sha


def _commit_all(repo: Path, message: str) -> str:
    _git(repo, "add", "-A")
    _git(repo, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "-m", message)
    return subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=str(repo), capture_output=True, text=True, check=True
    ).stdout.strip()


def _add_worktree(repo: Path, branch: str, base_sha: str, worktree_path: Path) -> None:
    subprocess.run(
        ["git", "worktree", "add", str(worktree_path), "-b", branch, base_sha],
        cwd=str(repo),
        check=True,
        capture_output=True,
        text=True,
    )


class _FakeCompleted:
    def __init__(self, returncode: int, stdout: str = "") -> None:
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = ""


def _agent_runner(*, fail_units: set[str] | None = None):
    """Route git commands to the real subprocess; fake agent CLI spawns."""

    def runner(argv, **kwargs):
        if argv[0] == "git":
            return subprocess.run(argv, **kwargs)
        prompt = " ".join(argv)
        for unit_id in fail_units or set():
            if unit_id in prompt:
                return _FakeCompleted(1, f"unit {unit_id} failed")
        return _FakeCompleted(0, "done")

    return runner


def _ready(paths, profile, **kwargs):
    return {"status": "ready", "profile": profile}


class SharedArtifactAllowlistTests(unittest.TestCase):
    """`plan_and_link_shared_artifacts` called directly against real git worktrees."""

    def test_allowlist_entries_carry_a_rationale(self) -> None:
        for entry in SHAREABLE_ARTIFACT_ALLOWLIST:
            self.assertTrue(entry["name"])
            self.assertTrue(entry["rationale"])

    @requires_symlinks
    def test_links_an_allowlisted_gitignored_directory(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo, sha = _make_repo(root, gitignore_lines=["node_modules"])
            (repo / "node_modules").mkdir()
            (repo / "node_modules" / "pkg.js").write_text("module.exports = 1;\n", encoding="utf-8")
            worktree = root / "unit-worktree"
            _add_worktree(repo, "agent/unit-a", sha, worktree)

            result = plan_and_link_shared_artifacts(repo_root=repo, worktree_path=worktree, runner=subprocess.run)

            self.assertEqual(result["schema_version"], SHARED_ARTIFACT_SCHEMA_VERSION)
            self.assertFalse(result["opted_out"])
            self.assertIn("node_modules", result["linked"])
            entry = next(e for e in result["entries"] if e["name"] == "node_modules")
            self.assertEqual(entry["action"], "linked")
            self.assertEqual(entry["reason"], "")
            self.assertTrue(entry["rationale"])
            self.assertTrue((worktree / "node_modules").is_symlink())
            self.assertEqual(
                (worktree / "node_modules" / "pkg.js").read_text(encoding="utf-8"),
                "module.exports = 1;\n",
            )

    def test_gitignore_guard_never_links_a_tracked_directory(self) -> None:
        # "build" is TRACKED in the parent checkout (never gitignored). A
        # fresh unit worktree therefore already contains it as a real
        # directory, and the guard must never displace real, checked-out
        # source with a symlink -- the exact MUST NOT the absorbed precedent
        # names.
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo, sha = _make_repo(root)
            (repo / "build").mkdir()
            (repo / "build" / "tracked_source.py").write_text("value = 1\n", encoding="utf-8")
            sha2 = _commit_all(repo, "track build/")
            worktree = root / "unit-worktree"
            _add_worktree(repo, "agent/unit-b", sha2, worktree)

            result = plan_and_link_shared_artifacts(repo_root=repo, worktree_path=worktree, runner=subprocess.run)

            self.assertNotIn("build", result["linked"])
            entry = next(e for e in result["entries"] if e["name"] == "build")
            self.assertEqual(entry["action"], "skipped")
            self.assertEqual(entry["reason"], "path_exists_in_worktree")
            self.assertFalse((worktree / "build").is_symlink())
            self.assertEqual(
                (worktree / "build" / "tracked_source.py").read_text(encoding="utf-8"), "value = 1\n"
            )

    def test_present_but_not_gitignored_source_is_never_linked(self) -> None:
        # Untracked in the parent AND not covered by any .gitignore rule: the
        # guard must refuse on the gitignore check alone.
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo, sha = _make_repo(root)
            (repo / "target").mkdir()
            (repo / "target" / "f.bin").write_bytes(b"x")
            worktree = root / "unit-worktree"
            _add_worktree(repo, "agent/unit-c", sha, worktree)

            result = plan_and_link_shared_artifacts(repo_root=repo, worktree_path=worktree, runner=subprocess.run)

            self.assertNotIn("target", result["linked"])
            entry = next(e for e in result["entries"] if e["name"] == "target")
            self.assertEqual(entry["reason"], "source_not_gitignored")
            self.assertFalse((worktree / "target").exists())

    def test_missing_source_falls_back_for_every_entry(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            names = [entry["name"] for entry in SHAREABLE_ARTIFACT_ALLOWLIST]
            repo, sha = _make_repo(root, gitignore_lines=names)
            worktree = root / "unit-worktree"
            _add_worktree(repo, "agent/unit-d", sha, worktree)

            result = plan_and_link_shared_artifacts(repo_root=repo, worktree_path=worktree, runner=subprocess.run)

            self.assertEqual(result["linked"], [])
            for entry in result["entries"]:
                self.assertEqual(entry["reason"], "source_missing")

    def test_opt_out_env_var_disables_linking_entirely(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo, sha = _make_repo(root, gitignore_lines=["node_modules"])
            (repo / "node_modules").mkdir()
            (repo / "node_modules" / "pkg.js").write_text("x\n", encoding="utf-8")
            worktree = root / "unit-worktree"
            _add_worktree(repo, "agent/unit-e", sha, worktree)

            result = plan_and_link_shared_artifacts(
                repo_root=repo,
                worktree_path=worktree,
                runner=subprocess.run,
                env={OPT_OUT_ENV_VAR: "0"},
            )

            self.assertTrue(result["opted_out"])
            self.assertEqual(result["entries"], [])
            self.assertEqual(result["linked"], [])
            self.assertFalse((worktree / "node_modules").exists())

    def test_shared_artifacts_opted_out_helper(self) -> None:
        self.assertFalse(shared_artifacts_opted_out({}))
        self.assertFalse(shared_artifacts_opted_out({OPT_OUT_ENV_VAR: "1"}))
        self.assertTrue(shared_artifacts_opted_out({OPT_OUT_ENV_VAR: "0"}))

    @requires_symlinks
    def test_dir_only_gitignore_pattern_is_recognized_and_refused_after_linking(self) -> None:
        # The empirically-verified hazard this module's second guard exists
        # for: a trailing-slash, dir-only gitignore pattern (`node_modules/`)
        # matches a real directory but NOT a symlink of the same name, because
        # git's dir-only match uses the on-disk type from lstat and a symlink
        # is never a directory to it. Left unguarded, the freshly created
        # symlink would show up as an untracked change in the unit worktree.
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo, sha = _make_repo(root, gitignore_lines=["node_modules/"])
            (repo / "node_modules").mkdir()
            (repo / "node_modules" / "pkg.js").write_text("x\n", encoding="utf-8")
            worktree = root / "unit-worktree"
            _add_worktree(repo, "agent/unit-f", sha, worktree)

            result = plan_and_link_shared_artifacts(repo_root=repo, worktree_path=worktree, runner=subprocess.run)

            entry = next(e for e in result["entries"] if e["name"] == "node_modules")
            self.assertEqual(entry["action"], "skipped")
            self.assertEqual(entry["reason"], "symlink_not_recognized_ignored")
            self.assertNotIn("node_modules", result["linked"])
            # The undo actually ran: nothing left behind for the unit to trip on.
            self.assertFalse((worktree / "node_modules").exists())

    def test_platform_unsupported_records_every_entry(self) -> None:
        # Runnable on every platform, including a real Windows CI host: it
        # exercises the DECISION only, never an actual symlink syscall, by
        # monkeypatching `_symlinks_supported` directly rather than gating on
        # POSIX. Every allowlist directory is real and gitignored here so each
        # one clears every earlier guard and reaches the platform check --
        # otherwise an entry with no source would report `source_missing`
        # instead, the same way it does on a platform that DOES support
        # symlinks.
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            names = [entry["name"] for entry in SHAREABLE_ARTIFACT_ALLOWLIST]
            repo, sha = _make_repo(root, gitignore_lines=names)
            for name in names:
                artifact_dir = repo / name
                artifact_dir.mkdir()
                (artifact_dir / "f.txt").write_text("x\n", encoding="utf-8")
            worktree = root / "unit-worktree"
            _add_worktree(repo, "agent/unit-g", sha, worktree)

            from omh.coding import fanout_artifact_sharing as sharing_module

            with mock.patch.object(sharing_module, "_symlinks_supported", return_value=False):
                result = plan_and_link_shared_artifacts(
                    repo_root=repo, worktree_path=worktree, runner=subprocess.run
                )

            self.assertEqual(result["linked"], [])
            for entry in result["entries"]:
                self.assertEqual(entry["reason"], "platform_unsupported")
            for name in names:
                self.assertFalse((worktree / name).exists())


class FanoutDispatchSharedArtifactsIntegrationTests(unittest.TestCase):
    """Wiring into `dispatch_fanout`: the unit result and journal both note it."""

    def _setup(self, tmp: str):
        root = Path(tmp)
        paths = OmhPaths(omh_home=root / ".omh", hermes_home=root / ".hermes")
        repo, sha = _make_repo(root, gitignore_lines=["node_modules"])
        (repo / "node_modules").mkdir()
        (repo / "node_modules" / "pkg.js").write_text("x\n", encoding="utf-8")
        goal = "warm-start a fanout unit"
        units = [{"unit_id": "core", "title": "Core work", "owner": "codex", "file_scope": ["src/core/"]}]
        contract = write_fanout_contract(paths, build_fanout_contract(goal, units))
        return paths, repo, sha, contract, goal

    @requires_symlinks
    def test_completed_unit_result_carries_shared_artifacts(self) -> None:
        with TemporaryDirectory() as tmp:
            paths, repo, sha, contract, goal = self._setup(tmp)

            summary = dispatch_fanout(
                paths,
                contract,
                goal_text=goal,
                repo_root=repo,
                base_sha=sha,
                runner=_agent_runner(),
                readiness=_ready,
            )

            core = {entry["unit_id"]: entry for entry in summary["units"]}["core"]
            self.assertIn("shared_artifacts", core)
            self.assertEqual(core["shared_artifacts"]["schema_version"], SHARED_ARTIFACT_SCHEMA_VERSION)
            self.assertIn("node_modules", core["shared_artifacts"]["linked"])
            worktree = Path(core["worktree_path"])
            self.assertTrue((worktree / "node_modules").is_symlink())

    @requires_symlinks
    def test_journal_worker_dispatch_event_notes_shared_artifacts(self) -> None:
        with TemporaryDirectory() as tmp:
            paths, repo, sha, contract, goal = self._setup(tmp)

            dispatch_fanout(
                paths,
                contract,
                goal_text=goal,
                repo_root=repo,
                base_sha=sha,
                runner=_agent_runner(),
                readiness=_ready,
            )

            events, errors = read_observation_events_result(paths)
            self.assertEqual(errors, [])
            dispatch_events = [
                event
                for event in events
                if event.get("event") == "executor_dispatch_observed" and event.get("worker_ref") == "core"
            ]
            self.assertTrue(dispatch_events)
            self.assertIn("shared_artifacts: node_modules", dispatch_events[-1]["summary"])

    def test_opted_out_dispatch_carries_no_linked_artifacts(self) -> None:
        with TemporaryDirectory() as tmp:
            paths, repo, sha, contract, goal = self._setup(tmp)

            with mock.patch.dict(os.environ, {OPT_OUT_ENV_VAR: "0"}):
                summary = dispatch_fanout(
                    paths,
                    contract,
                    goal_text=goal,
                    repo_root=repo,
                    base_sha=sha,
                    runner=_agent_runner(),
                    readiness=_ready,
                )

            core = {entry["unit_id"]: entry for entry in summary["units"]}["core"]
            self.assertTrue(core["shared_artifacts"]["opted_out"])
            self.assertEqual(core["shared_artifacts"]["linked"], [])
            worktree = Path(core["worktree_path"])
            self.assertFalse((worktree / "node_modules").exists())

    @requires_symlinks
    def test_replay_safety_is_unaffected_by_a_linked_artifact(self) -> None:
        # The rail this whole task is really about: a unit that fails
        # transiently but touches nothing of its own must still classify as
        # replay-safe (`recovery.outcome == "no_changes"`) even though its
        # worktree holds a live symlink into the parent's node_modules. If the
        # symlinked directory leaked into git's diff/status view, this would
        # misreport `recovery_available` for a unit that did nothing.
        with TemporaryDirectory() as tmp:
            paths, repo, sha, contract, goal = self._setup(tmp)

            summary = dispatch_fanout(
                paths,
                contract,
                goal_text=goal,
                repo_root=repo,
                base_sha=sha,
                runner=_agent_runner(fail_units={"core"}),
                readiness=_ready,
            )

            core = {entry["unit_id"]: entry for entry in summary["units"]}["core"]
            self.assertIn("node_modules", core["shared_artifacts"]["linked"])
            self.assertEqual(core["status"], "failed")
            self.assertEqual(core["recovery"]["outcome"], "no_changes")
            self.assertEqual(summary["recovery_available_units"], [])


if __name__ == "__main__":
    unittest.main()
