from __future__ import annotations

from copy import deepcopy
from collections.abc import Callable
import hashlib
import json
from pathlib import Path
import subprocess
import sys
from tempfile import TemporaryDirectory
from typing import Unpack
import unittest
from unittest.mock import patch

from _local_package import load_local_package

load_local_package()
from project_identity_fixture import PROJECT_IDENTITY, memory_paths as resolve_paths
from omh.plugin_bundle.omh import memory_recall_selector as selector
from omh.workflows import memory
from memory_recall_fixture import (
    GLOBAL, NOW, PROJECT, THREAD, Payload, SelectionOptions,
    decode, mapping, payload, text,
    included_ids as included_ids,
    reviewed as reviewed,
    selection as selection,
)


class CanonicalRecallTests(unittest.TestCase):
    def test_empty_required_scope_fails_closed(self) -> None:
        with TemporaryDirectory() as tmp:
            paths = resolve_paths(Path(tmp) / ".omh", Path(tmp) / ".hermes")
            captured = mapping(decode(json.dumps(memory.capture_project_memory_candidate(
                paths, "Release checklist requires tests", scope_kind="project", scope_ref="foreign-project",
            ))))
            candidate = mapping(captured["candidate"])
            _ = memory.approve_project_memory_candidate(paths, text(candidate["candidate_id"]))
            pack = mapping(decode(json.dumps(memory.build_project_memory_recall_pack(
                paths, "release", scope_kind="project", scope_ref="",
            ))))
            included = pack["included_records"]
            assert isinstance(included, list)
            self.assertEqual(len(included), 0, "An unresolved required scope must not become a wildcard")

    def test_delivery_requires_explicit_nonempty_scope_allowlist(self) -> None:
        pairs = [reviewed("mem-current"), reviewed("mem-global", scope=GLOBAL)]
        for scopes in (None, [], [GLOBAL], [{"kind": "project", "ref": ""}], [{"kind": "project", "ref": "   "}]):
            with self.subTest(scopes=scopes):
                result = selection(pairs, allowed_scopes=scopes)
                self.assertEqual(included_ids(result), [])
                self.assertEqual(result.scope_status, "unresolved")
                self.assertEqual(result.exclusion_reason_counts, {"scope_unresolved": 1})
        global_only = selection(pairs, allowed_scopes=[GLOBAL], required_scope_kinds=("user-global",))
        self.assertEqual(included_ids(global_only), ["mem-global"])

    def test_explicit_global_project_thread_and_perspective_allowlists(self) -> None:
        pairs = [
            reviewed("mem-project"), reviewed("mem-global", scope=GLOBAL), reviewed("mem-thread", scope=THREAD),
            reviewed("mem-foreign-project", scope={"kind": "project", "ref": "elsewhere"}),
            reviewed("mem-foreign-thread", scope={"kind": "thread", "ref": "session-b"}),
            reviewed("mem-foreign-global", scope={"kind": "user-global", "ref": "another-user"}),
            reviewed("mem-foreign-actor", perspective={"observer": "hermes", "observed": "codex"}),
            reviewed("mem-hermes", perspective={"observer": "hermes", "observed": "hermes"}),
        ]
        result = selection(pairs, allowed_scopes=[GLOBAL, PROJECT, THREAD], required_scope_kinds=("project", "thread"))
        self.assertEqual(set(included_ids(result)), {"mem-project", "mem-global", "mem-thread", "mem-hermes"})
        self.assertEqual(result.exclusion_reason_counts, {"perspective_mismatch": 1, "scope_mismatch": 3})
        self.assertNotIn("mem-foreign", json.dumps(payload(result)))
        self.assertEqual(payload(result)["scope"], PROJECT)
        blank_lens = selection(pairs, observed="", observer="")
        self.assertNotIn("mem-foreign-actor", included_ids(blank_lens))

    def test_user_global_is_explicit_and_legacy_records_are_not_upgraded(self) -> None:
        pairs = [reviewed("mem-foreign", scope={"kind": "project", "ref": "elsewhere"})]
        legacy, review = reviewed("mem-legacy")
        legacy["schema_version"] = "project_memory_record/v1"
        legacy["review_status"] = "approved"
        pairs.append((legacy, review))
        before = deepcopy(pairs)
        result = selection(pairs, allowed_scopes=[GLOBAL, PROJECT])
        self.assertEqual(included_ids(result), [])
        self.assertEqual(result.exclusion_reason_counts, {"scope_mismatch": 1, "review_required_legacy": 1})
        self.assertEqual(pairs, before)

    def test_lifecycle_and_immutable_review_exclusions(self) -> None:
        changes: dict[str, Callable[[Payload, Payload], None]] = {
            "review_required": lambda record, review: mapping(record["admission"]).update(state="pending_review"),
            "admission_rejected": lambda record, review: mapping(record["admission"]).update(state="rejected"),
            "admission_blocked": lambda record, review: mapping(record["admission"]).update(state="blocked"),
            "payload_digest_mismatch": lambda record, review: record.update(summary="Changed release checklist"),
            "review_payload_mismatch": lambda record, review: review.update(payload_digest="0" * 64),
            "review_identity_mismatch": lambda record, review: mapping(review["artifact_identity"]).update(revision=2),
            "review_not_found": lambda record, review: mapping(record["admission"]).update(review_id="absent"),
            "superseded": lambda record, review: record.update(superseded_by="mem-new"),
            "expired_standard": lambda record, review: mapping(record["retention"]).update(expires_at="2026-09-11T00:00:00Z"),
            "stale_review_required": lambda record, review: mapping(record["revalidation"]).update(deadline="2026-09-11T00:00:00Z"),
            "operation_incomplete": lambda record, review: record.update(operation_id="operation-a"),
        }
        for reason, mutate in changes.items():
            with self.subTest(reason=reason):
                record, review = reviewed("mem-lifecycle")
                mutate(record, review)
                result = selection([(record, review)], pins={"mem-lifecycle"})
                self.assertEqual(included_ids(result), [])
                self.assertEqual(result.exclusion_reason_counts, {reason: 1})
        pair = reviewed("mem-operation", operation_id="operation-a")
        self.assertEqual(included_ids(selection([pair], operation_states={"operation-a": "completed"})), ["mem-operation"])

    def test_source_freshness_and_stale_inspection_boundary(self) -> None:
        with TemporaryDirectory() as tmp:
            source = Path(tmp) / "source.txt"
            _ = source.write_text("synthetic source revision one", encoding="utf-8")
            pair = reviewed("mem-source", source_evidence={"path": str(source), "sha256": hashlib.sha256(source.read_bytes()).hexdigest()})
            self.assertEqual(included_ids(selection([pair])), ["mem-source"])
            _ = source.write_text("synthetic source revision two", encoding="utf-8")
            self.assertEqual(selection([pair]).exclusion_reason_counts, {"source_changed": 1})
            inspection = selection([pair], inspection=True, include_stale=True)
            self.assertEqual(included_ids(inspection), ["mem-source"])
            inspected = payload(inspection)["included_records"]
            assert isinstance(inspected, list)
            self.assertFalse(mapping(mapping(inspected[0])["replay_evaluation"])["eligible"])
            with self.assertRaises(ValueError):
                _ = selection([pair], include_stale=True)
            source.unlink()
            self.assertEqual(selection([pair]).exclusion_reason_counts, {"source_unverifiable": 1})

    def test_pins_attention_relevance_age_and_usage_share_one_order(self) -> None:
        pairs = [
            reviewed("mem-strong", summary="Release tests staging checklist", tags=["release", "tests"]),
            reviewed("mem-used", approved_at="2026-09-01T00:00:00Z"),
            reviewed("mem-unused", approved_at="2026-09-01T00:00:00Z"),
            reviewed("mem-old", approved_at="2025-01-01T00:00:00Z"),
            reviewed("mem-reference", attention={"tier": "reference"}, tags=["release", "tests"]),
            reviewed("mem-archive", attention={"tier": "archive"}),
            reviewed("mem-pin", summary="Architecture overview"),
        ]
        result = selection(pairs, query="release tests", pins={"mem-pin", "mem-archive"},
                           usage={"mem-used": {"times_recalled": 10}}, limit=10)
        self.assertEqual(included_ids(result), ["mem-pin", "mem-strong", "mem-used", "mem-unused", "mem-old", "mem-reference"])
        self.assertEqual(result.exclusion_reason_counts, {"archived_tier": 1})
        included = payload(result)["included_records"]
        assert isinstance(included, list)
        by_id = {text(mapping(item)["record_id"]): mapping(item) for item in included}
        self.assertEqual(mapping(by_id["mem-old"]["ranking"])["age_tier"], 2)
        self.assertEqual(mapping(by_id["mem-used"]["ranking"])["times_recalled"], 10)
        self.assertEqual(mapping(by_id["mem-reference"]["ranking"])["attention_rank"], 1)
        self.assertIn("mem-archive", included_ids(selection(pairs, include_archived=True, limit=10)))

    def test_record_and_character_budgets_cut_the_ranked_prefix(self) -> None:
        pairs = [reviewed("mem-first", summary="Release tests " * 20, tags=["release", "tests"]), reviewed("mem-second")]
        budgets: list[SelectionOptions] = [{"limit": 1}, {"max_chars": len(text(pairs[0][0]["summary"]))}]
        for kwargs in budgets:
            with self.subTest(kwargs=kwargs):
                result = selection(pairs, **kwargs)
                self.assertEqual(included_ids(result), ["mem-first"])
                self.assertEqual(result.exclusion_reason_counts, {"over_budget": 1})
                self.assertTrue(payload(result)["truncated"])
        result = selection(pairs, max_chars=40)
        self.assertEqual(included_ids(result), [], "A shorter low-priority record cannot jump over the cut")
        self.assertEqual(result.exclusion_reason_counts, {"over_budget": 2})

    def test_configuration_identity_is_stable_and_tracks_the_effective_lens(self) -> None:
        pairs = [reviewed("mem-current")]
        first = selection(pairs, allowed_scopes=[GLOBAL, PROJECT])
        same = selection(pairs, allowed_scopes=[PROJECT, GLOBAL, PROJECT])
        self.assertEqual(first.configuration_id, same.configuration_id)
        variants: list[SelectionOptions] = [{"limit": 1}, {"max_chars": 20}, {"observed": "codex"}, {"query_intent": "temporal"},
                                          {"include_archived": True}, {"allowed_scopes": [PROJECT]}, {"inspection": True}]
        for options in variants:
            with self.subTest(options=options):
                merged: SelectionOptions = {"allowed_scopes": [GLOBAL, PROJECT], **options}
                changed = selection(pairs, **merged)
                self.assertNotEqual(first.configuration_id, changed.configuration_id)
        self.assertIs(memory.effective_recall_configuration, selector.effective_recall_configuration)
        self.assertNotIn("summary", json.dumps(first.configuration))
        self.assertNotIn("release", json.dumps(first.configuration))

    def test_handoff_facade_uses_shared_selector_for_global_project_and_thread(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp) / "qa-project"
            (root / ".git").mkdir(parents=True)
            paths = resolve_paths(root / ".omh", Path(tmp) / ".hermes")
            expected: list[str] = []
            with patch.object(memory, "utc_now", return_value="2026-09-01T00:00:00Z"):
                for index, scope in enumerate((GLOBAL, {"kind": "project", "ref": PROJECT_IDENTITY}, THREAD, {"kind": "project", "ref": "foreign"})):
                    captured = mapping(decode(json.dumps(memory.capture_project_memory_candidate(
                        paths, f"Release checklist fixture {index}", scope_kind=scope["kind"], scope_ref=scope["ref"],
                    ))))
                    candidate = mapping(captured["candidate"])
                    approved = mapping(decode(json.dumps(memory.approve_project_memory_candidate(
                        paths, text(candidate["candidate_id"]),
                    ))))
                    record = mapping(approved["record"])
                    if index < 3:
                        expected.append(text(record["record_id"]))
            before = {str(path.relative_to(paths.memory_dir)): path.read_bytes() for path in paths.memory_dir.rglob("*.json")}
            def frozen_selection(
                records: list[Payload], query: str = "", **kwargs: Unpack[SelectionOptions],
            ) -> selector.MemoryRecallSelection:
                options: SelectionOptions = {**kwargs, "now": NOW}
                return selector.select_memory_recall(records, query, **options)

            with patch.object(memory, "select_memory_recall", side_effect=frozen_selection) as shared:
                pack = memory.memory_recall_pack_for_handoff(paths, "release", executor_target="hermes", session_id="session-a")
            assert pack is not None
            self.assertEqual(shared.call_count, 1)
            assert shared.call_args is not None
            self.assertFalse(mapping(decode(json.dumps(shared.call_args.kwargs, default=str)))["inspection"])
            included = mapping(decode(json.dumps(pack)))["included_records"]
            assert isinstance(included, list)
            self.assertEqual({text(mapping(item)["record_id"]) for item in included}, set(expected))
            self.assertEqual(memory.validate_project_memory_recall_pack(pack), [])
            after = {str(path.relative_to(paths.memory_dir)): path.read_bytes() for path in paths.memory_dir.rglob("*.json")}
            self.assertEqual(before, after, "Selection must not change usage, reviews, pins or records")

    def test_standalone_installed_bundle_never_imports_control_plane(self) -> None:
        bundle = Path(__file__).resolve().parents[1] / "src" / "plugin_bundle"
        code = (
            "import sys; sys.path.insert(0, sys.argv[1]); "
            "from omh.memory_recall_selector import select_memory_recall; "
            "result = select_memory_recall([], allowed_scopes=[{'kind':'project','ref':'qa-project'}]); "
            "assert result.scope_status == 'resolved'; "
            "assert 'omh.workflows' not in sys.modules; "
            "assert result.pack['record_count'] == 0"
        )
        process = subprocess.run([sys.executable, "-I", "-S", "-c", code, str(bundle)], capture_output=True, text=True, timeout=15)
        self.assertEqual(process.returncode, 0, process.stderr)


if __name__ == "__main__":
    _ = unittest.main()
