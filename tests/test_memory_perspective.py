from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from _local_package import load_local_package

load_local_package()
from omh.workflows.memory_lifecycle import (
    apply_memory_correction,
    apply_memory_reapproval,
    build_memory_correction,
    build_memory_reapproval,
)
from omh.workflows.memory_lifecycle_executor import execute_memory_lifecycle
from omh.memory import (
    approve_project_memory_candidate,
    build_handoff_context_pack,
    build_memory_perspectives,
    build_project_memory_recall_pack,
    capture_project_memory_candidate,
    memory_recall_pack_for_handoff,
    validate_project_memory_record,
    validate_project_memory_recall_pack,
)
from project_identity_fixture import memory_paths as resolve_paths
from omh.runtime.records import _compact_memory_recall_pack


def _approve_capture(paths, summary, **kwargs):
    captured = capture_project_memory_candidate(paths, summary, **kwargs)
    candidate_id = captured["candidate"]["candidate_id"]
    return approve_project_memory_candidate(paths, candidate_id)["record"]


class PerspectiveCaptureTests(unittest.TestCase):
    def test_observed_alone_defaults_observer_to_hermes(self) -> None:
        with TemporaryDirectory() as tmp:
            paths = resolve_paths(Path(tmp) / ".omh", Path(tmp) / ".hermes")
            record = _approve_capture(paths, "codex needs the full test command", observed="Codex")
            self.assertEqual(record["perspective"], {"observer": "hermes", "observed": "codex"})

    def test_observer_without_observed_is_rejected(self) -> None:
        with TemporaryDirectory() as tmp:
            paths = resolve_paths(Path(tmp) / ".omh", Path(tmp) / ".hermes")
            with self.assertRaisesRegex(ValueError, "requires --observed"):
                capture_project_memory_candidate(paths, "half a lens", observer="operator")

    def test_unsafe_actor_label_is_rejected(self) -> None:
        with TemporaryDirectory() as tmp:
            paths = resolve_paths(Path(tmp) / ".omh", Path(tmp) / ".hermes")
            with self.assertRaisesRegex(ValueError, "unsafe perspective actor"):
                capture_project_memory_candidate(paths, "bad label", observed="co dex/../x")

    def test_unscoped_capture_has_no_perspective_key(self) -> None:
        with TemporaryDirectory() as tmp:
            paths = resolve_paths(Path(tmp) / ".omh", Path(tmp) / ".hermes")
            record = _approve_capture(paths, "plain project fact")
            self.assertNotIn("perspective", record)


class PerspectiveLensTests(unittest.TestCase):
    def test_scoped_records_pass_only_a_matching_lens_and_unscoped_always_pass(self) -> None:
        with TemporaryDirectory() as tmp:
            paths = resolve_paths(Path(tmp) / ".omh", Path(tmp) / ".hermes")
            unscoped = _approve_capture(paths, "Project uses uv for everything")
            codex = _approve_capture(paths, "codex wants explicit test commands", observed="codex")
            claude = _approve_capture(paths, "claude-code prefers worktree isolation", observed="claude-code")

            no_lens = build_project_memory_recall_pack(paths, "")
            self.assertEqual(
                {item["record_id"] for item in no_lens["included_records"]},
                {unscoped["record_id"], codex["record_id"], claude["record_id"]},
                "a plain recall is an inspection surface and hides nothing",
            )

            codex_lens = build_project_memory_recall_pack(paths, "", observed="codex")
            self.assertEqual(
                {item["record_id"] for item in codex_lens["included_records"]},
                {unscoped["record_id"], codex["record_id"]},
            )
            self.assertEqual(codex_lens["perspective"], {"observer": "", "observed": "codex"})
            self.assertEqual(validate_project_memory_recall_pack(codex_lens), [])
            by_id = {item["record_id"]: item for item in codex_lens["included_records"]}
            self.assertEqual(by_id[codex["record_id"]]["perspective"], {"observer": "hermes", "observed": "codex"})
            self.assertEqual(by_id[unscoped["record_id"]]["perspective"], {})

    def test_observer_lens_filters_scoped_records(self) -> None:
        with TemporaryDirectory() as tmp:
            paths = resolve_paths(Path(tmp) / ".omh", Path(tmp) / ".hermes")
            hermes_view = _approve_capture(paths, "hermes-side note about codex", observed="codex")
            operator_view = _approve_capture(paths, "operator-side note about codex", observer="operator", observed="codex")

            pack = build_project_memory_recall_pack(paths, "", observer="operator")
            self.assertEqual([item["record_id"] for item in pack["included_records"]], [operator_view["record_id"]])
            self.assertNotIn(hermes_view["record_id"], json.dumps(pack["included_records"]))

    def test_handoff_pack_uses_executor_target_as_observed_lens(self) -> None:
        with TemporaryDirectory() as tmp:
            paths = resolve_paths(Path(tmp) / ".omh", Path(tmp) / ".hermes")
            unscoped = _approve_capture(paths, "Project deploy fact")
            codex = _approve_capture(paths, "codex deploy lesson", observed="codex")
            claude = _approve_capture(paths, "claude-code deploy lesson", observed="claude-code")

            codex_pack = memory_recall_pack_for_handoff(paths, "deploy", executor_target="codex")
            ids = {item["record_id"] for item in codex_pack["included_records"]}
            self.assertEqual(ids, {unscoped["record_id"], codex["record_id"]})
            self.assertNotIn(claude["record_id"], ids)

            generic_pack = memory_recall_pack_for_handoff(paths, "deploy", executor_target="generic")
            ids = {item["record_id"] for item in generic_pack["included_records"]}
            self.assertEqual(ids, {unscoped["record_id"]}, "another executor's lessons stay out of a generic handoff")


class PerspectiveHardeningTests(unittest.TestCase):
    def test_recall_lens_normalizes_case_and_whitespace_like_capture(self) -> None:
        with TemporaryDirectory() as tmp:
            paths = resolve_paths(Path(tmp) / ".omh", Path(tmp) / ".hermes")
            codex = _approve_capture(paths, "codex lesson", observed="codex")

            pack = build_project_memory_recall_pack(paths, "", observed=" Codex ")

            self.assertIn(codex["record_id"], {item["record_id"] for item in pack["included_records"]})
            self.assertEqual(pack["perspective"], {"observer": "", "observed": "codex"})

    def test_unresolved_executor_target_delivers_unscoped_records_only(self) -> None:
        with TemporaryDirectory() as tmp:
            paths = resolve_paths(Path(tmp) / ".omh", Path(tmp) / ".hermes")
            unscoped = _approve_capture(paths, "project fact")
            _approve_capture(paths, "codex lesson", observed="codex")

            for target in ("choose", ""):
                pack = memory_recall_pack_for_handoff(paths, "", executor_target=target)
                self.assertEqual(
                    [item["record_id"] for item in pack["included_records"]],
                    [unscoped["record_id"]],
                    f"target={target!r}: an unresolved executor must not receive scoped records",
                )

    def test_context_pack_applies_the_executor_lens(self) -> None:
        with TemporaryDirectory() as tmp:
            paths = resolve_paths(Path(tmp) / ".omh", Path(tmp) / ".hermes")
            unscoped = _approve_capture(paths, "project fact for context")
            codex = _approve_capture(paths, "codex-only context lesson", observed="codex")

            claude_pack = build_handoff_context_pack(paths, executor_target="claude-code")
            included_ids = {item["item_id"] for item in claude_pack["included_context"]}
            self.assertIn(unscoped["record_id"], included_ids)
            self.assertNotIn(codex["record_id"], included_ids)
            mismatches = [item for item in claude_pack["excluded_context"] if item.get("reason") == "perspective_mismatch"]
            self.assertEqual([item["item_id"] for item in mismatches], [codex["record_id"]])

            codex_pack = build_handoff_context_pack(paths, executor_target="codex")
            self.assertIn(codex["record_id"], {item["item_id"] for item in codex_pack["included_context"]})

    def test_validators_reject_malformed_perspective_blocks(self) -> None:
        with TemporaryDirectory() as tmp:
            paths = resolve_paths(Path(tmp) / ".omh", Path(tmp) / ".hermes")
            record = _approve_capture(paths, "codex lesson", observed="codex")

            self.assertEqual(validate_project_memory_record(record), [])
            broken = {**record, "perspective": "not-a-dict"}
            self.assertTrue(any("perspective" in error for error in validate_project_memory_record(broken)))
            empty_observed = {**record, "perspective": {"observer": "hermes", "observed": ""}}
            self.assertTrue(any("observed must name an actor" in error for error in validate_project_memory_record(empty_observed)))
            junk_keys = {**record, "perspective": {"observer": "hermes", "observed": "codex", "junk": "x"}}
            self.assertTrue(any("unsupported keys" in error for error in validate_project_memory_record(junk_keys)))

            pack = build_project_memory_recall_pack(paths, "")
            self.assertEqual(validate_project_memory_recall_pack(pack), [])
            bad_pack = {**pack, "perspective": "oops"}
            self.assertTrue(any("perspective must be an object" in error for error in validate_project_memory_recall_pack(bad_pack)))

    def test_compacted_recall_pack_keeps_perspective_ranking_and_provenance(self) -> None:
        with TemporaryDirectory() as tmp:
            paths = resolve_paths(Path(tmp) / ".omh", Path(tmp) / ".hermes")
            base = _approve_capture(paths, "base fact")
            _approve_capture(paths, "codex conclusion", observed="codex", derived_from=[base["record_id"]])

            pack = memory_recall_pack_for_handoff(paths, "", executor_target="codex")
            compacted = _compact_memory_recall_pack(pack)

            self.assertEqual(compacted["perspective"], {"observer": "", "observed": "codex"})
            by_id = {item["record_id"]: item for item in compacted["included_records"]}
            scoped = next(item for item in by_id.values() if item["perspective"])
            self.assertEqual(scoped["perspective"], {"observer": "hermes", "observed": "codex"})
            self.assertEqual(scoped["derived_from"], [base["record_id"]])
            self.assertIn("rrf_score_micro", scoped["ranking"])


class PerspectiveLifecycleAndInventoryTests(unittest.TestCase):
    def test_perspective_survives_correction_and_reapproval(self) -> None:
        with TemporaryDirectory() as tmp:
            paths = resolve_paths(Path(tmp) / ".omh", Path(tmp) / ".hermes")
            record = _approve_capture(paths, "codex lesson to correct", observed="codex")

            plan = build_memory_correction(
                paths, record["record_id"], 1, "corrected codex lesson",
                now=datetime.now(timezone.utc), candidate_id="cand-correct-perspective",
            )
            apply_memory_correction(paths, plan, transaction_executor=execute_memory_lifecycle)
            reapproval = build_memory_reapproval(
                paths, "cand-correct-perspective", reviewer_claim="operator", now=datetime.now(timezone.utc)
            )
            apply_memory_reapproval(paths, reapproval, transaction_executor=execute_memory_lifecycle)

            live = json.loads((paths.memory_dir / "records" / f"{record['record_id']}.json").read_text(encoding="utf-8"))
            self.assertEqual(live["revision"], 2)
            self.assertEqual(live["perspective"], {"observer": "hermes", "observed": "codex"})

    def test_perspectives_inventory_counts_pairs_and_unscoped(self) -> None:
        with TemporaryDirectory() as tmp:
            paths = resolve_paths(Path(tmp) / ".omh", Path(tmp) / ".hermes")
            _approve_capture(paths, "plain one")
            _approve_capture(paths, "plain two")
            _approve_capture(paths, "codex a", observed="codex")
            _approve_capture(paths, "codex b", observed="codex")
            _approve_capture(paths, "operator on claude-code", observer="operator", observed="claude-code")

            report = build_memory_perspectives(paths)

            self.assertEqual(report["unscoped_count"], 2)
            self.assertEqual(report["pair_count"], 2)
            self.assertEqual(
                report["pairs"],
                [
                    {"observer": "hermes", "observed": "codex", "record_count": 2},
                    {"observer": "operator", "observed": "claude-code", "record_count": 1},
                ],
            )


if __name__ == "__main__":
    unittest.main()
