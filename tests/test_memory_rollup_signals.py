from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from _local_package import load_local_package

load_local_package()
from omh.plugin_bundle.omh import memory_governance
from omh.memory import (
    approve_project_memory_candidate,
    build_memory_lineage,
    build_memory_rollup,
    build_project_memory_recall_pack,
    capture_project_memory_candidate,
    validate_project_memory_recall_pack,
)
from omh.paths import resolve_paths
from omh.profiles.setup import write_setup_profile
from omh.runtime.records import _compact_memory_recall_pack


def _approve_capture(paths, summary, **kwargs):
    captured = capture_project_memory_candidate(paths, summary, **kwargs)
    return approve_project_memory_candidate(paths, captured["candidate"]["candidate_id"])["record"]


def _rewrite_record(paths, record_id, **fields):
    record_path = paths.memory_dir / "records" / f"{record_id}.json"
    record = json.loads(record_path.read_text(encoding="utf-8"))
    record.update(fields)
    digest = memory_governance.canonical_payload_digest(record)
    record["admission"]["payload_digest"] = digest
    review_path = paths.memory_dir / "reviews" / f"{record['admission']['review_id']}.json"
    review = json.loads(review_path.read_text(encoding="utf-8"))
    review["payload_digest"] = digest
    review_path.write_text(json.dumps(review), encoding="utf-8")
    record_path.write_text(json.dumps(record), encoding="utf-8")


class EpisodeRollupTests(unittest.TestCase):
    def test_rollup_requires_a_selector_and_two_members(self) -> None:
        with TemporaryDirectory() as tmp:
            paths = resolve_paths(Path(tmp) / ".omh", Path(tmp) / ".hermes")
            with self.assertRaisesRegex(ValueError, "requires"):
                build_memory_rollup(paths)

            _approve_capture(paths, "lonely deploy fact", tags=["deploy"])
            report = build_memory_rollup(paths, tag="deploy")
            self.assertFalse(report["eligible"])
            self.assertEqual(report["reason_code"], "not_enough_members")
            self.assertFalse(report["applied"])

    def test_rollup_report_is_additive_and_apply_stages_a_reviewable_episode(self) -> None:
        with TemporaryDirectory() as tmp:
            paths = resolve_paths(Path(tmp) / ".omh", Path(tmp) / ".hermes")
            first = _approve_capture(paths, "Deploy failed on stale cache", tags=["deploy"])
            second = _approve_capture(paths, "Deploy fixed by cache purge", tags=["deploy"])
            _approve_capture(paths, "Unrelated routing fact", tags=["routing"])

            report = build_memory_rollup(paths, tag="deploy")
            self.assertTrue(report["eligible"])
            self.assertEqual({member["record_id"] for member in report["members"]}, {first["record_id"], second["record_id"]})
            self.assertFalse(report["applied"], "report-first: no candidate staged without --apply")

            applied = build_memory_rollup(paths, tag="deploy", apply=True)
            self.assertTrue(applied["applied"])
            candidate = applied["capture"]["candidate"]
            self.assertEqual(candidate["record_type"], "episode")
            self.assertEqual(set(candidate["derived_from"]), {first["record_id"], second["record_id"]})

            episode = approve_project_memory_candidate(paths, candidate["candidate_id"])["record"]
            lineage = build_memory_lineage(paths, episode["record_id"])
            self.assertEqual(
                {card["record_id"] for card in lineage["ancestors"]},
                {first["record_id"], second["record_id"]},
            )
            for member_id in (first["record_id"], second["record_id"]):
                self.assertTrue((paths.memory_dir / "records" / f"{member_id}.json").exists(), "rollup is additive")

    def test_rollup_skips_expired_and_episode_records_and_caps_members(self) -> None:
        with TemporaryDirectory() as tmp:
            paths = resolve_paths(Path(tmp) / ".omh", Path(tmp) / ".hermes")
            for index in range(10):
                _approve_capture(paths, f"deploy fact number {index}", tags=["deploy"])
            expired = _approve_capture(paths, "expired deploy fact", tags=["deploy"], retention_class="volatile", ttl_days=1)
            past = (datetime.now(timezone.utc) - timedelta(days=7)).replace(microsecond=0).isoformat().replace("+00:00", "Z")
            _rewrite_record(paths, expired["record_id"], ttl={"ttl_days": 1, "expires_at": past})

            report = build_memory_rollup(paths, tag="deploy", apply=True)

            member_ids = {member["record_id"] for member in report["members"]}
            self.assertEqual(report["member_count"], 8, "members cap at the derived-from limit")
            self.assertEqual(report["truncated_members"], 2)
            self.assertNotIn(expired["record_id"], member_ids)

            episode_candidate = report["capture"]["candidate"]
            follow_up = build_memory_rollup(paths, tag="deploy")
            approve_project_memory_candidate(paths, episode_candidate["candidate_id"])
            after_episode = build_memory_rollup(paths, tag="deploy")
            self.assertEqual(
                after_episode["considered_count"],
                follow_up["considered_count"],
                "an approved episode never becomes a rollup member itself",
            )


class RollupConfinementTests(unittest.TestCase):
    def test_partial_scope_selector_is_rejected(self) -> None:
        with TemporaryDirectory() as tmp:
            paths = resolve_paths(Path(tmp) / ".omh", Path(tmp) / ".hermes")
            with self.assertRaisesRegex(ValueError, "both"):
                build_memory_rollup(paths, tag="deploy", scope_kind="target")

    def test_mixed_perspectives_refuse_and_shared_perspective_is_inherited(self) -> None:
        with TemporaryDirectory() as tmp:
            paths = resolve_paths(Path(tmp) / ".omh", Path(tmp) / ".hermes")
            _approve_capture(paths, "codex deploy lesson one", tags=["deploy"], observed="codex")
            _approve_capture(paths, "unscoped deploy fact", tags=["deploy"])

            mixed = build_memory_rollup(paths, tag="deploy", apply=True)
            self.assertFalse(mixed["eligible"])
            self.assertEqual(mixed["reason_code"], "mixed_perspective")
            self.assertFalse(mixed["applied"])

            _approve_capture(paths, "codex deploy lesson two", tags=["codexonly"], observed="codex")
            _approve_capture(paths, "codex deploy lesson three", tags=["codexonly"], observed="codex")
            report = build_memory_rollup(paths, tag="codexonly", apply=True)
            self.assertTrue(report["applied"])
            episode = approve_project_memory_candidate(paths, report["capture"]["candidate"]["candidate_id"])["record"]
            self.assertEqual(episode["perspective"], {"observer": "hermes", "observed": "codex"})

            claude_lens = build_project_memory_recall_pack(paths, "", observed="claude-code")
            self.assertNotIn(
                episode["record_id"],
                {item["record_id"] for item in claude_lens["included_records"]},
                "a codex-member episode must not reach another executor's lens",
            )

    def test_mixed_scopes_refuse_and_shared_scope_is_inherited(self) -> None:
        with TemporaryDirectory() as tmp:
            paths = resolve_paths(Path(tmp) / ".omh", Path(tmp) / ".hermes")
            _approve_capture(paths, "alpha host fact", tags=["net"], scope_kind="target", scope_ref="alpha")
            _approve_capture(paths, "beta host fact", tags=["net"], scope_kind="target", scope_ref="beta")

            mixed = build_memory_rollup(paths, tag="net")
            self.assertEqual(mixed["reason_code"], "mixed_scope")

            _approve_capture(paths, "alpha port fact", tags=["alphanet"], scope_kind="target", scope_ref="alpha")
            _approve_capture(paths, "alpha dns fact", tags=["alphanet"], scope_kind="target", scope_ref="alpha")
            report = build_memory_rollup(paths, tag="alphanet", apply=True)
            self.assertEqual(report["selector"]["scope"], {"kind": "target", "ref": "alpha"})
            candidate = report["capture"]["candidate"]
            self.assertEqual(candidate["scope"], {"kind": "target", "ref": "alpha"}, "the episode stays in the members' scope")

    def test_rollup_never_auto_approves_and_reruns_report_already_staged(self) -> None:
        with TemporaryDirectory() as tmp:
            paths = resolve_paths(Path(tmp) / ".omh", Path(tmp) / ".hermes")
            write_setup_profile(paths, memory_mode="auto-safe")
            first = capture_project_memory_candidate(paths, "deploy fact one", tags=["deploy"])
            second = capture_project_memory_candidate(paths, "deploy fact two", tags=["deploy"])
            self.assertTrue(first["auto_approved"] and second["auto_approved"], "sanity: auto-safe active")

            report = build_memory_rollup(paths, tag="deploy", apply=True)

            self.assertTrue(report["applied"])
            self.assertEqual(report["candidate_status"], "pending_review")
            self.assertFalse(report["capture"]["auto_approved"], "a derived aggregate never auto-approves")

            rerun = build_memory_rollup(paths, tag="deploy", apply=True)
            self.assertEqual(rerun["reason_code"], "already_staged")
            self.assertEqual(rerun["staged_candidate_id"], report["capture"]["candidate"]["candidate_id"])
            self.assertFalse(rerun["applied"])

    def test_volatile_member_makes_the_episode_volatile_with_smallest_ttl(self) -> None:
        with TemporaryDirectory() as tmp:
            paths = resolve_paths(Path(tmp) / ".omh", Path(tmp) / ".hermes")
            _approve_capture(paths, "standard deploy fact", tags=["deploy"])
            _approve_capture(paths, "volatile deploy note", tags=["deploy"], retention_class="volatile", ttl_days=2)

            report = build_memory_rollup(paths, tag="deploy", apply=True)

            self.assertEqual(report["episode_retention"], {"class": "volatile", "ttl_days": 2})
            episode = approve_project_memory_candidate(paths, report["capture"]["candidate"]["candidate_id"])["record"]
            self.assertEqual(episode["retention"]["class"], "volatile")
            self.assertEqual(episode["retention"]["ttl_days"], 2, "volatile content must not outlive its strictest member TTL class")

    def test_selection_is_oldest_first_by_identity_and_summary_budget_names_every_member(self) -> None:
        with TemporaryDirectory() as tmp:
            paths = resolve_paths(Path(tmp) / ".omh", Path(tmp) / ".hermes")
            records = []
            base = datetime(2026, 7, 1, tzinfo=timezone.utc)
            for index in range(4):
                record = _approve_capture(paths, f"member-{index} " + ("detail " * 25), tags=["long"])
                stamp = (base + timedelta(days=index)).replace(microsecond=0).isoformat().replace("+00:00", "Z")
                _rewrite_record(paths, record["record_id"], approved_at=stamp)
                records.append(record)

            report = build_memory_rollup(paths, tag="long")

            self.assertEqual(
                [member["record_id"] for member in report["members"]],
                [record["record_id"] for record in records],
                "selection contract: oldest first, deterministic",
            )
            self.assertEqual(report["selector"]["selection"], "oldest_first")
            for index in range(4):
                self.assertIn(f"member-{index}", report["proposed_summary"], "the budgeted join must name every member")


class RecallSignalTests(unittest.TestCase):
    def test_manual_review_outweighs_auto_safe_at_equal_relevance(self) -> None:
        with TemporaryDirectory() as tmp:
            paths = resolve_paths(Path(tmp) / ".omh", Path(tmp) / ".hermes")
            manual = _approve_capture(paths, "manually reviewed decision")
            write_setup_profile(paths, memory_mode="auto-safe")
            auto = capture_project_memory_candidate(paths, "auto approved decision", tags=["ok"])
            self.assertTrue(auto["auto_approved"])
            auto_id = auto["record"]["record_id"]
            same_moment = "2026-07-10T00:00:00Z"
            _rewrite_record(paths, manual["record_id"], approved_at=same_moment)
            _rewrite_record(paths, auto_id, approved_at=same_moment)

            pack = build_project_memory_recall_pack(paths, "")

            by_id = {item["record_id"]: item for item in pack["included_records"]}
            self.assertEqual(by_id[manual["record_id"]]["ranking"]["veracity_weight_pct"], 100)
            self.assertEqual(by_id[auto_id]["ranking"]["veracity_weight_pct"], 90)
            self.assertEqual([item["record_id"] for item in pack["included_records"]][0], manual["record_id"])

    def test_temporal_queries_double_the_recency_weight(self) -> None:
        with TemporaryDirectory() as tmp:
            paths = resolve_paths(Path(tmp) / ".omh", Path(tmp) / ".hermes")
            older = _approve_capture(paths, "release process detail one", tags=["release"])
            newer = _approve_capture(paths, "release process detail two", tags=["release"])
            _rewrite_record(paths, older["record_id"], approved_at="2026-06-01T00:00:00Z")
            _rewrite_record(paths, newer["record_id"], approved_at="2026-07-20T00:00:00Z")

            plain = build_project_memory_recall_pack(paths, "release process detail")
            temporal = build_project_memory_recall_pack(paths, "most recent release process detail")

            self.assertEqual(plain["query_intent"], "default")
            self.assertEqual(temporal["query_intent"], "temporal")
            self.assertEqual(validate_project_memory_recall_pack(temporal), [])
            plain_by_id = {item["record_id"]: item for item in plain["included_records"]}
            temporal_by_id = {item["record_id"]: item for item in temporal["included_records"]}
            self.assertGreater(
                temporal_by_id[newer["record_id"]]["ranking"]["rrf_score_micro"],
                plain_by_id[newer["record_id"]]["ranking"]["rrf_score_micro"],
                "a temporal cue boosts the recency contribution",
            )

    def test_intent_never_changes_which_keyword_matches_win(self) -> None:
        with TemporaryDirectory() as tmp:
            paths = resolve_paths(Path(tmp) / ".omh", Path(tmp) / ".hermes")
            strong = _approve_capture(paths, "release gate checklist and ruff", tags=["release", "ruff"])
            weak = _approve_capture(paths, "release notes location")
            _rewrite_record(paths, strong["record_id"], approved_at="2026-01-01T00:00:00Z")
            _rewrite_record(paths, weak["record_id"], approved_at="2026-07-20T00:00:00Z")

            pack = build_project_memory_recall_pack(paths, "recent release ruff checklist")

            self.assertEqual(pack["query_intent"], "temporal")
            ids = [item["record_id"] for item in pack["included_records"]]
            self.assertEqual(ids[0], strong["record_id"], "relevance stays primary even under a temporal cue")

    def test_intent_overroute_guards_hold(self) -> None:
        from omh.workflows.memory import _recall_query_intent

        for query in (
            "Refactor the current implementation of the router",
            "Upgrade to the latest ruff version",
            "Fix the flaky test now",
            "Use the newest API",
            "the config is nowhere to be found",
            "check knownHosts handling",
            "현재 배포 상태 어때",
        ):
            self.assertEqual(_recall_query_intent(query), "default", query)
        for query in ("what changed yesterday", "most recent deploy failure", "deploys from last week"):
            self.assertEqual(_recall_query_intent(query), "temporal", query)

    def test_handoff_packs_accept_caller_stated_intent(self) -> None:
        # The cue table stays English-only by policy; the handoff surface is
        # where a caller that read the message -- in any language -- states
        # the intent instead. Auto stays default for Korean on purpose.
        from omh.workflows.memory import memory_recall_pack_for_handoff

        with TemporaryDirectory() as tmp:
            paths = resolve_paths(Path(tmp) / ".omh", Path(tmp) / ".hermes")
            _approve_capture(paths, "배포는 금요일에 하지 않는다")
            stated = memory_recall_pack_for_handoff(paths, "배포 결정", executor_target="codex", query_intent="temporal")
            self.assertEqual(stated["query_intent"], "temporal")
            auto = memory_recall_pack_for_handoff(paths, "배포 결정", executor_target="codex")
            self.assertEqual(auto["query_intent"], "default")

    def test_validator_rejects_malformed_query_intent(self) -> None:
        with TemporaryDirectory() as tmp:
            paths = resolve_paths(Path(tmp) / ".omh", Path(tmp) / ".hermes")
            _approve_capture(paths, "validator subject")
            pack = build_project_memory_recall_pack(paths, "")
            self.assertEqual(validate_project_memory_recall_pack(pack), [])
            for bogus in ("TOTALLY-BOGUS", 12345):
                errors = validate_project_memory_recall_pack({**pack, "query_intent": bogus})
                self.assertTrue(any("query_intent" in error for error in errors), bogus)

    def test_compacted_pack_keeps_intent_and_veracity_fields(self) -> None:
        with TemporaryDirectory() as tmp:
            paths = resolve_paths(Path(tmp) / ".omh", Path(tmp) / ".hermes")
            _approve_capture(paths, "compaction subject fact")

            pack = build_project_memory_recall_pack(paths, "recently changed compaction subject")
            compacted = _compact_memory_recall_pack(pack)

            self.assertEqual(compacted["query_intent"], "temporal")
            [item] = compacted["included_records"]
            self.assertEqual(item["ranking"]["veracity_weight_pct"], 100)


if __name__ == "__main__":
    unittest.main()
