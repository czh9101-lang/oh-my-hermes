from __future__ import annotations

import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from _cli_harness import run_cli
from _local_package import load_local_package

load_local_package()

from omh.quality.routing_log_calibration import (  # noqa: E402
    ROUTING_LOG_CALIBRATION_SCHEMA_VERSION,
    SKIP_REASONS,
    build_routing_log_calibration,
    collect_routing_records,
    format_routing_log_calibration,
    router_source_mtime,
)


def _record(run_id: str, *, updated_at: str, margin: int | None, stage: str, action: str) -> dict:
    decision = {
        "schema_version": "route_decision/v1",
        "router_stage": stage,
        "action": action,
        "selected_skill": "code-review",
        "selected_harness": "coding-handling",
        "confidence": "high",
    }
    if margin is not None:
        decision["margin"] = margin
    return {
        "schema_version": "omh_runtime/v1",
        "updated_at": updated_at,
        "source": "generic",
        "route_decision": decision,
        "message_sha256": "0" * 64,
        "message_length": 24,
        "run_id": run_id,
    }


def _write_runs(runs_dir: Path, records: dict[str, dict]) -> None:
    for run_id, record in records.items():
        run_dir = runs_dir / run_id
        run_dir.mkdir(parents=True, exist_ok=True)
        (run_dir / "routing.json").write_text(json.dumps(record), encoding="utf-8")


class ParseCoverageTests(unittest.TestCase):
    """A format skew that silently drops records would bias everything below it."""

    def test_every_skipped_record_is_counted_under_a_named_reason(self) -> None:
        with TemporaryDirectory() as tmp:
            runs = Path(tmp) / "runs"
            _write_runs(
                runs,
                {
                    "ok": _record("ok", updated_at="2026-08-30T01:00:00Z", margin=5, stage="recommendation", action="dispatch"),
                },
            )
            # A run directory with no routing record at all.
            (runs / "no-routing").mkdir(parents=True)
            # A routing record that is not JSON.
            (runs / "broken").mkdir(parents=True)
            (runs / "broken" / "routing.json").write_text("{not json", encoding="utf-8")
            # A routing record that parses but is not a routing record.
            (runs / "wrong-shape").mkdir(parents=True)
            (runs / "wrong-shape" / "routing.json").write_text(json.dumps({"hello": 1}), encoding="utf-8")
            # A routing record with no recorded timestamp.
            undated = _record("undated", updated_at="", margin=1, stage="fallback", action="fallback")
            undated["updated_at"] = ""
            (runs / "undated").mkdir(parents=True)
            (runs / "undated" / "routing.json").write_text(json.dumps(undated), encoding="utf-8")

            payload = build_routing_log_calibration(runs, since="")

        coverage = payload["coverage"]
        self.assertEqual(coverage["run_dirs_seen"], 5)
        self.assertEqual(coverage["routing_records_found"], 4)
        self.assertEqual(coverage["parsed"], 1)
        self.assertEqual(coverage["parse_coverage_percent"], 25.0)
        self.assertEqual(
            coverage["skipped"],
            {
                "no_routing_record": 1,
                "unreadable_json": 1,
                "unexpected_schema": 1,
                "missing_recorded_at": 1,
            },
        )
        self.assertEqual(coverage["skipped_total"], 4)
        for reason in coverage["skipped"]:
            self.assertIn(reason, SKIP_REASONS)
        # The named denominator travels with the percentage.
        self.assertEqual(coverage["parse_coverage_denominator"], "routing records found on disk")

    def test_an_empty_store_reports_unmeasured_coverage_not_zero_percent(self) -> None:
        with TemporaryDirectory() as tmp:
            payload = build_routing_log_calibration(Path(tmp) / "missing", since="")
        self.assertIsNone(payload["coverage"]["parse_coverage_percent"])
        self.assertEqual(payload["observed"]["decision_count"], 0)


class NormalizedMedianTests(unittest.TestCase):
    def test_a_heavy_day_cannot_dominate_the_normalized_median(self) -> None:
        # Given: one day with many low margins and two days with one high each.
        records = {}
        for index in range(9):
            records[f"heavy-{index}"] = _record(
                f"heavy-{index}",
                updated_at=f"2026-08-01T0{index}:00:00Z",
                margin=1,
                stage="recommendation",
                action="dispatch",
            )
        records["quiet-a"] = _record("quiet-a", updated_at="2026-08-02T01:00:00Z", margin=40, stage="recommendation", action="dispatch")
        records["quiet-b"] = _record("quiet-b", updated_at="2026-08-03T01:00:00Z", margin=50, stage="recommendation", action="dispatch")

        with TemporaryDirectory() as tmp:
            runs = Path(tmp) / "runs"
            _write_runs(runs, records)
            payload = build_routing_log_calibration(runs, since="")

        margin = payload["margin"]
        # The flat median is dragged to the heavy day's value.
        self.assertEqual(margin["median"], 1.0)
        # Per-day medians are 1, 40, 50 -> 40.
        self.assertEqual(margin["normalized_median"], 40.0)
        self.assertEqual(margin["day_count"], 3)
        self.assertEqual(margin["normalization_unit"], "recording_day")
        self.assertIn("no session id", str(margin["normalization_note"]))

    def test_decisions_without_a_margin_are_excluded_from_the_margin_denominator(self) -> None:
        with TemporaryDirectory() as tmp:
            runs = Path(tmp) / "runs"
            _write_runs(
                runs,
                {
                    "with": _record("with", updated_at="2026-08-01T01:00:00Z", margin=7, stage="recommendation", action="dispatch"),
                    "without": _record("without", updated_at="2026-08-01T02:00:00Z", margin=None, stage="fallback", action="fallback"),
                },
            )
            payload = build_routing_log_calibration(runs, since="")

        self.assertEqual(payload["margin"]["reported_count"], 1)
        self.assertEqual(payload["margin"]["reported_of_decisions"], 2)


class SinceWindowTests(unittest.TestCase):
    def test_since_defaults_to_the_router_source_mtime(self) -> None:
        # A decision made by a router that has since changed is evidence about a
        # router that no longer exists.
        default_since = router_source_mtime()
        self.assertTrue(default_since.endswith("Z"), default_since)

        with TemporaryDirectory() as tmp:
            runs = Path(tmp) / "runs"
            _write_runs(
                runs,
                {
                    "old": _record("old", updated_at="2000-01-01T00:00:00Z", margin=3, stage="recommendation", action="dispatch"),
                },
            )
            payload = build_routing_log_calibration(runs)

        self.assertEqual(payload["since"]["basis"], "router_source_mtime")
        self.assertEqual(payload["since"]["source"], "src/routing/chat.py")
        self.assertEqual(payload["coverage"]["skipped"], {"recorded_before_since": 1})
        self.assertEqual(payload["observed"]["decision_count"], 0)

    def test_an_explicit_empty_since_reads_every_record(self) -> None:
        with TemporaryDirectory() as tmp:
            runs = Path(tmp) / "runs"
            _write_runs(
                runs,
                {"old": _record("old", updated_at="2000-01-01T00:00:00Z", margin=3, stage="recommendation", action="dispatch")},
            )
            records, coverage = collect_routing_records(runs, since="")
        self.assertEqual(len(records), 1)
        self.assertEqual(coverage.parsed, 1)


class BoundaryTests(unittest.TestCase):
    def test_the_payload_states_that_message_text_is_never_recorded(self) -> None:
        with TemporaryDirectory() as tmp:
            payload = build_routing_log_calibration(Path(tmp), since="")
        boundary = str(payload["claim_boundary"])
        self.assertIn("never message text", boundary)
        self.assertIn("decision distributions", boundary)
        self.assertIn("never phrasings", boundary)
        self.assertEqual(payload["schema_version"], ROUTING_LOG_CALIBRATION_SCHEMA_VERSION)
        self.assertIn("operator's own message", str(payload["corpus"]["note"]))

    def test_the_text_report_leads_with_parse_coverage(self) -> None:
        with TemporaryDirectory() as tmp:
            runs = Path(tmp) / "runs"
            _write_runs(
                runs,
                {"ok": _record("ok", updated_at="2026-08-01T01:00:00Z", margin=4, stage="recommendation", action="dispatch")},
            )
            report = format_routing_log_calibration(build_routing_log_calibration(runs, since=""))
        self.assertIn("Parse coverage: 1/1 routing records parsed (100.0%)", report)
        self.assertIn("day-normalized median", report)


class RouteCalibrationCliTests(unittest.TestCase):
    def test_recorded_routes_reach_the_calibration_command(self) -> None:
        with TemporaryDirectory() as tmp:
            base = ["--omh-home", str(Path(tmp) / "omh"), "--hermes-home", str(Path(tmp) / "hermes")]
            for message in ("code review this diff", "why is the build failing on main"):
                status, _, stderr = run_cli([*base, "chat", "route", "--record", *message.split()])
                self.assertEqual(status, 0, stderr)

            status, stdout, stderr = run_cli([*base, "learning", "route-calibration", "--since", "", "--json"])
            self.assertEqual(status, 0, stderr)
            payload = json.loads(stdout)

        self.assertEqual(payload["coverage"]["parsed"], 2)
        self.assertEqual(payload["coverage"]["parse_coverage_percent"], 100.0)
        self.assertEqual(payload["observed"]["decision_count"], 2)
        self.assertGreaterEqual(payload["corpus"]["total_case_count"], 1)


if __name__ == "__main__":
    unittest.main()
