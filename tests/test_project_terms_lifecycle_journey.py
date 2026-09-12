from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from _cli_harness import run_cli
from _local_package import load_local_package
from domain_store_snapshot_support import domain_store_snapshot

load_local_package()

from omh.coding.handoff_input_manifest import (  # noqa: E402
    ManifestSelection,
    build_handoff_input_manifest,
    validate_handoff_input_manifest,
)
from omh.memory import build_handoff_context_pack, validate_handoff_context_pack  # noqa: E402
from omh.paths import resolve_paths  # noqa: E402
from project_identity_fixture import project_identity, seed_project_identity
from omh.routing.chat import route_chat_message  # noqa: E402
from omh.routing.domain_context_eligibility import classify_domain_context_eligibility  # noqa: E402
from omh.workflows.role_context_packs import (  # noqa: E402
    build_role_context_pack,
    validate_role_context_pack,
)

from domain_context_lifecycle_support import EN_SALES_QUESTION, _chat  # noqa: E402


_BOUNDARY = (
    "Project terminology only. This file is not agent instructions, routing rules, "
    "approval, execution, or evidence. Changes affect OMH only after explicit review."
)
_SOURCE_ONLY_SENTINEL = "human-definition-source-only-sentinel"
_VALID_SOURCE = f"""# Project Terms

{_BOUNDARY}

<!-- omh-project-terms/v1 -->

## domain: sales

- term: `revenue prism` = `revenue-prism`
  definition: {_SOURCE_ONLY_SENTINEL}
  say-instead: reviewed pipeline discussion
  localized[ko]: 파이프라인 리뷰
  distinct-from: `status` - A progress report rather than the unresolved sales concept.
- workflow-hint: `sales-development`
""".encode()
_EXECUTORS = ("generic", "codex", "claude-code", "hermes")


def _repository(root: Path, *, source: bytes = _VALID_SOURCE) -> Path:
    root.mkdir(parents=True)
    (root / ".git").mkdir()
    (root / "PROJECT_TERMS.md").write_bytes(source)
    seed_project_identity(root)
    return root.resolve()


def _store_snapshot(root: Path) -> dict[str, bytes]:
    return domain_store_snapshot(root)


def _run(root: Path, arguments: list[str]) -> tuple[int, dict[str, object] | None, str]:
    previous = Path.cwd()
    os.chdir(root)
    try:
        status, stdout, stderr = run_cli(["--scope", "project", "memory", *arguments])
    finally:
        os.chdir(previous)
    payload = json.loads(stdout) if stdout else None
    return status, payload, stderr


def _ok(root: Path, arguments: list[str]) -> dict[str, object]:
    status, payload, stderr = _run(root, arguments)
    if status != 0 or payload is None:
        raise AssertionError(f"command failed: {arguments!r}: {status}: {stderr}")
    return payload


def _domain_rows(pack: dict[str, object]) -> list[dict[str, object]]:
    return [
        row
        for row in pack["included_context"]
        if isinstance(row, dict) and row.get("source_kind") == "domain_intelligence_profile"
    ]


class ProjectTermsLifecycleJourneyTests(unittest.TestCase):
    maxDiff = None

    def test_preview_to_retirement_crosses_every_reviewed_handoff_surface(self) -> None:
        with TemporaryDirectory() as temporary:
            root = _repository(Path(temporary) / "journey-project")
            paths = resolve_paths(root / ".omh", root / ".hermes")
            source_sha = hashlib.sha256(_VALID_SOURCE).hexdigest()
            scope_ref = project_identity(root)
            unresolved = "something revenue prism feels unresolved"

            source_only = _chat(root, unresolved)
            self.assertNotIn("domain_routing_context", source_only)
            initial_store = _store_snapshot(root)

            preview = _ok(root, ["domain-capture", "--from-file", "PROJECT_TERMS.md", "--json"])
            self.assertEqual(preview["schema_version"], "project_terms_capture/v1")
            self.assertEqual((preview["state"], preview["reason"]), ("prepared_not_observed", "preview_ready"))
            self.assertEqual(preview["scope"]["ref"], scope_ref)
            self.assertEqual(preview["source_sha256"], source_sha)
            self.assertEqual(preview["mutation_set"], [])
            self.assertEqual(preview["capacity"]["required"], 1)
            self.assertEqual(_store_snapshot(root), initial_store)

            staged = _ok(root, ["domain-capture", "--from-file", "PROJECT_TERMS.md", "--stage", "--json"])
            self.assertEqual((staged["state"], staged["reason"]), ("prepared_not_observed", "pending_review_staged"))
            self.assertEqual(staged["mutation_set"], staged["candidate_ids"])
            self.assertEqual(len(staged["candidate_ids"]), 1)
            candidate_id = staged["candidate_ids"][0]
            self.assertRegex(candidate_id, r"^dicand_[0-9a-f]{16}$")
            pending_store = _store_snapshot(root)
            self.assertNotEqual(pending_store, initial_store)
            self.assertNotIn("domain_routing_context", _chat(root, unresolved))

            review_store = _store_snapshot(root)
            review = _ok(root, ["domain-review", "--candidate", candidate_id, "--source-freshness"])
            self.assertEqual(review["schema_version"], "domain_intelligence_review_queue/v1")
            self.assertEqual(review["counts"], {"pending_review": 1, "malformed_artifacts": 0})
            card = review["cards"][0]
            self.assertEqual(card["candidate_id"], candidate_id)
            self.assertEqual(card["status"], "pending_review")
            self.assertEqual(card["source_freshness"]["state"], "unchanged")
            self.assertEqual(card["source_freshness"]["reason"], "source_matches_candidate")
            self.assertEqual(_store_snapshot(root), review_store)

            approved = _ok(root, ["domain-approve", candidate_id, "--approved-by", "task-12"])
            self.assertEqual(approved["decision"], "approved")
            profile = approved["profile"]
            review_record = approved["review"]
            self.assertEqual((profile["schema_version"], profile["status"], profile["revision"]), ("domain_intelligence_profile/v1", "active", 1))
            self.assertEqual(profile["provenance"], {"source_class": "omh_local", "source_ref": f"pt_sha256:{source_sha}", "observation_count": 1, "raw_persisted": False})
            self.assertRegex(profile["payload_digest"], r"^[0-9a-f]{64}$")
            self.assertEqual(review_record["payload_digest"], profile["payload_digest"])

            listed_store = _store_snapshot(root)
            listed = _ok(root, ["domain-list", "--scope-kind", "project", "--scope-ref", scope_ref, "--domain", "sales", "--source-freshness"])
            self.assertEqual(listed["schema_version"], "domain_intelligence_profile_listing/v1")
            self.assertEqual(listed["counts"], {"profiles": 1, "malformed_artifacts": 0})
            self.assertEqual(listed["profiles"][0]["source_freshness"]["state"], "unchanged")
            self.assertEqual(_store_snapshot(root), listed_store)

            interaction = _chat(root, unresolved)
            context = interaction["domain_routing_context"]
            self.assertEqual(context["schema_version"], "domain_routing_context/v1")
            self.assertEqual(context["workflow_hint"], "sales-development")
            self.assertEqual(context["required_input"], "account or segment")
            self.assertEqual(context["question"], {"locale": "en", "text": EN_SALES_QUESTION})
            self.assertRegex(context["digest"], r"^[0-9a-f]{64}$")
            self.assertNotIn(_SOURCE_ONLY_SENTINEL, json.dumps(interaction, sort_keys=True))

            packs = {target: build_handoff_context_pack(paths, executor_target=target) for target in _EXECUTORS}
            rows = {target: _domain_rows(pack) for target, pack in packs.items()}
            self.assertTrue(all(len(value) == 1 for value in rows.values()), rows)
            semantics = [{key: value for key, value in rows[target][0].items() if key != "executor_target"} for target in _EXECUTORS]
            self.assertTrue(all(value == semantics[0] for value in semantics[1:]))
            self.assertEqual(semantics[0]["profile_id"], profile["profile_id"])
            self.assertEqual(semantics[0]["profile_revision"], profile["revision"])
            self.assertEqual(semantics[0]["profile_digest"], profile["payload_digest"])
            self.assertEqual(semantics[0]["review_id"], review_record["review_id"])
            self.assertEqual(semantics[0]["replay_evaluation"]["reason_code"], "eligible")
            self.assertNotIn(_SOURCE_ONLY_SENTINEL, json.dumps(packs, sort_keys=True))
            for pack in packs.values():
                self.assertEqual(validate_handoff_context_pack(pack, require_conflict_free=True), [])

            role_packs = {target: build_role_context_pack(context_pack=packs[target]) for target in _EXECUTORS}
            self.assertEqual(len({pack["pack_hash"] for pack in role_packs.values()}), 1)
            for pack in role_packs.values():
                self.assertEqual(validate_role_context_pack(pack), [])

            manifest = build_handoff_input_manifest(
                executor_target="codex",
                session_id="task-12",
                scope={"kind": "project", "ref": scope_ref},
                workspace_root=root,
                selections=[ManifestSelection("file", "path", "PROJECT_TERMS.md")],
                context_pack=packs["codex"],
            )
            self.assertEqual(validate_handoff_input_manifest(manifest), [])
            source_item = next(item for item in manifest["items"] if item["item_kind"] == "file")
            memory_item = next(
                item
                for item in manifest["items"]
                if item["item_kind"] == "reviewed_memory"
                and item["selector"]["expression"] == semantics[0]["item_id"]
            )
            self.assertEqual(source_item["selector"], {"kind": "path", "expression": "PROJECT_TERMS.md"})
            self.assertEqual(source_item["hash"], f"sha256:{source_sha}")
            self.assertEqual(source_item["byte_cost"], len(_VALID_SOURCE))
            self.assertEqual(source_item["inclusion_reason"], "explicit_selection")
            self.assertEqual(source_item["safety_result"]["status"], "safe")
            self.assertEqual(memory_item["provenance"]["truth_level"], "approved_context")
            self.assertRegex(manifest["digest"], r"^sha256:[0-9a-f]{64}$")
            self.assertEqual(manifest, build_handoff_input_manifest(
                executor_target="codex", session_id="task-12",
                scope={"kind": "project", "ref": scope_ref}, workspace_root=root,
                selections=[ManifestSelection("file", "path", "PROJECT_TERMS.md")], context_pack=packs["codex"],
            ))

            unchanged_store = _store_snapshot(root)
            changed_source = _VALID_SOURCE.replace(b"reviewed pipeline discussion", b"reviewed sales discussion")
            (root / "PROJECT_TERMS.md").write_bytes(changed_source)
            changed = _ok(root, ["domain-list", "--scope-kind", "project", "--scope-ref", scope_ref, "--domain", "sales", "--source-freshness"])
            self.assertEqual((changed["profiles"][0]["source_freshness"]["state"], changed["profiles"][0]["source_freshness"]["reason"]), ("changed", "source_digest_changed"))
            self.assertEqual(_store_snapshot(root), unchanged_store)
            self.assertIn("domain_routing_context", _chat(root, unresolved))

            (root / "PROJECT_TERMS.md").unlink()
            missing = _ok(root, ["domain-list", "--scope-kind", "project", "--scope-ref", scope_ref, "--domain", "sales", "--source-freshness"])
            self.assertEqual((missing["profiles"][0]["source_freshness"]["state"], missing["profiles"][0]["source_freshness"]["reason"]), ("missing", "source_file_missing"))
            self.assertEqual(_store_snapshot(root), unchanged_store)

            retired = _ok(root, ["domain-retire", "--scope-kind", "project", "--scope-ref", scope_ref, "--domain", "sales", "--retired-by", "task-12", "--reason", "superseded"])
            self.assertEqual(retired["decision"], "retired")
            self.assertEqual(retired["profile"]["status"], "retired")
            after = _chat(root, unresolved)
            self.assertNotIn("domain_routing_context", after)
            self.assertEqual(after["chat_response"]["body"], source_only["chat_response"]["body"])
            retired_pack = build_handoff_context_pack(paths, executor_target="generic")
            self.assertEqual(_domain_rows(retired_pack), [])
            self.assertIn("domain_profile_retired", {row["reason"] for row in retired_pack["excluded_context"]})

    def test_protected_source_and_isolation_controls_are_fail_closed_and_non_mutating(self) -> None:
        with TemporaryDirectory() as temporary:
            base = Path(temporary)
            root = _repository(base / "one" / "same-project")
            isolated = _repository(base / "two" / "same-project")
            staged = _ok(root, ["domain-capture", "--from-file", "PROJECT_TERMS.md", "--stage", "--json"])
            candidate_id = staged["candidate_ids"][0]
            _ok(root, ["domain-approve", candidate_id])

            controls = (
                ("direct-answer", "what's 2+2?"),
                ("file-lookup", "find PROJECT_TERMS.md"),
                ("help", "what OMH workflows are available?"),
                ("explicit", "$ulw-plan ship this"),
                ("status", "Codex 작업이 어디까지 진행됐는지 알려줘"),
                ("dispatch", "why is the build failing on main?"),
                ("definition-only", f"Pipeline review means {_SOURCE_ONLY_SENTINEL}."),
                ("glossary-avoid-only", "Say reviewed pipeline discussion, not sales ceremony."),
            )
            store_before = _store_snapshot(root)
            for name, message in controls:
                with self.subTest(name=name):
                    route = route_chat_message(message)
                    eligibility = classify_domain_context_eligibility(route, message)
                    interaction = _chat(root, message)
                    self.assertFalse(eligibility.eligible)
                    self.assertIn(eligibility.reason, {"protected_route", "dispatch_route"})
                    self.assertNotIn("domain_routing_context", interaction)
            self.assertEqual(_store_snapshot(root), store_before)
            self.assertNotIn("domain_routing_context", _chat(isolated, "something revenue prism feels unresolved"))
            self.assertEqual(_store_snapshot(isolated), {})

            refusal_root = _repository(base / "refusals")
            refusal_store = _store_snapshot(refusal_root)
            (refusal_root / "PROJECT_TERMS.md").write_bytes(_VALID_SOURCE.replace(_SOURCE_ONLY_SENTINEL.encode(), b"password=hunter2"))
            status, payload, stderr = _run(refusal_root, ["domain-capture", "--from-file", "PROJECT_TERMS.md", "--stage", "--json"])
            self.assertNotEqual(status, 0)
            self.assertIsNone(payload)
            self.assertIn("unsafe_project_terms_value", stderr)
            self.assertEqual(_store_snapshot(refusal_root), refusal_store)

            (refusal_root / "PROJECT_TERMS.md").write_bytes(_VALID_SOURCE + b" " * (65_537 - len(_VALID_SOURCE)))
            status, payload, stderr = _run(refusal_root, ["domain-capture", "--from-file", "PROJECT_TERMS.md", "--stage", "--json"])
            self.assertNotEqual(status, 0)
            self.assertIsNone(payload)
            self.assertIn("project_terms_source_too_large", stderr)
            self.assertEqual(_store_snapshot(refusal_root), refusal_store)

            outside = base / "outside-project-terms.md"
            outside.write_bytes(_VALID_SOURCE)
            (refusal_root / "PROJECT_TERMS.md").unlink()
            (refusal_root / "PROJECT_TERMS.md").symlink_to(outside)
            status, payload, stderr = _run(refusal_root, ["domain-capture", "--from-file", "PROJECT_TERMS.md", "--stage", "--json"])
            self.assertNotEqual(status, 0)
            self.assertIsNone(payload)
            self.assertIn("project_terms_source_must_not_be_symlink", stderr)
            self.assertEqual(_store_snapshot(refusal_root), refusal_store)

            stale_root = _repository(base / "stale")
            file_candidate = _ok(stale_root, ["domain-capture", "--from-file", "PROJECT_TERMS.md", "--stage", "--json"])["candidate_ids"][0]
            competing = _ok(stale_root, [
                "domain-capture", "--scope-kind", "project", "--scope-ref", project_identity(stale_root),
                "--domain", "sales", "--mapping", "revenue prism=other-review",
            ])["candidate"]["candidate_id"]
            _ok(stale_root, ["domain-approve", competing])
            stale_before = _store_snapshot(stale_root)
            freshness = _ok(stale_root, ["domain-review", "--candidate", file_candidate, "--source-freshness"])
            self.assertEqual(freshness["cards"][0]["source_freshness"]["state"], "unchanged")
            self.assertEqual(_store_snapshot(stale_root), stale_before)
            status, payload, stderr = _run(stale_root, ["domain-approve", file_candidate])
            self.assertNotEqual(status, 0)
            self.assertIsNone(payload)
            self.assertIn("stale_candidate", stderr)
            self.assertEqual(_store_snapshot(stale_root), stale_before)


if __name__ == "__main__":
    unittest.main()
