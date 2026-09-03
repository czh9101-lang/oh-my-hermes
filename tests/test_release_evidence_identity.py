"""Companion regression suite for revision-bound release evidence (issue #1280).

Covers the identity probe, the deterministic input manifest, and every verifier
verdict across the origin taxonomy (`git_checkout`, `source_archive`,
`installed_package`, `unknown`). The registered contract cases live in
`tests/test_release_revision_binding.py`; this suite is the full matrix.
"""

from __future__ import annotations

import hashlib
import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace

from omh.maintenance.release import release_evidence_bundle
from omh.maintenance.release_identity import (
    build_input_manifest,
    probe_source_identity,
    verify_release_evidence_bundle,
)
from omh.paths import OmhPaths

COMMIT = "a" * 40
TREE = "b" * 40
OTHER_COMMIT = "c" * 40
OTHER_TREE = "d" * 40
ARCHIVE_DIGEST = "sha256:" + "1" * 64
OTHER_ARCHIVE_DIGEST = "sha256:" + "2" * 64
ARTIFACT_DIGEST = "sha256:" + "3" * 64
OTHER_ARTIFACT_DIGEST = "sha256:" + "4" * 64


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


def git_bundle(paths: OmhPaths, *, artifact: Path | None = None) -> dict:
    identity = probe_source_identity("/repo", runner=fake_git_runner())
    identity["input_manifest"] = build_input_manifest(source_identity=identity, paths=paths, artifact=artifact)
    return {
        "schema_version": "omh_release_evidence_bundle/v2",
        "version": "1.0.1",
        "source_identity": identity,
    }


def digest_bundle(digest: str, *, kind: str, paths: OmhPaths) -> dict:
    if kind == "source_archive":
        identity = probe_source_identity(None, archive_digest=digest)
    else:
        identity = probe_source_identity(None, artifact_digest=digest)
    identity["input_manifest"] = build_input_manifest(source_identity=identity, paths=paths)
    return {
        "schema_version": "omh_release_evidence_bundle/v2",
        "version": "1.0.1",
        "source_identity": identity,
    }


def make_paths(tmp: str) -> OmhPaths:
    return OmhPaths(omh_home=Path(tmp) / ".omh", hermes_home=Path(tmp) / ".hermes")


class ReleaseEvidenceIdentityTests(unittest.TestCase):
    def test_clean_checkout_bundle_records_v2_identity(self) -> None:
        with TemporaryDirectory() as tmp:
            payload = release_evidence_bundle(
                version="v1.0.1",
                paths=make_paths(tmp),
                repo_root="/repo",
                runner=fake_git_runner(),
            )

        self.assertEqual(payload["schema_version"], "omh_release_evidence_bundle/v2")
        identity = payload["source_identity"]
        self.assertEqual(identity["origin"], "git_checkout")
        self.assertIs(identity["dirty"], False)
        self.assertEqual(identity["identity_status"], "available")
        self.assertEqual(len(identity["commit_sha"]), 40)
        self.assertEqual(len(identity["tree_sha"]), 40)
        self.assertEqual(payload["status"], "ready")
        self.assertTrue(payload["publication_ready"])
        self.assertIn("source_revision_bound", payload["claims"])
        self.assertIn("input_manifest_digest_recorded", payload["claims"])

    def test_verify_matching_on_unchanged_inputs(self) -> None:
        with TemporaryDirectory() as tmp:
            paths = make_paths(tmp)
            bundle = git_bundle(paths)
            verdict = verify_release_evidence_bundle(bundle, repo_root="/repo", paths=paths, runner=fake_git_runner())
        self.assertEqual(verdict["verification"], "matching")

    def test_dirty_tree_blocks_ready_and_verifies_dirty(self) -> None:
        with TemporaryDirectory() as tmp:
            paths = make_paths(tmp)
            payload = release_evidence_bundle(
                version="v1.0.1",
                paths=paths,
                repo_root="/repo",
                write=True,
                runner=fake_git_runner(porcelain=" M README.md\n"),
            )
            self.assertEqual(payload["source_identity"]["dirty"], True)
            self.assertEqual(payload["source_identity"]["dirty_file_count"], 1)
            # A dirty-but-identifiable checkout is still bound; dirtiness is a
            # verifier verdict, not an identity failure.
            self.assertEqual(payload["source_identity"]["identity_status"], "available")

            bundle = git_bundle(paths)
            verdict = verify_release_evidence_bundle(
                bundle,
                repo_root="/repo",
                paths=paths,
                runner=fake_git_runner(porcelain=" M README.md\n"),
            )
        self.assertEqual(verdict["verification"], "dirty")
        self.assertIn("uncommitted", " ".join(verdict["reasons"]))

    def test_same_version_new_commit_is_mismatched_revision(self) -> None:
        with TemporaryDirectory() as tmp:
            paths = make_paths(tmp)
            bundle = git_bundle(paths)
            verdict = verify_release_evidence_bundle(
                bundle,
                repo_root="/repo",
                paths=paths,
                runner=fake_git_runner(commit=OTHER_COMMIT, tree=OTHER_TREE),
            )
        self.assertEqual(verdict["verification"], "mismatched_revision")

    def test_same_commit_changed_manifest_is_stale(self) -> None:
        with TemporaryDirectory() as tmp:
            paths = make_paths(tmp)
            bundle = git_bundle(paths)
            store = paths.use_case_artifacts_dir
            store.mkdir(parents=True)
            (store / "g1.json").write_text("{}\n", encoding="utf-8")

            verdict = verify_release_evidence_bundle(bundle, repo_root="/repo", paths=paths, runner=fake_git_runner())
            self.assertEqual(verdict["verification"], "stale")

        with TemporaryDirectory() as tmp:
            paths = make_paths(tmp)
            artifact = Path(tmp) / "oh_my_hermes-1.0.1-py3-none-any.whl"
            artifact.write_bytes(b"wheel-bytes")
            bundle = git_bundle(paths, artifact=artifact)
            artifact.write_bytes(b"rebuilt-wheel-bytes")

            verdict = verify_release_evidence_bundle(
                bundle,
                repo_root="/repo",
                paths=paths,
                artifact=artifact,
                runner=fake_git_runner(),
            )
            self.assertEqual(verdict["verification"], "stale")

    def test_committed_gate_definition_change_changes_tree(self) -> None:
        with TemporaryDirectory() as tmp:
            paths = make_paths(tmp)
            bundle = git_bundle(paths)
            # Same commit, different tree: a committed gate-definition change is
            # exactly the case the tree comparison exists for.
            verdict = verify_release_evidence_bundle(
                bundle,
                repo_root="/repo",
                paths=paths,
                runner=fake_git_runner(tree=OTHER_TREE),
            )
        self.assertEqual(verdict["verification"], "stale")
        self.assertIn("tree", " ".join(verdict["reasons"]))

    def test_generated_file_drift_uncommitted_is_dirty(self) -> None:
        with TemporaryDirectory() as tmp:
            paths = make_paths(tmp)
            bundle = git_bundle(paths)
            verdict = verify_release_evidence_bundle(
                bundle,
                repo_root="/repo",
                paths=paths,
                runner=fake_git_runner(porcelain=" M skills/ultrawork/SKILL.md\n"),
            )
        self.assertEqual(verdict["verification"], "dirty")

    def test_source_archive_without_digest_is_unavailable(self) -> None:
        identity = probe_source_identity("/not-a-repo", runner=fake_git_runner(fail=True))
        self.assertEqual(identity["origin"], "unknown")
        self.assertEqual(identity["identity_status"], "unavailable")

        with TemporaryDirectory() as tmp:
            paths = make_paths(tmp)
            payload = release_evidence_bundle(
                version="v1.0.1",
                paths=paths,
                repo_root="/not-a-repo",
                write=True,
                runner=fake_git_runner(fail=True),
            )
            self.assertEqual(payload["source_identity"]["identity_status"], "unavailable")
            self.assertFalse(payload["publication_ready"])
            self.assertEqual(payload["status"], "needs_attention")

            bundle = git_bundle(paths)
            verdict = verify_release_evidence_bundle(
                bundle,
                repo_root="/not-a-repo",
                paths=paths,
                runner=fake_git_runner(fail=True),
            )
        self.assertEqual(verdict["verification"], "unverifiable")

    def test_source_archive_with_digest_binds_and_verifies(self) -> None:
        with TemporaryDirectory() as tmp:
            paths = make_paths(tmp)
            bundle = digest_bundle(ARCHIVE_DIGEST, kind="source_archive", paths=paths)
            self.assertEqual(bundle["source_identity"]["origin"], "source_archive")
            self.assertEqual(bundle["source_identity"]["identity_status"], "available")

            matching = verify_release_evidence_bundle(bundle, paths=paths, archive_digest=ARCHIVE_DIGEST)
            self.assertEqual(matching["verification"], "matching")

            missing_digest = verify_release_evidence_bundle(bundle, paths=paths)
            self.assertEqual(missing_digest["verification"], "unverifiable")

            changed = verify_release_evidence_bundle(bundle, paths=paths, archive_digest=OTHER_ARCHIVE_DIGEST)
            self.assertEqual(changed["verification"], "stale")

    def test_installed_package_without_artifact_digest_is_unverifiable(self) -> None:
        with TemporaryDirectory() as tmp:
            paths = make_paths(tmp)
            bundle = digest_bundle(ARTIFACT_DIGEST, kind="installed_package", paths=paths)
            self.assertEqual(bundle["source_identity"]["origin"], "installed_package")

            missing_digest = verify_release_evidence_bundle(bundle, paths=paths)
            self.assertEqual(missing_digest["verification"], "unverifiable")

            matching = verify_release_evidence_bundle(bundle, paths=paths, artifact_digest=ARTIFACT_DIGEST)
            self.assertEqual(matching["verification"], "matching")

            changed = verify_release_evidence_bundle(bundle, paths=paths, artifact_digest=OTHER_ARTIFACT_DIGEST)
            self.assertEqual(changed["verification"], "stale")

    def test_v1_bundle_gets_explicit_legacy_verdict(self) -> None:
        legacy = {"schema_version": "omh_release_evidence_bundle/v1", "version": "1.0.1", "status": "ready"}
        with TemporaryDirectory() as tmp:
            verdict = verify_release_evidence_bundle(legacy, repo_root="/repo", paths=make_paths(tmp), runner=fake_git_runner())
        self.assertEqual(verdict["verification"], "legacy_schema")
        self.assertNotEqual(verdict["verification"], "matching")

    def test_missing_bundle_reports_missing(self) -> None:
        verdict = verify_release_evidence_bundle(None, version="1.0.1", repo_root="/repo", runner=fake_git_runner())
        self.assertEqual(verdict["verification"], "missing")
        self.assertEqual(verdict["version"], "1.0.1")

    def test_verify_never_writes(self) -> None:
        with TemporaryDirectory() as tmp:
            paths = make_paths(tmp)
            bundle = git_bundle(paths)
            before = {
                path.relative_to(tmp).as_posix(): path.read_bytes()
                for path in Path(tmp).rglob("*")
                if path.is_file()
            }
            verdict = verify_release_evidence_bundle(bundle, repo_root="/repo", paths=paths, runner=fake_git_runner())
            after = {
                path.relative_to(tmp).as_posix(): path.read_bytes()
                for path in Path(tmp).rglob("*")
                if path.is_file()
            }
        self.assertEqual(verdict["verification"], "matching")
        self.assertEqual(after, before)

    def test_manifest_is_deterministic_across_runs(self) -> None:
        with TemporaryDirectory() as tmp:
            paths = make_paths(tmp)
            store = paths.use_case_artifacts_dir
            store.mkdir(parents=True)
            (store / "b.json").write_text("b\n", encoding="utf-8")
            (store / "a.json").write_text("a\n", encoding="utf-8")
            identity = probe_source_identity("/repo", runner=fake_git_runner())

            first = build_input_manifest(source_identity=identity, paths=paths)
            second = build_input_manifest(source_identity=identity, paths=paths)

            self.assertEqual(
                json.dumps(first, sort_keys=True, separators=(",", ":")),
                json.dumps(second, sort_keys=True, separators=(",", ":")),
            )
            self.assertEqual(first["digest"], second["digest"])
            paths_in_manifest = [entry["path"] for entry in first["local_artifact_store"]["entries"]]
            self.assertEqual(paths_in_manifest, sorted(paths_in_manifest))
            self.assertTrue(all("\\" not in path for path in paths_in_manifest))
            self.assertNotIn(str(Path.home()), json.dumps(first))
            self.assertNotIn(str(Path(tmp)), json.dumps(first))

    def test_persisted_bundle_contains_no_absolute_paths(self) -> None:
        with TemporaryDirectory() as tmp:
            paths = make_paths(tmp)
            written = release_evidence_bundle(
                version="v1.0.1",
                omh_command="/tmp/omh command",
                paths=paths,
                write=True,
                repo_root="/repo",
                runner=fake_git_runner(),
            )
            artifact_path = Path(str(written["artifact_path"]))
            persisted_text = artifact_path.read_text(encoding="utf-8")
            persisted = json.loads(persisted_text)

            self.assertNotIn(str(Path(tmp)), persisted_text)
            self.assertEqual(persisted["artifact_path"], artifact_path.name)
            self.assertNotIn("/tmp/omh command", json.dumps(persisted["source_identity"]))
            self.assertEqual(persisted["schema_version"], "omh_release_evidence_bundle/v2")

    def test_recorded_artifact_requires_an_artifact_at_verify_time(self) -> None:
        with TemporaryDirectory() as tmp:
            paths = make_paths(tmp)
            artifact = Path(tmp) / "oh_my_hermes-1.0.1-py3-none-any.whl"
            artifact.write_bytes(b"wheel-bytes")
            bundle = git_bundle(paths, artifact=artifact)

            without_artifact = verify_release_evidence_bundle(
                bundle, repo_root="/repo", paths=paths, runner=fake_git_runner()
            )
            self.assertEqual(without_artifact["verification"], "unverifiable")

            with_artifact = verify_release_evidence_bundle(
                bundle, repo_root="/repo", paths=paths, artifact=artifact, runner=fake_git_runner()
            )
            self.assertEqual(with_artifact["verification"], "matching")

            digest = "sha256:" + hashlib.sha256(b"wheel-bytes").hexdigest()
            with_digest = verify_release_evidence_bundle(
                bundle, repo_root="/repo", paths=paths, artifact_digest=digest, runner=fake_git_runner()
            )
            self.assertEqual(with_digest["verification"], "matching")

    def test_probe_rejects_malformed_git_output_and_bad_digests(self) -> None:
        short_hash_runner = fake_git_runner(commit="abc123", tree="def456")
        identity = probe_source_identity("/repo", runner=short_hash_runner)
        self.assertEqual(identity["identity_status"], "unavailable")
        self.assertEqual(identity["origin"], "unknown")

        identity = probe_source_identity(None, archive_digest="not-a-digest")
        self.assertEqual(identity["identity_status"], "unavailable")

    def test_unreadable_artifact_is_a_boundary_error(self) -> None:
        identity = probe_source_identity("/repo", runner=fake_git_runner())
        with TemporaryDirectory() as tmp:
            with self.assertRaises(ValueError):
                build_input_manifest(
                    source_identity=identity,
                    paths=make_paths(tmp),
                    artifact=Path(tmp) / "missing.whl",
                )


if __name__ == "__main__":
    unittest.main()
