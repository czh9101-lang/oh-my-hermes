"""Contract tests for revision-bound release evidence (issue #1280).

The release evidence bundle must bind the evidence it aggregates to the exact
source revision it was generated from: schema `omh_release_evidence_bundle/v2`
with a `source_identity` block, a deterministic input manifest, and a pure
verifier that re-computes and compares without ever writing or regenerating.
"""

from __future__ import annotations

import json
import os
import subprocess
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace

from _cli_harness import run_cli

from omh.maintenance.release import release_evidence_bundle
from omh.maintenance.release_identity import (
    VERIFY_VERDICTS,
    build_input_manifest,
    probe_source_identity,
    verify_release_evidence_bundle,
)
from omh.paths import OmhPaths

COMMIT = "a" * 40
TREE = "b" * 40
OTHER_COMMIT = "c" * 40
OTHER_TREE = "d" * 40


def fake_git_runner(*, commit: str = COMMIT, tree: str = TREE, porcelain: str = "", fail: bool = False):
    def runner(command, **kwargs):
        if fail:
            raise OSError("git is not available")
        args = list(command)
        if args[:2] == ["git", "rev-parse"] and len(args) == 3 and args[2] == "HEAD":
            return SimpleNamespace(returncode=0, stdout=commit + "\n", stderr="")
        if args[:2] == ["git", "rev-parse"] and len(args) == 3:
            return SimpleNamespace(returncode=0, stdout=tree + "\n", stderr="")
        if args[:2] == ["git", "status"]:
            return SimpleNamespace(returncode=0, stdout=porcelain, stderr="")
        return SimpleNamespace(returncode=1, stdout="", stderr="unexpected command")

    return runner


def recorded_bundle(paths: OmhPaths, *, commit: str = COMMIT, tree: str = TREE, artifact: Path | None = None) -> dict:
    identity = probe_source_identity("/repo", runner=fake_git_runner(commit=commit, tree=tree))
    identity["input_manifest"] = build_input_manifest(source_identity=identity, paths=paths, artifact=artifact)
    return {
        "schema_version": "omh_release_evidence_bundle/v2",
        "version": "1.0.1",
        "source_identity": identity,
    }


def init_git_repo(root: Path) -> None:
    for command in (
        ["git", "init"],
        ["git", "config", "user.email", "omh-tests@example.test"],
        ["git", "config", "user.name", "OMH Tests"],
    ):
        subprocess.run(command, cwd=root, check=True, capture_output=True)
    (root / "README.md").write_text("fixture\n", encoding="utf-8")
    subprocess.run(["git", "add", "README.md"], cwd=root, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "fixture"], cwd=root, check=True, capture_output=True)


def snapshot_tree(root: Path) -> dict[str, bytes]:
    if not root.exists():
        return {}
    return {
        path.relative_to(root).as_posix(): path.read_bytes()
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


class ReleaseRevisionBindingContractTests(unittest.TestCase):
    def test_evidence_bundle_is_v2_and_records_source_identity(self) -> None:
        with TemporaryDirectory() as tmp:
            paths = OmhPaths(omh_home=Path(tmp) / ".omh", hermes_home=Path(tmp) / ".hermes")

            payload = release_evidence_bundle(version="v1.0.1", omh_command="/tmp/omh command", paths=paths)

        self.assertEqual(payload.get("schema_version"), "omh_release_evidence_bundle/v2")
        self.assertIn("source_identity", payload)

    def test_v2_identity_records_full_hashes_for_a_clean_git_checkout(self) -> None:
        with TemporaryDirectory() as tmp:
            paths = OmhPaths(omh_home=Path(tmp) / ".omh", hermes_home=Path(tmp) / ".hermes")

            payload = release_evidence_bundle(
                version="v1.0.1",
                paths=paths,
                repo_root="/repo",
                runner=fake_git_runner(),
            )

        identity = payload["source_identity"]
        self.assertEqual(identity["schema_version"], "omh_release_source_identity/v1")
        self.assertEqual(identity["origin"], "git_checkout")
        self.assertEqual(identity["identity_status"], "available")
        self.assertEqual(identity["commit_sha"], COMMIT)
        self.assertEqual(identity["tree_sha"], TREE)
        self.assertIs(identity["dirty"], False)
        self.assertEqual(identity["dirty_file_count"], 0)
        self.assertTrue(payload["publication_ready"])
        self.assertEqual(payload["status"], "ready")
        manifest = identity["input_manifest"]
        self.assertEqual(manifest["schema_version"], "omh_release_input_manifest/v1")
        self.assertEqual(manifest["source_tree_sha"], TREE)
        self.assertTrue(str(manifest["digest"]).startswith("sha256:"))

    def test_verify_verdict_vocabulary_is_closed(self) -> None:
        self.assertEqual(
            VERIFY_VERDICTS,
            ("matching", "dirty", "mismatched_revision", "stale", "unverifiable", "legacy_schema", "missing"),
        )

    def test_verify_matching_on_unchanged_inputs(self) -> None:
        with TemporaryDirectory() as tmp:
            paths = OmhPaths(omh_home=Path(tmp) / ".omh", hermes_home=Path(tmp) / ".hermes")
            bundle = recorded_bundle(paths)

            verdict = verify_release_evidence_bundle(bundle, repo_root="/repo", paths=paths, runner=fake_git_runner())

        self.assertEqual(verdict["verification"], "matching")
        self.assertEqual(verdict["schema_version"], "omh_release_evidence_verification/v1")
        self.assertEqual(verdict["reasons"], [])

    def test_dirty_tree_verifies_dirty_and_dominates_stale(self) -> None:
        with TemporaryDirectory() as tmp:
            paths = OmhPaths(omh_home=Path(tmp) / ".omh", hermes_home=Path(tmp) / ".hermes")
            bundle = recorded_bundle(paths)
            dirty_runner = fake_git_runner(tree=OTHER_TREE, porcelain=" M README.md\n?? scratch.txt\n")

            verdict = verify_release_evidence_bundle(bundle, repo_root="/repo", paths=paths, runner=dirty_runner)

        self.assertEqual(verdict["verification"], "dirty")

    def test_different_head_commit_is_mismatched_revision(self) -> None:
        with TemporaryDirectory() as tmp:
            paths = OmhPaths(omh_home=Path(tmp) / ".omh", hermes_home=Path(tmp) / ".hermes")
            bundle = recorded_bundle(paths)

            verdict = verify_release_evidence_bundle(
                bundle,
                repo_root="/repo",
                paths=paths,
                runner=fake_git_runner(commit=OTHER_COMMIT, tree=OTHER_TREE),
            )

        self.assertEqual(verdict["verification"], "mismatched_revision")

    def test_v1_bundle_gets_explicit_legacy_verdict_and_never_matches(self) -> None:
        legacy = {"schema_version": "omh_release_evidence_bundle/v1", "version": "1.0.1", "status": "ready"}
        with TemporaryDirectory() as tmp:
            paths = OmhPaths(omh_home=Path(tmp) / ".omh", hermes_home=Path(tmp) / ".hermes")

            verdict = verify_release_evidence_bundle(legacy, repo_root="/repo", paths=paths, runner=fake_git_runner())

        self.assertEqual(verdict["verification"], "legacy_schema")

    def test_verify_never_writes_or_regenerates(self) -> None:
        with TemporaryDirectory() as tmp:
            paths = OmhPaths(omh_home=Path(tmp) / ".omh", hermes_home=Path(tmp) / ".hermes")
            bundle = recorded_bundle(paths)
            before = snapshot_tree(Path(tmp))

            verdict = verify_release_evidence_bundle(bundle, repo_root="/repo", paths=paths, runner=fake_git_runner())

            self.assertEqual(verdict["verification"], "matching")
            self.assertEqual(snapshot_tree(Path(tmp)), before)

    def test_manifest_is_deterministic_and_carries_relative_paths_only(self) -> None:
        with TemporaryDirectory() as tmp:
            paths = OmhPaths(omh_home=Path(tmp) / ".omh", hermes_home=Path(tmp) / ".hermes")
            store = paths.use_case_artifacts_dir
            store.mkdir(parents=True)
            (store / "g1.json").write_text('{"goal": "g1"}\n', encoding="utf-8")
            identity = probe_source_identity("/repo", runner=fake_git_runner())

            first = build_input_manifest(source_identity=identity, paths=paths)
            second = build_input_manifest(source_identity=identity, paths=paths)

            self.assertEqual(first, second)
            self.assertEqual(first["digest"], second["digest"])
            entries = first["local_artifact_store"]["entries"]
            self.assertEqual([entry["path"] for entry in entries], ["artifacts/g1.json"])
            self.assertNotIn(str(Path(tmp)), json.dumps(first))

    def test_persisted_bundle_contains_no_absolute_paths(self) -> None:
        with TemporaryDirectory() as tmp:
            paths = OmhPaths(omh_home=Path(tmp) / ".omh", hermes_home=Path(tmp) / ".hermes")

            written = release_evidence_bundle(
                version="v1.0.1",
                omh_command="/tmp/omh command",
                paths=paths,
                write=True,
                repo_root="/repo",
                runner=fake_git_runner(),
            )

            artifact_path = Path(str(written["artifact_path"]))
            self.assertTrue(artifact_path.is_absolute())
            persisted_text = artifact_path.read_text(encoding="utf-8")
            persisted = json.loads(persisted_text)
            self.assertEqual(persisted["artifact_path"], artifact_path.name)
            self.assertNotIn(str(Path(tmp)), persisted_text)
            self.assertNotIn("/tmp/omh command", json.dumps(persisted["source_identity"]))
            index = json.loads(paths.release_evidence_index_path.read_text(encoding="utf-8"))
            self.assertEqual(index["latest_artifact_path"], artifact_path.name)
            self.assertEqual(index["entries"][0]["commit_sha"], COMMIT)
            self.assertEqual(index["entries"][0]["tree_sha"], TREE)


class ReleaseRevisionBindingCliContractTests(unittest.TestCase):
    def test_cli_uses_version_filename_and_accepts_an_explicit_verify_path(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = root / "repo"
            repo.mkdir()
            init_git_repo(repo)
            omh_home = root / ".omh"
            base = ["--omh-home", str(omh_home), "--hermes-home", str(root / ".hermes")]
            previous_cwd = Path.cwd()
            try:
                os.chdir(repo)
                status, _, stderr = run_cli(
                    base + ["release", "evidence-bundle", "--version", "0.0.0", "--write", "--json"],
                    output_json=False,
                )
                canonical = omh_home / "runtime" / "release-evidence" / "0.0.0.json"
                self.assertTrue(canonical.is_file())
                self.assertEqual(status, 0, stderr)

                status, stdout, stderr = run_cli(
                    base + ["release", "evidence-bundle", "--version", "0.0.0", "--verify", str(canonical), "--json"],
                    output_json=False,
                )
                self.assertEqual(status, 0, stderr)
                self.assertEqual(json.loads(stdout)["verification"], "matching")

                status, stdout, stderr = run_cli(
                    base + ["release", "evidence-bundle", "--version", "0.0.0", "--verify", "--json"],
                    output_json=False,
                )
                self.assertEqual(status, 0, stderr)
                self.assertEqual(json.loads(stdout)["verification"], "matching")

                missing = root / "missing.json"
                status, stdout, _ = run_cli(
                    base + ["release", "evidence-bundle", "--version", "0.0.0", "--verify", str(missing), "--json"],
                    output_json=False,
                )
                self.assertEqual(status, 1)
                self.assertEqual(json.loads(stdout)["verification"], "missing")

                with self.assertRaises(SystemExit):
                    run_cli(
                        base + ["release", "evidence-bundle", "--version", "0.0.0", "--write", "--verify"],
                        output_json=False,
                    )
            finally:
                os.chdir(previous_cwd)

    def test_cli_write_then_verify_maps_verdicts_to_exit_codes(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = root / "repo"
            repo.mkdir()
            init_git_repo(repo)
            base = ["--omh-home", str(root / ".omh"), "--hermes-home", str(root / ".hermes")]

            status, stdout, stderr = run_cli(
                base + ["release", "evidence-bundle", "--write", "--repo-root", str(repo), "--json"],
                output_json=False,
            )
            self.assertEqual(status, 0, stderr)
            written = json.loads(stdout)
            self.assertEqual(written["schema_version"], "omh_release_evidence_bundle/v2")
            self.assertEqual(written["source_identity"]["origin"], "git_checkout")
            self.assertTrue(written["publication_ready"])

            status, stdout, stderr = run_cli(
                base + ["release", "evidence-bundle", "--verify", "--repo-root", str(repo), "--json"],
                output_json=False,
            )
            self.assertEqual(status, 0, stderr)
            verdict = json.loads(stdout)
            self.assertEqual(verdict["verification"], "matching")

            status, stdout, stderr = run_cli(
                base + ["release", "evidence-bundle", "--verify", "--repo-root", str(repo)],
                output_json=False,
            )
            self.assertEqual(status, 0, stderr)
            self.assertIn("Verification: matching", stdout)

            (repo / "README.md").write_text("mutated\n", encoding="utf-8")
            status, stdout, stderr = run_cli(
                base + ["release", "evidence-bundle", "--verify", "--repo-root", str(repo), "--json"],
                output_json=False,
            )
            self.assertEqual(status, 1)
            self.assertEqual(json.loads(stdout)["verification"], "dirty")

            subprocess.run(["git", "add", "README.md"], cwd=repo, check=True, capture_output=True)
            subprocess.run(["git", "commit", "-m", "second"], cwd=repo, check=True, capture_output=True)
            status, stdout, stderr = run_cli(
                base + ["release", "evidence-bundle", "--verify", "--repo-root", str(repo), "--json"],
                output_json=False,
            )
            self.assertEqual(status, 1)
            self.assertEqual(json.loads(stdout)["verification"], "mismatched_revision")

    def test_cli_verify_reports_missing_and_legacy_verdicts(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = root / "repo"
            repo.mkdir()
            init_git_repo(repo)
            base = ["--omh-home", str(root / ".omh"), "--hermes-home", str(root / ".hermes")]

            status, stdout, _ = run_cli(
                base + ["release", "evidence-bundle", "--verify", "--repo-root", str(repo), "--json"],
                output_json=False,
            )
            self.assertEqual(status, 1)
            self.assertEqual(json.loads(stdout)["verification"], "missing")

            evidence_dir = root / ".omh" / "runtime" / "release-evidence"
            evidence_dir.mkdir(parents=True)
            from omh.version import __version__ as current_version

            legacy_path = evidence_dir / f"{current_version}.json"
            legacy_path.write_text(
                json.dumps({"schema_version": "omh_release_evidence_bundle/v1", "version": current_version}),
                encoding="utf-8",
            )
            status, stdout, _ = run_cli(
                base + ["release", "evidence-bundle", "--verify", "--repo-root", str(repo), "--json"],
                output_json=False,
            )
            self.assertEqual(status, 1)
            self.assertEqual(json.loads(stdout)["verification"], "legacy_schema")

    def test_cli_write_without_source_identity_is_not_publication_ready(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            base = ["--omh-home", str(root / ".omh"), "--hermes-home", str(root / ".hermes")]

            previous_cwd = Path.cwd()
            try:
                os.chdir(root)
                status, stdout, _ = run_cli(
                    base + ["release", "evidence-bundle", "--write", "--json"], output_json=False
                )
            finally:
                os.chdir(previous_cwd)

            self.assertEqual(status, 1)
            payload = json.loads(stdout)
            self.assertEqual(payload["status"], "needs_attention")
            self.assertFalse(payload["publication_ready"])
            self.assertEqual(payload["source_identity"]["identity_status"], "unavailable")
            self.assertTrue(payload["written"])

    def test_cli_verify_never_writes(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = root / "repo"
            repo.mkdir()
            init_git_repo(repo)
            base = ["--omh-home", str(root / ".omh"), "--hermes-home", str(root / ".hermes")]
            status, _, stderr = run_cli(
                base + ["release", "evidence-bundle", "--write", "--repo-root", str(repo), "--json"],
                output_json=False,
            )
            self.assertEqual(status, 0, stderr)
            before = snapshot_tree(root / ".omh")

            status, _, _ = run_cli(
                base + ["release", "evidence-bundle", "--verify", "--repo-root", str(repo), "--json"],
                output_json=False,
            )

            self.assertEqual(status, 0)
            self.assertEqual(snapshot_tree(root / ".omh"), before)


if __name__ == "__main__":
    unittest.main()
