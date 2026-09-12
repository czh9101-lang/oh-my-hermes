from __future__ import annotations

from copy import deepcopy
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from _local_package import load_local_package

load_local_package()

from omh.memory import (  # noqa: E402
    approve_project_memory_candidate,
    build_handoff_context_pack,
    capture_project_memory_candidate,
    memory_recall_pack_for_handoff,
    validate_handoff_context_pack,
)
from omh.paths import OmhPaths  # noqa: E402
from project_identity_fixture import project_identity, memory_paths as resolve_paths
from omh.workflows.domain_intelligence import (  # noqa: E402
    approve_domain_candidate,
    capture_domain_candidate,
    reject_domain_candidate,
    retire_domain_profile,
)
from omh.workflows.role_context_packs import (  # noqa: E402
    build_role_context_pack,
    validate_role_context_pack,
)


_EXECUTOR_TARGETS = ("generic", "codex", "claude-code", "hermes")


def _repository(root: Path) -> tuple[Path, OmhPaths]:
    root.mkdir()
    (root / ".git").mkdir()
    return root, resolve_paths(root / ".omh", root / ".hermes")


def _capture(paths: OmhPaths, root: Path, *, domain_id: str = "delivery") -> dict[str, object]:
    return capture_domain_candidate(
        paths,
        scope_kind="project",
        scope_ref=project_identity(root),
        domain_id=domain_id,
        mappings=[("dispatch packet", "handoff"), ("handoff bundle", "handoff")],
        workflow_hints=["ralplan"],
        source_class="omh_local",
        source_ref="pt_sha256:" + "a" * 64,
        observation_count=2,
        confidence=0.875,
    )["candidate"]


def _domain_rows(pack: dict[str, object]) -> list[dict[str, object]]:
    return [
        row
        for row in pack["included_context"]
        if isinstance(row, dict) and row.get("source_kind") == "domain_intelligence_profile"
    ]


def _rewrite(path: Path, mutation: dict[str, object]) -> None:
    value = json.loads(path.read_text(encoding="utf-8"))
    value.update(mutation)
    path.write_text(json.dumps(value, sort_keys=True), encoding="utf-8")


class ReviewedDomainHandoffProjectionTests(unittest.TestCase):
    maxDiff = None

    def test_all_four_executor_targets_receive_identical_reviewed_domain_semantics(self) -> None:
        with TemporaryDirectory() as temporary:
            root, paths = _repository(Path(temporary) / "project")
            source_only_sentinel = "human-definition-source-only-sentinel"
            (root / "PROJECT_TERMS.md").write_text(source_only_sentinel, encoding="utf-8")
            candidate = _capture(paths, root)
            approved = approve_domain_candidate(paths, candidate["candidate_id"], approved_by="operator")

            packs = {
                target: build_handoff_context_pack(paths, executor_target=target)
                for target in _EXECUTOR_TARGETS
            }
            rows = {target: _domain_rows(pack) for target, pack in packs.items()}

            self.assertTrue(all(len(value) == 1 for value in rows.values()), rows)
            semantics = {
                target: {key: value for key, value in value[0].items() if key != "executor_target"}
                for target, value in rows.items()
            }
            self.assertEqual(len({json.dumps(value, sort_keys=True) for value in semantics.values()}), 1)
            row = rows["generic"][0]
            profile = approved["profile"]
            review = approved["review"]
            self.assertEqual(
                {
                    "item_id": row["item_id"],
                    "profile_id": row["profile_id"],
                    "profile_revision": row["profile_revision"],
                    "profile_digest": row["profile_digest"],
                    "review_id": row["review_id"],
                    "truth_level": row["truth_level"],
                    "scope": row["scope"],
                },
                {
                    "item_id": f"{profile['profile_id']}:r{profile['revision']}",
                    "profile_id": profile["profile_id"],
                    "profile_revision": profile["revision"],
                    "profile_digest": profile["payload_digest"],
                    "review_id": review["review_id"],
                    "truth_level": "approved_context",
                    "scope": {"kind": "project", "ref": project_identity(root)},
                },
            )
            self.assertEqual(row["replay_evaluation"]["eligible"], True)
            self.assertEqual(row["replay_evaluation"]["reason_code"], "eligible")
            self.assertIn("dispatch packet=handoff", row["summary"])
            self.assertIn("handoff bundle=handoff", row["summary"])
            self.assertIn("workflow hints: ralplan", row["summary"])
            self.assertNotIn(source_only_sentinel, json.dumps(packs, sort_keys=True))
            expected_scope = {"kind": "project", "ref": project_identity(root)}
            for pack in packs.values():
                self.assertEqual(pack["scope"], expected_scope)
                self.assertEqual(validate_handoff_context_pack(pack, require_conflict_free=True), [])

            damaged = deepcopy(packs["generic"])
            del _domain_rows(damaged)[0]["review_id"]
            errors = validate_handoff_context_pack(damaged, require_conflict_free=True)
            self.assertTrue(any("complete domain profile projection" in error for error in errors), errors)

    def test_role_packs_preserve_profile_revision_digest_and_review_linkage(self) -> None:
        with TemporaryDirectory() as temporary:
            root, paths = _repository(Path(temporary) / "project")
            approved = approve_domain_candidate(paths, _capture(paths, root)["candidate_id"])

            role_packs = {
                target: build_role_context_pack(
                    context_pack=build_handoff_context_pack(paths, executor_target=target)
                )
                for target in _EXECUTOR_TARGETS
            }
            domain_records = {
                target: [
                    record
                    for record in pack["records"]
                    if record.get("source_kind") == "domain_intelligence_profile"
                ]
                for target, pack in role_packs.items()
            }

            self.assertTrue(all(len(value) == 1 for value in domain_records.values()), domain_records)
            self.assertEqual(len({pack["pack_hash"] for pack in role_packs.values()}), 1)
            record = domain_records["generic"][0]
            self.assertEqual(record["profile_id"], approved["profile"]["profile_id"])
            self.assertEqual(record["profile_revision"], approved["profile"]["revision"])
            self.assertEqual(record["profile_digest"], approved["profile"]["payload_digest"])
            self.assertEqual(record["review_id"], approved["review"]["review_id"])
            self.assertEqual(record["scope"], {"kind": "project", "ref": project_identity(root)})
            self.assertTrue(all(pack["scope"] == record["scope"] for pack in role_packs.values()))

            changed_context = build_handoff_context_pack(paths, executor_target="generic")
            _domain_rows(changed_context)[0]["review_id"] = "direview_changed"
            self.assertNotEqual(
                build_role_context_pack(context_pack=changed_context)["pack_hash"],
                role_packs["generic"]["pack_hash"],
                "review linkage is part of immutable role-pack identity",
            )
            for pack in role_packs.values():
                self.assertEqual(validate_role_context_pack(pack), [])

    def test_repeated_handoff_and_role_pack_builds_are_byte_identical(self) -> None:
        with TemporaryDirectory() as temporary:
            root, paths = _repository(Path(temporary) / "project")
            approve_domain_candidate(paths, _capture(paths, root)["candidate_id"])

            first = build_handoff_context_pack(paths, executor_target="hermes")
            second = build_handoff_context_pack(paths, executor_target="hermes")
            self.assertEqual(
                json.dumps(first, sort_keys=True, separators=(",", ":")),
                json.dumps(second, sort_keys=True, separators=(",", ":")),
            )
            self.assertEqual(build_role_context_pack(context_pack=first), build_role_context_pack(context_pack=second))

    def test_repository_scoped_project_memory_survives_handoff_and_role_boundaries(self) -> None:
        with TemporaryDirectory() as temporary:
            root, paths = _repository(Path(temporary) / "project-memory")
            scope = {"kind": "project", "ref": project_identity(root)}
            captured = capture_project_memory_candidate(
                paths,
                "Repository verification uses the focused domain handoff tests",
                scope_kind=scope["kind"],
                scope_ref=scope["ref"],
            )
            record = approve_project_memory_candidate(paths, captured["candidate"]["candidate_id"])["record"]

            handoff = build_handoff_context_pack(paths, executor_target="codex")
            memory_item = next(item for item in handoff["included_context"] if item["item_id"] == record["record_id"])
            role = build_role_context_pack(context_pack=handoff)
            role_record = next(item for item in role["records"] if item["record_id"] == record["record_id"])
            recall = memory_recall_pack_for_handoff(paths, "focused domain handoff", executor_target="codex")
            assert recall is not None
            recall_role = build_role_context_pack(memory_recall_pack=recall)

            self.assertEqual(handoff["scope"], scope)
            self.assertEqual(memory_item["scope"], scope)
            self.assertEqual(role["scope"], scope)
            self.assertEqual(role_record["scope"], scope)
            self.assertEqual(recall["scope"], scope)
            self.assertEqual(recall_role["scope"], scope)
            self.assertEqual(recall_role["records"][0]["scope"], scope)

    def test_mixed_or_default_leakage_is_rejected_fail_closed(self) -> None:
        with TemporaryDirectory() as temporary:
            root, paths = _repository(Path(temporary) / "project")
            approve_domain_candidate(paths, _capture(paths, root)["candidate_id"])
            context = build_handoff_context_pack(paths, executor_target="generic")
            expected_scope = {"kind": "project", "ref": project_identity(root)}

            mixed = deepcopy(context)
            _domain_rows(mixed)[0]["scope"] = {"kind": "project", "ref": "another-project"}
            errors = validate_handoff_context_pack(mixed, require_conflict_free=True)
            self.assertTrue(any("scope must match context_pack.scope" in error for error in errors), errors)
            with self.assertRaisesRegex(ValueError, "scope must match"):
                build_role_context_pack(context_pack=mixed)

            role = build_role_context_pack(context_pack=context)
            default_leak = deepcopy(role)
            default_leak["scope"] = {"kind": "project", "ref": "default"}
            errors = validate_role_context_pack(default_leak)
            self.assertTrue(any("scope must match role_context_pack.scope" in error for error in errors), errors)

            missing = deepcopy(role)
            domain_record = next(
                item for item in missing["records"] if item.get("source_kind") == "domain_intelligence_profile"
            )
            del domain_record["scope"]
            errors = validate_role_context_pack(missing)
            self.assertTrue(any("scope" in error for error in errors), errors)
            self.assertEqual(context["scope"], expected_scope)


class IneligibleDomainProfileTests(unittest.TestCase):
    def _reasons(self, paths: OmhPaths) -> set[str]:
        pack = build_handoff_context_pack(paths, executor_target="generic")
        self.assertEqual(_domain_rows(pack), [])
        return {
            str(row["reason"])
            for row in pack["excluded_context"]
            if isinstance(row, dict) and str(row.get("item_id", "")).startswith("dprof_")
        }

    def test_pending_rejected_and_retired_profiles_fail_closed(self) -> None:
        cases = ("pending", "rejected", "retired")
        expected = {
            "pending": "domain_profile_pending_review",
            "rejected": "domain_profile_rejected",
            "retired": "domain_profile_retired",
        }
        for state in cases:
            with self.subTest(state=state), TemporaryDirectory() as temporary:
                root, paths = _repository(Path(temporary) / state)
                candidate = _capture(paths, root)
                if state == "rejected":
                    reject_domain_candidate(paths, candidate["candidate_id"])
                elif state == "retired":
                    approve_domain_candidate(paths, candidate["candidate_id"])
                    retire_domain_profile(
                        paths,
                        scope_kind="project",
                        scope_ref=project_identity(root),
                        domain_id="delivery",
                    )
                self.assertIn(expected[state], self._reasons(paths))

    def test_malformed_digest_and_review_mismatch_fail_closed_with_stable_reasons(self) -> None:
        expected = {
            "malformed": "domain_profile_malformed",
            "digest": "domain_profile_digest_mismatch",
            "review": "domain_profile_review_mismatch",
        }
        for state in expected:
            with self.subTest(state=state), TemporaryDirectory() as temporary:
                root, paths = _repository(Path(temporary) / state)
                approved = approve_domain_candidate(paths, _capture(paths, root)["candidate_id"])
                profile_path = next((root / ".omh/memory/domain-intelligence/profiles").glob("*.json"))
                review_path = next((root / ".omh/memory/domain-intelligence/reviews").glob("*.json"))
                if state == "malformed":
                    _rewrite(profile_path, {"unexpected": "field"})
                elif state == "digest":
                    _rewrite(profile_path, {"payload_digest": "0" * 64})
                else:
                    changed = deepcopy(approved["review"])
                    changed["reviewer_claim"] = "different-reviewer"
                    review_path.write_text(json.dumps(changed, sort_keys=True), encoding="utf-8")
                self.assertIn(expected[state], self._reasons(paths))


if __name__ == "__main__":
    unittest.main()
