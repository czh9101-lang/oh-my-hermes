"""Demotion moves an L1 entry's content into L2 and leaves a pointer behind.

Hermes memory is character-capped, so the only ways to make room are deleting
an entry (the fact is gone) or demoting it (the fact moves to OMH's governed
store and a short reference line stays). These tests pin the second one:

- an entry an approved OMH record already explains is *not* demotion work, it
  is deletable, and must never be proposed for a move that copies it down twice;
- the plan ranks by characters saved, because a plan whose first row frees the
  least is a plan nobody can act on under a full cap;
- the reference line is keyed by the entry's own sha256, which is what makes
  the pointer survive the candidate -> record lifecycle it points into.

Nothing here writes a Hermes file: the plan is prepared text and Hermes' own
memory tool is the only thing that may apply it.
"""

from __future__ import annotations

import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from _local_package import load_local_package

load_local_package()
from omh.memory import approve_project_memory_candidate, capture_project_memory_candidate
from omh.paths import resolve_paths
from omh.plugin_bundle.omh.hermes_memory import (
    DEFAULT_MEMORY_FILE_CAP_CHARS,
    DEMOTION_REFERENCE_LABEL_CHARS,
    HERMES_MEMORY_DELIMITER,
    MEMORY_DEMOTION_PLAN_SCHEMA_VERSION,
    build_memory_demotion_plan,
)

LONG_ENTRY = ("문서 하네스는 에이전트가 HTML을 먼저 작성하고 변환하는 시스템이다. " * 6).strip()
MEDIUM_ENTRY = ("release 스크립트는 --dry-run 플래그로 계획만 출력한다. " * 3).strip()
SHORT_ENTRY = "커피 원두는 밀봉 용기에 보관한다"


def _write_memory_file(home: Path, label: str, *entries: str) -> Path:
    path = home / "memories" / label
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(HERMES_MEMORY_DELIMITER.join(entries), encoding="utf-8")
    return path


def _write_memory(home: Path, *entries: str) -> Path:
    return _write_memory_file(home, "MEMORY.md", *entries)


def _approved(paths, summary: str) -> str:
    capture = capture_project_memory_candidate(paths, summary, scope_ref="demo")
    approval = approve_project_memory_candidate(paths, str(capture["candidate"]["candidate_id"]))
    return str(approval["record"]["record_id"])


class _PlanCase(unittest.TestCase):
    def _homes(self, tmp: str) -> tuple[Path, Path, object]:
        root = Path(tmp)
        paths = resolve_paths(root / ".omh", root / ".hermes")
        return root / ".omh", root / ".hermes", paths


class RankingTests(_PlanCase):
    def test_unsourced_entries_rank_by_characters_saved(self) -> None:
        with TemporaryDirectory() as tmp:
            omh_home, hermes_home, _ = self._homes(tmp)
            _write_memory(hermes_home, MEDIUM_ENTRY, LONG_ENTRY, SHORT_ENTRY)

            plan = build_memory_demotion_plan(omh_home, hermes_home)
            self.assertEqual(plan["schema_version"], MEMORY_DEMOTION_PLAN_SCHEMA_VERSION)
            # No approved records, so every entry qualifies; biggest first.
            self.assertEqual([row["entry_index"] for row in plan["rows"]], [1, 0, 2])
            self.assertEqual(plan["row_count"], 3)
            self.assertEqual(plan["already_covered"], [])
            self.assertEqual([row["chars"] for row in plan["rows"]], sorted((len(LONG_ENTRY), len(MEDIUM_ENTRY), len(SHORT_ENTRY)), reverse=True))

    def test_max_entries_caps_the_plan_at_the_biggest_rows(self) -> None:
        with TemporaryDirectory() as tmp:
            omh_home, hermes_home, _ = self._homes(tmp)
            _write_memory(hermes_home, MEDIUM_ENTRY, LONG_ENTRY, SHORT_ENTRY)

            plan = build_memory_demotion_plan(omh_home, hermes_home, max_entries=1)
            self.assertEqual(plan["row_count"], 1)
            self.assertEqual(plan["rows"][0]["entry_text"], LONG_ENTRY)

    def test_per_file_summary_projects_the_headroom_the_rows_would_leave(self) -> None:
        with TemporaryDirectory() as tmp:
            omh_home, hermes_home, _ = self._homes(tmp)
            _write_memory(hermes_home, LONG_ENTRY)

            plan = build_memory_demotion_plan(omh_home, hermes_home)
            summary = plan["files"][0]
            row = plan["rows"][0]
            self.assertEqual(summary["label"], "MEMORY.md")
            self.assertEqual(summary["cap"], DEFAULT_MEMORY_FILE_CAP_CHARS)
            self.assertEqual(summary["entry_count"], 1)
            self.assertEqual(summary["planned_demotions"], 1)
            self.assertFalse(summary["over_cap"])
            self.assertEqual(
                summary["estimated_headroom_after"],
                summary["headroom_chars"] + row["savings_chars"],
            )
            self.assertEqual(plan["estimated_savings_chars"], row["savings_chars"])

    def test_the_second_file_is_planned_too_and_owns_its_own_rows(self) -> None:
        with TemporaryDirectory() as tmp:
            omh_home, hermes_home, _ = self._homes(tmp)
            _write_memory(hermes_home, MEDIUM_ENTRY)
            _write_memory_file(hermes_home, "USER.md", LONG_ENTRY)

            plan = build_memory_demotion_plan(omh_home, hermes_home)
            self.assertEqual([row["file"] for row in plan["rows"]], ["USER.md", "MEMORY.md"])
            planned = {summary["label"]: summary["planned_demotions"] for summary in plan["files"]}
            self.assertEqual(planned, {"MEMORY.md": 1, "USER.md": 1})


class AlreadyCoveredTests(_PlanCase):
    def test_an_entry_an_approved_record_explains_is_deletable_not_demotable(self) -> None:
        with TemporaryDirectory() as tmp:
            omh_home, hermes_home, paths = self._homes(tmp)
            record_id = _approved(paths, "document-harness는 HTML을 먼저 작성하고 PPTX로 변환하는 문서 시스템이다")
            entry = "document-harness: HTML을 먼저 작성하고 PPTX로 변환하는 문서 시스템"
            _write_memory(hermes_home, entry, SHORT_ENTRY)

            plan = build_memory_demotion_plan(omh_home, hermes_home)
            self.assertEqual(len(plan["already_covered"]), 1)
            covered = plan["already_covered"][0]
            self.assertEqual(covered["entry_index"], 0)
            self.assertEqual(covered["matched_record_id"], record_id)
            self.assertTrue(covered["already_in_omh"])
            self.assertGreaterEqual(covered["similarity"], 0.6)
            # Demoting it would copy into L2 what L2 already holds.
            self.assertEqual([row["entry_text"] for row in plan["rows"]], [SHORT_ENTRY])

    def test_an_unrelated_record_leaves_every_entry_demotable(self) -> None:
        with TemporaryDirectory() as tmp:
            omh_home, hermes_home, paths = self._homes(tmp)
            _approved(paths, "release 스크립트는 --dry-run 플래그로 계획만 출력한다")
            _write_memory(hermes_home, SHORT_ENTRY)

            plan = build_memory_demotion_plan(omh_home, hermes_home)
            self.assertEqual(plan["already_covered"], [])
            self.assertEqual(plan["row_count"], 1)


class FileLabelTests(_PlanCase):
    def test_a_label_filter_plans_only_that_file(self) -> None:
        with TemporaryDirectory() as tmp:
            omh_home, hermes_home, _ = self._homes(tmp)
            _write_memory(hermes_home, MEDIUM_ENTRY)
            _write_memory_file(hermes_home, "USER.md", LONG_ENTRY)

            plan = build_memory_demotion_plan(omh_home, hermes_home, file_label="USER.md")
            self.assertEqual([summary["label"] for summary in plan["files"]], ["USER.md"])
            self.assertEqual([row["file"] for row in plan["rows"]], ["USER.md"])

    def test_an_unknown_label_says_so_instead_of_reporting_nothing_to_do(self) -> None:
        with TemporaryDirectory() as tmp:
            omh_home, hermes_home, _ = self._homes(tmp)
            _write_memory(hermes_home, LONG_ENTRY)

            plan = build_memory_demotion_plan(omh_home, hermes_home, file_label="MEMORIES.md")
            self.assertEqual(plan["reason_code"], "unknown_file_label")
            self.assertEqual(plan["requested_file_label"], "MEMORIES.md")
            self.assertEqual(plan["known_file_labels"], ["MEMORY.md", "USER.md"])
            self.assertEqual(plan["rows"], [])
            self.assertEqual(plan["files"], [])
            self.assertEqual(plan["row_count"], 0)


class ReferenceLineTests(_PlanCase):
    def test_the_line_carries_the_sha_prefix_and_a_collapsed_label(self) -> None:
        with TemporaryDirectory() as tmp:
            omh_home, hermes_home, _ = self._homes(tmp)
            entry = "첫 줄\n\n  둘째   줄은 아주 길어서 라벨이 잘려야 한다 " * 4
            _write_memory(hermes_home, entry.strip())

            row = build_memory_demotion_plan(omh_home, hermes_home)["rows"][0]
            collapsed = " ".join(row["entry_text"].split())
            expected = f"[omh#{row['sha256'][:12]}] {collapsed[:DEMOTION_REFERENCE_LABEL_CHARS]}…"
            self.assertEqual(row["reference_line"], expected)
            self.assertEqual(row["reference_chars"], len(row["reference_line"]))
            self.assertNotIn("\n", row["reference_line"])
            self.assertEqual(row["savings_chars"], row["chars"] - row["reference_chars"])

    def test_a_short_entry_keeps_its_whole_label_and_no_ellipsis(self) -> None:
        with TemporaryDirectory() as tmp:
            omh_home, hermes_home, _ = self._homes(tmp)
            _write_memory(hermes_home, SHORT_ENTRY)

            row = build_memory_demotion_plan(omh_home, hermes_home)["rows"][0]
            self.assertEqual(row["reference_line"], f"[omh#{row['sha256'][:12]}] {SHORT_ENTRY}")
            self.assertNotIn("…", row["reference_line"])

    def test_an_entry_smaller_than_its_pointer_reports_a_negative_saving(self) -> None:
        with TemporaryDirectory() as tmp:
            omh_home, hermes_home, _ = self._homes(tmp)
            _write_memory(hermes_home, "ok")

            plan = build_memory_demotion_plan(omh_home, hermes_home)
            self.assertLess(plan["rows"][0]["savings_chars"], 0)
            # A row that costs characters must not be counted as a saving.
            self.assertEqual(plan["estimated_savings_chars"], 0)
            self.assertEqual(plan["files"][0]["estimated_headroom_after"], plan["files"][0]["headroom_chars"])


class EmptyStoreTests(_PlanCase):
    def test_an_empty_hermes_home_still_returns_a_valid_plan(self) -> None:
        with TemporaryDirectory() as tmp:
            omh_home, hermes_home, _ = self._homes(tmp)

            plan = build_memory_demotion_plan(omh_home, hermes_home)
            self.assertEqual(plan["rows"], [])
            self.assertEqual(plan["already_covered"], [])
            self.assertEqual(plan["row_count"], 0)
            self.assertEqual(plan["estimated_savings_chars"], 0)
            self.assertEqual([summary["entry_count"] for summary in plan["files"]], [0, 0])
            self.assertEqual(plan["redaction_policy"], "local_content_plan")
            self.assertIn("cannot change it", str(plan["claim_boundary"]))
            self.assertIn("prepared_not_observed", str(plan["claim_boundary"]))

    def test_an_empty_record_store_makes_every_entry_qualify(self) -> None:
        with TemporaryDirectory() as tmp:
            omh_home, hermes_home, _ = self._homes(tmp)
            _write_memory(hermes_home, MEDIUM_ENTRY, SHORT_ENTRY)

            plan = build_memory_demotion_plan(omh_home, hermes_home)
            self.assertEqual(plan["already_covered"], [])
            self.assertEqual(plan["row_count"], 2)


class ArgumentTests(_PlanCase):
    def test_max_entries_below_one_is_refused(self) -> None:
        with TemporaryDirectory() as tmp:
            omh_home, hermes_home, _ = self._homes(tmp)
            _write_memory(hermes_home, LONG_ENTRY)
            for value in (0, -1, "3", 2.0):
                with self.subTest(value=value), self.assertRaises(ValueError):
                    build_memory_demotion_plan(omh_home, hermes_home, max_entries=value)


class StagingTests(_PlanCase):
    """`omh memory demote --stage`: the L2 half of a demotion, review-first."""

    def test_stage_captures_rows_as_candidates_and_restage_is_idempotent(self) -> None:
        from omh.memory import build_project_memory_recall_pack, stage_memory_demotion

        with TemporaryDirectory() as tmp:
            _omh_home, hermes_home, paths = self._homes(tmp)
            _write_memory(hermes_home, MEDIUM_ENTRY, LONG_ENTRY)
            staged = stage_memory_demotion(paths)
            self.assertEqual(staged["schema_version"], "memory_demotion_stage/v1")
            self.assertEqual(staged["staged_count"], 2)
            self.assertEqual([row["status"] for row in staged["staged"]], ["pending_review", "pending_review"])
            self.assertTrue(all(row["candidate_id"] for row in staged["staged"]))
            self.assertTrue(all(row["reference_line"].startswith("[omh#") for row in staged["staged"]))
            self.assertIn("Hermes's own memory tool", staged["next_action"])
            # Staging again must not stack duplicate candidates for the same
            # entries: the origin ref is the stable key.
            restaged = stage_memory_demotion(paths)
            self.assertEqual([row["status"] for row in restaged["staged"]], ["already_staged", "already_staged"])
            # Approving the staged candidates makes the content recallable
            # from L2, which is the entire point of the move.
            for row in staged["staged"]:
                approve_project_memory_candidate(paths, str(row["candidate_id"]))
            pack = build_project_memory_recall_pack(paths, LONG_ENTRY.split()[0])
            self.assertEqual(pack["record_count"], 1)

    def test_an_entry_the_summary_bound_would_truncate_is_refused_intact(self) -> None:
        from omh.memory import build_memory_demotion, stage_memory_demotion

        with TemporaryDirectory() as tmp:
            _omh_home, hermes_home, paths = self._homes(tmp)
            oversized = ("이 항목은 요약 경계보다 길어서 그대로 옮길 수 없다. " * 10).strip()
            self.assertGreater(len(oversized), 240)
            _write_memory(hermes_home, oversized)
            staged = stage_memory_demotion(paths)
            self.assertEqual([row["status"] for row in staged["staged"]], ["summary_bound_exceeded"])
            self.assertEqual(staged["captured_count"], 0)
            self.assertEqual(staged["refused_count"], 1)
            self.assertIn("stays in L1 intact", staged["staged"][0]["detail"])
            # Nothing was captured: the L1 entry remains the only copy and
            # the next plan still proposes it as unstaged work.
            replan = build_memory_demotion(paths)
            self.assertEqual([row["staging_status"] for row in replan["rows"]], ["unstaged"])

    def test_a_sensitive_looking_entry_is_refused_not_hollowed(self) -> None:
        from omh.memory import stage_memory_demotion

        with TemporaryDirectory() as tmp:
            _omh_home, hermes_home, paths = self._homes(tmp)
            _write_memory(hermes_home, "the deploy token for staging lives in the infra vault")
            staged = stage_memory_demotion(paths)
            # The capture redactor would store the literal "[redacted]" and
            # the workflow's next step deletes the L1 original -- refusal is
            # the only honest verdict.
            self.assertEqual([row["status"] for row in staged["staged"]], ["redacted_cannot_demote"])
            self.assertEqual(staged["captured_count"], 0)

    def test_a_cleanly_staged_entry_survives_byte_for_byte(self) -> None:
        from omh.memory import stage_memory_demotion

        with TemporaryDirectory() as tmp:
            _omh_home, hermes_home, paths = self._homes(tmp)
            _write_memory(hermes_home, LONG_ENTRY)
            staged = stage_memory_demotion(paths)
            record = approve_project_memory_candidate(paths, str(staged["staged"][0]["candidate_id"]))["record"]
            # The whole point of the move: the approved L2 record holds the
            # exact entry, tail included, before anyone deletes the original.
            self.assertEqual(record["summary"], LONG_ENTRY)

    def test_stage_on_an_empty_store_still_speaks_the_stage_schema(self) -> None:
        from omh.memory import stage_memory_demotion

        with TemporaryDirectory() as tmp:
            _omh_home, _hermes_home, paths = self._homes(tmp)
            staged = stage_memory_demotion(paths)
            self.assertEqual(staged["schema_version"], "memory_demotion_stage/v1")
            self.assertEqual(staged["staged"], [])
            self.assertEqual(staged["staged_count"], 0)

    def test_a_rejected_demotion_reads_as_the_standing_decision(self) -> None:
        from omh.memory import reject_project_memory_candidate, stage_memory_demotion

        with TemporaryDirectory() as tmp:
            _omh_home, hermes_home, paths = self._homes(tmp)
            _write_memory(hermes_home, LONG_ENTRY)
            staged = stage_memory_demotion(paths)
            reject_project_memory_candidate(paths, str(staged["staged"][0]["candidate_id"]), rejected_by="user")
            restaged = stage_memory_demotion(paths)
            # The reviewer said no to exactly this entry; restaging reports
            # that standing decision instead of `already_staged` (work in
            # progress) and never mints a fresh candidate around it.
            self.assertEqual([row["status"] for row in restaged["staged"]], ["previously_rejected"])
            self.assertEqual(restaged["staged"][0]["candidate_id"], "")

    def test_approved_demotions_turn_into_deletable_already_covered_rows(self) -> None:
        from omh.memory import stage_memory_demotion

        with TemporaryDirectory() as tmp:
            _omh_home, hermes_home, paths = self._homes(tmp)
            _write_memory(hermes_home, LONG_ENTRY)
            staged = stage_memory_demotion(paths)
            approve_project_memory_candidate(paths, str(staged["staged"][0]["candidate_id"]))
            replanned = stage_memory_demotion(paths)
            # Nothing left to stage; the entry is now L1 content an approved
            # L2 record already explains, reported as deletable.
            self.assertEqual(replanned.get("rows", replanned.get("staged", [])), [])
            covered = replanned["already_covered"]
            self.assertEqual(len(covered), 1)
            self.assertTrue(covered[0]["matched_record_id"].startswith("mem_"))


if __name__ == "__main__":
    unittest.main()
