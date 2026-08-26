from __future__ import annotations

import unittest

from _local_package import load_local_package

load_local_package()
from omh.commands.main import build_parser


class MemoryParserTests(unittest.TestCase):
    def test_new_memory_control_commands_parse_with_explicit_gates(self) -> None:
        cases = (
            (["memory", "inventory"], "cmd_memory_inventory"),
            (["memory", "inventory", "--write-ledger"], "cmd_memory_inventory"),
            (["memory", "reactivate", "legacy-record", "--revision", "1", "--review-id", "review-record"], "cmd_memory_reactivate"),
            (["memory", "batch-stage", "--batch", "batch.json"], "cmd_memory_batch_stage"),
            (["memory", "batch-review", "batch-one", "--decisions", "decisions.json"], "cmd_memory_batch_review"),
            (["memory", "batch-apply", "batch-one", "--apply"], "cmd_memory_batch_apply"),
            (["memory", "restore", "record-one", "--revision", "1"], "cmd_memory_restore"),
            (["memory", "prune", "record-one", "--revision", "1", "--apply", "--confirm-hard-delete-local"], "cmd_memory_prune"),
            (["memory", "correct", "record-one", "--revision", "1", "Corrected summary"], "cmd_memory_correct"),
            (["memory", "lineage", "record-one"], "cmd_memory_lineage"),
            (["memory", "lineage", "record-one", "--depth", "5"], "cmd_memory_lineage"),
            (["memory", "perspectives"], "cmd_memory_perspectives"),
            (["memory", "pin", "record-one"], "cmd_memory_pin"),
            (["memory", "unpin", "record-one"], "cmd_memory_unpin"),
            (["memory", "attention", "record-one", "--tier", "reference"], "cmd_memory_attention"),
            (["memory", "attention", "record-one", "--tier", "archive", "--apply"], "cmd_memory_attention"),
            (["memory", "recall", "query", "--include-archived"], "cmd_memory_recall"),
            (["memory", "rollup", "--tag", "deploy"], "cmd_memory_rollup"),
            (["memory", "rollup", "--tag", "deploy", "--apply"], "cmd_memory_rollup"),
            (["memory", "capture", "summary", "--observed", "codex"], "cmd_memory_capture"),
            (["memory", "recall", "query", "--observer", "operator", "--observed", "codex"], "cmd_memory_recall"),
            (["memory", "confirm", "record-one"], "cmd_memory_confirm"),
            (["memory", "confirm", "--all-due", "--stale-after-days", "120"], "cmd_memory_confirm"),
            (["memory", "confirm", "record-one", "--stale-after", "2027-01-15"], "cmd_memory_confirm"),
            (["memory", "approve", "cand-one", "--retention-class", "durable"], "cmd_memory_approve"),
            (["memory", "capture", "summary", "--stale-after", "2027-01-15"], "cmd_memory_capture"),
            (["memory", "capture", "summary", "--expires-at", "2027-01-15T09:00:00Z"], "cmd_memory_capture"),
            (["memory", "demote"], "cmd_memory_demote"),
            (["memory", "demote", "--file", "USER.md", "--max", "3", "--stage"], "cmd_memory_demote"),
        )

        for argv, handler_name in cases:
            with self.subTest(argv=argv):
                args = build_parser().parse_args(argv)
                self.assertEqual(args.func.__name__, handler_name)

    def test_memory_confirm_requires_exactly_one_target(self) -> None:
        from omh.installer import OmhError

        for argv in (["memory", "confirm"], ["memory", "confirm", "record-one", "--all-due"]):
            with self.subTest(argv=argv):
                args = build_parser().parse_args(argv)
                with self.assertRaises(OmhError):
                    args.func(args)

    def test_memory_capture_rejects_invalid_source_class_at_parser_boundary(self) -> None:
        with self.assertRaises(SystemExit) as raised:
            build_parser().parse_args(["memory", "capture", "--source-class", "unknown", "summary"])
        self.assertEqual(raised.exception.code, 2)

    def test_memory_capture_collects_repeatable_derived_from_refs(self) -> None:
        args = build_parser().parse_args(
            ["memory", "capture", "summary", "--derived-from", "mem_a", "--derived-from", "mem_b"]
        )
        self.assertEqual(args.derived_from, ["mem_a", "mem_b"])

    def test_memory_attention_rejects_an_unknown_tier_at_the_parser_boundary(self) -> None:
        with self.assertRaises(SystemExit) as raised:
            build_parser().parse_args(["memory", "attention", "record-one", "--tier", "cold-storage"])
        self.assertEqual(raised.exception.code, 2)

    def test_memory_attention_requires_an_explicit_tier(self) -> None:
        with self.assertRaises(SystemExit) as raised:
            build_parser().parse_args(["memory", "attention", "record-one"])
        self.assertEqual(raised.exception.code, 2)

    def test_memory_attention_defaults_to_report_only(self) -> None:
        args = build_parser().parse_args(["memory", "attention", "record-one", "--tier", "archive"])
        self.assertFalse(args.apply)

    def test_memory_lineage_defaults_to_three_hops(self) -> None:
        args = build_parser().parse_args(["memory", "lineage", "record-one"])
        self.assertEqual(args.depth, 3)


if __name__ == "__main__":
    unittest.main()
